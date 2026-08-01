"""Workspace filesystem undo stack — snapshot before mutating ops."""

from __future__ import annotations

import contextvars
import hashlib
import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from ..core.config import get_settings

MAX_UNDO = 40
_lock = threading.Lock()

# Bound to the active top-level chat turn so FS mutations can be restored on edit.
_ctx_session_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "fs_undo_session_id", default=None
)
_ctx_user_turn: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "fs_undo_user_turn", default=None
)


def set_turn_context(session_id: Optional[str], user_turn: Optional[int]) -> None:
    _ctx_session_id.set(session_id)
    _ctx_user_turn.set(user_turn)


def clear_turn_context() -> None:
    _ctx_session_id.set(None)
    _ctx_user_turn.set(None)


def _stamp_turn(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("session_id") is None:
        sid = _ctx_session_id.get()
        if sid:
            record["session_id"] = sid
    if record.get("user_turn") is None:
        turn = _ctx_user_turn.get()
        if turn is not None:
            record["user_turn"] = turn
    return record


def _workspace_key(workspace: Path) -> str:
    return hashlib.sha1(str(workspace.resolve()).encode("utf-8")).hexdigest()[:16]


def _undo_root(workspace: Optional[Path] = None) -> Path:
    settings = get_settings()
    ws = (workspace or settings.workspace).resolve()
    root = settings.root / "data" / "fs_undo" / _workspace_key(ws)
    root.mkdir(parents=True, exist_ok=True)
    (root / "blobs").mkdir(exist_ok=True)
    return root


def _stack_path(workspace: Optional[Path] = None) -> Path:
    return _undo_root(workspace) / "stack.json"


def _load_stack(workspace: Optional[Path] = None) -> list[dict[str, Any]]:
    path = _stack_path(workspace)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_stack(stack: list[dict[str, Any]], workspace: Optional[Path] = None) -> None:
    path = _stack_path(workspace)
    path.write_text(json.dumps(stack[-MAX_UNDO:], ensure_ascii=False, indent=2), encoding="utf-8")


def _new_blob_id() -> str:
    return uuid.uuid4().hex


def _store_file_blob(src: Path, workspace: Optional[Path] = None) -> str:
    bid = _new_blob_id()
    dest = _undo_root(workspace) / "blobs" / bid
    shutil.copy2(src, dest)
    return bid


def _store_dir_blob(src: Path, workspace: Optional[Path] = None) -> str:
    bid = _new_blob_id()
    dest = _undo_root(workspace) / "blobs" / bid
    shutil.copytree(src, dest)
    return bid


def push(record: dict[str, Any], workspace: Optional[Path] = None) -> None:
    """Push an undo record. Caller fills op-specific fields."""
    with _lock:
        stack = _load_stack(workspace)
        record = _stamp_turn(
            {
                **record,
                "id": record.get("id") or _new_blob_id(),
                "ts": time.time(),
            }
        )
        stack.append(record)
        # prune old blobs when trimming stack
        trimmed = stack[:-MAX_UNDO] if len(stack) > MAX_UNDO else []
        stack = stack[-MAX_UNDO:]
        _save_stack(stack, workspace)
        for old in trimmed:
            _discard_blob(old, workspace)


def _discard_blob(rec: dict[str, Any], workspace: Optional[Path] = None) -> None:
    bid = rec.get("blob")
    if not bid:
        return
    path = _undo_root(workspace) / "blobs" / str(bid)
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)
    except OSError:
        pass


def push_before_write(rel: str, abs_path: Path, workspace: Optional[Path] = None) -> None:
    if abs_path.exists() and abs_path.is_file():
        try:
            blob = _store_file_blob(abs_path, workspace)
            push(
                {
                    "op": "write",
                    "path": rel,
                    "blob": blob,
                    "label": f"修改 {rel}",
                    "had_file": True,
                },
                workspace,
            )
            return
        except OSError:
            pass
    push(
        {
            "op": "write",
            "path": rel,
            "blob": None,
            "label": f"新建 {rel}",
            "had_file": False,
        },
        workspace,
    )


def push_before_create(rel: str, kind: str, workspace: Optional[Path] = None) -> None:
    push(
        {
            "op": "create",
            "path": rel,
            "kind": kind,
            "label": f"新建{'目录' if kind == 'dir' else '文件'} {rel}",
        },
        workspace,
    )


def push_before_delete(rel: str, abs_path: Path, workspace: Optional[Path] = None) -> None:
    if abs_path.is_dir():
        blob = _store_dir_blob(abs_path, workspace)
        push(
            {
                "op": "delete",
                "path": rel,
                "kind": "dir",
                "blob": blob,
                "label": f"删除目录 {rel}",
            },
            workspace,
        )
    else:
        blob = _store_file_blob(abs_path, workspace)
        push(
            {
                "op": "delete",
                "path": rel,
                "kind": "file",
                "blob": blob,
                "label": f"删除 {rel}",
            },
            workspace,
        )


def push_before_move(
    from_rel: str,
    to_rel: str,
    workspace: Optional[Path] = None,
) -> None:
    push(
        {
            "op": "move",
            "from": from_rel,
            "to": to_rel,
            "label": f"移动 {from_rel} → {to_rel}",
        },
        workspace,
    )


def push_before_rename(
    from_rel: str,
    to_rel: str,
    workspace: Optional[Path] = None,
) -> None:
    push(
        {
            "op": "rename",
            "from": from_rel,
            "to": to_rel,
            "label": f"重命名 {from_rel} → {to_rel}",
        },
        workspace,
    )


def push_checkpoint(
    session_id: str,
    user_turn: int,
    workspace: Optional[Path] = None,
) -> None:
    """Mark the start of a user turn so edit can restore files to this point."""
    push(
        {
            "op": "checkpoint",
            "session_id": session_id,
            "user_turn": user_turn,
            "label": f"对话轮次 {user_turn + 1}",
        },
        workspace,
    )


def status(workspace: Optional[Path] = None) -> dict[str, Any]:
    with _lock:
        stack = _load_stack(workspace)
    items = [
        {
            "id": r.get("id"),
            "label": r.get("label") or r.get("op"),
            "op": r.get("op"),
            "ts": r.get("ts"),
            "session_id": r.get("session_id"),
            "user_turn": r.get("user_turn"),
        }
        for r in reversed(stack[-20:])
    ]
    return {"count": len(stack), "items": items}


def undo_to_turn(
    session_id: str,
    before_user_turn: int,
    workspace: Optional[Path] = None,
) -> dict[str, Any]:
    """Restore workspace to the state before `before_user_turn` of this session.

    Undoes every stack entry from the top down through the matching checkpoint
    (inclusive). Untagged / other-session ops above that checkpoint are also
    reversed so the workspace matches that moment.
    """
    with _lock:
        stack = _load_stack(workspace)
        target_idx: Optional[int] = None
        for i, rec in enumerate(stack):
            if (
                rec.get("op") == "checkpoint"
                and rec.get("session_id") == session_id
                and rec.get("user_turn") == before_user_turn
            ):
                target_idx = i
                break
        if target_idx is None:
            # Fallback: drop tagged ops for this session at/after the turn
            n = 0
            for rec in reversed(stack):
                if rec.get("session_id") != session_id:
                    break
                turn = rec.get("user_turn")
                if turn is None or int(turn) < before_user_turn:
                    break
                n += 1
            to_undo = n
            partial = True
        else:
            to_undo = len(stack) - target_idx
            partial = False

    undone: list[dict[str, Any]] = []
    errors: list[str] = []
    for _ in range(to_undo):
        try:
            result = undo_one(workspace)
            if result.get("undone"):
                undone.append(result["undone"])
        except ValueError:
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            break

    with _lock:
        remaining = len(_load_stack(workspace))
    return {
        "status": "ok",
        "undone_count": len(undone),
        "undone": undone,
        "remaining": remaining,
        "partial": partial,
        "errors": errors,
    }


def undo_one(workspace: Optional[Path] = None) -> dict[str, Any]:
    """Pop and reverse the latest FS mutation. Returns summary."""
    from . import fs_api

    with _lock:
        stack = _load_stack(workspace)
        if not stack:
            raise ValueError("nothing to undo")
        rec = stack.pop()
        _save_stack(stack, workspace)

    op = rec.get("op")
    blob = rec.get("blob")
    blob_path = (_undo_root(workspace) / "blobs" / str(blob)) if blob else None

    try:
        if op == "checkpoint":
            pass
        elif op == "write":
            path = str(rec.get("path") or "")
            fp = fs_api.safe_resolve(path)
            if rec.get("had_file") and blob_path and blob_path.is_file():
                fp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(blob_path, fp)
            else:
                if fp.exists() and fp.is_file():
                    fp.unlink()
        elif op == "create":
            path = str(rec.get("path") or "")
            fp = fs_api.safe_resolve(path)
            if fp.exists():
                if fp.is_dir():
                    shutil.rmtree(fp)
                else:
                    fp.unlink()
        elif op == "delete":
            path = str(rec.get("path") or "")
            fp = fs_api.safe_resolve(path)
            if blob_path and blob_path.exists():
                fp.parent.mkdir(parents=True, exist_ok=True)
                if rec.get("kind") == "dir":
                    if fp.exists():
                        shutil.rmtree(fp, ignore_errors=True)
                    shutil.copytree(blob_path, fp)
                else:
                    shutil.copy2(blob_path, fp)
        elif op in ("move", "rename"):
            frm = str(rec.get("from") or "")
            to = str(rec.get("to") or "")
            src = fs_api.safe_resolve(to)
            dest = fs_api.safe_resolve(frm)
            if not src.exists():
                raise FileNotFoundError(to)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                raise FileExistsError(frm)
            shutil.move(str(src), str(dest))
        else:
            raise ValueError(f"unknown undo op: {op}")
    finally:
        _discard_blob(rec, workspace)

    with _lock:
        remaining = len(_load_stack(workspace))
    return {
        "status": "ok",
        "undone": {"id": rec.get("id"), "label": rec.get("label"), "op": op},
        "remaining": remaining,
    }
