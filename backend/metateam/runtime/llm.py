"""OpenAI-compatible client (DeepSeek-ready) + demo stub."""

from __future__ import annotations

import json
import re
from typing import Any, Iterator, Optional

from openai import OpenAI

from .think_tags import ThinkTagSplitter, split_think_tags
from ..core.config import Settings


class LLM:
    def __init__(self, settings: Settings, model: Optional[str] = None):
        self.settings = settings
        self.model = model or settings.model
        self.demo = settings.demo_mode
        self.client: OpenAI | None = None
        self._active_stream: Any = None
        if not self.demo:
            if not settings.api_key:
                raise RuntimeError("API key empty — configure in UI or META_DEMO_MODE=1")
            self.client = OpenAI(api_key=settings.api_key, base_url=settings.base_url)

    def close_active_stream(self) -> None:
        """Best-effort close of the in-flight OpenAI stream (interrupts provider read)."""
        stream = self._active_stream
        self._active_stream = None
        if stream is None:
            return
        for closer in (
            getattr(stream, "close", None),
            getattr(getattr(stream, "response", None), "close", None),
            getattr(getattr(stream, "http_response", None), "close", None),
        ):
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass

    def _call_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]],
        temperature: float,
        stream: bool = False,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": _strip_for_api(messages),
            "temperature": temperature,
            "stream": stream,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        # DeepSeek / reasoning models
        effort = getattr(self.settings, "reasoning_effort", None)
        if effort:
            kwargs["reasoning_effort"] = effort

        extra: dict[str, Any] = {}
        if getattr(self.settings, "thinking_enabled", False):
            extra["thinking"] = {"type": "enabled"}
        if extra:
            kwargs["extra_body"] = extra
        return kwargs

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: Optional[float] = None,
    ) -> dict[str, Any]:
        if self.demo:
            return _demo_chat(messages, tools)

        assert self.client is not None
        temp = self.settings.temperature if temperature is None else temperature
        kwargs = self._call_kwargs(messages, tools, temp, stream=False)
        # create() doesn't take stream=False as needed if we pop it
        kwargs.pop("stream", None)

        try:
            resp = self.client.chat.completions.create(**kwargs)
        except TypeError:
            # Older SDKs may not accept reasoning_effort
            kwargs.pop("reasoning_effort", None)
            resp = self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            # Retry without thinking if provider rejects
            msg = str(exc).lower()
            if "thinking" in msg or "reasoning" in msg or "extra_body" in msg:
                kwargs.pop("extra_body", None)
                kwargs.pop("reasoning_effort", None)
                resp = self.client.chat.completions.create(**kwargs)
            else:
                raise

        msg = resp.choices[0].message
        content = extract_content_text(msg)
        reasoning = extract_reasoning_text(msg)
        content, tagged = split_think_tags(content)
        reasoning = f"{reasoning}{tagged}"

        out: dict[str, Any] = {"role": "assistant", "content": content}
        if reasoning:
            out["reasoning"] = reasoning
        if msg.tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in msg.tool_calls
            ]
        return out

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        cancel_check: Optional[Any] = None,
    ) -> Iterator[tuple[str, Any]]:
        """Yield ("delta", text) then a final ("done", assistant_message)."""
        if self.demo:
            msg = _demo_chat(messages, tools)
            text = msg.get("content") or ""
            for i in range(0, len(text), 12):
                if callable(cancel_check) and cancel_check():
                    break
                yield ("delta", text[i : i + 12])
            yield ("done", msg)
            return

        assert self.client is not None
        temp = self.settings.temperature if temperature is None else temperature
        kwargs = self._call_kwargs(messages, tools, temp, stream=True)
        try:
            stream = self.client.chat.completions.create(**kwargs)
        except TypeError:
            kwargs.pop("reasoning_effort", None)
            stream = self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            msg = str(exc).lower()
            if "thinking" in msg or "reasoning" in msg or "extra_body" in msg:
                kwargs.pop("extra_body", None)
                kwargs.pop("reasoning_effort", None)
                stream = self.client.chat.completions.create(**kwargs)
            else:
                raise

        self._active_stream = stream
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_acc: dict[int, dict[str, str]] = {}
        cancelled = False
        # Stateful splitter for <think>…</think> embedded in content stream
        tag_split = ThinkTagSplitter()

        try:
            for chunk in stream:
                if callable(cancel_check) and cancel_check():
                    cancelled = True
                    break
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if not delta:
                    continue

                # 1) Native reasoning_content / reasoning fields
                reasoning_piece = extract_reasoning_text(delta)
                if reasoning_piece:
                    reasoning_parts.append(reasoning_piece)
                    yield ("reasoning_delta", reasoning_piece)

                # 2) Visible content, with <think> tags peeled into reasoning
                content_piece = extract_content_text(delta)
                if content_piece:
                    for kind, piece in tag_split.feed(content_piece):
                        if not piece:
                            continue
                        if kind == "reasoning":
                            reasoning_parts.append(piece)
                            yield ("reasoning_delta", piece)
                        else:
                            content_parts.append(piece)
                            yield ("delta", piece)

                for tc in getattr(delta, "tool_calls", None) or []:
                    idx = int(getattr(tc, "index", 0) or 0)
                    slot = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    changed = False
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                        changed = True
                    fn = getattr(tc, "function", None)
                    arg_piece = ""
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] = (slot["name"] or "") + str(fn.name)
                            changed = True
                        if getattr(fn, "arguments", None):
                            arg_piece = str(fn.arguments)
                            slot["arguments"] = (slot["arguments"] or "") + arg_piece
                            changed = True
                    if changed:
                        yield (
                            "tool_delta",
                            {
                                "index": idx,
                                "id": slot.get("id") or "",
                                "name": slot.get("name") or "",
                                "arguments_delta": arg_piece,
                                "arguments": slot.get("arguments") or "",
                            },
                        )
        finally:
            if self._active_stream is stream:
                self._active_stream = None
            for closer in (
                getattr(stream, "close", None),
                getattr(getattr(stream, "response", None), "close", None),
                getattr(getattr(stream, "http_response", None), "close", None),
            ):
                if callable(closer):
                    try:
                        closer()
                    except Exception:
                        pass

        for kind, piece in tag_split.flush():
            if not piece:
                continue
            if kind == "reasoning":
                reasoning_parts.append(piece)
                yield ("reasoning_delta", piece)
            else:
                content_parts.append(piece)
                yield ("delta", piece)

        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        out: dict[str, Any] = {"role": "assistant", "content": content}
        if reasoning:
            out["reasoning"] = reasoning
        if tool_acc and not cancelled:
            out["tool_calls"] = [
                {
                    "id": tool_acc[i].get("id") or f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": tool_acc[i].get("name") or "",
                        "arguments": tool_acc[i].get("arguments") or "{}",
                    },
                }
                for i in sorted(tool_acc)
                if tool_acc[i].get("name")
            ]
        yield ("done", out)

    def stream_text(
        self,
        messages: list[dict[str, Any]],
        temperature: Optional[float] = None,
        cancel_check: Optional[Any] = None,
    ) -> Iterator[str]:
        for kind, payload in self.stream_chat(
            messages, tools=None, temperature=temperature, cancel_check=cancel_check
        ):
            if kind == "delta":
                yield str(payload)

    def complete_text(self, system: str, user: str, temperature: float = 0.1) -> str:
        msg = self.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tools=None,
            temperature=temperature,
        )
        return (msg.get("content") or "").strip()


def extract_content_text(msg: Any) -> str:
    """Visible assistant content only (never reasoning/thinking)."""
    if msg is None:
        return ""
    content = getattr(msg, "content", None)
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                # Skip explicit thinking parts in content arrays
                ptype = str(p.get("type") or "")
                if ptype in {"thinking", "reasoning", "reasoning_content"}:
                    continue
                if p.get("text"):
                    parts.append(str(p["text"]))
            else:
                ptype = str(getattr(p, "type", "") or "")
                if ptype in {"thinking", "reasoning", "reasoning_content"}:
                    continue
                t = getattr(p, "text", None)
                if t:
                    parts.append(str(t))
        return "".join(parts)
    return ""


def extract_reasoning_text(msg: Any) -> str:
    """Model thinking / reasoning stream pieces only."""
    if msg is None:
        return ""
    parts: list[str] = []
    for attr in ("reasoning_content", "reasoning", "thinking"):
        v = getattr(msg, attr, None)
        if isinstance(v, str) and v:
            parts.append(v)
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for p in content:
            if isinstance(p, dict):
                ptype = str(p.get("type") or "")
                if ptype in {"thinking", "reasoning", "reasoning_content"} and p.get("text"):
                    parts.append(str(p["text"]))
            else:
                ptype = str(getattr(p, "type", "") or "")
                if ptype in {"thinking", "reasoning", "reasoning_content"}:
                    t = getattr(p, "text", None)
                    if t:
                        parts.append(str(t))
    extra = getattr(msg, "model_extra", None)
    if isinstance(extra, dict):
        for k in ("reasoning_content", "reasoning", "thinking"):
            v = extra.get(k)
            if isinstance(v, str) and v:
                parts.append(v)
    return "".join(parts)


def extract_message_text(msg: Any) -> str:
    """Prefer visible content; do not fall back to reasoning (kept for compat)."""
    content = extract_content_text(msg)
    if content.strip():
        visible, _ = split_think_tags(content)
        return visible
    return ""


def parse_tool_args(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
        return {"_raw": raw}


def _strip_for_api(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        item = {
            k: v
            for k, v in m.items()
            if k in ("role", "content", "tool_calls", "tool_call_id", "name")
        }
        if item.get("role") == "assistant" and not item.get("content") and not item.get("tool_calls"):
            item["content"] = ""
        out.append(item)
    return out


def _demo_chat(
    messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]]
) -> dict[str, Any]:
    last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = str(m.get("content") or "")
            break

    if messages and messages[-1].get("role") == "tool":
        content = str(messages[-1].get("content") or "")[:500]
        return {
            "role": "assistant",
            "content": f"【Demo】工具结果已收到。\n\n{content[:280]}",
        }

    tool_names = set()
    if tools:
        for t in tools:
            fn = t.get("function") or {}
            tool_names.add(fn.get("name"))

    low = last_user.lower()

    # Prefer skill_* function tools
    skill_tools = [n for n in tool_names if n and str(n).startswith("skill_")]
    if ("skill" in low or "技能" in last_user) and skill_tools:
        name = "skill_hello_workspace" if "skill_hello_workspace" in skill_tools else skill_tools[0]
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "demo_sk_1",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps({"task": last_user}, ensure_ascii=False),
                    },
                }
            ],
        }

    if "并行" in last_user or "delegate" in low:
        if "delegate_task" in tool_names:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "demo_del_1",
                        "type": "function",
                        "function": {
                            "name": "delegate_task",
                            "arguments": json.dumps(
                                {
                                    "tasks": [
                                        {"goal": "List workspace", "context": "list_dir ."},
                                        {
                                            "goal": "Use hello skill",
                                            "context": "call skill_hello_workspace",
                                        },
                                    ]
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            }

    if any(k in last_user for k in ("列出", "list", "目录", "workspace")) and "list_dir" in tool_names:
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "demo_ls_1",
                    "type": "function",
                    "function": {"name": "list_dir", "arguments": json.dumps({"path": "."})},
                }
            ],
        }

    if ("写" in last_user or "write" in low or "hello" in low) and "write_file" in tool_names:
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "demo_w_1",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps(
                            {
                                "path": "notes/demo.md",
                                "content": "# Demo\n\nHello from Sidekick.\n",
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        }

    return {
        "role": "assistant",
        "content": (
            "【Demo 模式】配置模型 API Key 后可使用真实推理。\n"
            "可试：列出 workspace / 调用 hello skill / 并行委派。"
        ),
    }
