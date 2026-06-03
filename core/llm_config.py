# -*- coding: utf-8 -*-
"""Unified LLM API configuration resolver.

Mnemos uses the host agent to call tools and consume knowledge, but distillation
itself should go through an OpenAI-compatible LLM API by default. This helper
keeps environment-variable and JSON-config resolution consistent across CLI,
MCP tools, and distillation pipelines.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional


SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"
SILICONFLOW_MODEL = "deepseek-ai/DeepSeek-V3"
OPENAI_MODEL = "gpt-4o-mini"


@dataclass(frozen=True)
class LLMApiConfig:
    provider: str
    api_key: str
    base_url: str
    model: str
    source: str

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def masked_key(self) -> str:
        if not self.api_key:
            return ""
        if len(self.api_key) <= 8:
            return "***"
        return f"{self.api_key[:4]}...{self.api_key[-4:]}"


def _cfg_get(config: Any, key: str, default: Any = None) -> Any:
    try:
        return config.get(key, default)
    except Exception:
        return default


def _provider_defaults(provider: str) -> tuple[str, str]:
    provider = (provider or "").lower()
    if provider == "siliconflow":
        return SILICONFLOW_BASE_URL, SILICONFLOW_MODEL
    return OPENAI_BASE_URL, OPENAI_MODEL


def _provider_from_base_url(base_url: str, fallback: str = "openai") -> str:
    if "siliconflow" in (base_url or "").lower():
        return "siliconflow"
    return fallback or "openai"


def resolve_llm_api_config(config: Optional[Any] = None) -> LLMApiConfig:
    """Resolve LLM API config with a stable priority order.

    Priority:
    1. Environment variables.
    2. ``llm.providers.<provider>`` in main.json.
    3. top-level ``llm.*`` fields.
    4. SiliconFlow embedding config as a compatibility fallback.
    """

    # Environment variables win. If SILICONFLOW_API_KEY exists and no explicit
    # OPENAI_BASE_URL is provided, pick SiliconFlow defaults so first-time users
    # do not accidentally hit the OpenAI default endpoint with a SiliconFlow key.
    env_base = (os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
    sf_key = os.environ.get("SILICONFLOW_API_KEY") or ""
    openai_key = os.environ.get("OPENAI_API_KEY") or ""

    if env_base:
        provider = _provider_from_base_url(env_base, fallback="openai")
        if provider == "siliconflow":
            api_key = sf_key or openai_key
            model = os.environ.get("OPENAI_MODEL") or SILICONFLOW_MODEL
            source = "env:SILICONFLOW_API_KEY" if sf_key else "env:OPENAI_API_KEY"
        else:
            api_key = openai_key or sf_key
            model = os.environ.get("OPENAI_MODEL") or OPENAI_MODEL
            source = "env:OPENAI_API_KEY" if openai_key else "env:SILICONFLOW_API_KEY"
        return LLMApiConfig(provider, api_key, env_base, model, source if api_key else "missing")

    if sf_key:
        return LLMApiConfig(
            "siliconflow",
            sf_key,
            os.environ.get("SILICONFLOW_BASE_URL", SILICONFLOW_BASE_URL).rstrip("/"),
            os.environ.get("SILICONFLOW_MODEL") or os.environ.get("OPENAI_MODEL") or SILICONFLOW_MODEL,
            "env:SILICONFLOW_API_KEY",
        )

    if openai_key:
        return LLMApiConfig(
            "openai",
            openai_key,
            OPENAI_BASE_URL,
            os.environ.get("OPENAI_MODEL") or OPENAI_MODEL,
            "env:OPENAI_API_KEY",
        )

    if config is None:
        try:
            from core.config import get_config
            config = get_config()
        except Exception:
            config = None

    provider = str(_cfg_get(config, "llm.provider", "siliconflow") or "siliconflow").lower()
    base_default, model_default = _provider_defaults(provider)

    providers = _cfg_get(config, "llm.providers", {}) or {}
    provider_cfg = providers.get(provider, {}) if isinstance(providers, dict) else {}
    if isinstance(provider_cfg, dict):
        api_key = provider_cfg.get("api_key") or ""
        if api_key:
            return LLMApiConfig(
                provider,
                api_key,
                (provider_cfg.get("base_url") or base_default).rstrip("/"),
                provider_cfg.get("model") or model_default,
                f"config:llm.providers.{provider}.api_key",
            )

    top_key = _cfg_get(config, "llm.api_key", "") or ""
    if top_key:
        return LLMApiConfig(
            provider,
            top_key,
            str(_cfg_get(config, "llm.base_url", base_default) or base_default).rstrip("/"),
            str(_cfg_get(config, "llm.model", model_default) or model_default),
            "config:llm.api_key",
        )

    # Compatibility fallback: many existing installs only configured
    # SiliconFlow under embedding.*. Distillation can use the same provider key.
    embedding_provider = str(_cfg_get(config, "embedding.provider", "") or "").lower()
    embed_key = _cfg_get(config, "embedding.api_key", "") or ""
    if embed_key and embedding_provider == "siliconflow":
        return LLMApiConfig(
            "siliconflow",
            embed_key,
            str(_cfg_get(config, "embedding.base_url", SILICONFLOW_BASE_URL) or SILICONFLOW_BASE_URL).rstrip("/"),
            str(_cfg_get(config, "llm.model", SILICONFLOW_MODEL) or SILICONFLOW_MODEL),
            "config:embedding.api_key",
        )

    return LLMApiConfig(provider, "", base_default, model_default, "missing")

