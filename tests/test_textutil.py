"""safe_clip should not leave dangling markdown bold."""

from __future__ import annotations

from metateam.core.textutil import safe_clip


def test_safe_clip_avoids_dangling_bold() -> None:
    text = "完成页面后，请**插入**一张封面图并导出。"
    # Cut right after the opening ** and first Chinese char of 插入
    cut_at = text.index("**插") + len("**插")
    out = safe_clip(text, cut_at)
    assert "**插" not in out
    assert out.endswith("…")
    assert out.count("**") % 2 == 0


def test_safe_clip_short_passthrough() -> None:
    assert safe_clip("hello", 20) == "hello"
