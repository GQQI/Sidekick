"""Claude Code–style shell sandbox: real disk, path allowlist (not a copy FS).

Bash/cmd still runs with cwd=workspace on the host. We only restrict which
paths the command may touch (heuristic scan + cwd fence) and scrub the env.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

# Absolute / drive / UNC / home-relative path-like tokens in a shell command.
_PATH_TOKEN_RE = re.compile(
    r"(?:"
    r'(?P<q>["\'])(?P<qp>(?:[A-Za-z]:[\\/]|\\\\|~/|/)[^"\']*)(?P=q)'
    r"|"
    r"(?P<u>(?:[A-Za-z]:[\\/]|\\\\|~/|/)[^\s\"';|&<>]+)"
    r")"
)

# Relative segments that climb out of cwd when resolved.
_DOTDOT_RE = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")


@dataclass(frozen=True)
class ShellSandboxPolicy:
    """Writable/readable roots for sandboxed shell (host paths)."""

    roots: tuple[Path, ...]
    enabled: bool = True

    @classmethod
    def for_workspace(
        cls,
        workspace: Path,
        *,
        extra: Optional[Iterable[Path]] = None,
        enabled: bool = True,
    ) -> "ShellSandboxPolicy":
        roots: list[Path] = []
        try:
            roots.append(workspace.expanduser().resolve())
        except OSError:
            roots.append(workspace.expanduser())
        try:
            roots.append(Path(tempfile.gettempdir()).resolve())
        except OSError:
            roots.append(Path(tempfile.gettempdir()))
        for p in extra or ():
            try:
                roots.append(Path(p).expanduser().resolve())
            except OSError:
                continue
        # Dedupe
        seen: set[str] = set()
        uniq: list[Path] = []
        for r in roots:
            key = str(r).lower() if os.name == "nt" else str(r)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(r)
        return cls(roots=tuple(uniq), enabled=enabled)


def _norm(p: Path) -> Path:
    try:
        return p.expanduser().resolve()
    except OSError:
        return p.expanduser()


def path_allowed(path: Path, policy: ShellSandboxPolicy) -> bool:
    target = _norm(path)
    for root in policy.roots:
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _extract_path_candidates(command: str) -> list[str]:
    found: list[str] = []
    for m in _PATH_TOKEN_RE.finditer(command or ""):
        raw = m.group("qp") or m.group("u") or ""
        raw = raw.strip()
        if raw:
            found.append(raw)
    return found


def check_command(
    command: str,
    *,
    cwd: Path,
    policy: ShellSandboxPolicy,
) -> Optional[str]:
    """Return an error string if the command violates the sandbox; else None."""
    if not policy.enabled:
        return None
    cmd = (command or "").strip()
    if not cmd:
        return "ERROR: empty command"

    cwd_r = _norm(cwd)
    if not path_allowed(cwd_r, policy):
        return f"ERROR: shell cwd outside sandbox: {cwd_r}"

    # Bare `cd ..` / `cd ../..` style escapes (common in agent output).
    if re.search(r"(?:^|[;&|]\s*)cd\s+\.\.(?:\s|$|[;&|])", cmd, re.IGNORECASE):
        return "ERROR: shell sandbox blocked path escape (cd ..)"

    for token in _extract_path_candidates(cmd):
        expanded = token
        if expanded.startswith("~/") or expanded.startswith("~\\"):
            expanded = str(Path.home() / expanded[2:])
        p = Path(expanded)
        if not p.is_absolute():
            # Relative with .. that would leave cwd
            if _DOTDOT_RE.search(token.replace("/", os.sep)):
                try:
                    resolved = (cwd_r / p).resolve()
                except OSError:
                    return f"ERROR: shell sandbox blocked path: {token}"
                if not path_allowed(resolved, policy):
                    return f"ERROR: shell sandbox blocked path outside allowlist: {token}"
            continue
        if not path_allowed(p, policy):
            return f"ERROR: shell sandbox blocked path outside allowlist: {token}"

    return None


def sandbox_env(base: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Env for sandboxed subprocess — keep PATH/HOME, drop obvious secrets noise optional."""
    src = dict(base or os.environ)
    # Always force UTF-8 for Python child output
    src["PYTHONIOENCODING"] = "utf-8"
    src["SIDEKICK_SHELL_SANDBOX"] = "1"
    return src


def describe_policy(policy: ShellSandboxPolicy) -> str:
    if not policy.enabled:
        return "shell sandbox: off"
    roots = ", ".join(str(r) for r in policy.roots)
    return f"shell sandbox: on · allowlist=[{roots}]"
