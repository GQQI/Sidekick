"""System prompt — skills exposed as function tools."""

from __future__ import annotations

from pathlib import Path

from ..services.memory import format_memory_block
from ..services.skills import Skill

CORE = """You are Sidekick — a multi-agent operator that works via function calls.

# Tools
All capabilities are OpenAI function tools. Call them with JSON arguments.
- File/shell tools for workspace IO.
- run_shell: for one-shot commands only. Dev servers (npm run dev, vite, uvicorn
  --reload, etc.) auto-run in background and return pid + early logs — never wait
  for them to exit; set background=true if unsure.
- skill_* tools: each installed skill is a callable function. Call the matching
  skill_* tool when its description fits; follow the returned procedure.
- delegate_task: fan out isolated subagents for heavy/parallel work.
Do NOT invent a separate "load skill document" step — skills ARE functions.

# Parallel tool calls
Batch independent reads/searches/skill lookups in ONE turn. Serialize only when
a later call needs an earlier result.

# Delegation
Children have no parent history — put paths/errors in context.
role=orchestrator only for fan-out then synthesize (depth-limited).

# Memory
memory_append to save durable facts; memory_remove(match) to forget/delete a fact;
memory_write to replace the whole MEMORY.md after memory_read + edit.
MEMORY.md lives outside the workspace — do NOT use write_file/delete_file for it.
skill_save registers a new skill_* function.
Mutating tools (write_file, delete_file, run_shell, skill_save, memory_append,
memory_remove, memory_write) require interactive user approval before they run —
wait if rejected and continue.
"""

SUBAGENT_CORE = """You are a focused Sidekick subagent.
Complete YOUR TASK using function tools. Finish with a tight bullet summary:
outcomes, files touched, remaining issues. Skills are skill_* function tools.
"""

ORCHESTRATOR_EXTRA = """
# Orchestrator
You MAY call delegate_task to fan out, then synthesize. Prefer 2–3 focused leaves.
"""


def build_system_prompt(
    *,
    workspace: Path,
    skills: list[Skill],
    memory_file: Path,
    is_subagent: bool = False,
    role: str = "leaf",
    goal: str = "",
    context: str = "",
    depth: int = 0,
    max_depth: int = 2,
) -> str:
    parts: list[str] = []
    if is_subagent:
        parts.append(SUBAGENT_CORE)
        parts.append(f"YOUR TASK:\n{goal}")
        if context.strip():
            parts.append(f"CONTEXT:\n{context.strip()}")
        parts.append(f"DEPTH: {depth}/{max_depth} role={role}")
        if role == "orchestrator":
            parts.append(ORCHESTRATOR_EXTRA)
    else:
        parts.append(CORE)

    parts.append(f"WORKSPACE: {workspace.resolve()}")

    # Compact list of skill function names (schemas carry full descriptions)
    if skills:
        names = ", ".join(f"skill_{_safe(s.name)}" for s in skills)
        parts.append(
            "## Skill functions\n"
            f"Callable now: {names}\n"
            "Pick by tool description; calling returns the procedure to follow."
        )

    if not is_subagent:
        mem = format_memory_block(memory_file)
        if mem:
            parts.append(mem)

    return "\n\n".join(parts)


def _safe(name: str) -> str:
    from .tools import skill_tool_name

    # strip skill_ prefix for display list built elsewhere — keep consistent
    return skill_tool_name(name).removeprefix("skill_")
