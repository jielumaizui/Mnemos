"""Configuration and required-model helpers for ``scripts.auto_setup``."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, Set

from core.ops.durable_io import DurableIOError, inspect_path_kind
from core.ops.durable_io import read_native_bytes
from scripts.setup_model_endpoints import OPTIONAL_MODEL_ENDPOINT_SPECS

VALID_PERFORMANCE_TIERS = ("low_power", "eco", "default", "performance", "dev")
HEAVY_SERVICES = (
    "distill_and_merge",
    "persona_analyzer",
    "persona_challenge",
    "signal_collector",
    "eventbus",
    "inbox_scanner",
    "observation_engine",
    "reflection_engine",
    "dispute_scan",
    "reminder_scan",
    "freshness_refresh",
    "entropy_scan",
    "link_probe",
)

LLM_PROVIDER_DEFAULTS = {
    "siliconflow": {
        "api_key_env": "SILICONFLOW_API_KEY",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V4-Flash",
    },
    "dmxapi": {
        "api_key_env": "DMXAPI_API_KEY",
        "base_url": "https://www.dmxapi.cn/v1",
        "model": "kimi-k2.5-free",
    },
    "openai": {
        "api_key_env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
}

MODEL_ENDPOINT_SPECS = {
    "llm": {
        "label": "LLM（对话/蒸馏模型）",
        "config_section": "llm",
        "env_prefix": "MNEMOS_LLM",
    },
    "embedding": {
        "label": "Embedding（向量/语义召回模型）",
        "config_section": "embedding",
        "env_prefix": "MNEMOS_EMBEDDING",
    },
    "reranker": {
        "label": "Reranker（搜索重排模型）",
        "config_section": "reranker",
        "env_prefix": "MNEMOS_RERANKER",
    },
}


def deep_merge(
    base: dict,
    override: dict,
    visited_ids: Optional[Set[int]] = None,
) -> None:
    """Merge nested mappings while terminating safely on cyclic input."""
    visited = visited_ids if visited_ids is not None else set()
    override_id = id(override)
    if override_id in visited:
        return
    visited.add(override_id)
    for key, val in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            deep_merge(base[key], val, visited)
        else:
            base[key] = val
    visited.discard(override_id)


def load_config_data(host: Any, config_file: Path, preserve: bool) -> dict:
    """Load defaults plus any existing runtime configuration."""
    from core.config import DEFAULT_CONFIG

    data = copy.deepcopy(DEFAULT_CONFIG)
    try:
        config_kind = inspect_path_kind(config_file)
    except DurableIOError as exc:
        host.print_warn(f"现有配置读取失败，将重新生成: {exc}")
        return data
    if config_kind == "missing":
        return data
    if config_kind != "file":
        host.print_warn("现有配置不是安全的普通文件，将重新生成")
        return data
    if preserve:
        try:
            existing = json.loads(read_native_bytes(config_file).decode("utf-8"))
            if isinstance(existing, dict):
                host.print_ok("保留现有配置，仅更新必要字段")
                host._deep_merge(data, existing)
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            host.print_warn(f"现有配置读取失败，将重新生成: {exc}")
        return data
    try:
        existing = json.loads(read_native_bytes(config_file).decode("utf-8"))
        if isinstance(existing, dict):
            host._deep_merge(data, existing)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        host.print_warn(f"现有 JSON 配置读取失败，将重新生成: {exc}")
    return data


def apply_performance_tier(host: Any, data: dict) -> None:
    """Apply the selected performance tier and its service defaults."""
    tier = os.environ.get("MNEMOS_PERFORMANCE_TIER", "default").lower()
    if tier not in VALID_PERFORMANCE_TIERS:
        tier = "default"
    data["performance_tier"] = tier
    services = data.setdefault("daemon", {}).setdefault("services", {})
    if tier in ("low_power", "eco"):
        for service_name in HEAVY_SERVICES:
            services[service_name] = False


def apply_vault_paths(
    host: Any,
    data: dict,
    mnemos_vault: Path,
    raw_vault: Path,
) -> None:
    """Bind all storage projections to the selected vault paths."""
    data["mnemos_dir"] = str(host._runtime_config_path().parent.parent)
    vaults = data.setdefault("vaults", {})
    vaults.setdefault("mnemos", {})["path"] = str(mnemos_vault)
    vaults.setdefault("raw", {})["path"] = str(raw_vault)
    data.setdefault("wiki", {})["vault_path"] = str(mnemos_vault)
    data.setdefault("storage", {}).setdefault("obsidian", {})["vault_path"] = str(raw_vault)
    cognitive_graph = data.setdefault("cognitive_graph", {})
    cognitive_graph.setdefault("db_path", str(host._mnemos_dir() / "cognitive_graph.db"))
    l1_storage = data.setdefault("l1_storage", {})
    l1_storage["enabled"] = False
    l1_storage["api_url"] = ""
    l1_storage["token"] = ""


def apply_default_services(data: dict) -> None:
    """Enable the canonical setup-time service owners."""
    services = data.setdefault("daemon", {}).setdefault("services", {})
    services["capture_worker"] = True
    services["raw_sync"] = True
    services.pop("l1_sync", None)
    services["eventbus"] = True
    data.setdefault("integrations", {}).setdefault("mcp", {})["enabled"] = True


def model_endpoint_spec(kind: str) -> dict:
    """Return the canonical setup specification for one model endpoint."""

    if kind in MODEL_ENDPOINT_SPECS:
        return MODEL_ENDPOINT_SPECS[kind]
    return OPTIONAL_MODEL_ENDPOINT_SPECS[kind]


def llm_provider_default(provider: str) -> dict:
    """Return immutable setup defaults for an LLM provider."""

    return LLM_PROVIDER_DEFAULTS.get(provider, LLM_PROVIDER_DEFAULTS["siliconflow"])


def llm_cost_level(provider: str) -> str:
    """Classify the setup-time provider cost level."""

    return "free" if provider == "dmxapi" else "paid"


def infer_provider_from_base_url(base_url: str) -> str:
    """Infer the provider label used by setup from a configured base URL."""

    lowered = (base_url or "").lower()
    if "siliconflow" in lowered:
        return "siliconflow"
    if "dmxapi" in lowered or "dmxapi.cn" in lowered:
        return "dmxapi"
    if "openai" in lowered:
        return "openai"
    return "openai-compatible"


def resolve_model_env_name(api_key_env: str) -> str:
    """Derive the model environment-variable name for one key source."""

    if api_key_env == "DMX_API_KEY":
        return "DMX_API_MODEL"
    return f"{api_key_env.split('_')[0]}_MODEL"


def resolve_base_url_env_name(api_key_env: str) -> str:
    """Derive the base-URL environment-variable name for one key source."""

    if api_key_env == "DMX_API_KEY":
        return "DMX_API_BASE_URL"
    return f"{api_key_env.split('_')[0]}_BASE_URL"


def configure_llm_source(
    llm: dict,
    provider: str,
    api_key_source: str,
    *,
    api_key_env: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> None:
    """Write a complete single-provider LLM chain without storing a key."""
    provider = (provider or "siliconflow").lower()
    defaults = llm_provider_default(provider)
    api_key_env = api_key_env or defaults["api_key_env"]
    base_url = base_url or os.environ.get(resolve_base_url_env_name(api_key_env), "")
    model = model or os.environ.get(resolve_model_env_name(api_key_env), "")
    llm["provider"] = provider
    llm["api_key"] = ""
    llm["api_key_env"] = api_key_env
    llm["api_key_source"] = api_key_source
    llm["base_url"] = base_url
    llm["model"] = model
    provider_cfg = llm.setdefault("providers", {}).setdefault(provider, {})
    provider_cfg["api_key"] = ""
    provider_cfg["api_key_env"] = api_key_env
    provider_cfg["api_key_source"] = api_key_source
    provider_cfg["base_url"] = base_url
    provider_cfg["model"] = model
    llm["chain"] = [
        {
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "api_key": "",
            "api_key_source": api_key_source,
            "cost_level": llm_cost_level(provider),
            "timeout": 120,
        }
    ]


def configure_model_endpoint(
    data: dict,
    kind: str,
    *,
    base_url: str,
    model: str,
    api_key_source: str,
    api_key_env: str = "",
) -> None:
    """Write one required or optional model endpoint contract."""
    spec = model_endpoint_spec(kind)
    base_url = str(base_url or "").strip().rstrip("/")
    model = str(model or "").strip()
    provider = infer_provider_from_base_url(base_url)
    env_name = api_key_env or f"{spec['env_prefix']}_API_KEY"
    if kind == "llm":
        configure_llm_source(
            data.setdefault("llm", {}),
            provider,
            api_key_source,
            api_key_env=env_name,
            base_url=base_url,
            model=model,
        )
        return
    section = data.setdefault(str(spec["config_section"]), {})
    section["enabled"] = True
    section["provider"] = provider
    section["base_url"] = base_url
    section["model"] = model
    section["api_key"] = ""
    section["api_key_env"] = env_name
    section["api_key_source"] = api_key_source
    if kind == "embedding":
        section["embedding_model"] = model
        section["use_rerank"] = True


def resolve_key_ref(api_key: str, api_key_source: str) -> tuple[str, str]:
    """Resolve a setup-time key reference through the runtime authority."""

    from core.llm_config import _resolve_api_key

    return _resolve_api_key(api_key or "", api_key_source or "")


class DottedDictConfig:
    """Minimal dotted-key config adapter for canonical endpoint resolution."""

    def __init__(self, data: dict):
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._data:
            return self._data[key]
        value: Any = self._data
        for part in str(key).split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return default
        return value


def direct_model_config(data: dict, kind: str) -> Any:
    """Resolve one model endpoint directly from its config section."""
    spec = model_endpoint_spec(kind)
    section = data.get(str(spec["config_section"]), {})
    if not isinstance(section, dict):
        section = {}
    api_key_source = str(section.get("api_key_source", "") or "")
    api_key, source_desc = resolve_key_ref(
        str(section.get("api_key", "") or ""),
        api_key_source,
    )
    base_url = str(section.get("base_url", "") or "").strip().rstrip("/")
    model = str(section.get("model", "") or "").strip()
    if kind == "embedding" and not model:
        model = str(section.get("embedding_model", "") or "").strip()
    provider = str(section.get("provider", "") or infer_provider_from_base_url(base_url)).lower()
    configured = bool(api_key and base_url and model)
    return SimpleNamespace(
        kind=kind,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        source=source_desc if api_key else "missing",
        configured=configured,
        timeout=section.get("timeout", 120),
    )


def resolve_required_model_configs(data: dict) -> dict[str, Any]:
    """Resolve the three required setup endpoints through runtime authority."""
    from core import llm_config

    cfg = DottedDictConfig(data)
    return {
        "llm": llm_config.resolve_effective_llm_api_config(cfg),
        "embedding": llm_config.resolve_embedding_api_config(cfg),
        "reranker": llm_config.resolve_reranker_api_config(cfg),
    }


def model_cfg_ready(cfg: Any) -> bool:
    """Return whether one resolved model endpoint is dispatch-ready."""

    return bool(
        getattr(cfg, "configured", False)
        and str(getattr(cfg, "base_url", "") or "").strip()
        and str(getattr(cfg, "model", "") or "").strip()
    )


def smoke_required_model_endpoints(
    host: Any,
    data: dict,
    only: Set[str] | None = None,
) -> tuple[bool, dict[str, str]]:
    """Smoke all requested model endpoints through the patchable facade."""
    errors: dict[str, str] = {}
    configs = host._resolve_required_model_configs(data)
    targets = only or set(host._MODEL_ENDPOINT_SPECS)
    for kind in ("llm", "embedding", "reranker"):
        if kind not in targets:
            continue
        spec = host._MODEL_ENDPOINT_SPECS[kind]
        cfg = configs[kind]
        if not host._model_cfg_ready(cfg):
            errors[kind] = "缺少 model id、API 地址或 API key"
            host.print_err(f"{spec['label']} 未配置完整：{errors[kind]}")
            continue
        ok, message = host._smoke_model_endpoint(kind, cfg)
        if ok:
            host.print_ok(
                f"{spec['label']} smoke 通过: model={cfg.model}, "
                f"base_url={cfg.base_url}, source={cfg.source}"
            )
        else:
            errors[kind] = message
            host.print_err(f"{spec['label']} smoke 失败: {message}")
    return not errors, errors


def setup_llm_providers(llm: dict) -> None:
    """Fill the runtime provider catalog without embedding credentials."""
    providers = llm.setdefault("providers", {})
    for name, defaults in LLM_PROVIDER_DEFAULTS.items():
        provider = providers.setdefault(name, {})
        provider.setdefault("api_key", "")
        provider.setdefault("api_key_env", defaults["api_key_env"])
        provider.setdefault(
            "base_url",
            os.environ.get(
                f"{defaults['api_key_env'].split('_')[0]}_BASE_URL",
                defaults["base_url"],
            ),
        )
        provider.setdefault(
            "model",
            os.environ.get(
                f"{defaults['api_key_env'].split('_')[0]}_MODEL",
                defaults["model"],
            ),
        )
    llm.setdefault("api_key", "")
    llm.setdefault("api_key_env", "MNEMOS_LLM_API_KEY")
    llm.setdefault("routing_strategy", "sequential")
    llm.setdefault(
        "rate_limits",
        {
            "dmxapi": {
                "rpm": 5,
                "models": {
                    "kimi-k2.5-free": {"rpm": 5},
                    "MiniMax-M2.7-free": {"rpm": 5},
                },
            },
            "siliconflow": {"rpm": 500, "tpm": 2000000},
        },
    )
    if not llm.get("chain"):
        llm["chain"] = [
            {
                "provider": "siliconflow",
                "base_url": "https://api.siliconflow.cn/v1",
                "model": "deepseek-ai/DeepSeek-V4-Flash",
                "api_key": "",
                "api_key_source": "env:SILICONFLOW_API_KEY",
                "cost_level": "paid",
                "timeout": 120,
            },
            {
                "provider": "dmxapi",
                "base_url": "https://www.dmxapi.cn/v1",
                "model": "kimi-k2.5-free",
                "api_key": "",
                "api_key_source": "env:DMXAPI_API_KEY",
                "cost_level": "free",
                "timeout": 120,
            },
            {
                "provider": "dmxapi",
                "base_url": "https://www.dmxapi.cn/v1",
                "model": "MiniMax-M2.7-free",
                "api_key": "",
                "api_key_source": "env:DMXAPI_API_KEY",
                "cost_level": "free",
                "timeout": 120,
            },
        ]


def reset_deployment_model_defaults(data: dict) -> None:
    """Remove vendor/model defaults so fresh setup requires explicit values."""
    llm = data.setdefault("llm", {})
    if not llm.get("api_key") and not llm.get("api_key_source"):
        llm["base_url"] = ""
        llm["model"] = ""
        llm["chain"] = []
    for kind in ("embedding", "reranker"):
        section = data.setdefault(kind, {})
        if section.get("api_key") or section.get("api_key_source"):
            continue
        section["base_url"] = ""
        section["model"] = ""
        if kind == "embedding":
            section["embedding_model"] = ""


def apply_llm_env_overrides(host: Any, llm: dict) -> None:
    """Apply a complete explicit LLM environment triple when present."""
    if not (
        os.environ.get("MNEMOS_LLM_API_KEY")
        and os.environ.get("MNEMOS_LLM_BASE_URL")
        and os.environ.get("MNEMOS_LLM_MODEL")
    ):
        return
    base_url = os.environ["MNEMOS_LLM_BASE_URL"]
    provider = (
        os.environ.get("MNEMOS_LLM_PROVIDER") or host._infer_provider_from_base_url(base_url)
    ).lower()
    host._configure_llm_source(
        llm,
        provider,
        "env:MNEMOS_LLM_API_KEY",
        api_key_env="MNEMOS_LLM_API_KEY",
        base_url=base_url,
        model=os.environ["MNEMOS_LLM_MODEL"],
    )
    host.print_ok("LLM API 蒸馏已自动启用（从 MNEMOS_LLM_API_KEY 读取）")


def enable_semantic_search_from_env(
    host: Any,
    embed: dict,
    reranker: dict,
    siliconflow_key: str | None,
) -> None:
    """Apply complete embedding/reranker environment triples when present."""
    del siliconflow_key  # retained for the stable setup facade signature
    embedding_ready = bool(
        os.environ.get("MNEMOS_EMBEDDING_API_KEY")
        and os.environ.get("MNEMOS_EMBEDDING_BASE_URL")
        and os.environ.get("MNEMOS_EMBEDDING_MODEL")
    )
    reranker_ready = bool(
        os.environ.get("MNEMOS_RERANKER_API_KEY")
        and os.environ.get("MNEMOS_RERANKER_BASE_URL")
        and os.environ.get("MNEMOS_RERANKER_MODEL")
    )
    if embedding_ready and not embed.get("api_key"):
        env_name = "MNEMOS_EMBEDDING_API_KEY"
        base_url = os.environ["MNEMOS_EMBEDDING_BASE_URL"]
        model = os.environ["MNEMOS_EMBEDDING_MODEL"]
        embed["enabled"] = True
        embed["provider"] = host._infer_provider_from_base_url(base_url)
        embed["api_key"] = ""
        embed["api_key_env"] = env_name
        embed["api_key_source"] = f"env:{env_name}"
        embed["base_url"] = base_url
        embed["model"] = model
        embed["embedding_model"] = model
        host.print_ok(f"语义搜索已自动启用（从 {env_name} 读取）")
    if reranker_ready and not reranker.get("api_key"):
        env_name = "MNEMOS_RERANKER_API_KEY"
        base_url = os.environ["MNEMOS_RERANKER_BASE_URL"]
        model = os.environ["MNEMOS_RERANKER_MODEL"]
        reranker["enabled"] = True
        reranker["provider"] = host._infer_provider_from_base_url(base_url)
        reranker["api_key"] = ""
        reranker["api_key_env"] = env_name
        reranker["api_key_source"] = f"env:{env_name}"
        reranker["base_url"] = base_url
        reranker["model"] = model


def setup_semantic_search(host: Any, data: dict, yes_mode: bool) -> None:
    """Initialize semantic-search endpoint sections and apply environment input."""
    del yes_mode  # retained for the stable setup facade signature
    embed = data.setdefault("embedding", {})
    reranker = data.setdefault("reranker", {})
    embed.setdefault("enabled", True)
    embed.setdefault("api_key", "")
    embed.setdefault("api_key_env", "MNEMOS_EMBEDDING_API_KEY")
    embed.setdefault("base_url", "")
    embed.setdefault("model", "")
    embed.setdefault("embedding_model", embed.get("model", ""))
    embed.setdefault("use_rerank", True)
    reranker.setdefault("enabled", True)
    reranker.setdefault("api_key", "")
    reranker.setdefault("api_key_env", "MNEMOS_RERANKER_API_KEY")
    reranker.setdefault("base_url", "")
    reranker.setdefault("model", "")
    host._enable_semantic_search_from_env(embed, reranker, None)
