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


def _run_shell_background(command: str, *, cwd: str, collect_secs: float = 8.0) -> str:
    """Start a process and return after collecting early logs (does not wait for exit)."""
    popen_kwargs: dict[str, Any] = {
        "shell": True,
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": {**os.environ, "PYTHONIOENCODING": "utf-8"},
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        # Detach from parent session so Ctrl+C on the server doesn't kill child servers
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(command, **popen_kwargs)
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


def build_registry(
    settings: Settings,
    *,
    skills: list[Skill],
    allow_delegate: bool = True,
    run_child: Optional[Callable[..., str]] = None,
) -> ToolRegistry:
    reg = ToolRegistry()
    ws = settings.workspace

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

    def write_file(path: str, content: str) -> str:
        from ..services import fs_api

        try:
            # Relative to workspace for undo + consistent path rules
            try:
                rel = str(Path(path)).replace("\\", "/")
                if Path(path).is_absolute():
                    rel = str(Path(path).resolve().relative_to(ws.resolve())).replace("\\", "/")
            except Exception:
                rel = path.replace("\\", "/")
            res = fs_api.write_text(rel, content)
            return f"wrote {res['path']} ({res['size']} chars)"
        except Exception as exc:
            return f"ERROR: {exc}"

    def delete_file(path: str) -> str:
        from ..services import fs_api

        try:
            rel = path.replace("\\", "/")
            try:
                if Path(path).is_absolute():
                    rel = str(Path(path).resolve().relative_to(ws.resolve())).replace("\\", "/")
            except Exception:
                pass
            res = fs_api.delete_entry(rel, recursive=False)
            return f"deleted {res['path']}"
        except Exception as exc:
            return f"ERROR: {exc}"

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
            return "ERROR: shell disabled"
        bad = ("rm -rf /", "format c:", ":(){:|:&};:", "del /s /q c:")
        low = command.lower().strip()
        if any(b in low for b in bad):
            return "ERROR: blocked dangerous command"

        # Dev servers / watchers never exit — must not block the agent.
        long_running = background or _is_long_running_command(low)
        if long_running:
            return _run_shell_background(command, cwd=str(ws), collect_secs=8.0)

        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(ws),
                capture_output=True,
                text=True,
                timeout=settings.shell_timeout,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
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
            "Write/overwrite a text file under the workspace. Requires user approval.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
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
