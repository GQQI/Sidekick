"""Per-request tenant (local multi-user) context."""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Optional

from ..core.config import ROOT

# "default" = legacy single-user / pre-setup bucket
DEFAULT_USER_ID = "default"

_current_user_id: ContextVar[str] = ContextVar("sidekick_user_id", default=DEFAULT_USER_ID)
_current_username: ContextVar[str] = ContextVar("sidekick_username", default="")


def get_user_id() -> str:
    return _current_user_id.get() or DEFAULT_USER_ID


def get_username() -> str:
    return _current_username.get() or ""


def set_user(user_id: str, username: str = "") -> None:
    _current_user_id.set(user_id or DEFAULT_USER_ID)
    _current_username.set(username or "")


def reset_user() -> None:
    _current_user_id.set(DEFAULT_USER_ID)
    _current_username.set("")


def tenants_root() -> Path:
    p = ROOT / "data" / "tenants"
    p.mkdir(parents=True, exist_ok=True)
    return p


def tenant_dir(user_id: Optional[str] = None) -> Path:
    uid = (user_id or get_user_id() or DEFAULT_USER_ID).strip() or DEFAULT_USER_ID
    # sanitize path segment
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in uid)[:64] or DEFAULT_USER_ID
    p = tenants_root() / safe
    p.mkdir(parents=True, exist_ok=True)
    return p


def tenant_model_path(user_id: Optional[str] = None) -> Path:
    return tenant_dir(user_id) / "model.json"


def tenant_workspace_path(user_id: Optional[str] = None) -> Path:
    return tenant_dir(user_id) / "workspace.json"


def tenant_mcp_path(user_id: Optional[str] = None) -> Path:
    return tenant_dir(user_id) / "mcp.json"


def tenant_sessions_dir(user_id: Optional[str] = None) -> Path:
    """Sessions live under src/sessions/<user_id>/."""
    uid = (user_id or get_user_id() or DEFAULT_USER_ID).strip() or DEFAULT_USER_ID
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in uid)[:64] or DEFAULT_USER_ID
    p = ROOT / "sessions" / safe
    p.mkdir(parents=True, exist_ok=True)
    return p
