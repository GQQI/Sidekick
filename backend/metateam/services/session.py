"""Optional JSON session persistence (transcript + light meta)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


@dataclass
class SessionMeta:
    id: str
    created_at: str
    model: str
    workspace: str
    updated_at: str = ""


def _now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sessions_dir(root: Path) -> Path:
    d = root / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_session(
    root: Path,
    messages: list[dict[str, Any]],
    *,
    model: str,
    workspace: Path,
    session_id: Optional[str] = None,
) -> Path:
    sid = session_id or _now_id()
    path = sessions_dir(root) / f"{sid}.json"
    created = _now_iso()
    if path.exists():
        try:
            old_meta, _ = load_session(path)
            if old_meta.created_at:
                created = old_meta.created_at
        except Exception:
            pass
    meta = SessionMeta(
        id=sid,
        created_at=created,
        updated_at=_now_iso(),
        model=model,
        workspace=str(workspace),
    )
    path.write_text(
        json.dumps({"meta": asdict(meta), "messages": messages}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load_session(path: Path) -> tuple[SessionMeta, list[dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    m = data.get("meta") or {}
    meta = SessionMeta(
        id=str(m.get("id") or path.stem),
        created_at=str(m.get("created_at") or ""),
        updated_at=str(m.get("updated_at") or ""),
        model=str(m.get("model") or ""),
        workspace=str(m.get("workspace") or ""),
    )
    messages = data.get("messages") or []
    if not isinstance(messages, list):
        raise ValueError("invalid session: messages must be a list")
    return meta, messages


def latest_session(root: Path) -> Optional[Path]:
    files = sorted(sessions_dir(root).glob("*.json"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None
