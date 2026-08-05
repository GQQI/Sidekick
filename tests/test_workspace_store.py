"""Workspace persistence must rebind after tenant context is set."""

from __future__ import annotations

import json
from pathlib import Path

from metateam.core.config import get_settings
from metateam.services.tenant_context import set_user
from metateam.services import workspace_store as ws


def test_apply_saved_workspace_after_login(tmp_path: Path, monkeypatch) -> None:
    user = "u_ws_test"
    tenants = tmp_path / "tenants"
    tenant = tenants / user
    tenant.mkdir(parents=True)
    chosen = tmp_path / "my-project"
    chosen.mkdir()
    (tenant / "workspace.json").write_text(
        json.dumps({"path": str(chosen), "recent": [str(chosen)]}),
        encoding="utf-8",
    )

    def _tenant_path(uid=None):
        from metateam.services.tenant_context import get_user_id

        u = uid or get_user_id()
        return tenants / u / "workspace.json"

    monkeypatch.setattr(ws, "tenant_workspace_path", _tenant_path)
    monkeypatch.setattr(ws, "ROOT", tmp_path)
    monkeypatch.setattr(ws, "REPO_ROOT", tmp_path)

    # Simulate startup: no user → tenant file invisible, no legacy either
    set_user("default", "")
    settings = get_settings()
    settings.workspace = tmp_path / "default-ws"
    assert ws.apply_saved_workspace() is False

    # After login, saved path must win
    set_user(user, "tester")
    changed = ws.apply_saved_workspace()
    assert changed is True
    assert Path(settings.workspace).resolve() == chosen.resolve()

    active = ws.get_active_workspace()
    assert active["configured"] is True
    assert Path(active["path"]).resolve() == chosen.resolve()
