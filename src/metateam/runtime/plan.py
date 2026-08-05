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
  "shape_contract": {
    "reuse": "existing files/symbols to extend, or 'none yet'",
    "create_only_if": "when new files are allowed (only if nothing reusable)",
    "config_placement": "where variable rules/copy/lists should live (not hardcoded)",
    "control_flow": "how to keep branches/loops small (map/table/reuse helpers)",
    "why_not_smaller": "why this is already the minimal shape",
    "verify_command": "optional shell check e.g. pytest path or npm test (empty if N/A)"
  },
  "tasks": [
    {"title": "short step title", "detail": "what to do in this step"}
  ]
}

Rules:
- 2–8 tasks, ordered, each actionable with file/shell tools.
- Match the user's language (Chinese if they wrote Chinese).
- shape_contract is mandatory: fight overlay (parallel reimplementation), hardcoding,
  and if/loop piling — prefer extending existing workspace assets.
- Do not include meta steps like "ask the user" unless truly required.
"""


def extract_plan_goal(user_text: str) -> str:
    """Prefer the user's real ask over Skill-injection scaffolding for planning."""
    t = (user_text or "").strip()
    if not t:
        return ""
    if "【Skill 已注入】" in t or "----- SKILL START -----" in t:
        m = re.search(
            r"用户本次附加指令：\s*\n(.+?)(?:\n\n请按该 Skill|\Z)",
            t,
            re.S,
        )
        if m:
            return m.group(1).strip()
        # Skill only, no task — do not treat the template as a planning goal
        return ""
    return t


_NEEDS_PLAN_SYSTEM = """You decide whether a coding agent should show the user a
multi-step plan to confirm BEFORE acting, or should act immediately.

Reply with ONLY valid JSON (no markdown fences):
{"plan": true|false, "reason": "one short sentence"}

plan=true when confirming an approach first clearly helps, e.g.:
- multiple sequential steps / files / phases are needed
- a non-trivial deliverable (presentation/deck, feature, refactor, deploy)
- the ask is ambiguous enough that a wrong first move wastes work

plan=false when the agent should just do it, e.g.:
- one straightforward action (single file, small fix, short answer)
- a question / explanation, not a multi-step delivery
- trivial or empty request
"""


def _parse_loose_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
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
    return {}


def parse_needs_plan_reply(raw: str) -> Optional[bool]:
    """Parse model reply into True/False; None if unparseable."""
    data = _parse_loose_json_object(raw)
    if data:
        if "plan" in data:
            return bool(data["plan"])
        if "needs_plan" in data:
            return bool(data["needs_plan"])
    low = (raw or "").strip().lower()
    if not low:
        return None
    if re.search(r'"plan"\s*:\s*true', low) or re.search(
        r'"needs_plan"\s*:\s*true', low
    ):
        return True
    if re.search(r'"plan"\s*:\s*false', low) or re.search(
        r'"needs_plan"\s*:\s*false', low
    ):
        return False
    if low in ("true", "yes", "plan"):
        return True
    if low in ("false", "no", "agent"):
        return False
    return None


def needs_plan(llm: "LLM", user_text: str) -> bool:
    """Ask the model whether this turn should open Plan-confirm first.

    No keyword heuristics — generalization comes from the model. Skill-only
    injections with no user task skip the call and return False. On model
    failure, default to False so the agent can still act.
    """
    goal = extract_plan_goal(user_text).strip()
    if len(goal) < 2:
        return False
    try:
        raw = llm.complete_text(
            _NEEDS_PLAN_SYSTEM,
            f"User request:\n{goal[:4000]}\n\nJSON:",
            temperature=0.0,
        )
    except Exception:
        return False
    decided = parse_needs_plan_reply(raw)
    if decided is None:
        return False
    return decided


def _parse_plan_json(raw: str) -> dict[str, Any]:
    data = _parse_loose_json_object(raw)
    if data:
        return data
    text = (raw or "").strip()
    return {"summary": text[:500], "tasks": []}


def generate_plan(llm: "LLM", goal: str) -> dict[str, Any]:
    """Return {plan_id, summary, shape_contract, tasks: [...]}."""
    from .coherence import normalize_shape_contract

    raw = llm.complete_text(
        _PLAN_SYSTEM,
        f"User goal:\n{goal.strip()}\n\nJSON plan:",
    )
    data = _parse_plan_json(raw)
    summary = str(data.get("summary") or "").strip()
    shape_contract = normalize_shape_contract(data.get("shape_contract"))
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
        "shape_contract": shape_contract,
        "tasks": tasks,
    }


def format_plan_markdown(plan: dict[str, Any], *, awaiting_confirm: bool = False) -> str:
    from .coherence import format_shape_contract_markdown, normalize_shape_contract

    lines = [f"## {plan.get('summary') or '计划'}", ""]
    contract = normalize_shape_contract(plan.get("shape_contract"))
    if any(contract.values()):
        lines.append(format_shape_contract_markdown(contract))
        lines.append("")
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
