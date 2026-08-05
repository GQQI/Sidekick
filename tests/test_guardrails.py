# -*- coding: utf-8 -*-
"""Guardrails: fail loops + read thrashing."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from metateam.core.guardrails import Guardrails


def test_read_file_path_limit() -> None:
    g = Guardrails(max_reads_per_path=3, max_reads_total=20)
    g.begin_turn()
    for _ in range(3):
        assert g.before("read_file", {"path": "a.py", "offset": 1}) is None
        g.after("read_file", {"path": "a.py"}, "ok lines")
    blocked = g.before("read_file", {"path": "a.py", "offset": 200})
    assert blocked and "read_file limit" in blocked


def test_explore_streak_nudge() -> None:
    g = Guardrails(max_explore_streak=5, max_reads_per_path=50, max_reads_total=100)
    g.begin_turn()
    for i in range(4):
        assert g.before("list_dir", {"path": "."}) is None
        g.after("list_dir", {"path": "."}, "file\tx")
    assert g.force_progress_hint is True
    nudge = g.progress_nudge()
    assert nudge and "exploring" in nudge.lower()
    # After streak hits max, before blocks
    g.after("list_dir", {"path": "."}, "file\ty")  # streak 5
    blocked = g.before("search_text", {"query": "x"})
    assert blocked and "explore streak" in blocked


if __name__ == "__main__":
    test_read_file_path_limit()
    test_explore_streak_nudge()
    print("ok")
