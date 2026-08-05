# -*- coding: utf-8 -*-
"""Git ops smoke tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from metateam.services import git_ops


def test_git_status_on_repo() -> None:
    # This project itself is a git repo
    out = git_ops.git_status(ROOT)
    assert not out.startswith("ERROR"), out
    assert out


def test_git_log_on_repo() -> None:
    out = git_ops.git_log(ROOT, limit=3)
    assert not out.startswith("ERROR"), out


def test_not_a_repo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        assert git_ops.git_status(scratch).startswith("ERROR")


if __name__ == "__main__":
    test_git_status_on_repo()
    test_git_log_on_repo()
    test_not_a_repo()
    print("ok")
