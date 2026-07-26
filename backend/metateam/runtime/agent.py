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
from ..core.config import Settings, get_settings
from .context import debug_dump_budget, ensure_fit, messages_tokens
from ..core.events import EventBus, emit, new_id
from ..core.guardrails import Guardrails
from .llm import LLM, parse_tool_args
from .prompts import build_system_prompt
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
        self._children: list[Agent] = []

        self.skills: list[Skill] = load_skills(self.settings.skills_dir)
        model = self.settings.subagent_model if is_subagent else self.settings.model
        self.llm = LLM(self.settings, model=model)
        self.compress_llm = LLM(self.settings, model=self.settings.compress_model)

        can_delegate = (not is_subagent) or (
            self.role == "orchestrator" and depth < self.settings.max_spawn_depth
        )
        self.registry: ToolRegistry = build_registry(
            self.settings,
            skills=self.skills,
            allow_delegate=can_delegate,
            run_child=self._run_child if can_delegate else None,
        )

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

    def request_cancel(self) -> None:
        self._cancel.set()
        # Close in-flight provider stream ASAP (do not wait for next chunk)
        try:
            self.llm.close_active_stream()
        except Exception:
            pass
        # Propagate to nested subagents
        for child in list(self._children):
            try:
                child.request_cancel()
            except Exception:
                pass
        # Unblock any waiting approvals as rejected
        self.approval.cancel_all()

    def clear_cancel(self) -> None:
        self._cancel.clear()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _last_user_index(self) -> int:
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "user":
                return i
        return -1

    def _seal_cancelled_turn(self, partial_text: str = "") -> str:
        """Drop unfinished tool chains after stop so the next turn won't resume them.

        Keeps the last user message; replaces any trailing assistant/tool messages
        with a single plain assistant note (optionally including streamed partial text).
        """
        last_user = self._last_user_index()
        if last_user < 0:
            return partial_text
        # Only rewrite the open turn (messages after the latest user)
        tail = self.messages[last_user + 1 :]
        if not tail and not (partial_text or "").strip():
            note = (
                "（用户已停止本轮生成。请等待下一条用户指令；"
                "不要继续或恢复刚才未完成的任务。）"
            )
            self.messages.append({"role": "assistant", "content": note})
            return note

        # Collect any plain assistant text already in the tail (ignore tool_calls msgs)
        kept_bits: list[str] = []
        if (partial_text or "").strip():
            kept_bits.append(partial_text.strip())
        for m in tail:
            if m.get("role") != "assistant":
                continue
            if m.get("tool_calls"):
                continue
            text = str(m.get("content") or "").strip()
            if text and text not in kept_bits:
                kept_bits.append(text)

        body = "\n\n".join(kept_bits).strip()
        note = (
            "（用户已停止本轮生成。请等待下一条用户指令；"
            "不要继续或恢复刚才未完成的任务。）"
        )
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

    def _maybe_compress(self) -> None:
        before = messages_tokens(self.messages)
        limit = self.settings.context_limit
        self._emit(
            "context_usage",
            {
                "tokens": before,
                "limit": limit,
                "ratio": round(before / max(1, limit), 4),
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

        self.messages, meta = ensure_fit(
            self.messages,
            context_limit=limit,
            keep_recent_tokens=self.settings.keep_recent_tokens,
            trigger_ratio=self.settings.compress_trigger_ratio,
            max_attempts=self.settings.max_compress_attempts,
            llm=self.compress_llm,
            on_progress=_progress,
        )
        after = messages_tokens(self.messages)
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
        preapproved = bool(needs_ok and not self.is_subagent and self.approval.is_preapproved(name))
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

        if needs_ok and not self.is_subagent:
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
            content = content[: self.settings.tool_result_cap] + "\n…[truncated]"

        self._emit(
            "tool_end",
            {
                "name": name,
                "args": args,
                "call_id": call_id,
                "ok": not content.startswith("ERROR"),
                "preview": content[:400],
                "result": content[:12_000],
                "message": f"← {name} ({len(content)} chars)",
            },
        )
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "name": name,
            "content": content,
        }

    def _execute_tools(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for batch in plan_parallel_batches(tool_calls, self.registry):
            if len(batch) == 1:
                results.append(self._execute_one(batch[0]))
                continue
            self._emit("parallel_batch", {"size": len(batch)})
            ordered: list[dict[str, Any] | None] = [None] * len(batch)
            with ThreadPoolExecutor(max_workers=min(8, len(batch))) as pool:
                ctx = contextvars.copy_context()
                futs = {
                    pool.submit(ctx.run, self._execute_one, tc): i
                    for i, tc in enumerate(batch)
                }
                for fut in as_completed(futs):
                    ordered[futs[fut]] = fut.result()
            results.extend(r for r in ordered if r is not None)
        return results

    def run(self, user_text: str, *, do_review: bool = True) -> AgentResult:
        # Top-level turns reset cancel; subagents keep a cancel already set by parent.
        if not self.is_subagent:
            self.clear_cancel()
            self.approval.begin_turn()
            # Previous stop may have left unfinished tool_calls in history
            self._repair_dangling_tool_calls()
        user_turn = sum(1 for m in self.messages if m.get("role") == "user")
        self.messages.append({"role": "user", "content": user_text})
        self.turn_counter += 1
        if not self.is_subagent and self.session_id:
            from ..services import fs_undo

            fs_undo.push_checkpoint(self.session_id, user_turn)
            fs_undo.set_turn_context(self.session_id, user_turn)
        self._emit(
            "turn_start",
            {"text": user_text[:500], "message": "user turn"},
        )

        max_iters = (
            self.settings.subagent_max_iterations
            if self.is_subagent
            else self.settings.max_iterations
        )
        compressed = False
        final = ""
        turned = 0
        was_cancelled = False

        try:
            for i in range(1, max_iters + 1):
                if self.cancelled():
                    was_cancelled = True
                    break
                turned = i
                self._maybe_compress()
                self._emit(
                    "llm_start",
                    {
                        "turn": i,
                        "budget": json.loads(debug_dump_budget(self.messages)),
                        "tokens": messages_tokens(self.messages),
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
                    # Fallback to non-streaming if provider rejects stream+tools
                    if self.cancelled():
                        was_cancelled = True
                        break
                    assistant = self.llm.chat(self.messages, tools=self.registry.schemas())
                    preamble_fb = (assistant.get("content") or "").strip()
                    if preamble_fb:
                        self._stream_text_to_ui(preamble_fb)

                if was_cancelled:
                    # Preserve whatever was already streamed instead of dropping it
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
                    final = preamble
                    if not final:
                        # Empty content (common on reasoning models) — drop stub & stream
                        self.messages.pop()
                        final = self._stream_final_reply()
                    break

                names = [
                    (tc.get("function") or {}).get("name", "?") for tc in tool_calls
                ]
                self._emit(
                    "assistant_status",
                    {
                        "text": f"调用工具：{', '.join(names)}",
                        "tools": names,
                    },
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
                    "tokens": messages_tokens(self.messages),
                    "message": "turn complete",
                    "cancelled": was_cancelled,
                },
            )
            return AgentResult(
                text=final,
                messages=self.messages,
                iterations=turned,
                compressed=compressed,
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
        step = 24
        for i in range(0, len(text), step):
            if self.cancelled():
                break
            self._emit("assistant_delta", {"chunk": text[i : i + step]})
            time.sleep(0.012)

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
