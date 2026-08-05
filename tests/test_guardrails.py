# -*- coding: utf-8 -*-
"""Guardrails: fail loops + explore streak (read_file uncapped)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from metateam.core.guardrails import Guardrails


def test_read_file_has_no_path_limit() -> None:
    g = Guardrails(max_explore_streak=3)
    g.begin_turn()
    for i in range(20):
        assert g.before("read_file", {"path": "a.py", "offset": i * 100 + 1}) is None
        g.after("read_file", {"path": "a.py"}, "ok lines")
    # Still not hard-blocked even after a long explore streak of reads
    assert g.before("read_file", {"path": "a.py", "offset": 1}) is None


def test_explore_streak_nudge() -> None:
    g = Guardrails(max_explore_streak=5)
    g.begin_turn()
    for i in range(4):
        assert g.before("list_dir", {"path": "."}) is None
        g.after("list_dir", {"path": "."}, "file\tx")
    assert g.force_progress_hint is True
    nudge = g.progress_nudge()
    assert nudge and "exploring" in nudge.lower()
    # After streak hits max, non-read explore tools are blocked
    g.after("list_dir", {"path": "."}, "file\ty")  # streak 5
    blocked = g.before("search_text", {"query": "x"})
    assert blocked and "explore streak" in blocked
    # read_file remains allowed
    assert g.before("read_file", {"path": "x.py"}) is None


if __name__ == "__main__":
    test_read_file_has_no_path_limit()
    test_explore_streak_nudge()
    print("ok")
