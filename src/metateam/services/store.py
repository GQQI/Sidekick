"""In-memory chat sessions for the API layer (+ disk history)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..runtime.agent import Agent
from ..core.config import Settings, get_settings, reload_settings
from ..core.events import EventBus, new_id
from ..core.logutil import get_logger, log_exception
from .session import load_session, save_session, sessions_dir

_log = get_logger("metateam.store")

@dataclass
class ChatSession:
    id: str
    agent: Agent
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    title: str = "New chat"
    user_id: str = ""


_UNTITLED_TITLES = frozenset({"新会话", "New chat", "Untitled", ""})


def is_untitled_session(title: str) -> bool:
    return (title or "").strip() in _UNTITLED_TITLES


def _is_internal_message(m: dict[str, Any]) -> bool:
    """Hide Sidekick-injected prompts (plan steps, etc.) from the UI transcript."""
    if m.get("sidekick_internal") or m.get("internal"):
        return True
    meta = m.get("sidekick")
    if isinstance(meta, dict) and meta.get("internal"):
        return True
    content = str(m.get("content") or "").lstrip()
    return content.startswith("[Plan step ") or content.startswith("[sidekick:")


def _is_skill_or_command_message(content: str) -> bool:
    text = (content or "").strip()
    if not text:
        return True
    if text.startswith("【Skill 已注入】") or "----- SKILL START -----" in text:
        return True
    if text.startswith("/") or text.startswith("／"):
        return True
    return False


def _summarize_title(content: str, limit: int = 20) -> str:
    """First-line summary of the first real user turn, ≤ limit chars."""
    text = (content or "").strip()
    if not text or _is_skill_or_command_message(text):
        return ""
    line = text.splitlines()[0].strip()
    line = " ".join(line.split())
    if len(line) > limit:
        return line[:limit]
    return line


def _title_from_messages(messages: list[dict[str, Any]], fallback: str = "New chat") -> str:
    for m in messages:
        if m.get("role") != "user" or _is_internal_message(m):
            continue
        title = _summarize_title(str(m.get("content") or ""))
        if title:
            return title
    return fallback


class SessionStore:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._lock = threading.Lock()
        self._sessions: dict[str, ChatSession] = {}

    def refresh_settings(self) -> Settings:
        """Reload global model settings and rebind live session agents (LLM clients)."""
        self.settings = reload_settings()
        from ..runtime.llm import LLM
        from .model_config import apply_to_settings, load_model_config

        cfg = load_model_config()
        with self._lock:
            sessions = list(self._sessions.values())
        for sess in sessions:
            try:
                apply_to_settings(sess.agent.settings, cfg)
                # Rebuild clients so demo→API (or model/base_url changes) take effect
                sess.agent.llm = LLM(sess.agent.settings)
                sess.agent.compress_llm = LLM(
                    sess.agent.settings,
                    model=sess.agent.settings.compress_model,
                    api_key=getattr(sess.agent.settings, "compress_api_key", None)
                    or sess.agent.settings.api_key,
                    base_url=getattr(sess.agent.settings, "compress_base_url", None)
                    or sess.agent.settings.base_url,
                )
            except Exception as exc:
                log_exception(_log, f"refresh_settings failed for session {sess.id}", exc)
        return self.settings

    def create(self) -> ChatSession:
        # Always use latest settings for new chats (model switch)
        from .tenant_context import get_user_id

        self.settings = get_settings()
        bus = EventBus()
        from ..runtime.approval import ApprovalGate

        agent = Agent(self.settings, bus=bus, approval=ApprovalGate())
        sess = ChatSession(id=new_id("sess"), agent=agent, user_id=get_user_id())
        agent.session_id = sess.id
        with self._lock:
            self._sessions[sess.id] = sess
        return sess

    def decide_approval(
        self,
        session_id: str,
        approval_id: str,
        approved: bool,
        *,
        remember: bool = False,
    ) -> bool:
        sess = self.get(session_id)
        if not sess:
            return False
        return sess.agent.approval.decide(approval_id, approved, remember=remember)

    def answer_ask(
        self,
        session_id: str,
        ask_id: str,
        *,
        choice: str,
        text: str = "",
        option_label: str = "",
    ) -> bool:
        sess = self.get(session_id)
        if not sess:
            return False
        return sess.agent.ask.answer(
            ask_id,
            choice=choice,
            text=text,
            option_label=option_label,
        )

    def decide_plan(self, session_id: str, plan_id: str, approved: bool) -> bool:
        sess = self.get(session_id)
        if not sess:
            return False
        return sess.agent.plan_gate.decide(plan_id, approved)

    def get(self, session_id: str) -> Optional[ChatSession]:
        from .tenant_context import get_user_id

        uid = get_user_id()
        with self._lock:
            hit = self._sessions.get(session_id)
        if hit:
            if hit.user_id and hit.user_id != uid:
                return None
            return hit
        return self._restore_from_disk(session_id)

    def stop(self, session_id: str) -> bool:
        sess = self.get(session_id)
        if not sess:
            return False
        sess.agent.request_cancel()
        return True

    def _session_paths_for_user(self) -> list[Any]:
        from pathlib import Path

        from .session import legacy_sessions_dir
        from .tenant_context import DEFAULT_USER_ID, get_user_id

        uid = get_user_id()
        paths: list[Any] = list(sessions_dir(self.settings.root, uid).glob("*.json"))
        # Pre-setup / default: also surface legacy flat sessions once
        if uid == DEFAULT_USER_ID:
            for path in legacy_sessions_dir(self.settings.root).glob("sess_*.json"):
                paths.append(path)
        return paths

    def _restore_from_disk(self, session_id: str) -> Optional[ChatSession]:
        from .tenant_context import get_user_id

        uid = get_user_id()
        candidates = [
            sessions_dir(self.settings.root, uid) / f"{session_id}.json",
        ]
        from .session import legacy_sessions_dir
        from .tenant_context import DEFAULT_USER_ID

        if uid == DEFAULT_USER_ID:
            candidates.append(legacy_sessions_dir(self.settings.root) / f"{session_id}.json")

        path = next((p for p in candidates if p.exists()), None)
        if not path:
            return None
        try:
            meta, messages = load_session(path)
        except Exception:
            return None
        if meta.user_id and meta.user_id != uid:
            return None
        self.settings = get_settings()
        bus = EventBus()
        agent = Agent(self.settings, bus=bus, messages=messages)
        mtime = path.stat().st_mtime
        sess = ChatSession(
            id=meta.id or session_id,
            agent=agent,
            created_at=mtime,
            updated_at=mtime,
            title=_title_from_messages(messages, meta.id),
            user_id=meta.user_id or uid,
        )
        agent.session_id = sess.id
        with self._lock:
            self._sessions[sess.id] = sess
        return sess

    def list(self, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        from .tenant_context import get_user_id

        uid = get_user_id()
        items: dict[str, dict[str, Any]] = {}

        # Disk history first
        for path in self._session_paths_for_user():
            try:
                meta, messages = load_session(path)
                if meta.user_id and meta.user_id != uid:
                    continue
                sid = meta.id or path.stem
                user_count = sum(
                    1
                    for m in messages
                    if m.get("role") == "user" and not _is_internal_message(m)
                )
                mtime = path.stat().st_mtime
                updated = mtime
                if meta.updated_at:
                    try:
                        from datetime import datetime

                        updated = max(
                            mtime,
                            datetime.fromisoformat(meta.updated_at.replace("Z", "+00:00")).timestamp(),
                        )
                    except ValueError:
                        pass
                created = path.stat().st_ctime
                if meta.created_at:
                    try:
                        from datetime import datetime

                        created = datetime.fromisoformat(
                            meta.created_at.replace("Z", "+00:00")
                        ).timestamp()
                    except ValueError:
                        pass
                items[sid] = {
                    "id": sid,
                    "title": _title_from_messages(messages, sid),
                    "created_at": created,
                    "updated_at": updated,
                    "messages": len(messages),
                    "user_turns": user_count,
                    "demo": False,
                    "source": "disk",
                }
            except Exception:
                continue

        with self._lock:
            live = list(self._sessions.values())
        for s in live:
            if s.user_id and s.user_id != uid:
                continue
            prev = items.get(s.id)
            # Prefer the freshest interaction time (memory vs disk)
            updated = s.updated_at
            if prev and float(prev.get("updated_at") or 0) > updated:
                updated = float(prev["updated_at"])
            items[s.id] = {
                "id": s.id,
                "title": _title_from_messages(s.agent.messages, s.title or s.id),
                "created_at": s.created_at,
                "updated_at": updated,
                "messages": len(s.agent.messages),
                "user_turns": sum(
                    1
                    for m in s.agent.messages
                    if m.get("role") == "user" and not _is_internal_message(m)
                ),
                "demo": s.agent.settings.demo_mode,
                "source": "memory",
            }

        all_items = sorted(
            items.values(),
            key=lambda x: float(x.get("updated_at") or 0),
            reverse=True,
        )
        total = len(all_items)
        page = max(1, int(page or 1))
        page_size = max(1, min(100, int(page_size or 20)))
        total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
        if page > total_pages:
            page = total_pages
        start = (page - 1) * page_size
        chunk = all_items[start : start + page_size]
        return {
            "items": chunk,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }

    def delete(self, session_id: str) -> bool:
        from .tenant_context import get_user_id

        removed = False
        with self._lock:
            hit = self._sessions.get(session_id)
            if hit and hit.user_id and hit.user_id != get_user_id():
                return False
            if session_id in self._sessions:
                del self._sessions[session_id]
                removed = True
        path = sessions_dir(self.settings.root) / f"{session_id}.json"
        if path.exists():
            try:
                path.unlink()
                removed = True
            except OSError:
                pass
        return removed

    def truncate_before_user_turn(self, session_id: str, keep_user_turns: int) -> bool:
        """Keep the first `keep_user_turns` user messages; drop that turn and everything after."""
        sess = self.get(session_id)
        if not sess:
            return False
        if keep_user_turns < 0:
            raise ValueError("keep_user_turns must be >= 0")
        msgs = sess.agent.messages
        count = 0
        cut = len(msgs)
        for i, m in enumerate(msgs):
            if m.get("role") == "user" and not _is_internal_message(m):
                if count == keep_user_turns:
                    cut = i
                    break
                count += 1
        sess.agent.messages = msgs[:cut]
        sess.updated_at = time.time()
        try:
            self.persist(session_id)
        except Exception as exc:
            log_exception(_log, f"persist after truncate failed for {session_id}", exc)
        return True

    def persist(self, session_id: str) -> Optional[str]:
        sess = self.get(session_id)
        if not sess:
            return None
        path = save_session(
            self.settings.root,
            sess.agent.messages,
            model=self.settings.model,
            workspace=self.settings.workspace,
            session_id=session_id,
            user_id=sess.user_id or None,
        )
        return str(path)

    def ui_messages(self, sess: ChatSession) -> list[dict[str, Any]]:
        """User-facing transcript: only user + assistant text (no tools/system)."""
        out: list[dict[str, Any]] = []
        for m in sess.agent.messages:
            role = m.get("role")
            if role == "user":
                if _is_internal_message(m):
                    continue
                content = str(m.get("content") or "").strip()
                if content and not content.startswith("Iteration budget exhausted"):
                    out.append({"role": "user", "content": content})
            elif role == "assistant":
                content = str(m.get("content") or "").strip()
                # Skip tool-call-only stubs and internal bookkeeping notes
                if content and not m.get("tool_calls") and not _is_internal_message(m):
                    out.append({"role": "assistant", "content": content})
        return out


STORE = SessionStore()
