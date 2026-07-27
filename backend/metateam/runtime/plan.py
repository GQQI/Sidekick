"""Task planning — break complex goals into steps (plan vs agent execution)."""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

from ..core.events import new_id

if TYPE_CHECKING:
    from .llm import LLM


@dataclass
class PlanConfirmRequest:
    id: str
    summary: str
    tasks: list[dict[str, Any]]
    created_at: float = field(default_factory=time.time)


class PlanGate:
    """Blocks until the UI confirms or rejects a generated plan."""

    def __init__(self, timeout_sec: float = 600.0) -> None:
        self.timeout_sec = timeout_sec
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        self._decisions: dict[str, bool] = {}
        self._pending: dict[str, PlanConfirmRequest] = {}

    def request(
        self,
        plan_id: str,
        *,
        summary: str,
        tasks: list[dict[str, Any]],
    ) -> bool:
        """Wait for UI. Returns True if user approved execution."""
        ev = threading.Event()
        req = PlanConfirmRequest(id=plan_id, summary=summary, tasks=tasks)
        with self._lock:
            self._events[plan_id] = ev
            self._pending[plan_id] = req
            self._decisions.pop(plan_id, None)
        ok = ev.wait(timeout=self.timeout_sec)
        with self._lock:
            approved = self._decisions.pop(plan_id, False) if ok else False
            self._events.pop(plan_id, None)
            self._pending.pop(plan_id, None)
        if not ok:
            return False
        return bool(approved)

    def decide(self, plan_id: str, approved: bool) -> bool:
        with self._lock:
            ev = self._events.get(plan_id)
            if not ev:
                return True  # already resolved
            self._decisions[plan_id] = bool(approved)
            ev.set()
            return True

    def cancel_all(self) -> None:
        with self._lock:
            ids = list(self._events.keys())
        for plan_id in ids:
            self.decide(plan_id, False)

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "id": r.id,
                    "summary": r.summary,
                    "tasks": r.tasks,
                    "created_at": r.created_at,
                }
                for r in self._pending.values()
            ]

_PLAN_SYSTEM = """You break user goals into clear, sequential tasks for a coding agent.

Output ONLY valid JSON (no markdown fences):
{
  "summary": "one-line overview",
  "tasks": [
    {"title": "short step title", "detail": "what to do in this step"}
  ]
}

Rules:
- 2–8 tasks, ordered, each actionable with file/shell tools.
- Match the user's language (Chinese if they wrote Chinese).
- Do not include meta steps like "ask the user" unless truly required.
"""


def needs_plan(user_text: str) -> bool:
    """Heuristic: complex / multi-step goals should plan before acting."""
    t = (user_text or "").strip()
    if len(t) < 24:
        return False

    markers = (
        # Chinese multi-step / delivery cues
        "并且",
        "然后",
        "同时",
        "首先",
        "其次",
        "最后",
        "步骤",
        "方案",
        "规划",
        "重构",
        "部署",
        "实现",
        "优化",
        "修复",
        "添加",
        "生成",
        "创建",
        "编写",
        "制作",
        "设计",
        "开发",
        "搭建",
        "写一个",
        "做一个",
        "帮我写",
        "帮我做",
        "完整",
        "整套",
        # Deliverable nouns that usually need multiple steps
        "ppt",
        "pptx",
        "幻灯片",
        "演示文稿",
        "报告",
        "文档",
        "网站",
        "页面",
        "项目",
        "模块",
        "接口",
        "api",
        "数据库",
        "脚本",
        # English
        " and ",
        " then ",
        " also ",
        "implement",
        "refactor",
        "deploy",
        "build",
        "create",
        "generate",
        "design",
        "develop",
    )
    low = t.lower()
    hits = sum(1 for m in markers if m in low or m in t)
    if hits >= 1 and len(t) >= 28:
        return True
    if len(t) >= 80:
        return True
    if t.count("，") + t.count(",") + t.count("；") + t.count(";") >= 2:
        return True
    if t.count("\n") >= 2:
        return True
    return False


def _parse_plan_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {"summary": "", "tasks": []}
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {"summary": text[:500], "tasks": []}


def generate_plan(llm: "LLM", goal: str) -> dict[str, Any]:
    """Return {plan_id, summary, tasks: [{id, title, detail, status}]}."""
    raw = llm.complete_text(
        _PLAN_SYSTEM,
        f"User goal:\n{goal.strip()}\n\nJSON plan:",
    )
    data = _parse_plan_json(raw)
    summary = str(data.get("summary") or "").strip()
    raw_tasks = data.get("tasks") or []
    tasks: list[dict[str, Any]] = []
    if isinstance(raw_tasks, list):
        for item in raw_tasks[:8]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("name") or "").strip()
            if not title:
                continue
            tasks.append(
                {
                    "id": new_id("task"),
                    "title": title,
                    "detail": str(item.get("detail") or item.get("description") or "").strip(),
                    "status": "pending",
                }
            )
    if not tasks:
        tasks = [
            {
                "id": new_id("task"),
                "title": "分析需求并执行",
                "detail": goal.strip()[:500],
                "status": "pending",
            }
        ]
    return {
        "plan_id": new_id("plan"),
        "summary": summary or "执行计划",
        "tasks": tasks,
    }


def format_plan_markdown(plan: dict[str, Any], *, awaiting_confirm: bool = False) -> str:
    lines = [f"## {plan.get('summary') or '计划'}", ""]
    for i, task in enumerate(plan.get("tasks") or [], 1):
        title = task.get("title") or f"步骤 {i}"
        detail = task.get("detail") or ""
        lines.append(f"{i}. **{title}**")
        if detail:
            lines.append(f"   {detail}")
    lines.append("")
    if awaiting_confirm:
        lines.append("_请在下方确认方案后执行，或取消。_")
    else:
        lines.append("_方案已取消，未执行。可切换到 Agent 模式后重试。_")
    return "\n".join(lines)
