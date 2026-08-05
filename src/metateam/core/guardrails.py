"""Stop identical failing tool loops and read-file thrashing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


def _sig(name: str, args: dict[str, Any]) -> str:
    blob = json.dumps({"n": name, "a": args}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _norm_path(raw: Any) -> str:
    return str(raw or "").strip().replace("\\", "/").lstrip("./").lower()


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
    max_reads_per_path: int = 6
    max_reads_total: int = 28
    max_explore_streak: int = 8
    fails: dict[str, int] = field(default_factory=dict)
    blocked: set[str] = field(default_factory=set)
    read_counts: dict[str, int] = field(default_factory=dict)
    explore_streak: int = 0
    force_progress_hint: bool = False

    def begin_turn(self) -> None:
        """Reset per-user-turn budgets (keep fail memory soft-cleared)."""
        self.read_counts.clear()
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

        if name == "read_file":
            path = _norm_path(args.get("path"))
            per = self.read_counts.get(path, 0)
            total = sum(self.read_counts.values())
            if per >= self.max_reads_per_path:
                return (
                    f"ERROR: read_file limit for `{path or args.get('path')}` "
                    f"({self.max_reads_per_path}x this turn). "
                    "Do NOT paginate the rest of the file. Use what you already read, "
                    "search_text for a symbol, or edit/act now."
                )
            if total >= self.max_reads_total:
                return (
                    f"ERROR: read_file budget exhausted ({self.max_reads_total} reads this turn). "
                    "Stop exploring — implement the change, ask_user, or summarize blockers."
                )

        if name in _EXPLORE_TOOLS and self.explore_streak >= self.max_explore_streak:
            return (
                f"ERROR: explore streak limit ({self.max_explore_streak} explore-only tool "
                "batches). Stop reading/searching; write/edit, ask_user, or give a status answer."
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

        if name == "read_file" and not looks_failed(content):
            path = _norm_path(args.get("path"))
            self.read_counts[path] = self.read_counts.get(path, 0) + 1

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
            "Do NOT keep reading the rest of files. Either: (1) make the minimal edit now, "
            "(2) ask_user one concrete question, or (3) reply with a short status of what "
            "blocks you. Prefer acting over more read_file."
        )
