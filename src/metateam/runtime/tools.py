"""Tool registry + builtins (files, shell, skills, memory, delegate)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from ..core.config import Settings
from ..services.skills import Skill, load_skills
from .ask import normalize_option_labels


_LONG_RUNNING_RE = re.compile(
    r"("
    r"npm\s+run\s+(dev|start|serve)|"
    r"yarn\s+(dev|start)|"
    r"pnpm\s+(dev|start)|"
    r"\bvite\b|"
    r"webpack-dev-server|"
    r"next\s+dev|"
    r"uvicorn\b.*(--reload|\breload\b)|"
    r"flask\s+run|"
    r"django(-admin)?\s+runserver|"
    r"python\s+-m\s+http\.server|"
    r"npx\s+serve|"
    r"nodemon\b|"
    r"tail\s+-f|"
    r"--watch\b"
    r")",
    re.I,
)


def _is_long_running_command(command: str) -> bool:
    return bool(_LONG_RUNNING_RE.search(command))


_DANGEROUS_SHELL_RE = re.compile(
    r"("
    r"rm\s+-rf\s+/|"
    r"rm\s+-rf\s+~|"
    r"format\s+c:|"
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;|"
    r"del\s+/s\s+/q\s+c:|"
    r"rd\s+/s\s+/q\s+c:|"
    r"remove-item\s+.*-recurse|"
    r"mkfs\.|"
    r"dd\s+if=.*of=/dev/|"
    r">\s*/dev/sd|"
    r"\bshutdown\b|"
    r"\breboot\b"
    r")",
    re.I,
)


def _shell_argv(command: str) -> list[str]:
    """Run via explicit shell binary — avoids Python shell=True string risks."""
    if os.name == "nt":
        return ["cmd.exe", "/d", "/c", command]
    return ["/bin/bash", "-lc", command]


def _shell_policy(settings: Settings, workspace: Path):
    from ..services.shell_sandbox import ShellSandboxPolicy

    return ShellSandboxPolicy.for_workspace(
        workspace,
        enabled=bool(getattr(settings, "shell_sandbox", True)),
    )


def _guard_shell(command: str, *, settings: Settings, workspace: Path) -> Optional[str]:
    from ..services.shell_sandbox import check_command

    return check_command(
        command,
        cwd=workspace,
        policy=_shell_policy(settings, workspace),
    )


def _sandboxed_env(settings: Settings) -> dict[str, str]:
    from ..services.shell_sandbox import sandbox_env

    return sandbox_env()


def _run_shell_background(command: str, *, cwd: str, collect_secs: float = 8.0, env: Optional[dict[str, str]] = None) -> str:
    """Start a process and return after collecting early logs (does not wait for exit)."""
    popen_kwargs: dict[str, Any] = {
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": env or {**os.environ, "PYTHONIOENCODING": "utf-8"},
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        # Detach from parent session so Ctrl+C on the server doesn't kill child servers
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(_shell_argv(command), **popen_kwargs)
    chunks: list[str] = []
    done = threading.Event()

    def _reader() -> None:
        assert proc.stdout is not None
        try:
            while not done.is_set():
                line = proc.stdout.readline()
                if not line:
                    break
                chunks.append(line)
                if sum(len(c) for c in chunks) > 12_000:
                    break
        except Exception:
            pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(timeout=collect_secs)
    done.set()
    # Give reader a moment to finish current line
    t.join(timeout=0.3)

    still = proc.poll() is None
    preview = "".join(chunks)[-8000:] or "(no output yet)"
    if still:
        return (
            f"background=true pid={proc.pid} status=running\n"
            f"command={command!r}\n"
            f"Collected first ~{collect_secs:.0f}s of logs (process keeps running; "
            f"agent will NOT wait for it to exit).\n"
            f"--- log ---\n{preview}"
        )
    out = preview
    return f"background=true pid={proc.pid} status=exited code={proc.returncode}\n--- log ---\n{out}"


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., str]
    parallel_safe: bool = False
    requires_approval: bool = False

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.openai_schema() for t in self._tools.values()]

    def names(self) -> list[str]:
        return sorted(self._tools.keys())


def _safe_path(workspace: Path, raw: str) -> Path:
    """Resolve a path that must stay inside the active workspace."""
    p = Path(raw).expanduser()
    ws = workspace.resolve()
    if not p.is_absolute():
        p = (ws / p).resolve()
    else:
        p = p.resolve()
    try:
        p.relative_to(ws)
    except ValueError as exc:
        raise PermissionError(f"path outside workspace: {p}") from exc
    return p


def skill_tool_name(name: str) -> str:
    # Normalize to [a-z0-9_] for broad provider compatibility
    safe = "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")
    while "__" in safe:
        safe = safe.replace("__", "_")
    return f"skill_{safe}"


def _skill_as_tool(skill: Skill) -> Tool:
    """Expose a SKILL.md as a callable function tool."""

    tname = skill_tool_name(skill.name)
    desc = (skill.description or f"Apply the '{skill.name}' skill procedure.").strip()
    if len(desc) > 400:
        desc = desc[:397] + "..."

    def handler(task: str = "") -> str:
        header = f"# Function skill: {skill.name}\n"
        if task.strip():
            header += f"Requested task: {task.strip()}\n\n"
        header += (
            "Follow the procedure below with other tools (read_file/write_file/…). "
            "Do not stop after reading — execute the steps.\n\n"
        )
        return header + skill.read_body()

    return Tool(
        name=tname,
        description=desc,
        parameters={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Optional: what you want this skill to accomplish now.",
                }
            },
            "required": [],
        },
        handler=handler,
        parallel_safe=True,
    )


def save_skill_file(settings: Settings, name: str, description: str, content: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name.lower()).strip("-")
    if not safe:
        raise ValueError("invalid skill name")
    dest = settings.skills_dir / "learned" / safe
    dest.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {safe}\ndescription: {description[:80]}\n---\n\n{content.strip()}\n"
    path = dest / "SKILL.md"
    path.write_text(text, encoding="utf-8")
    return path


# back-compat alias for review.py
_save_skill_file = save_skill_file


def _looks_like_code_path(path: str) -> bool:
    suffix = Path(path).suffix.lower()
    if not suffix:
        return False
    from ..services.codebase_memory import CODE_SUFFIXES

    return suffix in CODE_SUFFIXES


def build_registry(
    settings: Settings,
    *,
    skills: list[Skill],
    allow_delegate: bool = True,
    run_child: Optional[Callable[..., str]] = None,
    ask_user_fn: Optional[Callable[..., str]] = None,
) -> ToolRegistry:
    reg = ToolRegistry()
    ws = settings.workspace
    # Codebase-as-Memory: new code files require a prior similarity align in this run.
    align_state: dict[str, Any] = {"aligned": False, "queries": []}

    def read_file(path: str, offset: int = 1, limit: int = 200) -> str:
        fp = _safe_path(ws, path)
        if not fp.exists():
            return f"ERROR: not found: {fp}"
        lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        offset = max(1, int(offset))
        limit = max(1, min(int(limit), 2000))
        chunk = lines[offset - 1 : offset - 1 + limit]
        body = "\n".join(f"{i}|{line}" for i, line in enumerate(chunk, start=offset))
        if offset - 1 + limit < len(lines):
            body += f"\n… next_offset={offset + limit} total_lines={len(lines)}"
        return body

    def write_file(path: str, content: str, force_create: bool = False) -> str:
        from ..services import fs_api
        from ..services import codebase_memory as cbm

        try:
            # Relative to workspace for undo + consistent path rules
            try:
                rel = str(Path(path)).replace("\\", "/")
                if Path(path).is_absolute():
                    rel = str(Path(path).resolve().relative_to(ws.resolve())).replace("\\", "/")
            except Exception:
                rel = path.replace("\\", "/")

            target = _safe_path(ws, rel)
            is_new = not target.exists()
            if (
                is_new
                and _looks_like_code_path(rel)
                and not bool(force_create)
                and not align_state["aligned"]
            ):
                return (
                    "ERROR: codebase_align_required — before creating a new code file, "
                    "call codebase_find_similar with what you intend to build. "
                    "If no reusable asset exists, retry write_file with force_create=true."
                )

            res = fs_api.write_text(rel, content)
            cbm.invalidate_index(ws)
            return f"wrote {res['path']} ({res['size']} chars)"
        except Exception as exc:
            return f"ERROR: {exc}"

    def delete_file(path: str) -> str:
        from ..services import fs_api
        from ..services import codebase_memory as cbm

        try:
            rel = path.replace("\\", "/")
            try:
                if Path(path).is_absolute():
                    rel = str(Path(path).resolve().relative_to(ws.resolve())).replace("\\", "/")
            except Exception:
                pass
            res = fs_api.delete_entry(rel, recursive=False)
            cbm.invalidate_index(ws)
            return f"deleted {res['path']}"
        except Exception as exc:
            return f"ERROR: {exc}"

    def codebase_overview(refresh: bool = False) -> str:
        from ..services import codebase_memory as cbm

        index = cbm.get_or_build_index(ws, force=bool(refresh))
        ov = cbm.overview(index)
        return json.dumps(ov, ensure_ascii=False, indent=2)

    def codebase_find_similar(query: str, limit: int = 12) -> str:
        from ..services import codebase_memory as cbm

        q = (query or "").strip()
        if not q:
            return "ERROR: empty query"
        index = cbm.get_or_build_index(ws)
        hits = cbm.find_similar(index, q, limit=max(1, min(int(limit), 30)))
        align_state["aligned"] = True
        align_state["queries"].append(q)
        payload = {
            "query": q,
            "aligned": True,
            "match_count": len(hits),
            "matches": hits,
            "guidance": (
                "If matches exist, prefer extending/reusing them over creating a parallel file. "
                "If match_count is 0, you may write_file with force_create=true."
            ),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def codebase_impact(symbol_or_path: str, limit: int = 40) -> str:
        from ..services import codebase_memory as cbm

        needle = (symbol_or_path or "").strip()
        if not needle:
            return "ERROR: empty symbol_or_path"
        index = cbm.get_or_build_index(ws)
        refs = cbm.find_references(ws, index, needle, limit=max(1, min(int(limit), 80)))
        payload = {
            "target": needle,
            "reference_files": len(refs),
            "hits": refs,
            "guidance": "Treat listed files as blast radius; avoid breaking callers.",
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def coherence_checklist() -> str:
        from .coherence import PILE_CHECKLIST

        return (
            PILE_CHECKLIST
            + "\n\nReply against each item with evidence (paths). "
            "If any fail, fix by extending existing assets before you stop."
        )

    def git_status() -> str:
        from ..services import git_ops

        return git_ops.git_status(ws)

    def git_diff(staged: bool = False, path: str = "") -> str:
        from ..services import git_ops

        return git_ops.git_diff(ws, staged=bool(staged), path=(path or "").strip())

    def git_log(limit: int = 12) -> str:
        from ..services import git_ops

        return git_ops.git_log(ws, limit=limit)

    def git_branch() -> str:
        from ..services import git_ops

        return git_ops.git_branch(ws)

    def git_commit(message: str) -> str:
        from ..services import git_ops

        return git_ops.git_commit(ws, message)

    def verify_run(command: str, timeout_sec: int = 120) -> str:
        """Run a verification command (tests/lint). Requires approval; needs shell enabled."""
        if not settings.allow_shell:
            return (
                "ERROR: shell disabled (META_ALLOW_SHELL=0). "
                "Enable shell to run verify_run, or tell the user the verify command to run locally."
            )
        cmd = (command or "").strip()
        if not cmd:
            return "ERROR: empty command"
        if _DANGEROUS_SHELL_RE.search(cmd):
            return "ERROR: command blocked by safety denylist"
        blocked = _guard_shell(cmd, settings=settings, workspace=ws)
        if blocked:
            return blocked
        if _is_long_running_command(cmd):
            return "ERROR: verify_run is for one-shot checks, not long-running servers"
        timeout = max(15, min(int(timeout_sec or 120), 600))
        try:
            proc = subprocess.run(
                _shell_argv(cmd),
                cwd=str(ws.resolve()),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                env=_sandboxed_env(settings),
            )
        except subprocess.TimeoutExpired:
            return f"VERIFY FAIL timeout={timeout}s command={cmd!r}"
        except Exception as exc:  # noqa: BLE001
            return f"VERIFY FAIL error={exc}"
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if len(out) > 12_000:
            out = out[:12_000] + "\n…[truncated]"
        status = "PASS" if proc.returncode == 0 else "FAIL"
        return f"VERIFY {status} exit={proc.returncode}\ncommand={cmd!r}\n---\n{out or '(no output)'}"

    def list_dir(path: str = ".") -> str:
        fp = _safe_path(ws, path)
        if not fp.exists():
            return f"ERROR: not found: {fp}"
        if fp.is_file():
            return f"FILE {fp}"
        entries = sorted(fp.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        lines = []
        for e in entries[:240]:
            lines.append(f"{'dir' if e.is_dir() else 'file'}\t{e.name}")
        if len(entries) > 240:
            lines.append(f"… {len(entries) - 240} more")
        return "\n".join(lines) or "(empty)"

    def search_text(query: str, path: str = ".", glob: str = "*.*") -> str:
        base = _safe_path(ws, path)
        if not base.exists():
            return f"ERROR: not found: {base}"
        hits: list[str] = []
        paths = base.rglob(glob) if base.is_dir() else [base]
        skip = {".git", ".venv", "node_modules", "__pycache__", "sessions"}
        for fp in paths:
            if not fp.is_file() or any(p in skip for p in fp.parts):
                continue
            try:
                text = fp.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if query in line:
                    try:
                        rel = fp.relative_to(ws)
                    except ValueError:
                        rel = fp
                    hits.append(f"{rel}:{i}:{line.strip()[:200]}")
                    if len(hits) >= 50:
                        return "\n".join(hits)
        return "\n".join(hits) if hits else "no matches"

    def run_shell(command: str, background: bool = False) -> str:
        if not settings.allow_shell:
            return "ERROR: shell disabled (set META_ALLOW_SHELL=1 to enable)"
        low = command.lower().strip()
        if _DANGEROUS_SHELL_RE.search(low):
            return "ERROR: blocked dangerous command"
        blocked = _guard_shell(command, settings=settings, workspace=ws)
        if blocked:
            return blocked

        env = _sandboxed_env(settings)
        # Dev servers / watchers never exit — must not block the agent.
        long_running = background or _is_long_running_command(low)
        if long_running:
            return _run_shell_background(
                command, cwd=str(ws.resolve()), collect_secs=8.0, env=env
            )

        try:
            proc = subprocess.run(
                _shell_argv(command),
                cwd=str(ws.resolve()),
                capture_output=True,
                text=True,
                timeout=settings.shell_timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            partial = (exc.stdout or "") + (("\n" + (exc.stderr or "")) if exc.stderr else "")
            partial = partial[-8000:]
            return (
                f"ERROR: timeout after {settings.shell_timeout}s — command still running or hung.\n"
                f"For servers (npm run dev / vite / uvicorn), call run_shell with background=true.\n"
                f"partial_output:\n{partial or '(none)'}"
            )
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        if len(out) > 14_000:
            out = out[:14_000] + "\n…[truncated]"
        return f"exit={proc.returncode}\n{out}"

    def skill_save(name: str, description: str, content: str) -> str:
        path = save_skill_file(settings, name, description, content)
        skills[:] = load_skills(settings.skills_dir)
        return f"saved skill_* function → {path} (reload session to refresh tools)"

    def memory_append(note: str) -> str:
        from ..services.memory import append_memory

        return append_memory(settings.memory_file, note)

    def memory_read() -> str:
        from ..services.memory import read_memory

        return read_memory(settings.memory_file, max_chars=8000) or "(empty)"

    def memory_remove(match: str) -> str:
        from ..services.memory import remove_memory

        return remove_memory(settings.memory_file, match)

    def memory_write(content: str) -> str:
        from ..services.memory import replace_memory

        return replace_memory(settings.memory_file, content)

    reg.register(
        Tool(
            "read_file",
            "Read a text file with line numbers. Use offset/limit for large files.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "default": 1},
                    "limit": {"type": "integer", "default": 200},
                },
                "required": ["path"],
            },
            read_file,
            parallel_safe=True,
        )
    )
    reg.register(
        Tool(
            "write_file",
            "Write/overwrite a text file under the workspace. Requires user approval. "
            "Creating a NEW code file requires a prior codebase_find_similar call in this run "
            "(or force_create=true after confirming nothing reusable exists).",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "force_create": {
                        "type": "boolean",
                        "description": (
                            "Set true only after codebase_find_similar shows no reusable asset "
                            "and you must create a new file."
                        ),
                    },
                },
                "required": ["path", "content"],
            },
            write_file,
            parallel_safe=False,
            requires_approval=True,
        )
    )
    reg.register(
        Tool(
            "delete_file",
            "Delete a file (or empty directory) under the workspace. Requires user approval.",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            delete_file,
            parallel_safe=False,
            requires_approval=True,
        )
    )
    reg.register(
        Tool(
            "list_dir",
            "List files in a directory.",
            {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
                "required": [],
            },
            list_dir,
            parallel_safe=True,
        )
    )
    reg.register(
        Tool(
            "search_text",
            "Recursive substring search in files.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "glob": {"type": "string", "default": "*.*"},
                },
                "required": ["query"],
            },
            search_text,
            parallel_safe=True,
        )
    )
    reg.register(
        Tool(
            "codebase_overview",
            "Summarize workspace structure from the codebase index (dirs, suffixes, sample symbols). "
            "Use to understand what already exists before designing new work. "
            "Pass refresh=true after large external file changes.",
            {
                "type": "object",
                "properties": {
                    "refresh": {
                        "type": "boolean",
                        "description": "Force rebuild the index from disk.",
                    }
                },
                "required": [],
            },
            codebase_overview,
            parallel_safe=True,
        )
    )
    reg.register(
        Tool(
            "codebase_find_similar",
            "Find existing files/symbols similar to an intended capability. "
            "REQUIRED before creating a new code file. Prefer reuse/extension when matches exist.",
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What you intend to build or change (capability, name, or path hint).",
                    },
                    "limit": {"type": "integer", "default": 12},
                },
                "required": ["query"],
            },
            codebase_find_similar,
            parallel_safe=True,
        )
    )
    reg.register(
        Tool(
            "codebase_impact",
            "Estimate blast radius: files that reference a symbol or path. "
            "Call before editing shared modules.",
            {
                "type": "object",
                "properties": {
                    "symbol_or_path": {"type": "string"},
                    "limit": {"type": "integer", "default": 40},
                },
                "required": ["symbol_or_path"],
            },
            codebase_impact,
            parallel_safe=True,
        )
    )
    reg.register(
        Tool(
            "coherence_checklist",
            "Return the Anti-Piling checklist (overlay / hardcode / control-flow / blast). "
            "Call near the end of LARGE structural work; answer each item with file evidence.",
            {"type": "object", "properties": {}, "required": []},
            coherence_checklist,
            parallel_safe=True,
        )
    )
    reg.register(
        Tool(
            "git_status",
            "Show git status --short --branch for the workspace.",
            {"type": "object", "properties": {}, "required": []},
            git_status,
            parallel_safe=True,
        )
    )
    reg.register(
        Tool(
            "git_diff",
            "Show git diff (optionally staged, optionally one path).",
            {
                "type": "object",
                "properties": {
                    "staged": {"type": "boolean", "default": False},
                    "path": {"type": "string", "description": "Optional path filter"},
                },
                "required": [],
            },
            git_diff,
            parallel_safe=True,
        )
    )
    reg.register(
        Tool(
            "git_log",
            "Show recent commits (oneline).",
            {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 12}},
                "required": [],
            },
            git_log,
            parallel_safe=True,
        )
    )
    reg.register(
        Tool(
            "git_branch",
            "List local branches (-vv).",
            {"type": "object", "properties": {}, "required": []},
            git_branch,
            parallel_safe=True,
        )
    )
    reg.register(
        Tool(
            "git_commit",
            "Stage tracked changes (git add -u) and commit with a message. Requires approval. "
            "Does not force-add untracked files.",
            {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            git_commit,
            parallel_safe=False,
            requires_approval=True,
        )
    )
    reg.register(
        Tool(
            "verify_run",
            "Run a one-shot verification command (tests/lint). Requires approval and META_ALLOW_SHELL=1. "
            "Prefer this over open-ended shell for acceptance checks. "
            "If shape_contract.verify_command is set, run that before claiming done.",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_sec": {"type": "integer", "default": 120},
                },
                "required": ["command"],
            },
            verify_run,
            parallel_safe=False,
            requires_approval=True,
        )
    )
    if settings.allow_shell:
        reg.register(
            Tool(
                "run_shell",
                "Run a shell command in the workspace. Prefer read_file for reading files. "
                "IMPORTANT: long-running servers (npm run dev, vite, uvicorn --reload, etc.) "
                "are auto-started in background and return early with pid + startup logs — "
                "do NOT wait for them to exit. Set background=true to force background mode.",
                {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "background": {
                            "type": "boolean",
                            "description": "If true, start and return without waiting for exit.",
                        },
                    },
                    "required": ["command"],
                },
                run_shell,
                parallel_safe=False,
                requires_approval=True,
            )
        )
    for sk in list(skills):
        reg.register(_skill_as_tool(sk))

    reg.register(
        Tool(
            "skill_save",
            "Register a new skill_* function tool (writes SKILL.md under skills/learned).",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["name", "description", "content"],
            },
            skill_save,
            parallel_safe=False,
            requires_approval=True,
        )
    )
    reg.register(
        Tool(
            "memory_append",
            "Append a durable fact to MEMORY.md. Requires user approval.",
            {
                "type": "object",
                "properties": {"note": {"type": "string"}},
                "required": ["note"],
            },
            memory_append,
            parallel_safe=False,
            requires_approval=True,
        )
    )
    reg.register(
        Tool(
            "memory_remove",
            "Delete entries from MEMORY.md whose lines contain the given text "
            "(case-insensitive). Use when the user asks to forget/delete a memory. "
            "Requires user approval.",
            {
                "type": "object",
                "properties": {
                    "match": {
                        "type": "string",
                        "description": "Substring to find in memory lines to remove",
                    }
                },
                "required": ["match"],
            },
            memory_remove,
            parallel_safe=False,
            requires_approval=True,
        )
    )
    reg.register(
        Tool(
            "memory_write",
            "Overwrite MEMORY.md with the full new content. Prefer memory_remove for "
            "deleting one fact; use this for larger edits. Requires user approval.",
            {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
            },
            memory_write,
            parallel_safe=False,
            requires_approval=True,
        )
    )
    reg.register(
        Tool(
            "memory_read",
            "Read MEMORY.md.",
            {"type": "object", "properties": {}, "required": []},
            memory_read,
            parallel_safe=True,
        )
    )

    if ask_user_fn is not None:

        def ask_user(
            question: str,
            options: list[str],
            allow_custom: bool = True,
            custom_label: str = "其他（请补充）",
        ) -> str:
            return ask_user_fn(
                question=question,
                options=normalize_option_labels(options),
                allow_custom=bool(allow_custom),
                custom_label=custom_label or "其他（请补充）",
            )

        reg.register(
            Tool(
                "ask_user",
                "Ask the user to clarify ONLY when a real decision or missing info blocks progress. "
                "Do NOT use ask_user to summarize the conversation, list past user tasks, or answer "
                "meta questions answerable from chat history — reply in normal assistant text instead. "
                "The UI shows clickable buttons; NEVER print numbered/lettered option "
                "lists in assistant text. Provide question + options (array of 2–12 "
                "short labels). Set allow_custom=true so the user can type a custom "
                "answer. Wait for the result before continuing.",
                {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "Clear question explaining what you need.",
                        },
                        "options": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 2,
                            "maxItems": 12,
                            "description": "2–12 choice labels shown as buttons.",
                        },
                        "allow_custom": {
                            "type": "boolean",
                            "description": "Show a free-text 'other' field (default true).",
                            "default": True,
                        },
                        "custom_label": {
                            "type": "string",
                            "description": "Label for the custom/other choice.",
                            "default": "其他（请补充）",
                        },
                    },
                    "required": ["question", "options"],
                },
                ask_user,
                parallel_safe=False,
            )
        )

    if allow_delegate and run_child is not None:

        def delegate_task(
            goal: str = "",
            context: str = "",
            role: str = "leaf",
            tasks: Optional[list[dict[str, Any]]] = None,
        ) -> str:
            items: list[dict[str, Any]]
            if tasks:
                items = tasks
            elif goal:
                items = [{"goal": goal, "context": context, "role": role}]
            else:
                return "ERROR: provide goal or tasks[]"

            if len(items) > settings.max_concurrent_children:
                return (
                    f"ERROR: max {settings.max_concurrent_children} children; got {len(items)}"
                )

            results: list[str | None] = [None] * len(items)

            def _one(idx: int, item: dict[str, Any]) -> tuple[int, str]:
                g = str(item.get("goal") or "").strip()
                ctx = str(item.get("context") or "").strip()
                r = str(item.get("role") or role or "leaf")
                if not g:
                    return idx, "ERROR: empty goal"
                try:
                    summary = run_child(goal=g, context=ctx, role=r)
                except Exception as exc:  # noqa: BLE001
                    summary = f"ERROR: child failed: {exc}"
                return idx, summary

            with ThreadPoolExecutor(max_workers=settings.max_concurrent_children) as pool:
                futs = [pool.submit(_one, i, it) for i, it in enumerate(items)]
                for fut in as_completed(futs):
                    i, summary = fut.result()
                    results[i] = summary

            payload = [
                {
                    "index": i,
                    "goal": items[i].get("goal"),
                    "summary": results[i],
                }
                for i in range(len(items))
            ]
            return json.dumps(payload, ensure_ascii=False, indent=2)

        reg.register(
            Tool(
                "delegate_task",
                "Spawn isolated subagent(s). Single: goal(+context,+role). "
                "Parallel: tasks=[{goal,context,role?}]. Only summaries return. "
                "Children have no parent history. role=orchestrator may re-delegate "
                "if depth allows.",
                {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        "context": {"type": "string"},
                        "role": {
                            "type": "string",
                            "enum": ["leaf", "orchestrator"],
                            "default": "leaf",
                        },
                        "tasks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "goal": {"type": "string"},
                                    "context": {"type": "string"},
                                    "role": {"type": "string"},
                                },
                                "required": ["goal"],
                            },
                        },
                    },
                    "required": [],
                },
                delegate_task,
                parallel_safe=False,
            )
        )

    if getattr(settings, "mcp_enabled", True):
        try:
            from ..services.mcp_runtime import register_mcp_tools

            register_mcp_tools(reg, Tool=Tool)
        except Exception as exc:  # noqa: BLE001
            from ..core.logutil import get_logger, log_exception

            log_exception(get_logger("metateam.tools"), "MCP tool registration failed", exc)

    return reg


def plan_parallel_batches(
    tool_calls: list[dict[str, Any]], registry: ToolRegistry
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for tc in tool_calls:
        name = (tc.get("function") or {}).get("name", "")
        tool = registry.get(name)
        safe = bool(tool and tool.parallel_safe)
        if safe:
            current.append(tc)
        else:
            if current:
                batches.append(current)
                current = []
            batches.append([tc])
    if current:
        batches.append(current)
    return batches
