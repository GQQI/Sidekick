"""Smoke tests for Codebase-as-Memory index + align gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from metateam.core.config import Settings
from metateam.runtime.tools import build_registry
from metateam.services import codebase_memory as cbm


def _scratch() -> Path:
    d = Path(__file__).resolve().parent / ".scratch" / "codebase_memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_find_similar_prefers_existing_symbol() -> None:
    ws = _scratch() / "proj"
    if ws.exists():
        for p in sorted(ws.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                try:
                    p.rmdir()
                except OSError:
                    pass
    (ws / "ui" / "components").mkdir(parents=True, exist_ok=True)
    (ws / "ui" / "components" / "Button.tsx").write_text(
        "export function Button() { return null }\n",
        encoding="utf-8",
    )
    (ws / "ui" / "components" / "Modal.tsx").write_text(
        "export function Modal() { return null }\n",
        encoding="utf-8",
    )
    cbm.invalidate_index(ws)
    index = cbm.build_index(ws)
    hits = cbm.find_similar(index, "button component")
    assert hits, "expected similar hits"
    assert any("Button" in h["path"] for h in hits)


def test_stale_index_rebuilds_after_delete() -> None:
    ws = _scratch() / "stale"
    if ws.exists():
        for p in sorted(ws.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                try:
                    p.rmdir()
                except OSError:
                    pass
    ws.mkdir(parents=True, exist_ok=True)
    target = ws / "Gone.tsx"
    target.write_text("export function Gone(){return null}\n", encoding="utf-8")
    cbm.invalidate_index(ws)
    idx1 = cbm.get_or_build_index(ws, force=True)
    assert idx1.file_count() == 1
    target.unlink()
    idx2 = cbm.get_or_build_index(ws, force=False)
    assert idx2.file_count() == 0


def test_write_file_html_skips_align() -> None:
    from metateam.core.config import get_settings

    ws = _scratch() / "html_gate"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "existing.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    s = get_settings()
    prev = s.workspace
    s.workspace = ws
    try:
        settings = Settings(workspace=ws, allow_shell=False, demo_mode=True)
        reg = build_registry(settings, skills=[])
        write = reg.get("write_file")
        assert write is not None
        ok = write.handler(path="deck.html", content="<html><body>hi</body></html>\n")
        assert ok.startswith("wrote"), ok
    finally:
        s.workspace = prev


def test_write_file_requires_align_for_new_code() -> None:
    from metateam.core.config import get_settings

    ws = _scratch() / "gate"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "existing.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    # fs_api.write_text uses global settings.workspace
    s = get_settings()
    prev = s.workspace
    s.workspace = ws
    try:
        settings = Settings(workspace=ws, allow_shell=False, demo_mode=True)
        reg = build_registry(settings, skills=[])
        write = reg.get("write_file")
        assert write is not None
        target = ws / "brand_new_widget.py"
        if target.exists():
            target.unlink()
        blocked = write.handler(path="brand_new_widget.py", content="def x():\n    pass\n")
        assert "codebase_align_required" in blocked

        find = reg.get("codebase_find_similar")
        assert find is not None
        raw = find.handler(query="widget")
        payload = json.loads(raw)
        assert payload.get("aligned") is True

        ok = write.handler(path="brand_new_widget.py", content="def x():\n    pass\n")
        assert ok.startswith("wrote"), ok
    finally:
        s.workspace = prev


if __name__ == "__main__":
    test_find_similar_prefers_existing_symbol()
    test_stale_index_rebuilds_after_delete()
    test_write_file_requires_align_for_new_code()
    print("ok")
