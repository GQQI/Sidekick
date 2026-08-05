"""Workspace directory selection — any local absolute folder path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.config import ROOT, get_settings
from .tenant_context import DEFAULT_USER_ID, get_user_id, tenant_workspace_path

MAX_RECENT = 12


def _state_path() -> Path:
    uid = get_user_id()
    path = tenant_workspace_path(uid)
    if path.exists():
        return path
    legacy = ROOT / "data" / "workspace.json"
    if uid == DEFAULT_USER_ID and legacy.exists():
        return legacy
    return path


# Back-compat
STATE_PATH = ROOT / "data" / "workspace.json"


def _read_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(path: Path, recent: list[str] | None = None) -> None:
    state_path = tenant_workspace_path(get_user_id())
    state_path.parent.mkdir(parents=True, exist_ok=True)
    prev = _read_state()
    items = recent if recent is not None else list(prev.get("recent") or [])
    resolved = str(path.resolve())
    items = [resolved, *[p for p in items if p != resolved]]
    items = items[:MAX_RECENT]
    state_path.write_text(
        json.dumps({"path": resolved, "recent": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def is_configured() -> bool:
    data = _read_state()
    raw = str(data.get("path") or "").strip()
    if not raw:
        return False
    try:
        return Path(raw).expanduser().resolve().is_dir()
    except Exception:
        return False


def list_workspaces() -> list[dict[str, Any]]:
    """Recent workspace folders (absolute paths on this machine)."""
    data = _read_state()
    recent = list(data.get("recent") or [])
    active = str(data.get("path") or "").strip()
    if active and active not in recent:
        recent = [active, *recent]

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in recent:
        try:
            p = Path(str(raw)).expanduser().resolve()
        except Exception:
            continue
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        if not p.is_dir():
            continue
        items.append(
            {
                "id": key,
                "name": p.name or key,
                "path": key,
                "is_default": False,
            }
        )
    return items[:MAX_RECENT]


def get_active_workspace() -> dict[str, Any]:
    configured = is_configured()
    if not configured:
        return {"path": "", "name": "", "configured": False}
    s = get_settings()
    path = Path(s.workspace).resolve()
    return {"path": str(path), "name": path.name or str(path), "configured": True}


def set_workspace(path_or_name: str, *, create: bool = False) -> dict[str, Any]:
    """Set active workspace to an absolute folder on this machine."""
    raw = path_or_name.strip().strip('"').strip("'")
    if not raw:
        raise ValueError("请填写本机文件夹的绝对路径")

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError("请使用绝对路径，例如 D:\\Projects\\my-app 或 /home/user/proj")

    candidate = candidate.resolve()
    if not candidate.exists():
        if create:
            candidate.mkdir(parents=True, exist_ok=True)
        else:
            raise ValueError(f"路径不存在：{candidate}")
    if not candidate.is_dir():
        raise ValueError(f"不是文件夹：{candidate}")

    _write_state(candidate)

    s = get_settings()
    s.workspace = candidate
    return {
        "path": str(candidate),
        "name": candidate.name or str(candidate),
        "configured": True,
    }


def create_workspace(path: str) -> dict[str, Any]:
    """Create folder (if needed) and switch to it. `path` must be absolute."""
    return set_workspace(path, create=True)


def apply_saved_workspace() -> None:
    data = _read_state()
    raw = str(data.get("path") or "").strip()
    if not raw:
        return
    try:
        path = Path(raw).expanduser().resolve()
        if path.is_dir():
            get_settings().workspace = path
    except Exception:
        pass
