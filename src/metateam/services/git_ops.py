"""Git helpers for workspace-scoped agent tools (no shell=True)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def _run_git(workspace: Path, args: list[str], *, timeout: float = 60.0) -> tuple[int, str, str]:
    ws = workspace.resolve()
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(ws),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", "git executable not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"git timed out after {timeout}s"


def is_git_repo(workspace: Path) -> bool:
    code, out, _ = _run_git(workspace, ["rev-parse", "--is-inside-work-tree"], timeout=10)
    return code == 0 and out.strip().lower() == "true"


def git_status(workspace: Path) -> str:
    if not is_git_repo(workspace):
        return "ERROR: not a git repository"
    code, out, err = _run_git(workspace, ["status", "--short", "--branch"])
    if code != 0:
        return f"ERROR: git status failed ({code}): {err.strip() or out.strip()}"
    return out.strip() or "(clean)"


def git_diff(workspace: Path, *, staged: bool = False, path: str = "") -> str:
    if not is_git_repo(workspace):
        return "ERROR: not a git repository"
    args = ["diff"]
    if staged:
        args.append("--cached")
    rel = (path or "").strip()
    if rel:
        args.extend(["--", rel])
    code, out, err = _run_git(workspace, args, timeout=90)
    if code != 0 and not out.strip():
        return f"ERROR: git diff failed ({code}): {err.strip() or out.strip()}"
    text = out.strip()
    if len(text) > 24_000:
        text = text[:24_000] + "\n…[diff truncated]"
    return text or "(no diff)"


def git_log(workspace: Path, *, limit: int = 12) -> str:
    if not is_git_repo(workspace):
        return "ERROR: not a git repository"
    n = max(1, min(int(limit), 40))
    code, out, err = _run_git(
        workspace,
        ["log", f"-{n}", "--oneline", "--decorate"],
        timeout=30,
    )
    if code != 0:
        return f"ERROR: git log failed ({code}): {err.strip() or out.strip()}"
    return out.strip() or "(no commits)"


def git_branch(workspace: Path) -> str:
    if not is_git_repo(workspace):
        return "ERROR: not a git repository"
    code, out, err = _run_git(workspace, ["branch", "-vv"])
    if code != 0:
        return f"ERROR: git branch failed ({code}): {err.strip() or out.strip()}"
    return out.strip() or "(no branches)"


def git_commit(workspace: Path, message: str) -> str:
    """Stage tracked modifications + create commit. Does not force-add untracked files."""
    if not is_git_repo(workspace):
        return "ERROR: not a git repository"
    msg = (message or "").strip()
    if not msg:
        return "ERROR: empty commit message"
    if len(msg) > 2000:
        return "ERROR: commit message too long"

    code, out, err = _run_git(workspace, ["add", "-u"])
    if code != 0:
        return f"ERROR: git add -u failed ({code}): {err.strip() or out.strip()}"

    code, out, err = _run_git(workspace, ["commit", "-m", msg], timeout=60)
    if code != 0:
        detail = (err or out).strip()
        return f"ERROR: git commit failed ({code}): {detail or 'nothing to commit?'}"
    return (out or err).strip() or "committed"


def format_git_snapshot(workspace: Path) -> dict[str, Any]:
    repo = is_git_repo(workspace)
    return {
        "is_repo": repo,
        "status": git_status(workspace) if repo else "",
        "branch": git_branch(workspace) if repo else "",
    }
