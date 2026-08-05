"""Tests for Claude Code–style shell path allowlist sandbox."""

from __future__ import annotations

from pathlib import Path

from metateam.services.shell_sandbox import (
    ShellSandboxPolicy,
    check_command,
    path_allowed,
)


def test_path_allowed_under_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    ws.mkdir()
    policy = ShellSandboxPolicy.for_workspace(ws)
    assert path_allowed(ws / "src" / "a.py", policy)
    assert not path_allowed(tmp_path / "other" / "x", policy)


def test_blocks_absolute_outside(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    ws.mkdir()
    policy = ShellSandboxPolicy.for_workspace(ws)
    # Must be outside workspace AND outside system temp (temp is allowlisted)
    outside = Path("C:/SidekickSandboxForbidden/secret.txt")
    err = check_command(f'type "{outside}"', cwd=ws, policy=policy)
    assert err and "allowlist" in err


def test_allows_workspace_relative(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    ws.mkdir()
    policy = ShellSandboxPolicy.for_workspace(ws)
    assert check_command("pytest -q", cwd=ws, policy=policy) is None
    assert check_command("npm test", cwd=ws, policy=policy) is None


def test_blocks_cd_dotdot(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    ws.mkdir()
    policy = ShellSandboxPolicy.for_workspace(ws)
    err = check_command("cd .. && dir", cwd=ws, policy=policy)
    assert err and "escape" in err


def test_disabled_policy(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    ws.mkdir()
    policy = ShellSandboxPolicy.for_workspace(ws, enabled=False)
    outside = tmp_path / "x.txt"
    assert check_command(f'cat "{outside}"', cwd=ws, policy=policy) is None
