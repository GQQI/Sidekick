"""FastAPI server — REST + SSE streaming for the Sidekick console."""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.base import BaseHTTPMiddleware

from ..core.config import REPO_ROOT, get_settings
from ..runtime.context import context_budget_tokens, messages_tokens, schemas_tokens
from ..core.events import Event
from ..services.memory import read_memory, write_memory
from ..services.model_config import load_model_config, select_model_role, update_model_config
from ..services.local_auth import (
    TOKEN_HEADER,
    get_token,
    is_loopback_bind,
    load_or_create_token,
    peer_is_loopback,
)
from ..services.user_auth import (
    auth_status,
    create_user,
    list_users,
    login as user_login,
    multi_user_enabled,
    resolve_token,
    revoke_token,
    setup_admin,
)
from ..services.tenant_context import reset_user, set_user
from ..services.mcp_config import McpServerConfig, load_mcp_config, update_mcp_config
from ..services.mcp_runtime import test_server as mcp_test_server
from ..services.skills import load_skills
from ..services.store import STORE
from ..runtime.tools import skill_tool_name
from ..services.workspace_store import (
    apply_saved_workspace,
    create_workspace,
    get_active_workspace,
    is_configured,
    list_workspaces,
    set_workspace,
)
from ..services.folder_picker import pick_folder
from ..services import fs_api

app = FastAPI(title="Sidekick", version="0.3.1")

# Local console only — never reflect arbitrary browser Origins.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", TOKEN_HEADER, "Content-Type", "Accept"],
)


class LocalAuthMiddleware(BaseHTTPMiddleware):
    """Require X-Sidekick-Token on API routes; reject non-loopback peers."""

    PUBLIC_PREFIXES = (
        "/api/health",
        "/api/bootstrap",
        "/api/auth/status",
        "/api/auth/setup",
        "/api/auth/login",
        "/assets/",
        "/favicon",
    )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path or "/"
        client_host = request.client.host if request.client else None
        reset_user()

        # CORS preflight
        if request.method == "OPTIONS":
            return await call_next(request)

        if path.startswith("/api/") and not peer_is_loopback(client_host):
            return JSONResponse({"detail": "only loopback clients allowed"}, status_code=403)

        public = path == "/" or any(path.startswith(p) for p in self.PUBLIC_PREFIXES)
        # Static SPA files (js/css) are public; API is not.
        if path.startswith("/api/") and not public:
            token = request.headers.get(TOKEN_HEADER) or request.headers.get("X-Sidekick-Token")
            if not token:
                token = request.query_params.get("token")
            resolved = resolve_token(token)
            if not resolved:
                return JSONResponse({"detail": "missing or invalid local token"}, status_code=401)
            uid, uname = resolved
            set_user(uid, uname)
            request.state.user_id = uid
            request.state.username = uname
            # Startup applied workspace with no tenant context — rebind now.
            if apply_saved_workspace():
                try:
                    STORE.refresh_settings()
                except Exception:
                    pass
        elif path.startswith("/api/") and public:
            # Optional token on public routes (e.g. status while logged in)
            token = request.headers.get(TOKEN_HEADER) or request.headers.get("X-Sidekick-Token")
            resolved = resolve_token(token) if token else None
            if resolved:
                uid, uname = resolved
                set_user(uid, uname)
                request.state.user_id = uid
                request.state.username = uname
                if apply_saved_workspace():
                    try:
                        STORE.refresh_settings()
                    except Exception:
                        pass

        try:
            return await call_next(request)
        finally:
            reset_user()


app.add_middleware(LocalAuthMiddleware)


def _require_loopback(request: Request) -> None:
    host = request.client.host if request.client else None
    if not peer_is_loopback(host):
        raise HTTPException(403, "loopback only")


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    session_id: Optional[str] = None
    mode: str = "agent"  # "plan" | "agent"
    # Optional UI-facing text (e.g. "/skill name task") while `message` is the model prompt.
    display: Optional[str] = None


class MemoryUpdate(BaseModel):
    content: str


class ModelUpdate(BaseModel):
    # v3 multi-provider payload
    version: Optional[int] = None
    providers: Optional[list[dict[str, Any]]] = None
    main: Optional[dict[str, Any]] = None
    subagent: Optional[dict[str, Any]] = None
    compress: Optional[dict[str, Any]] = None
    # shared / legacy flat fields
    provider: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    subagent_model: Optional[str] = None
    compress_model: Optional[str] = None
    review_model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    thinking_enabled: Optional[bool] = None
    demo_mode: Optional[bool] = None
    temperature: Optional[float] = None


class ModelSelect(BaseModel):
    role: str  # main | subagent | compress
    provider_id: str
    model: str  # model id (or name fallback)


class WorkspaceSet(BaseModel):
    path: Optional[str] = None
    name: Optional[str] = None
    create: bool = False


class WorkspaceCreate(BaseModel):
    path: Optional[str] = None
    name: Optional[str] = None  # alias for path


class FileWrite(BaseModel):
    path: str = Field(..., min_length=1)
    content: str = ""


class FileCreate(BaseModel):
    path: str = Field(..., min_length=1)
    kind: str = "file"  # file | dir


class FileRename(BaseModel):
    path: str = Field(..., min_length=1)
    new_name: str = Field(..., min_length=1)


class FileMove(BaseModel):
    path: str = Field(..., min_length=1)
    dest_dir: str = "."


@app.get("/api/health")
def health() -> dict[str, Any]:
    s = get_settings()
    active = get_active_workspace()
    configured = bool(active.get("configured"))
    return {
        "ok": True,
        "demo": s.demo_mode,
        "model": s.model,
        "base_url": s.base_url,
        "provider": getattr(s, "provider", ""),
        "workspace": str(active.get("path") or ""),
        "workspace_configured": configured,
        "thinking_enabled": getattr(s, "thinking_enabled", False),
        "reasoning_effort": getattr(s, "reasoning_effort", ""),
        "context_limit": s.context_limit,
        "compress_trigger_ratio": s.compress_trigger_ratio,
        "allow_shell": bool(getattr(s, "allow_shell", False)),
        "shell_sandbox": bool(getattr(s, "shell_sandbox", True)),
        "mcp_enabled": bool(getattr(s, "mcp_enabled", True)),
    }


@app.get("/api/bootstrap")
def bootstrap(request: Request) -> dict[str, Any]:
    """Hand the local UI its API token (loopback only).

    After multi-user setup, returns auth_required and no device token —
    the UI must login via /api/auth/login.
    """
    _require_loopback(request)
    status = auth_status()
    if status["needs_setup"]:
        # Legacy single-device token until first admin is created
        return {
            **status,
            "token": get_token(),
            "token_header": "X-Sidekick-Token",
            "auth_required": False,
        }
    return {
        **status,
        "token": None,
        "token_header": "X-Sidekick-Token",
        "auth_required": True,
    }


class AuthSetupBody(BaseModel):
    username: str = Field(..., min_length=2)
    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=6)


class AuthLoginBody(BaseModel):
    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=1)
    # back-compat: old clients may still send username
    username: Optional[str] = None


class AuthCreateUserBody(BaseModel):
    username: str = Field(..., min_length=2)
    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=6)


@app.get("/api/auth/status")
def api_auth_status(request: Request) -> dict[str, Any]:
    _require_loopback(request)
    status = auth_status()
    user = None
    uid = getattr(request.state, "user_id", None)
    uname = getattr(request.state, "username", None)
    if uid and uname:
        user = {"id": uid, "username": uname}
    return {**status, "user": user, "authenticated": bool(user)}


@app.post("/api/auth/setup")
def api_auth_setup(body: AuthSetupBody, request: Request) -> dict[str, Any]:
    _require_loopback(request)
    try:
        user, token = setup_admin(body.username, body.password, email=body.email)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    STORE.refresh_settings()
    return {
        "status": "ok",
        "token": token,
        "token_header": "X-Sidekick-Token",
        "user": user.public(),
    }


@app.post("/api/auth/login")
def api_auth_login(body: AuthLoginBody, request: Request) -> dict[str, Any]:
    _require_loopback(request)
    try:
        email = (body.email or "").strip() or (body.username or "").strip()
        user, token = user_login(email=email, password=body.password)
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from exc
    return {
        "status": "ok",
        "token": token,
        "token_header": "X-Sidekick-Token",
        "user": user.public(),
    }


@app.post("/api/auth/logout")
def api_auth_logout(request: Request) -> dict[str, Any]:
    token = request.headers.get(TOKEN_HEADER) or request.headers.get("X-Sidekick-Token")
    revoke_token(token)
    return {"status": "ok"}


@app.get("/api/auth/me")
def api_auth_me(request: Request) -> dict[str, Any]:
    uid = getattr(request.state, "user_id", None)
    uname = getattr(request.state, "username", None)
    if not uid:
        raise HTTPException(401, "not authenticated")
    return {"id": uid, "username": uname or ""}


@app.get("/api/auth/users")
def api_auth_users() -> dict[str, Any]:
    return {"items": [u.public() for u in list_users()]}


@app.post("/api/auth/users")
def api_auth_create_user(body: AuthCreateUserBody) -> dict[str, Any]:
    if not multi_user_enabled():
        raise HTTPException(400, "complete setup first")
    try:
        user = create_user(body.username, body.password, email=body.email)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "ok", "user": user.public()}


class McpUpdateBody(BaseModel):
    version: Optional[int] = 1
    servers: list[dict[str, Any]] = Field(default_factory=list)


class McpTestBody(BaseModel):
    id: str = ""
    name: str = ""
    transport: str = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


@app.get("/api/mcp")
def api_mcp_get() -> dict[str, Any]:
    return load_mcp_config().public_dict()


@app.put("/api/mcp")
def api_mcp_put(body: McpUpdateBody) -> dict[str, Any]:
    setup = update_mcp_config(body.model_dump())
    return {"status": "ok", **setup.public_dict()}


@app.post("/api/mcp/test")
def api_mcp_test(body: McpTestBody) -> dict[str, Any]:
    server = McpServerConfig(
        id=body.id or "test",
        name=body.name or body.id or "test",
        transport=body.transport or "stdio",
        command=body.command or "",
        args=list(body.args or []),
        env=dict(body.env or {}),
        url=body.url or "",
        headers=dict(body.headers or {}),
        enabled=True,
    )
    return mcp_test_server(server)


@app.get("/api/workspaces")
def api_workspaces() -> dict[str, Any]:
    configured = is_configured()
    active = get_active_workspace()
    return {
        "configured": configured,
        "items": list_workspaces(),
        "active": active if configured else None,
    }


@app.post("/api/workspaces")
def api_workspaces_create(body: WorkspaceCreate) -> dict[str, Any]:
    target = (body.path or body.name or "").strip()
    if not target:
        raise HTTPException(400, "path required")
    try:
        active = create_workspace(target)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    STORE.refresh_settings()
    return {
        "status": "ok",
        "configured": True,
        "active": active,
        "items": list_workspaces(),
    }


@app.put("/api/workspaces/active")
def api_workspaces_set(body: WorkspaceSet) -> dict[str, Any]:
    target = (body.path or body.name or "").strip()
    if not target:
        raise HTTPException(400, "path required")
    try:
        active = set_workspace(target, create=body.create)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    STORE.refresh_settings()
    return {
        "status": "ok",
        "configured": True,
        "active": active,
        "items": list_workspaces(),
    }


@app.post("/api/workspaces/browse")
def api_workspaces_browse(request: Request) -> dict[str, Any]:
    """Open a native folder dialog on this machine and return the selected path."""
    _require_loopback(request)
    path = pick_folder(title="选择工作区文件夹")
    if not path:
        return {"cancelled": True, "path": None}
    return {"cancelled": False, "path": path}


@app.get("/api/files")
def api_files_list(path: str = ".") -> dict[str, Any]:
    try:
        return fs_api.list_entries(path)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/files/search")
def api_files_search(q: str = "", path: str = ".") -> dict[str, Any]:
    try:
        return fs_api.search_workspace(q, path=path)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/files/upload")
async def api_files_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    raw_name = (file.filename or "upload.bin").replace("\\", "/").split("/")[-1]
    safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in raw_name).strip() or "upload.bin"
    try:
        data = await file.read()
        meta = fs_api.write_bytes(f"_uploads/{safe}", data)
        # Prefer structured preview when readable
        try:
            preview = fs_api.read_file(meta["path"])
            return {**preview, "uploaded": True}
        except Exception:
            return {**meta, "uploaded": True, "kind": "unsupported"}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/files/content")
def api_files_read(path: str) -> dict[str, Any]:
    try:
        return fs_api.read_file(path)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/files/raw")
def api_files_raw(path: str) -> FileResponse:
    try:
        fp = fs_api.safe_resolve(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not fp.exists() or not fp.is_file():
        raise HTTPException(404, f"not found: {path}")
    return FileResponse(
        path=fp,
        media_type=fs_api.guess_mime(fp),
        filename=fp.name,
        content_disposition_type="inline",
    )


@app.put("/api/files/content")
def api_files_write(body: FileWrite) -> dict[str, Any]:
    try:
        return fs_api.write_text(body.path, body.content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/files")
def api_files_create(body: FileCreate) -> dict[str, Any]:
    kind = body.kind if body.kind in ("file", "dir") else "file"
    try:
        return fs_api.create_entry(body.path, kind)
    except FileExistsError as exc:
        raise HTTPException(409, f"already exists: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/files")
def api_files_delete(path: str, recursive: bool = False) -> dict[str, Any]:
    try:
        return fs_api.delete_entry(path, recursive=recursive)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/files/rename")
def api_files_rename(body: FileRename) -> dict[str, Any]:
    try:
        return fs_api.rename_entry(body.path, body.new_name)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(409, f"already exists: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/files/move")
def api_files_move(body: FileMove) -> dict[str, Any]:
    try:
        return fs_api.move_entry(body.path, body.dest_dir)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(409, f"already exists: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class FileReveal(BaseModel):
    path: str = "."


@app.post("/api/files/reveal")
def api_files_reveal(request: Request, body: FileReveal) -> dict[str, Any]:
    """Reveal a workspace path in the OS file manager."""
    _require_loopback(request)
    try:
        return fs_api.reveal_in_os(body.path)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"reveal failed: {exc}") from exc


@app.get("/api/files/undo")
def api_files_undo_status() -> dict[str, Any]:
    from ..services import fs_undo

    return fs_undo.status()


@app.post("/api/files/undo")
def api_files_undo() -> dict[str, Any]:
    from ..services import fs_undo

    try:
        return fs_undo.undo_one()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(409, f"already exists: {exc}") from exc


@app.get("/api/model")
def get_model() -> dict[str, Any]:
    cfg = load_model_config()
    return cfg.masked()


@app.put("/api/model")
def put_model(body: ModelUpdate) -> dict[str, Any]:
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    cfg = update_model_config(patch)
    STORE.refresh_settings()
    mode = "demo" if cfg.demo_mode else "api"
    _, model_name, _, _ = cfg.resolve(cfg.main)
    return {
        "status": "ok",
        "config": cfg.masked(),
        "note": (
            "已保存（Demo 模式）。"
            if cfg.demo_mode
            else f"已保存并生效（{mode} · {model_name or 'model'}）。"
        ),
    }


@app.patch("/api/model/select")
def patch_model_select(body: ModelSelect) -> dict[str, Any]:
    try:
        cfg = select_model_role(body.role, body.provider_id, body.model)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    STORE.refresh_settings()
    _, model_name, _, _ = cfg.resolve(
        cfg.main if body.role == "main" else cfg.subagent if body.role == "subagent" else cfg.compress
    )
    return {
        "status": "ok",
        "config": cfg.masked(),
        "note": f"已切换 {body.role} → {model_name}",
    }


@app.get("/api/sessions")
def list_sessions(page: int = 1, page_size: int = 20) -> dict[str, Any]:
    return STORE.list(page=page, page_size=page_size)


@app.post("/api/sessions")
def create_session() -> dict[str, Any]:
    sess = STORE.create()
    return {"id": sess.id, "demo": sess.agent.settings.demo_mode}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    sess = STORE.get(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    schemas = sess.agent.registry.schemas()
    budget = context_budget_tokens(sess.agent.messages, schemas)
    return {
        "id": sess.id,
        "title": sess.title,
        "messages": STORE.ui_messages(sess),
        "tokens": budget,
        "messages_tokens": messages_tokens(sess.agent.messages),
        "schemas_tokens": schemas_tokens(schemas),
        "limit": sess.agent.settings.context_limit,
        "tools": sess.agent.registry.names(),
        "demo": sess.agent.settings.demo_mode,
    }


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str) -> dict[str, Any]:
    if not STORE.delete(session_id):
        raise HTTPException(404, "session not found")
    return {"status": "ok", "session_id": session_id}


class TruncateBody(BaseModel):
    keep_user_turns: int = Field(0, ge=0)
    restore_files: bool = False


@app.post("/api/sessions/{session_id}/truncate")
def truncate_session(session_id: str, body: TruncateBody) -> dict[str, Any]:
    """Rewind conversation: keep first N user turns, drop that turn and everything after.

    When restore_files is true, also undo workspace FS changes back to that turn.
    """
    if not STORE.get(session_id):
        raise HTTPException(404, "session not found")
    file_undo: dict[str, Any] | None = None
    if body.restore_files:
        from ..services import fs_undo

        try:
            file_undo = fs_undo.undo_to_turn(session_id, body.keep_user_turns)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"file restore failed: {exc}") from exc
    try:
        ok = STORE.truncate_before_user_turn(session_id, body.keep_user_turns)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not ok:
        raise HTTPException(404, "session not found")
    sess = STORE.get(session_id)
    out: dict[str, Any] = {
        "status": "ok",
        "session_id": session_id,
        "keep_user_turns": body.keep_user_turns,
        "messages": len(sess.agent.messages) if sess else 0,
        "restore_files": body.restore_files,
    }
    if file_undo is not None:
        out["file_undo"] = file_undo
    return out


@app.post("/api/sessions/{session_id}/stop")
def stop_session(session_id: str) -> dict[str, Any]:
    if not STORE.stop(session_id):
        raise HTTPException(404, "session not found")
    return {"status": "ok", "session_id": session_id}


class ApprovalDecision(BaseModel):
    approved: bool
    remember: bool = False  # approve this tool for the rest of the turn


@app.post("/api/sessions/{session_id}/approvals/{approval_id}")
def decide_approval(session_id: str, approval_id: str, body: ApprovalDecision) -> dict[str, Any]:
    sess = STORE.get(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    # Always idempotent: missing/already-resolved approvals return ok
    STORE.decide_approval(
        session_id,
        approval_id,
        body.approved,
        remember=bool(body.remember),
    )
    return {
        "status": "ok",
        "approval_id": approval_id,
        "approved": body.approved,
        "remember": bool(body.remember) and bool(body.approved),
    }


class AskAnswer(BaseModel):
    choice: str = ""  # option key (1, 2, …) or "custom"
    text: str = ""  # free-form when custom (or optional note)
    option_label: str = ""


@app.post("/api/sessions/{session_id}/asks/{ask_id}")
def answer_ask(session_id: str, ask_id: str, body: AskAnswer) -> dict[str, Any]:
    sess = STORE.get(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    choice = (body.choice or "").strip()
    if not choice and not (body.text or "").strip():
        raise HTTPException(400, "choice or text required")
    STORE.answer_ask(
        session_id,
        ask_id,
        choice=choice or "custom",
        text=body.text or "",
        option_label=body.option_label or "",
    )
    return {
        "status": "ok",
        "ask_id": ask_id,
        "choice": choice or "custom",
    }


class PlanConfirm(BaseModel):
    approved: bool = True


@app.post("/api/sessions/{session_id}/plans/{plan_id}")
def confirm_plan(session_id: str, plan_id: str, body: PlanConfirm) -> dict[str, Any]:
    sess = STORE.get(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    STORE.decide_plan(session_id, plan_id, bool(body.approved))
    return {
        "status": "ok",
        "plan_id": plan_id,
        "approved": bool(body.approved),
    }


@app.post("/api/chat")
async def chat_sse(req: ChatRequest) -> EventSourceResponse:
    sess = STORE.get(req.session_id) if req.session_id else None
    if sess is None:
        sess = STORE.create()

    from ..services.store import (
        _summarize_title,
        generate_session_title,
        is_untitled_session,
    )

    display = (req.display or "").strip()
    title_src = display or req.message
    title_candidate = _summarize_title(title_src)
    needs_llm_title = is_untitled_session(sess.title) or len(sess.title) < 4
    if title_candidate and needs_llm_title:
        sess.title = title_candidate

    q: queue.Queue[Optional[dict[str, Any]]] = queue.Queue()

    def on_bus(ev: Event) -> None:
        q.put(ev.to_dict())

    unsub = sess.agent.bus.subscribe(on_bus)

    def worker() -> None:
        try:
            q.put(
                {
                    "type": "session",
                    "data": {"session_id": sess.id, "demo": sess.agent.settings.demo_mode},
                    "ts": 0,
                    "agent_id": sess.agent.agent_id,
                    "parent_id": "",
                }
            )
            if needs_llm_title:
                try:
                    llm_title = generate_session_title(
                        sess.agent.llm,
                        req.message,
                        display=display,
                    )
                    if llm_title:
                        sess.title = llm_title
                except Exception as exc:
                    from ..core.logutil import get_logger, log_exception

                    log_exception(
                        get_logger("metateam.api"),
                        f"session title LLM failed for {sess.id}",
                        exc,
                    )
            sess.updated_at = time.time()
            result = sess.agent.run(
                req.message,
                mode=req.mode or "agent",
                display=display,
            )
            sess.updated_at = time.time()
            try:
                path = STORE.persist(sess.id)
                if not path:
                    from ..core.logutil import get_logger

                    get_logger("metateam.api").error(
                        "persist returned None for session %s", sess.id
                    )
            except Exception as exc:
                from ..core.logutil import get_logger, log_exception

                log_exception(get_logger("metateam.api"), f"persist failed for {sess.id}", exc)
            q.put(
                {
                    "type": "final",
                    "data": {
                        "text": result.text,
                        "iterations": result.iterations,
                        "tokens": messages_tokens(result.messages),
                        "review": result.review,
                        "session_id": sess.id,
                        "cancelled": result.cancelled,
                        "title": sess.title,
                    },
                    "ts": 0,
                    "agent_id": sess.agent.agent_id,
                    "parent_id": "",
                }
            )
        except Exception as exc:  # noqa: BLE001
            # Still try to keep whatever transcript we have.
            try:
                STORE.persist(sess.id)
            except Exception:
                pass
            q.put(
                {
                    "type": "error",
                    "data": {"message": str(exc)},
                    "ts": 0,
                    "agent_id": "",
                    "parent_id": "",
                }
            )
        finally:
            unsub()
            q.put(None)

    # Preserve request tenant ContextVar inside the SSE worker thread.
    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(worker,), daemon=True).start()

    async def gen() -> AsyncIterator[dict[str, str]]:
        try:
            while True:
                item = await asyncio.get_event_loop().run_in_executor(None, q.get)
                if item is None:
                    break
                yield {"event": item.get("type") or "message", "data": json.dumps(item, ensure_ascii=False)}
        except asyncio.CancelledError:
            sess.agent.request_cancel()
            raise

    return EventSourceResponse(
        gen(),
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


class BrowserStartBody(BaseModel):
    url: str = ""
    headless: bool = False


class BrowserNavigateBody(BaseModel):
    url: str


class BrowserPickBody(BaseModel):
    timeout_ms: int = 60000
    with_screenshot: bool = True


@app.get("/api/browser/status")
def api_browser_status() -> dict[str, Any]:
    from ..services.browser_sandbox import SANDBOX

    return SANDBOX.status()


@app.post("/api/browser/session")
def api_browser_session(body: BrowserStartBody) -> dict[str, Any]:
    from ..services.browser_sandbox import SANDBOX

    try:
        return SANDBOX.ensure_session(url=body.url, headless=bool(body.headless))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/browser/session")
def api_browser_session_close() -> dict[str, str]:
    from ..services.browser_sandbox import SANDBOX

    SANDBOX.close()
    return {"status": "ok"}


@app.post("/api/browser/navigate")
def api_browser_navigate(body: BrowserNavigateBody) -> dict[str, Any]:
    from ..services.browser_sandbox import SANDBOX

    try:
        return SANDBOX.navigate(body.url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/browser/screenshot")
def api_browser_screenshot(full_page: bool = False):
    from fastapi.responses import Response

    from ..services.browser_sandbox import SANDBOX

    try:
        png = SANDBOX.screenshot_png(full_page=bool(full_page))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc
    return Response(content=png, media_type="image/png")


@app.get("/api/browser/console")
def api_browser_console(limit: int = 80) -> dict[str, Any]:
    from ..services.browser_sandbox import SANDBOX

    logs = SANDBOX.console_logs(limit=limit)
    return {"count": len(logs), "logs": logs}


@app.post("/api/browser/select")
def api_browser_select(body: BrowserPickBody) -> dict[str, Any]:
    """Arm Select Mode in the CDP window; blocks until click or timeout."""
    from ..services.browser_sandbox import SANDBOX

    try:
        payload = SANDBOX.pick_element(
            timeout_ms=int(body.timeout_ms),
            with_screenshot=bool(body.with_screenshot),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc
    if payload is None:
        return {"ok": False, "element": None, "message": "cancelled or timed out"}
    return {"ok": True, "element": payload.model_dump()}


@app.post("/api/browser/select/cancel")
def api_browser_select_cancel() -> dict[str, str]:
    from ..services.browser_sandbox import SANDBOX

    SANDBOX.cancel_pick()
    return {"status": "ok"}


@app.get("/api/skills")
def api_skills() -> list[dict[str, Any]]:
    s = get_settings()
    skills = load_skills(s.skills_dir)
    return [
        {
            "name": sk.name,
            "tool": skill_tool_name(sk.name),
            "description": sk.description,
            "path": str(sk.path),
            "mode": "function_call",
        }
        for sk in skills
    ]


@app.get("/api/skills/{name}")
def api_skill(name: str) -> dict[str, Any]:
    s = get_settings()
    skills = load_skills(s.skills_dir)
    for sk in skills:
        if sk.name == name or skill_tool_name(sk.name) == name:
            return {
                "name": sk.name,
                "tool": skill_tool_name(sk.name),
                "description": sk.description,
                "path": str(sk.path),
                "body": sk.read_body(),
                "mode": "function_call",
            }
    raise HTTPException(404, "skill not found")


@app.get("/api/memory")
def api_memory() -> dict[str, str]:
    s = get_settings()
    return {"content": read_memory(s.memory_file, max_chars=100_000)}


@app.put("/api/memory")
def api_memory_put(body: MemoryUpdate) -> dict[str, str]:
    s = get_settings()
    write_memory(s.memory_file, body.content)
    return {"status": "ok"}


@app.post("/api/sessions/{session_id}/save")
def api_save(session_id: str) -> dict[str, str]:
    path = STORE.persist(session_id)
    if not path:
        raise HTTPException(404, "session not found")
    return {"path": path}


# Serve built UI if present (repo_root/ui/dist)
_WEB_DIST = REPO_ROOT / "ui" / "dist"
if _WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=_WEB_DIST / "assets"), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_WEB_DIST / "index.html")

    @app.get("/{full_path:path}")
    def spa(full_path: str) -> FileResponse:
        candidate = _WEB_DIST / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_WEB_DIST / "index.html")


def main() -> None:
    import uvicorn

    s = get_settings()
    allow_remote = os.getenv("META_ALLOW_REMOTE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if not is_loopback_bind(s.host) and not allow_remote:
        raise SystemExit(
            f"Refusing to bind META_HOST={s.host!r} (not loopback). "
            "Use 127.0.0.1 or set META_ALLOW_REMOTE=1 (unsafe)."
        )
    token = load_or_create_token()
    print(f"Sidekick → http://{s.host}:{s.port}  demo={s.demo_mode} model={s.model}")
    print(f"Local token ready (header X-Sidekick-Token). Preview: {token[:8]}…")
    if not s.allow_shell:
        print("Shell tools disabled (META_ALLOW_SHELL=0). Set META_ALLOW_SHELL=1 to enable.")
    uvicorn.run(
        "metateam.api.app:app",
        host=s.host,
        port=s.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
