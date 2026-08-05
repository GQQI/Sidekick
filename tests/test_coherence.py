# -*- coding: utf-8 -*-
"""Tests for Anti-Piling turn routing and shape contracts."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from metateam.runtime.coherence import (
    TurnKind,
    classify_turn,
    format_shape_contract_markdown,
    merge_policy_into_system,
    normalize_shape_contract,
    policy_for_turn,
)


def test_chat_skips_align() -> None:
    p = policy_for_turn("\u5f53\u524d\u5bf9\u8bdd\u4e2d\u6211\u63d0\u4e86\u54ea\u4e9b\u4efb\u52a1\uff1f")
    assert p.kind == TurnKind.CHAT
    assert not p.require_align
    assert not p.require_shape_contract


def test_targeted_skips_align() -> None:
    p = policy_for_turn("\u628aindex.html\u7684\u6807\u9898\u6539\u6210Demo")
    assert p.kind == TurnKind.TARGETED
    assert not p.require_align


def test_structural_requires_align() -> None:
    p = policy_for_turn(
        "\u5e2e\u6211\u505a\u4e00\u4e2a\u767b\u5f55\u9875\u9762\u7ec4\u4ef6\uff0c\u8981\u80fd\u590d\u7528\u73b0\u6709\u6837\u5f0f"
    )
    assert p.kind in (TurnKind.STRUCTURAL, TurnKind.LARGE)
    assert p.require_align
    assert p.require_shape_contract


def test_large_requires_pile_check() -> None:
    p = policy_for_turn(
        "\u4ece\u96f6\u91cd\u6784\u6574\u4e2a\u524d\u7aef\u9879\u76ee\uff0c\u62c6\u5206\u6a21\u5757\u5e76\u7edf\u4e00\u8bf7\u6c42\u5c42"
    )
    assert p.kind == TurnKind.LARGE
    assert p.require_align
    assert p.require_pile_check


def test_shape_contract_normalize_and_merge() -> None:
    c = normalize_shape_contract({"reuse": "Button.tsx", "extra": 1})
    assert c["reuse"] == "Button.tsx"
    assert "create_only_if" in c
    md = format_shape_contract_markdown(c)
    assert "Button.tsx" in md
    merged = merge_policy_into_system("sys", "## Turn coherence policy (Anti-Piling)\nold")
    assert merged.startswith("sys")
    assert merged.count("## Turn coherence policy") == 1


def test_classify_smoke() -> None:
    assert classify_turn("hello") == TurnKind.CHAT


if __name__ == "__main__":
    test_chat_skips_align()
    test_targeted_skips_align()
    test_structural_requires_align()
    test_large_requires_pile_check()
    test_shape_contract_normalize_and_merge()
    test_classify_smoke()
    print("ok")
