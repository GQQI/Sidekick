"""Stop identical failing tool loops."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


def _sig(name: str, args: dict[str, Any]) -> str:
    blob = json.dumps({"n": name, "a": args}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def looks_failed(content: str) -> bool:
    low = (content or "")[:400].lower()
    return (
        low.startswith("error")
        or '"error"' in low
        or "traceback" in low
        or low.startswith("failed")
    )


@dataclass
class Guardrails:
    same_call_fail_limit: int = 4
    fails: dict[str, int] = field(default_factory=dict)
    blocked: set[str] = field(default_factory=set)

    def before(self, name: str, args: dict[str, Any]) -> str | None:
        s = _sig(name, args)
        if s in self.blocked or self.fails.get(s, 0) >= self.same_call_fail_limit:
            self.blocked.add(s)
            return (
                f"ERROR: blocked repeated failing call `{name}` with identical args "
                f"({self.same_call_fail_limit}x). Change strategy or explain the blocker."
            )
        return None

    def after(self, name: str, args: dict[str, Any], content: str) -> None:
        s = _sig(name, args)
        if looks_failed(content):
            self.fails[s] = self.fails.get(s, 0) + 1
            if self.fails[s] >= self.same_call_fail_limit:
                self.blocked.add(s)
        else:
            self.fails.pop(s, None)
