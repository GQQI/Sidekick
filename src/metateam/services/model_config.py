"""Persisted model provider settings (UI-editable)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..core.config import ROOT

CONFIG_PATH = ROOT / "data" / "model.json"

DEFAULTS = {
    "provider": "deepseek",
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-v4-pro",
    # Cheaper defaults for secondary roles — override in UI if needed
    "subagent_model": "deepseek-chat",
    "compress_model": "deepseek-chat",
    "review_model": "deepseek-chat",
    "reasoning_effort": "medium",
    "thinking_enabled": True,
    "demo_mode": False,
    "temperature": 0.2,
}


@dataclass
class ModelConfig:
    provider: str = "deepseek"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-pro"
    subagent_model: str = "deepseek-chat"
    compress_model: str = "deepseek-chat"
    review_model: str = "deepseek-chat"
    reasoning_effort: str = "medium"
    thinking_enabled: bool = True
    demo_mode: bool = False
    temperature: float = 0.2

    def masked(self) -> dict[str, Any]:
        d = asdict(self)
        key = d.get("api_key") or ""
        if len(key) > 10:
            d["api_key_masked"] = key[:6] + "…" + key[-4:]
            d["api_key_set"] = True
        else:
            d["api_key_masked"] = ""
            d["api_key_set"] = bool(key)
        # Do not echo the raw key to the browser; blank draft + api_key_set keeps the stored key
        d["api_key"] = ""
        return d


def load_model_config() -> ModelConfig:
    data = dict(DEFAULTS)
    # env bootstrap
    env_key = (
        os.getenv("OPENAI_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or ""
    ).strip()
    if env_key:
        data["api_key"] = env_key
    if os.getenv("OPENAI_BASE_URL"):
        data["base_url"] = os.getenv("OPENAI_BASE_URL", data["base_url"])
    if os.getenv("OPENAI_MODEL"):
        data["model"] = os.getenv("OPENAI_MODEL", data["model"])
    if os.getenv("META_DEMO_MODE") is not None:
        data["demo_mode"] = os.getenv("META_DEMO_MODE", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    if CONFIG_PATH.exists():
        try:
            file_data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(file_data, dict):
                data.update({k: v for k, v in file_data.items() if k in DEFAULTS})
        except Exception:
            pass

    # Key present → real API mode. Empty key → demo. Never keep stale demo_mode=true with a key.
    if str(data.get("api_key") or "").strip():
        data["demo_mode"] = False
    else:
        data["demo_mode"] = True

    return ModelConfig(**{k: data.get(k, DEFAULTS[k]) for k in DEFAULTS})


def save_model_config(cfg: ModelConfig) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Normalize before persist so UI/file never disagree
    if str(cfg.api_key or "").strip():
        cfg.demo_mode = False
    else:
        cfg.demo_mode = True
    CONFIG_PATH.write_text(
        json.dumps(asdict(cfg), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def apply_to_settings(settings: Any, cfg: ModelConfig | None = None) -> Any:
    cfg = cfg or load_model_config()
    settings.api_key = cfg.api_key
    settings.base_url = cfg.base_url.rstrip("/")
    settings.model = cfg.model
    settings.subagent_model = cfg.subagent_model or cfg.model
    settings.compress_model = cfg.compress_model or cfg.model
    settings.review_model = cfg.review_model or cfg.model
    # API key always wins over a leftover demo_mode flag from older saves / UI patches
    settings.demo_mode = not bool(str(cfg.api_key or "").strip())
    settings.reasoning_effort = cfg.reasoning_effort
    settings.thinking_enabled = bool(cfg.thinking_enabled)
    settings.temperature = float(cfg.temperature)
    settings.provider = cfg.provider
    return settings


def update_model_config(patch: dict[str, Any]) -> ModelConfig:
    cfg = load_model_config()
    for k, v in patch.items():
        if k == "api_key" and (v is None or v == "" or v == "***"):
            continue  # keep existing
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    # Explicit demo preset: clear key. Otherwise a non-empty key exits demo.
    if patch.get("demo_mode") is True and not str(patch.get("api_key") or cfg.api_key or "").strip():
        cfg.api_key = ""
        cfg.demo_mode = True
    elif str(cfg.api_key or "").strip():
        cfg.demo_mode = False
    else:
        cfg.demo_mode = True
    save_model_config(cfg)
    return cfg
