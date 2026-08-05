"""Runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# src/metateam/core/config.py
# parents[0]=core, [1]=metateam, [2]=src, [3]=repo
SRC_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[3]
# Runtime data lives under src/ (data/skills/memory/sessions/workspace)
ROOT = SRC_ROOT
BACKEND_ROOT = SRC_ROOT  # back-compat alias

load_dotenv(REPO_ROOT / ".env")


def _bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


@dataclass
class Settings:
    api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    base_url: str = field(
        default_factory=lambda: os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    )
    model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    subagent_model: str = field(
        default_factory=lambda: os.getenv("META_SUBAGENT_MODEL")
        or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    )
    compress_model: str = field(
        default_factory=lambda: os.getenv("META_COMPRESS_MODEL")
        or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    )
    review_model: str = field(
        default_factory=lambda: os.getenv("META_REVIEW_MODEL")
        or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    )

    # Optional per-role credentials when subagent/compress use another provider
    subagent_api_key: str = ""
    subagent_base_url: str = ""
    compress_api_key: str = ""
    compress_base_url: str = ""

    demo_mode: bool = field(
        default_factory=lambda: _bool("META_DEMO_MODE", False)
        or not os.getenv("OPENAI_API_KEY", "").strip()
    )

    context_limit: int = int(os.getenv("META_CONTEXT_LIMIT", "48000"))
    keep_recent_tokens: int = int(os.getenv("META_KEEP_RECENT", "12000"))
    compress_trigger_ratio: float = float(os.getenv("META_COMPRESS_RATIO", "0.72"))
    max_compress_attempts: int = int(os.getenv("META_COMPRESS_ATTEMPTS", "3"))

    max_iterations: int = int(os.getenv("META_MAX_ITERS", "48"))
    subagent_max_iterations: int = int(os.getenv("META_SUB_MAX_ITERS", "28"))
    max_concurrent_children: int = int(os.getenv("META_MAX_CHILDREN", "3"))
    max_spawn_depth: int = int(os.getenv("META_MAX_SPAWN_DEPTH", "2"))

    same_call_fail_limit: int = int(os.getenv("META_SAME_CALL_FAIL", "4"))
    tool_result_cap: int = int(os.getenv("META_TOOL_RESULT_CAP", "18000"))

    review_every_n_turns: int = int(os.getenv("META_REVIEW_EVERY", "6"))
    # Off by default — silent review burns tokens and can mutate MEMORY/skills
    auto_skill_review: bool = field(default_factory=lambda: _bool("META_AUTO_REVIEW", False))

    root: Path = ROOT
    workspace: Path = field(default_factory=lambda: ROOT / "workspace")
    skills_dir: Path = field(default_factory=lambda: ROOT / "skills")
    memory_file: Path = field(default_factory=lambda: ROOT / "memory" / "MEMORY.md")
    sessions_dir: Path = field(default_factory=lambda: ROOT / "sessions")
    data_dir: Path = field(default_factory=lambda: ROOT / "data")

    # On by default for local desktop use; mutating shell still requires approval.
    allow_shell: bool = field(default_factory=lambda: _bool("META_ALLOW_SHELL", True))
    # Path-allowlist sandbox for shell/verify (host cwd=workspace; not a copy FS)
    shell_sandbox: bool = field(default_factory=lambda: _bool("META_SHELL_SANDBOX", True))
    shell_timeout: int = int(os.getenv("META_SHELL_TIMEOUT", "90"))
    # Enable MCP tool discovery when mcp package + mcp.json servers are present
    mcp_enabled: bool = field(default_factory=lambda: _bool("META_MCP_ENABLED", True))

    host: str = field(default_factory=lambda: os.getenv("META_HOST", "127.0.0.1"))
    port: int = int(os.getenv("META_PORT", "8787"))

    provider: str = "deepseek"
    reasoning_effort: str = "medium"
    thinking_enabled: bool = True
    temperature: float = 0.2
    main_endpoint: Any = None
    subagent_endpoint: Any = None
    compress_endpoint: Any = None

    def ensure_dirs(self) -> None:
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.memory_file.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        if not self.memory_file.exists():
            self.memory_file.write_text(
                "# MEMORY\n\nDurable facts about the user and environment.\n",
                encoding="utf-8",
            )


_SETTINGS: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    global _SETTINGS
    if _SETTINGS is None or reload:
        from ..services.model_config import apply_to_settings, load_model_config

        _SETTINGS = Settings()
        _SETTINGS.ensure_dirs()
        apply_to_settings(_SETTINGS, load_model_config())
        try:
            from ..services.workspace_store import apply_saved_workspace

            apply_saved_workspace()
        except Exception as exc:
            from .logutil import get_logger, log_exception

            log_exception(get_logger("metateam.config"), "apply_saved_workspace failed", exc)
    return _SETTINGS


def reload_settings() -> Settings:
    return get_settings(reload=True)
