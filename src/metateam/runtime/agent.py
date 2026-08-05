"""ReAct agent with events, guardrails, compression, nested delegation."""

from __future__ import annotations

import contextvars
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .approval import ApprovalGate, summarize_tool_call, tool_needs_approval
from .ask import (
    AskGate,
    MIN_ASK_OPTIONS,
    build_ask_options,
    normalize_option_labels,
    try_parse_inline_ask,
)
from ..core.config import Settings, get_settings
from .context import (
    context_budget_tokens,
    debug_dump_budget,
    ensure_fit,
    messages_tokens,
    schemas_tokens,
)
from ..core.events import EventBus, emit, new_id
from ..core.guardrails import Guardrails
from .llm import LLM, parse_tool_args
from .plan import (
    PlanGate,
    extract_plan_goal,
    format_plan_markdown,
    generate_plan,
    needs_plan,
)
from .prompts import build_system_prompt
from .coherence import (
    inject_contract_into_goal,
    format_turn_policy_block,
    merge_policy_into_system,
    policy_for_turn,
    shape_contract_from_plan,
)
from .review import run_review
from ..services.skills import Skill, load_skills
from .tools import ToolRegistry, build_registry, plan_parallel_batches

PrintFn = Callable[[str], None]


@dataclass
class AgentResult:
    text: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    compressed: bool = False
    agent_id: str = ""
    review: dict[str, Any] = field(default_factory=dict)
    cancelled: bool = False


class Agent:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        is_subagent: bool = False,
        role: str = "leaf",
        goal: str = "",
        context: str = "",
        depth: int = 0,
        parent_id: str = "",
        agent_id: Optional[str] = None,
        bus: Optional[EventBus] = None,
        on_event: Optional[PrintFn] = None,
        messages: Optional[list[dict[str, Any]]] = None,
        turn_counter: int = 0,
        approval: Optional[ApprovalGate] = None,
        ask: Optional[AskGate] = None,
        plan_gate: Optional[PlanGate] = None,
    ):
        self.settings = settings or get_settings()
        self.is_subagent = is_subagent
        self.role = role if role in ("leaf", "orchestrator") else "leaf"
        self.goal = goal
        self.context = context
        self.depth = depth
        self.parent_id = parent_id
        self.agent_id = agent_id or new_id("agent")
        self.session_id: Optional[str] = None
        self.bus = bus or EventBus()
        self.on_event = on_event
        self.turn_counter = turn_counter
        self.guard = Guardrails(same_call_fail_limit=self.settings.same_call_fail_limit)
        self._cancel = threading.Event()
        self.approval = approval or ApprovalGate()
        self.ask = ask or AskGate()
        self.plan_gate = plan_gate or PlanGate()
        self._children: list[Agent] = []

        self.skills: list[Skill] = load_skills(self.settings.skills_dir)
        if is_subagent:
            self.llm = LLM(
                self.settings,
                model=self.settings.subagent_model,
                api_key=getattr(self.settings, "subagent_api_key", None) or self.settings.api_key,
                base_url=getattr(self.settings, "subagent_base_url", None) or self.settings.base_url,
            )
        else:
            self.llm = LLM(self.settings, model=self.settings.model)
        self.compress_llm = LLM(
            self.settings,
            model=self.settings.compress_model,
            api_key=getattr(self.settings, "compress_api_key", None) or self.settings.api_key,
            base_url=getattr(self.settings, "compress_base_url", None) or self.settings.base_url,
        )

        can_delegate = (not is_subagent) or (
            self.role == "orchestrator" and depth < self.settings.max_spawn_depth
        )
        self.registry: ToolRegistry = build_registry(
            self.settings,
            skills=self.skills,
            allow_delegate=can_delegate,
            run_child=self._run_child if can_delegate else None,
            ask_user_fn=self._ask_user,
        )
        # Sticky facts from tools (list_dir / codebase_*) so later turns don't invent paths.
        self.workspace_facts: list[str] = []
        self._turn_policy = None

        if messages is not None:
            self.messages = messages
        else:
            system = build_system_prompt(
                workspace=self.settings.workspace,
                skills=self.skills,
                memory_file=self.settings.memory_file,
                is_subagent=is_subagent,
                role=self.role,
                goal=goal,
                context=context,
                depth=depth,
                max_depth=self.settings.max_spawn_depth,
            )
            self.messages = [{"role": "system", "content": system}]
        if not is_subagent:
            self._refresh_workspace_grounding()

    def request_cancel(self) -> None:
        self._cancel.set()
        # Close in-flight provider stream ASAP (do not wait for next chunk)
        try:
            self.llm.close_active_stream()
        except Exception as exc:
            from ..core.logutil import get_logger, log_exception

            log_exception(get_logger("metateam.agent"), "close_active_stream failed", exc)
        # Propagate to nested subagents
        for child in list(self._children):
            try:
                child.request_cancel()
            except Exception as exc:
                from ..core.logutil import get_logger, log_exception

                log_exception(get_logger("metateam.agent"), "child cancel failed", exc)
        # Unblock any waiting approvals / asks / plan confirms as rejected
        self.approval.cancel_all()
        self.ask.cancel_all()
        self.plan_gate.cancel_all()

    def clear_cancel(self) -> None:
        self._cancel.clear()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _is_internal_message(self, m: dict[str, Any]) -> bool:
        if m.get("sidekick_internal") or m.get("internal"):
            return True
        meta = m.get("sidekick")
        if isinstance(meta, dict) and meta.get("internal"):
            return True
        content = str(m.get("content") or "").lstrip()
        return content.startswith("[Plan step ") or content.startswith("[sidekick:")

    def _last_user_index(self) -> int:
        for i in range(len(self.messages) - 1, -1, -1):
            m = self.messages[i]
            if m.get("role") == "user" and not self._is_internal_message(m):
                return i
        return -1

    def _seal_cancelled_turn(self, partial_text: str = "") -> str:
        """Drop unfinished tool chains after stop so the next turn won't resume them.

        Keeps the last real user message; replaces any trailing assistant/tool/plan-step
        messages with a single plain assistant note (optionally including streamed partial text).
        """
        last_user = self._last_user_index()
        if last_user < 0:
            return partial_text
        # Only rewrite the open turn (messages after the latest real user)
        tail = self.messages[last_user + 1 :]
        if not tail and not (partial_text or "").strip():
            note = (
                "（用户已停止本轮生成。请等待下一条用户指令；"
                "不要继续或恢复刚才未完成的任务。）"
            )
            self.messages.append({"role": "assistant", "content": note})
            return note

        # Collect any plain assistant text already in the tail (ignore tool_calls msgs
        # and plan-step internal prompts). Prefer the latest partial stream.
        kept_bits: list[str] = []
        if (partial_text or "").strip():
            kept_bits.append(partial_text.strip())
        for m in tail:
            if m.get("role") != "assistant":
                continue
            if m.get("tool_calls") or self._is_internal_message(m):
                continue
            text = str(m.get("content") or "").strip()
            if text and text not in kept_bits:
                kept_bits.append(text)

        body = "\n\n".join(kept_bits).strip()
        note = (
            "（用户已停止本轮生成。请等待下一条用户指令；"
            "不要继续或恢复刚才未完成的任务。）"
        )
        # Keep the user-visible stop short — do not paste the whole tool dump again
        # when we already streamed it; prefer a brief seal when body is huge.
        if len(body) > 2500:
            from ..core.textutil import safe_clip

            body = safe_clip(body, 2500)
        content = f"{body}\n\n{note}" if body else note
        self.messages = self.messages[: last_user + 1]
        self.messages.append({"role": "assistant", "content": content})
        return content

    def _repair_dangling_tool_calls(self) -> None:
        """If history ends mid tool-call (e.g. crash), seal it before a new turn."""
        if not self.messages:
            return
        # Find last assistant with tool_calls that lacks matching tool results
        last_asst = -1
        for i in range(len(self.messages) - 1, -1, -1):
            m = self.messages[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                last_asst = i
                break
            if m.get("role") == "user":
                break
        if last_asst < 0:
            return
        needed: set[str] = set()
        for tc in self.messages[last_asst].get("tool_calls") or []:
            cid = str(tc.get("id") or "")
            if cid:
                needed.add(cid)
        if not needed:
            return
        have: set[str] = set()
        for m in self.messages[last_asst + 1 :]:
            if m.get("role") == "tool":
                have.add(str(m.get("tool_call_id") or ""))
            elif m.get("role") in ("assistant", "user"):
                break
        if needed <= have:
            return
        # Incomplete — treat like a cancelled seal from the user before this assistant
        user_idx = last_asst - 1
        while user_idx >= 0 and self.messages[user_idx].get("role") != "user":
            user_idx -= 1
        if user_idx < 0:
            return
        self.messages = self.messages[: user_idx + 1]
        self.messages.append(
            {
                "role": "assistant",
                "content": (
                    "（上一轮生成已中断。请等待用户下一条指令；"
                    "不要继续或恢复未完成的任务。）"
                ),
            }
        )

    def _log(self, msg: str) -> None:
        if self.on_event:
            self.on_event(msg)

    def _emit(self, type_: str, data: Optional[dict[str, Any]] = None) -> None:
        emit(self.bus, type_, data, agent_id=self.agent_id, parent_id=self.parent_id)
        if data and "message" in (data or {}):
            self._log(str(data["message"]))

    def _run_child(self, *, goal: str, context: str = "", role: str = "leaf") -> str:
        child_role = role if role in ("leaf", "orchestrator") else "leaf"
        # Depth gate: only allow orchestrator if next depth still can spawn
        next_depth = self.depth + 1
        if child_role == "orchestrator" and next_depth >= self.settings.max_spawn_depth:
            child_role = "leaf"

        child_id = new_id("agent")
        self._emit(
            "subagent_start",
            {
                "child_id": child_id,
                "goal": goal,
                "role": child_role,
                "depth": next_depth,
                "message": f"spawn {child_role}: {goal[:100]}",
            },
        )
        child = Agent(
            self.settings,
            is_subagent=True,
            role=child_role,
            goal=goal,
            context=context,
            depth=next_depth,
            parent_id=self.agent_id,
            agent_id=child_id,
            bus=self.bus,
            on_event=self.on_event,
            approval=self.approval,
            ask=self.ask,
            plan_gate=self.plan_gate,
        )
        self._children.append(child)
        if self.cancelled():
            child.request_cancel()
        try:
            result = child.run("Begin now. Use tools, then summarize.")
        finally:
            self._children = [c for c in self._children if c is not child]
        self._emit(
            "subagent_end",
            {
                "child_id": child_id,
                "goal": goal,
                "summary": (result.text or "")[:2000],
                "iterations": result.iterations,
                "cancelled": bool(result.cancelled),
                "message": f"done: {goal[:60]}",
            },
        )
        return result.text or "(empty summary)"

    def _ask_user(
        self,
        *,
        question: str,
        options: list[str] | None = None,
        allow_custom: bool = True,
        custom_label: str = "其他（请补充）",
        # Legacy flat args (older prompts / cached tool calls)
        option_a: str = "",
        option_b: str = "",
        option_c: str = "",
        option_d: str = "",
    ) -> str:
        q = (question or "").strip()
        if not q:
            return "ERROR: empty question"

        labels = normalize_option_labels(options)
        if len(labels) < MIN_ASK_OPTIONS:
            legacy = [
                str(option_a or "").strip(),
                str(option_b or "").strip(),
                str(option_c or "").strip(),
            ]
            labels = [x for x in legacy if x]
            if str(option_d or "").strip():
                labels.append(str(option_d).strip())

        built = build_ask_options(labels)
        if len(built) < MIN_ASK_OPTIONS:
            return (
                f"ERROR: ask_user needs at least {MIN_ASK_OPTIONS} options; "
                f"got {len(built)}"
            )

        allow_other = bool(allow_custom)
        other_label = str(custom_label or "其他（请补充）").strip() or "其他（请补充）"
        ask_id = new_id("ask")
        call_id = new_id("call")
        self._emit("assistant_delta", {"chunk": "", "reset": True, "discard": True})
        self._emit(
            "ask_request",
            {
                "ask_id": ask_id,
                "call_id": call_id,
                "session_id": self.session_id or "",
                "question": q,
                "options": built,
                "allow_custom": allow_other,
                "custom_label": other_label,
                "summary": f"询问用户: {q[:120]}",
                "message": f"等待用户选择：{q[:80]}",
            },
        )
        answer = self.ask.request(
            ask_id,
            q,
            built,
            allow_custom=allow_other,
            custom_label=other_label,
        )
        self._emit(
            "ask_resolved",
            {
                "ask_id": ask_id,
                "call_id": call_id,
                "answer": answer,
                "message": "用户已回答" if not str(answer).startswith("ERROR:") else "询问已取消或超时",
            },
        )
        return answer

    def _maybe_compress(self) -> None:
        schemas = self.registry.schemas()
        before = context_budget_tokens(self.messages, schemas)
        limit = self.settings.context_limit
        self._emit(
            "context_usage",
            {
                "tokens": before,
                "limit": limit,
                "ratio": round(before / max(1, limit), 4),
                "messages_tokens": messages_tokens(self.messages),
                "schemas_tokens": schemas_tokens(schemas),
            },
        )
        trigger = int(limit * self.settings.compress_trigger_ratio)
        if before < trigger:
            return

        self._emit(
            "compress_start",
            {
                "before": before,
                "limit": limit,
                "attempt": 0,
                "max_attempts": self.settings.max_compress_attempts,
                "phase": "start",
                "message": "上下文接近上限，开始重置…",
            },
        )

        def _progress(info: dict[str, Any]) -> None:
            self._emit(
                "compress_progress",
                {
                    **info,
                    "before": before,
                    "limit": limit,
                },
            )

        # Reserve room for tools[] so compressed messages still fit with schemas
        msg_limit = max(4000, limit - schemas_tokens(schemas) - 256)
        self.messages, meta = ensure_fit(
            self.messages,
            context_limit=msg_limit,
            keep_recent_tokens=self.settings.keep_recent_tokens,
            trigger_ratio=self.settings.compress_trigger_ratio,
            max_attempts=self.settings.max_compress_attempts,
            llm=self.compress_llm,
            on_progress=_progress,
        )
        after = context_budget_tokens(self.messages, schemas)
        if meta.get("compressed"):
            self._last_compressed = True
        self._emit(
            "compress",
            {
                "before": before,
                "after": after,
                "limit": limit,
                "meta": meta,
                "phase": "done",
                "message": f"上下文已重置 {before}→{after}",
            },
        )
        self._emit(
            "context_usage",
            {
                "tokens": after,
                "limit": limit,
                "ratio": round(after / max(1, limit), 4),
                "messages_tokens": messages_tokens(self.messages),
                "schemas_tokens": schemas_tokens(schemas),
            },
        )

    def _execute_one(self, tc: dict[str, Any]) -> dict[str, Any]:
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        args = parse_tool_args(fn.get("arguments") or "{}")
        tool = self.registry.get(name)
        call_id = str(tc.get("id") or new_id("call"))
        needs_ok = bool(
            (tool and getattr(tool, "requires_approval", False)) or tool_needs_approval(name)
        )
        summary = summarize_tool_call(name, args)
        # Subagents share the parent's ApprovalGate — mutating tools still need confirm
        preapproved = bool(needs_ok and self.approval.is_preapproved(name))
        self._emit(
            "tool_start",
            {
                "name": name,
                "args": args,
                "call_id": call_id,
                "needs_approval": needs_ok and not preapproved,
                "summary": summary,
                "message": f"→ {name}",
            },
        )

        if needs_ok:
            if preapproved:
                self._emit(
                    "approval_auto",
                    {
                        "call_id": call_id,
                        "name": name,
                        "summary": summary,
                        "message": f"本轮已放行：{summary}",
                    },
                )
            else:
                approval_id = new_id("appr")
                self._emit(
                    "approval_request",
                    {
                        "approval_id": approval_id,
                        "call_id": call_id,
                        "name": name,
                        "args": args,
                        "summary": summary,
                        "message": f"等待确认：{summary}",
                    },
                )
                approved = self.approval.request(approval_id, name, args, summary)
                self._emit(
                    "approval_resolved",
                    {
                        "approval_id": approval_id,
                        "call_id": call_id,
                        "name": name,
                        "approved": approved,
                        "message": "已批准" if approved else "已拒绝或超时",
                    },
                )
                if not approved:
                    content = f"ERROR: user rejected or approval timed out — {summary}"
                    self.guard.after(name, args, content)
                    self._emit(
                        "tool_end",
                        {
                            "name": name,
                            "args": args,
                            "call_id": call_id,
                            "ok": False,
                            "preview": content[:400],
                            "result": content,
                            "message": f"← {name} rejected",
                        },
                    )
                    return {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": content,
                    }

        blocked = self.guard.before(name, args)
        if blocked:
            content = blocked
        elif not tool:
            content = f"ERROR: unknown tool {name}"
        else:
            try:
                content = tool.handler(**{k: v for k, v in args.items() if not k.startswith("_")})
            except TypeError as exc:
                content = f"ERROR: bad arguments for {name}: {exc}"
            except Exception as exc:  # noqa: BLE001
                content = f"ERROR: {name} failed: {exc}"

        self.guard.after(name, args, content)
        if len(content) > self.settings.tool_result_cap:
            from ..core.textutil import safe_clip

            content = safe_clip(
                content, self.settings.tool_result_cap, ellipsis="\n…[truncated]"
            )

        self._ingest_workspace_fact(name, args, content)
        self._emit_coherence_tool_events(name, args, content)

        from ..core.textutil import safe_clip

        self._emit(
            "tool_end",
            {
                "name": name,
                "args": args,
                "call_id": call_id,
                "ok": not content.startswith("ERROR"),
                "preview": safe_clip(content, 400),
                "result": safe_clip(content, 12_000, ellipsis="\n…[truncated]")
                if len(content) > 12_000
                else content,
                "message": f"← {name} ({len(content)} chars)",
            },
        )
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "name": name,
            "content": content,
        }

    def _emit_coherence_tool_events(
        self, name: str, args: dict[str, Any], content: str
    ) -> None:
        """Surface Anti-Piling / verify signals to the UI."""
        if self.is_subagent:
            return
        if name == "codebase_find_similar":
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                data = {}
            matches = data.get("matches") if isinstance(data, dict) else None
            top = []
            if isinstance(matches, list):
                for m in matches[:5]:
                    if isinstance(m, dict) and m.get("path"):
                        top.append(
                            {
                                "path": str(m.get("path")),
                                "score": m.get("score"),
                                "symbols": m.get("symbols") or [],
                            }
                        )
            self._emit(
                "coherence_align",
                {
                    "query": str(args.get("query") or data.get("query") or ""),
                    "match_count": int(data.get("match_count") or len(top)),
                    "matches": top,
                    "message": f"对齐检索：{data.get('match_count', len(top))} 个候选",
                },
            )
        elif name == "coherence_checklist":
            self._emit(
                "coherence_pile",
                {
                    "status": "checklist_issued",
                    "message": "已下发检堆砌清单，请对照证据作答",
                },
            )
        elif name == "verify_run":
            passed = content.startswith("VERIFY PASS")
            self._emit(
                "verify_result",
                {
                    "ok": passed,
                    "command": str(args.get("command") or ""),
                    "preview": content[:500],
                    "message": "验收通过" if passed else "验收未通过",
                },
            )

    def _ingest_workspace_fact(self, name: str, args: dict[str, Any], content: str) -> None:
        """Pin layout discoveries so the next user turn still respects them."""
        if self.is_subagent:
            return
        key_tools = {
            "list_dir",
            "codebase_overview",
            "codebase_find_similar",
            "codebase_impact",
        }
        if name not in key_tools:
            # Also note definitive missing-path reads
            if name == "read_file" and content.startswith("ERROR: not found"):
                path = str(args.get("path") or "")
                note = f"read_file missing: {path}"
            else:
                return
        else:
            preview = content.strip().replace("\r\n", "\n")
            if len(preview) > 600:
                preview = preview[:600] + "…"
            path_hint = str(args.get("path") or args.get("query") or args.get("symbol_or_path") or ".")
            note = f"{name}({path_hint}): {preview}"

        self.workspace_facts.append(note)
        # Keep newest discoveries; drop oldest
        if len(self.workspace_facts) > 8:
            self.workspace_facts = self.workspace_facts[-8:]

    def _refresh_workspace_grounding(self) -> None:
        """Rewrite pinned ground-truth in the system message each user turn."""
        if self.is_subagent:
            return
        if not self.messages or self.messages[0].get("role") != "system":
            return

        ws = self.settings.workspace.resolve()
        lines = [
            "## Workspace ground truth (authoritative)",
            f"Root: {ws}",
            "Do NOT invent paths like src/, app/, components/ unless listed below or just confirmed by a tool this turn.",
            "If a previous tool showed only certain files (e.g. index.html), treat that as current reality until tools say otherwise.",
        ]
        try:
            from ..services import codebase_memory as cbm

            idx = cbm.get_or_build_index(ws)
            paths = [fe.path for fe in idx.files[:60]]
            lines.append(f"Indexed files ({len(idx.files)}):")
            if paths:
                lines.extend(f"- {p}" for p in paths)
            else:
                lines.append("- (none)")
        except Exception as exc:  # noqa: BLE001
            from ..core.logutil import get_logger, log_exception

            log_exception(get_logger("metateam.agent"), "grounding index failed", exc)

        # Always include live top-level listing (catches index.html even if index lags)
        try:
            entries = sorted(ws.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            shown = []
            for e in entries[:40]:
                if e.name.startswith(".") and e.name not in {".sidekick"}:
                    continue
                shown.append(f"{'dir' if e.is_dir() else 'file'}:{e.name}")
            if shown:
                lines.append("Top-level now: " + ", ".join(shown))
            else:
                lines.append("Top-level now: (empty)")
        except OSError:
            pass

        if self.workspace_facts:
            lines.append("Session discoveries (from tools):")
            lines.extend(f"- {f}" for f in self.workspace_facts[-6:])

        block = "\n".join(lines)
        marker = "## Workspace ground truth (authoritative)"
        content = str(self.messages[0].get("content") or "")
        if marker in content:
            content = content.split(marker, 1)[0].rstrip()
        # Also strip older codebase memory overview block to avoid contradicting fresh truth
        old_cm = "## Codebase memory (structure projection)"
        if old_cm in content:
            # keep everything before old_cm; grounding replaces it for live truth
            head, _, tail = content.partition(old_cm)
            # drop until next ## or end — crude but ok
            rest = tail.split("\n## ", 1)
            if len(rest) == 2:
                content = (head.rstrip() + "\n\n## " + rest[1]).strip()
            else:
                content = head.rstrip()

        self.messages[0]["content"] = (content.rstrip() + "\n\n" + block).strip()

    def _apply_turn_coherence_policy(self, user_text: str) -> None:
        """Pin per-turn Anti-Piling policy (align/contract/pile) into system message."""
        if self.is_subagent:
            return
        if not self.messages or self.messages[0].get("role") != "system":
            return
        policy = policy_for_turn(user_text)
        self._turn_policy = policy
        block = format_turn_policy_block(policy)
        self.messages[0]["content"] = merge_policy_into_system(
            str(self.messages[0].get("content") or ""),
            block,
        )
        self._emit(
            "coherence_policy",
            {
                "kind": policy.label,
                "require_align": policy.require_align,
                "require_shape_contract": policy.require_shape_contract,
                "require_pile_check": policy.require_pile_check,
                "message": f"连贯策略：{policy.label}",
            },
        )

    def _execute_tools(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for batch in plan_parallel_batches(tool_calls, self.registry):
            if len(batch) == 1:
                results.append(self._execute_one(batch[0]))
                continue
            self._emit("parallel_batch", {"size": len(batch)})
            ordered: list[dict[str, Any] | None] = [None] * len(batch)
            with ThreadPoolExecutor(max_workers=min(8, len(batch))) as pool:
                # One Context per task — a shared Context cannot be entered by
                # multiple worker threads at once (RuntimeError: already entered).
                futs = {
                    pool.submit(
                        contextvars.copy_context().run, self._execute_one, tc
                    ): i
                    for i, tc in enumerate(batch)
                }
                for fut in as_completed(futs):
                    ordered[futs[fut]] = fut.result()
            results.extend(r for r in ordered if r is not None)
        return results

    def _run_plan_only(self, user_text: str) -> tuple[str, int, bool, bool]:
        """Generate a plan, wait for user confirm, then execute if approved."""
        self._emit(
            "assistant_status",
            {"text": "正在生成方案（反堆砌形态合同）…", "tools": []},
        )
        plan = generate_plan(self.compress_llm, user_text)
        plan_id = str(plan["plan_id"])
        tasks: list[dict[str, Any]] = list(plan.get("tasks") or [])
        summary = str(plan.get("summary") or "执行计划")
        shape_contract = shape_contract_from_plan(plan)
        self._emit(
            "plan_created",
            {
                "plan_id": plan_id,
                "session_id": self.session_id or "",
                "summary": summary,
                "tasks": tasks,
                "shape_contract": shape_contract,
                "mode": "plan",
                "awaiting_confirm": True,
            },
        )
        self._emit(
            "plan_confirm_request",
            {
                "plan_id": plan_id,
                "session_id": self.session_id or "",
                "summary": summary,
                "tasks": tasks,
                "shape_contract": shape_contract,
                "message": f"等待确认方案：{summary[:80]}",
            },
        )
        approved = self.plan_gate.request(plan_id, summary=summary, tasks=tasks)
        self._emit(
            "plan_confirm_resolved",
            {
                "plan_id": plan_id,
                "approved": approved,
                "message": "方案已确认，开始执行" if approved else "方案已取消",
            },
        )
        if not approved or self.cancelled():
            md = format_plan_markdown(plan, awaiting_confirm=False)
            self._emit("assistant_delta", {"chunk": "", "reset": True, "discard": True})
            self._emit("assistant_delta", {"chunk": md})
            self.messages.append({"role": "assistant", "content": md})
            self._emit(
                "plan_done",
                {
                    "plan_id": plan_id,
                    "message": "方案未执行",
                    "cancelled": True,
                },
            )
            return md, 0, self.cancelled(), False

        return self._execute_plan(plan, user_text)

    def _run_planned_agent(self, user_text: str) -> tuple[str, int, bool, bool]:
        plan = generate_plan(self.compress_llm, user_text)
        return self._execute_plan(plan, user_text)

    def _execute_plan(self, plan: dict[str, Any], user_text: str) -> tuple[str, int, bool, bool]:
        from .coherence import format_shape_contract_markdown

        plan_id = str(plan["plan_id"])
        tasks: list[dict[str, Any]] = list(plan.get("tasks") or [])
        shape_contract = shape_contract_from_plan(plan)
        self._emit(
            "plan_created",
            {
                "plan_id": plan_id,
                "summary": plan.get("summary") or "",
                "tasks": tasks,
                "shape_contract": shape_contract,
                "mode": "agent",
                "awaiting_confirm": False,
            },
        )
        intro = f"## {plan.get('summary') or '执行计划'}\n\n"
        if any(shape_contract.values()):
            intro += format_shape_contract_markdown(shape_contract) + "\n\n"
        intro += "将按任务列表逐步执行…\n"
        self._emit("assistant_delta", {"chunk": "", "reset": True})
        self._emit("assistant_delta", {"chunk": intro})

        per_step = max(8, self.settings.max_iterations // 3)
        turned = 0
        was_cancelled = False
        compressed = bool(getattr(self, "_last_compressed", False))
        step_notes: list[str] = []

        for i, task in enumerate(tasks):
            if self.cancelled():
                was_cancelled = True
                break
            tid = str(task.get("id") or new_id("task"))
            title = str(task.get("title") or f"步骤 {i + 1}")
            self._emit(
                "plan_step",
                {
                    "plan_id": plan_id,
                    "task_id": tid,
                    "index": i,
                    "status": "running",
                    "title": title,
                },
            )
            prior = "\n".join(f"- {s}" for s in step_notes) if step_notes else "(none)"
            goal_hint = (user_text or "").strip().replace("\n", " ")[:240]
            step_body = (
                f"[Plan step {i + 1}/{len(tasks)}] {title}\n"
                f"{task.get('detail') or ''}\n\n"
                f"Goal (context only — do NOT re-do the whole goal): {goal_hint}\n"
                f"Already done (do NOT repeat):\n{prior}\n\n"
                "Complete ONLY this step's scope. Do not redo prior steps or pull in "
                "later steps. Reply with a brief summary of what THIS step changed."
            )
            # Full shape contract only on step 1; later steps get a short reminder.
            if i == 0:
                step_msg = inject_contract_into_goal(step_body, shape_contract)
            elif any(shape_contract.values()):
                step_msg = (
                    f"{step_body}\n\n"
                    "Keep following the plan's shape contract from step 1 "
                    "(reuse existing assets; no parallel reimplementation)."
                )
            else:
                step_msg = step_body
            self.messages.append(
                {
                    "role": "user",
                    "content": step_msg,
                    "sidekick_internal": True,
                    "sidekick": {"internal": True, "kind": "plan_step", "index": i},
                }
            )
            step_final, step_iters, step_cancelled, step_compressed = self._run_agent_loop(
                per_step
            )
            turned += step_iters
            if step_compressed:
                compressed = True
            if step_cancelled:
                was_cancelled = True
            from ..core.textutil import safe_clip

            note = f"{title}: {safe_clip((step_final or '').strip(), 400)}"
            step_notes.append(note)
            status = "done"
            if step_cancelled:
                status = "cancelled"
            elif (step_final or "").startswith("ERROR"):
                status = "error"
            # Emit completion immediately so the UI can advance before the next step starts
            self._emit(
                "plan_step",
                {
                    "plan_id": plan_id,
                    "task_id": tid,
                    "index": i,
                    "status": status,
                    "title": title,
                },
            )
            if was_cancelled:
                break

        self._emit("plan_done", {"plan_id": plan_id, "message": "计划执行完成"})
        lines = [intro, "### 执行结果", ""]
        for note in step_notes:
            lines.append(f"- ✅ {note}")
        final = "\n".join(lines)
        self.messages.append({"role": "assistant", "content": final})
        self._emit("assistant_delta", {"chunk": "", "reset": True})
        self._emit("assistant_delta", {"chunk": final})
        return final, turned, was_cancelled, compressed

    def _run_agent_loop(self, max_iters: int) -> tuple[str, int, bool, bool]:
        """Run tool-calling loop until the model stops with text or max iters."""
        compressed = False
        final = ""
        turned = 0
        was_cancelled = False

        for i in range(1, max_iters + 1):
            if self.cancelled():
                was_cancelled = True
                break
            turned = i
            nudge = self.guard.progress_nudge()
            if nudge:
                self.messages.append(
                    {
                        "role": "user",
                        "content": nudge,
                        "sidekick_internal": True,
                        "sidekick": {"internal": True, "kind": "progress_nudge"},
                    }
                )
                self._emit(
                    "assistant_status",
                    {"text": "探索过多，已要求停止翻页并开始行动", "tools": []},
                )
            self._maybe_compress()
            if getattr(self, "_last_compressed", False):
                compressed = True
            schemas = self.registry.schemas()
            self._emit(
                "llm_start",
                {
                    "turn": i,
                    "budget": json.loads(debug_dump_budget(self.messages)),
                    "tokens": context_budget_tokens(self.messages, schemas),
                    "messages_tokens": messages_tokens(self.messages),
                    "schemas_tokens": schemas_tokens(schemas),
                    "limit": self.settings.context_limit,
                },
            )
            assistant: Optional[dict[str, Any]] = None
            streamed_buf = ""
            self._emit("assistant_delta", {"chunk": "", "reset": True})
            try:
                for kind, payload in self.llm.stream_chat(
                    self.messages,
                    tools=self.registry.schemas(),
                    cancel_check=self.cancelled,
                ):
                    if self.cancelled():
                        was_cancelled = True
                        break
                    if kind == "delta":
                        streamed_buf += str(payload)
                        self._emit("assistant_delta", {"chunk": str(payload)})
                    elif kind == "reasoning_delta":
                        self._emit(
                            "assistant_reasoning_delta",
                            {"chunk": str(payload)},
                        )
                    elif kind == "tool_delta" and isinstance(payload, dict):
                        self._emit("tool_call_delta", payload)
                    elif kind == "done":
                        assistant = payload  # type: ignore[assignment]
            except Exception:
                if self.cancelled():
                    was_cancelled = True
                    if streamed_buf.strip() and not final:
                        final = streamed_buf.strip()
                        self.messages.append({"role": "assistant", "content": final})
                    break
                if self.cancelled():
                    was_cancelled = True
                    break
                assistant = self.llm.chat(self.messages, tools=self.registry.schemas())
                preamble_fb = (assistant.get("content") or "").strip()
                if preamble_fb:
                    self._stream_text_to_ui(preamble_fb)

            if was_cancelled:
                if not final:
                    partial = ""
                    if isinstance(assistant, dict):
                        partial = str(assistant.get("content") or "").strip()
                    if not partial:
                        partial = streamed_buf.strip()
                    if partial:
                        final = partial
                        self.messages.append({"role": "assistant", "content": final})
                break
            if assistant is None:
                was_cancelled = True
                if streamed_buf.strip() and not final:
                    final = streamed_buf.strip()
                    self.messages.append({"role": "assistant", "content": final})
                break
            self.messages.append(assistant)

            tool_calls = assistant.get("tool_calls") or []
            preamble = (assistant.get("content") or "").strip()

            if not tool_calls:
                parsed_ask = try_parse_inline_ask(preamble)
                if parsed_ask:
                    self.messages.pop()
                    self._emit("assistant_delta", {"chunk": "", "reset": True, "discard": True})
                    answer = self._ask_user(
                        question=str(parsed_ask.get("question") or ""),
                        options=list(parsed_ask.get("options") or []),
                        allow_custom=bool(parsed_ask.get("allow_custom", True)),
                    )
                    self.messages.append({"role": "user", "content": answer})
                    continue

                final = preamble
                if not final:
                    reasoning = str(assistant.get("reasoning") or "").strip()
                    if reasoning:
                        final = (
                            reasoning
                            if len(reasoning) <= 3000
                            else reasoning[:3000] + "…"
                        )
                    else:
                        final = "（本轮已完成）"
                    self.messages[-1]["content"] = final
                    self._emit("assistant_delta", {"chunk": "", "reset": True})
                    self._emit("assistant_delta", {"chunk": final})
                break

            names = [(tc.get("function") or {}).get("name", "?") for tc in tool_calls]
            self._emit(
                "assistant_status",
                {"text": f"调用工具：{', '.join(names)}", "tools": names},
            )
            if self.cancelled():
                was_cancelled = True
                break
            for tr in self._execute_tools(tool_calls):
                if self.cancelled():
                    was_cancelled = True
                    break
                self.messages.append(tr)
            if was_cancelled:
                break
        else:
            if not was_cancelled:
                self._emit("max_iterations", {"n": max_iters})
                self.messages.append(
                    {
                        "role": "user",
                        "content": "Iteration budget exhausted. Summarize status and stop.",
                    }
                )
                final = self._stream_final_reply()

        return final, turned, was_cancelled, compressed

    def run(
        self,
        user_text: str,
        *,
        mode: str = "agent",
        do_review: bool = True,
        display: str = "",
    ) -> AgentResult:
        # Top-level turns reset cancel; subagents keep a cancel already set by parent.
        if not self.is_subagent:
            self.clear_cancel()
            self.approval.begin_turn()
            self.guard.begin_turn()
            # Previous stop may have left unfinished tool_calls in history
            self._repair_dangling_tool_calls()
            # Re-pin live workspace truth so the model does not fall back to src/ priors
            self._refresh_workspace_grounding()
            self._apply_turn_coherence_policy(user_text)
        user_turn = sum(1 for m in self.messages if m.get("role") == "user")
        user_msg: dict[str, Any] = {"role": "user", "content": user_text}
        disp = (display or "").strip()
        if disp and disp != user_text:
            user_msg["sidekick"] = {"display": disp}
        self.messages.append(user_msg)
        self.turn_counter += 1
        if not self.is_subagent and self.session_id:
            from ..services import fs_undo

            fs_undo.push_checkpoint(self.session_id, user_turn)
            fs_undo.set_turn_context(self.session_id, user_turn)
            # Checkpoint early so a restart mid-turn still keeps the user message.
            try:
                from ..services.store import STORE

                STORE.persist(self.session_id)
            except Exception as exc:
                from ..core.logutil import get_logger, log_exception

                log_exception(
                    get_logger("metateam.agent"),
                    f"early persist failed for {self.session_id}",
                    exc,
                )
        self._emit(
            "turn_start",
            {"text": (disp or user_text)[:500], "message": "user turn"},
        )

        max_iters = (
            self.settings.subagent_max_iterations
            if self.is_subagent
            else self.settings.max_iterations
        )
        compressed = False
        self._last_compressed = False
        final = ""
        turned = 0
        was_cancelled = False
        mode_n = (mode or "agent").strip().lower()

        try:
            plan_goal = extract_plan_goal(user_text) or user_text
            if not self.is_subagent and mode_n == "plan":
                final, turned, was_cancelled, compressed = self._run_plan_only(plan_goal)
            elif not self.is_subagent and mode_n == "agent":
                self._emit(
                    "assistant_status",
                    {"text": "正在判断是否需要先出方案…", "tools": []},
                )
                if needs_plan(self.compress_llm, user_text):
                    # Model decides Plan-confirm; plan against the user ask,
                    # not Skill-template scaffolding.
                    final, turned, was_cancelled, compressed = self._run_plan_only(
                        plan_goal
                    )
                else:
                    final, turned, was_cancelled, compressed = self._run_agent_loop(
                        max_iters
                    )
            else:
                final, turned, was_cancelled, compressed = self._run_agent_loop(max_iters)

            if was_cancelled:
                self._emit("cancelled", {"message": "已停止生成"})
                # Strip unfinished tool chains so the next user message won't resume them
                final = self._seal_cancelled_turn(final or "")

            review: dict[str, Any] = {}
            if (
                do_review
                and not was_cancelled
                and not self.is_subagent
                and self.settings.auto_skill_review
                and self.turn_counter % max(1, self.settings.review_every_n_turns) == 0
            ):
                try:
                    review = run_review(self.settings, self.messages, llm=self.compress_llm)
                    if review.get("memory") or review.get("skill"):
                        self.skills = load_skills(self.settings.skills_dir)
                        self._emit("review", {"result": review, "message": "self-improve review"})
                except Exception as exc:  # noqa: BLE001
                    review = {"error": str(exc)}

            self._emit(
                "turn_end",
                {
                    "iterations": turned,
                    "tokens": context_budget_tokens(self.messages, self.registry.schemas()),
                    "message": "turn complete",
                    "cancelled": was_cancelled,
                },
            )
            return AgentResult(
                text=final,
                messages=self.messages,
                iterations=turned,
                compressed=compressed or bool(getattr(self, "_last_compressed", False)),
                agent_id=self.agent_id,
                review=review,
                cancelled=was_cancelled,
            )
        finally:
            if not self.is_subagent and self.session_id:
                from ..services import fs_undo

                fs_undo.clear_turn_context()

    def _stream_text_to_ui(self, text: str) -> None:
        if not text:
            return
        self._emit("assistant_delta", {"chunk": "", "reset": True})
        self._emit("assistant_delta", {"chunk": text})

    def _stream_final_reply(self) -> str:
        """Stream a tool-less completion into the UI; return full text."""
        self._emit("assistant_delta", {"chunk": "", "reset": True})
        parts: list[str] = []
        try:
            for piece in self.llm.stream_text(self.messages, cancel_check=self.cancelled):
                if self.cancelled():
                    break
                if not piece:
                    continue
                parts.append(piece)
                self._emit("assistant_delta", {"chunk": piece})
        except Exception:
            if self.cancelled():
                text = "".join(parts)
                self.messages.append({"role": "assistant", "content": text})
                return text
            assistant = self.llm.chat(self.messages, tools=None)
            text = assistant.get("content") or ""
            if text:
                self._stream_text_to_ui(text)
            self.messages.append({"role": "assistant", "content": text})
            return text

        text = "".join(parts)
        self.messages.append({"role": "assistant", "content": text})
        return text

def run_once(prompt: str, settings: Optional[Settings] = None) -> AgentResult:
    return Agent(settings).run(prompt)
