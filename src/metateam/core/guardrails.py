"""Stop identical failing tool loops and read-file thrashing."""

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


# Tools that only explore — too many in a row means the agent is stuck browsing.
_EXPLORE_TOOLS = frozenset(
    {
        "read_file",
        "list_dir",
        "search_text",
        "codebase_overview",
        "codebase_find_similar",
        "codebase_impact",
        "coherence_checklist",
        "memory_read",
    }
)


@dataclass
class Guardrails:
    same_call_fail_limit: int = 4
    # Kept for backwards-compatible construction in tests; read_file is no longer capped.
    max_reads_per_path: int = 0
    max_reads_total: int = 0
    max_explore_streak: int = 8
    fails: dict[str, int] = field(default_factory=dict)
    blocked: set[str] = field(default_factory=set)
    explore_streak: int = 0
    force_progress_hint: bool = False

    def begin_turn(self) -> None:
        """Reset per-user-turn budgets (keep fail memory soft-cleared)."""
        self.explore_streak = 0
        self.force_progress_hint = False
        # Do not clear fails/blocked mid-session — identical fail loops still matter.

    def before(self, name: str, args: dict[str, Any]) -> str | None:
        s = _sig(name, args)
        if s in self.blocked or self.fails.get(s, 0) >= self.same_call_fail_limit:
            self.blocked.add(s)
            return (
                f"ERROR: blocked repeated failing call `{name}` with identical args "
                f"({self.same_call_fail_limit}x). Change strategy or explain the blocker."
            )

        # read_file is never hard-capped — models may re-read / page freely.
        if (
            name in _EXPLORE_TOOLS
            and name != "read_file"
            and self.explore_streak >= self.max_explore_streak
        ):
            return (
                f"ERROR: explore streak limit ({self.max_explore_streak} explore-only tool "
                "batches). Stop searching; write/edit, ask_user, or give a status answer."
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

        if name in _EXPLORE_TOOLS:
            self.explore_streak += 1
            if self.explore_streak >= max(4, self.max_explore_streak - 2):
                self.force_progress_hint = True
        else:
            self.explore_streak = 0
            self.force_progress_hint = False

    def progress_nudge(self) -> str | None:
        """Optional user-role nudge injected into the agent loop."""
        if not self.force_progress_hint:
            return None
        self.force_progress_hint = False
        return (
            "[Coherence] You have been exploring (read/search) for several steps. "
            "Prefer acting soon: make the minimal edit, ask_user one concrete question, "
            "or reply with a short status of what blocks you."
        )
