"""Human-in-the-loop approval gate for write/destructive tools."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ApprovalRequest:
    id: str
    tool: str
    args: dict[str, Any]
    summary: str
    created_at: float = field(default_factory=time.time)


class ApprovalGate:
    """Blocks worker threads until the UI decides approve/reject.

    ``_allowed_tools`` holds tool names pre-approved for the current agent turn
    (cleared via ``begin_turn``).
    """

    def __init__(self, timeout_sec: float = 300.0) -> None:
        self.timeout_sec = timeout_sec
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        self._decisions: dict[str, bool] = {}
        self._pending: dict[str, ApprovalRequest] = {}
        self._allowed_tools: set[str] = set()

    def begin_turn(self) -> None:
        with self._lock:
            self._allowed_tools.clear()

    def is_preapproved(self, tool: str) -> bool:
        with self._lock:
            return tool in self._allowed_tools

    def remember_tool(self, tool: str) -> None:
        name = (tool or "").strip()
        if not name:
            return
        with self._lock:
            self._allowed_tools.add(name)

    def request(self, approval_id: str, tool: str, args: dict[str, Any], summary: str) -> bool:
        with self._lock:
            if tool in self._allowed_tools:
                return True
        ev = threading.Event()
        req = ApprovalRequest(id=approval_id, tool=tool, args=args, summary=summary)
        with self._lock:
            self._events[approval_id] = ev
            self._pending[approval_id] = req
            self._decisions.pop(approval_id, None)
        ok = ev.wait(timeout=self.timeout_sec)
        with self._lock:
            decided = self._decisions.pop(approval_id, False) if ok else False
            self._events.pop(approval_id, None)
            self._pending.pop(approval_id, None)
        return bool(ok and decided)

    def decide(
        self,
        approval_id: str,
        approved: bool,
        *,
        remember: bool = False,
    ) -> bool:
        with self._lock:
            ev = self._events.get(approval_id)
            if not ev:
                # Idempotent: already resolved / cancelled / timed out
                return True
            if approved and remember:
                req = self._pending.get(approval_id)
                if req and req.tool:
                    self._allowed_tools.add(req.tool)
            self._decisions[approval_id] = bool(approved)
            ev.set()
            return True

    def cancel_all(self) -> None:
        """Reject every pending approval (used on stop)."""
        with self._lock:
            ids = list(self._events.keys())
        for approval_id in ids:
            self.decide(approval_id, False)

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "id": r.id,
                    "tool": r.tool,
                    "args": r.args,
                    "summary": r.summary,
                    "created_at": r.created_at,
                }
                for r in self._pending.values()
            ]


# Tools that mutate the workspace / system and need confirmation
APPROVAL_TOOLS = {
    "write_file",
    "delete_file",
    "run_shell",
    "skill_save",
    "memory_append",
    "memory_remove",
    "memory_write",
}


def tool_needs_approval(name: str) -> bool:
    if name in APPROVAL_TOOLS:
        return True
    return False


def _short(text: str, n: int = 80) -> str:
    t = " ".join((text or "").split())
    if len(t) <= n:
        return t
    return t[: n - 1] + "…"


def summarize_tool_call(name: str, args: dict[str, Any]) -> str:
    if name == "write_file":
        path = str(args.get("path") or "")
        content = str(args.get("content") or "")
        return f"写入 {path or '（路径待定）'}（{len(content)} 字符）"
    if name == "delete_file":
        return f"删除 {args.get('path') or ''}"
    if name == "read_file":
        path = str(args.get("path") or "")
        return f"读取 {path}" if path else "读取文件"
    if name == "list_dir":
        path = str(args.get("path") or ".")
        return f"列出 {path}"
    if name == "search_text":
        q = str(args.get("query") or args.get("pattern") or "")
        path = str(args.get("path") or ".")
        return f"搜索 “{_short(q, 40)}” @ {path}"
    if name == "run_shell":
        cmd = str(args.get("command") or "")
        bg = " · 后台" if args.get("background") else ""
        return f"shell{bg}: {_short(cmd, 100)}"
    if name == "skill_save":
        return f"保存技能 {args.get('name') or ''}"
    if name == "memory_append":
        note = str(args.get("note") or "")
        return f"追加记忆: {_short(note, 80)}"
    if name == "memory_remove":
        match = str(args.get("match") or "")
        return f"删除记忆: {_short(match, 80)}"
    if name == "memory_write":
        content = str(args.get("content") or "")
        return f"覆写记忆（{len(content)} 字符）"
    if name == "memory_read":
        return "读取记忆"
    if name == "delegate_task":
        goal = str(args.get("goal") or args.get("task") or "")
        return f"委派: {_short(goal, 80)}"
    if name.startswith("skill_"):
        return f"调用技能 {name}"
    for key in ("path", "command", "query", "name", "goal", "note"):
        if key in args and args[key]:
            return f"{name}: {_short(str(args[key]), 80)}"
    return name
