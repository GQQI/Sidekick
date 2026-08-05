# -*- coding: utf-8 -*-
"""Inline ask parser must not hijack task summaries into choice dialogs."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from metateam.runtime.ask import try_parse_inline_ask


def test_summary_list_is_not_ask() -> None:
    text = (
        "\u5f53\u524d\u5bf9\u8bdd\u4e2d\u4f60\u63d0\u51fa\u4e86\u8fd9\u4e9b\u4efb\u52a1\uff1a\n"
        "1. \u5148\u770b\u770b\u5de5\u4f5c\u533a\u6709\u4ec0\u4e48\u6587\u4ef6\n"
        "2. \u4f18\u5316\u8fd9\u4e2a\u9875\u9762\u7684\u4ea4\u4e92\u548c\u6837\u5f0f\n"
        "3. \u590d\u7528\u73b0\u6709\u6309\u94ae\u7ec4\u4ef6"
    )
    assert try_parse_inline_ask(text) is None


def test_real_choice_is_ask() -> None:
    text = (
        "\u8bf7\u9009\u62e9\u4e0b\u4e00\u6b65\u8981\u505a\u7684\u4e8b\uff1a\n"
        "1. \u53ea\u6539\u6837\u5f0f\n"
        "2. \u6539\u4ea4\u4e92\u903b\u8f91\n"
        "3. \u4e24\u8005\u90fd\u505a"
    )
    parsed = try_parse_inline_ask(text)
    assert parsed is not None
    assert len(parsed["options"]) >= 2


if __name__ == "__main__":
    test_summary_list_is_not_ask()
    test_real_choice_is_ask()
    print("ok")
