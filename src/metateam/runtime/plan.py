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

Output ONLY valid JSON (no markdown fences). Keep every string short and JSON-safe:
{
  "summary": "one-line overview",
  "shape_contract": {
    "reuse": "existing files/symbols to extend, or 'none yet'",
    "create_only_if": "when new files are allowed (only if nothing reusable)",
    "config_placement": "where variable rules/copy/lists should live (not hardcoded)",
    "control_flow": "how to keep branches/loops small (map/table/reuse helpers)",
    "why_not_smaller": "why this is already the minimal shape",
    "verify_command": ""
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
- Scope must be mutually exclusive: each task owns a distinct deliverable
  (file/symbol/phase). Adjacent steps must NOT restate or partially redo the same work.
- title = short action (verb + object); detail = ONLY this step's in-scope work.
  Explicitly omit anything that belongs to another step (no overlapping wording).
- Forbidden splits: "implement X" then "improve/complete/polish X", or repeating the
  same files/outcome under different titles. Merge those into one task.
- Prefer fewer sharper steps over many overlapping ones.
- verify_command MUST be empty OR a short one-liner under 80 chars with NO nested
  quotes and NO `powershell -Command ...` wrapping (shell is already PowerShell).
  Good: "Test-Path .\\\\index.html" or "npm test" or "". Bad: long regex / escaped quotes.
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


_NEEDS_PLAN_SYSTEM = """You decide whether a coding agent must show a multi-step PLAN
for the user to confirm BEFORE acting. Default to acting immediately.

Reply with ONLY valid JSON (no markdown fences):
{"plan": true|false, "reason": "one short sentence"}

plan=true ONLY for genuinely complex work where a wrong approach wastes a lot of effort:
- multi-file / multi-module features or large refactors
- architecture or approach choices with several valid designs that need user buy-in
- multi-phase deliverables (e.g. new subsystem, migration, deploy pipeline)
- highly ambiguous goals where the first move could go in very different directions

plan=false for ordinary work — even if it takes a few tool calls:
- edit / fix / tweak one file (HTML image swaps, CSS, copy, small bugfix)
- clear single-outcome asks ("改这六个图", "加个按钮", "修报错")
- questions, explanations, lookups
- anything a competent agent can finish without needing the user to approve a roadmap

When unsure, choose plan=false.
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


def _json_unescape(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return (
            value.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )


def _extract_json_string_field(raw: str, key: str) -> str:
    m = re.search(
        rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"',
        raw or "",
        re.S,
    )
    if not m:
        return ""
    return _json_unescape(m.group(1)).strip()


def _salvage_plan_fields(raw: str) -> dict[str, Any]:
    """Best-effort field extraction when full JSON parse fails (bad escapes, truncation)."""
    text = (raw or "").strip()
    if not text:
        return {}
    out: dict[str, Any] = {}
    summary = _extract_json_string_field(text, "summary")
    if summary:
        out["summary"] = summary

    contract: dict[str, str] = {}
    for key in (
        "reuse",
        "create_only_if",
        "config_placement",
        "control_flow",
        "why_not_smaller",
        "verify_command",
    ):
        val = _extract_json_string_field(text, key)
        if val:
            # Drop broken / overlong verify commands that usually break JSON.
            if key == "verify_command" and (
                len(val) > 120 or "powershell -Command" in val or '"' in val
            ):
                val = ""
            contract[key] = val
    if contract:
        out["shape_contract"] = contract

    tasks: list[dict[str, str]] = []
    for m in re.finditer(
        r'\{\s*"title"\s*:\s*"((?:\\.|[^"\\])*)"\s*,\s*"detail"\s*:\s*"((?:\\.|[^"\\])*)"\s*\}',
        text,
        re.S,
    ):
        title = _json_unescape(m.group(1)).strip()
        detail = _json_unescape(m.group(2)).strip()
        if title:
            tasks.append({"title": title, "detail": detail})
    if not tasks:
        for m in re.finditer(
            r'"title"\s*:\s*"((?:\\.|[^"\\])*)"',
            text,
        ):
            title = _json_unescape(m.group(1)).strip()
            if title and title not in {t["title"] for t in tasks}:
                tasks.append({"title": title, "detail": ""})
    if tasks:
        out["tasks"] = tasks[:8]
    return out


def _looks_like_raw_json_blob(text: str) -> bool:
    t = (text or "").strip()
    return t.startswith("{") and (
        '"shape_contract"' in t or '"tasks"' in t or '"summary"' in t
    )


def _parse_plan_json(raw: str) -> dict[str, Any]:
    data = _parse_loose_json_object(raw)
    if data and not _looks_like_raw_json_blob(str(data.get("summary") or "")):
        # Soft-clean verify_command even on successful parse
        sc = data.get("shape_contract")
        if isinstance(sc, dict):
            vc = str(sc.get("verify_command") or "")
            if len(vc) > 120 or "powershell -Command" in vc:
                sc = {**sc, "verify_command": ""}
                data = {**data, "shape_contract": sc}
        return data
    salvaged = _salvage_plan_fields(raw)
    if salvaged:
        return salvaged
    # Never surface the raw JSON blob as the human-readable summary.
    return {"summary": "", "tasks": []}


def _plan_host_hint() -> str:
    """Brief host shell note so verify_command / shell steps match the machine."""
    import os
    import platform

    if os.name == "nt":
        return (
            f"Host OS: {platform.system()} — shell is PowerShell. "
            "verify_command must be empty or a SHORT PowerShell one-liner "
            "(no nested quotes, no powershell -Command wrapping)."
        )
    return (
        f"Host OS: {platform.system()} — shell is bash. "
        "verify_command must be empty or a short portable command."
    )


def _obviously_simple_goal(goal: str) -> bool:
    """Cheap backstop: tiny / single-file tweaks should never open Plan confirm."""
    g = (goal or "").strip()
    if len(g) < 12:
        return True
    # Clear single-file edits / swaps / fixes
    simple = re.compile(
        r"(改|换|修|补|删|加|替换|调整|更新|fix|tweak|swap|rename|replace|"
        r"图片|图像|img|css|样式|文案|按钮|链接|标题)",
        re.I,
    )
    complex_hint = re.compile(
        r"(架构|重构|迁移|部署|多模块|子系统|从零|整站|系统设计|"
        r"refactor|architect|migrate|deploy|multi[- ]?file|greenfield)",
        re.I,
    )
    if complex_hint.search(g):
        return False
    # Short ask mentioning a single concrete asset → simple
    if len(g) <= 80 and simple.search(g):
        return True
    return False


def needs_plan(llm: "LLM", user_text: str) -> bool:
    """Ask the model whether this turn should open Plan-confirm first.

    Biased toward false: only complex work should pause for a plan. Skill-only
    injections with no user task skip the call. On model failure, default False.
    """
    goal = extract_plan_goal(user_text).strip()
    if len(goal) < 2:
        return False
    if _obviously_simple_goal(goal):
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


def generate_plan(llm: "LLM", goal: str) -> dict[str, Any]:
    """Return {plan_id, summary, shape_contract, tasks: [...]}."""
    from .coherence import normalize_shape_contract

    raw = llm.complete_text(
        _PLAN_SYSTEM,
        f"User goal:\n{goal.strip()}\n\n{_plan_host_hint()}\n\nJSON plan:",
    )
    data = _parse_plan_json(raw)
    summary = str(data.get("summary") or "").strip()
    if _looks_like_raw_json_blob(summary):
        summary = _extract_json_string_field(summary, "summary") or ""
    shape_contract = normalize_shape_contract(data.get("shape_contract"))
    vc = str(shape_contract.get("verify_command") or "")
    if len(vc) > 120 or "powershell -Command" in vc:
        shape_contract["verify_command"] = ""
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
    # Prefer a short human summary derived from the goal if parsing failed.
    if not summary or _looks_like_raw_json_blob(summary):
        summary = (goal.strip().splitlines()[0].strip()[:40] if goal.strip() else "执行计划")
    return {
        "plan_id": new_id("plan"),
        "summary": summary,
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
