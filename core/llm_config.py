# -*- coding: utf-8 -*-
"""Unified LLM API configuration resolver.

Mnemos directly calls LLM APIs for all distillation tasks.
This helper keeps environment-variable and JSON-config resolution consistent
across CLI, MCP tools, and distillation pipelines.

Ordered failover: primary → same-provider backup → cross-provider backups.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from core.llm_key_pool import KeyPool

SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"
DMXAPI_BASE_URL = "https://www.dmxapi.cn/v1"
SILICONFLOW_MODEL = "deepseek-ai/DeepSeek-V4-Flash"
OPENAI_MODEL = "gpt-4o-mini"
DMXAPI_MODEL = "kimi-k2.5-free"
SILICONFLOW_EMBEDDING_MODEL = "BAAI/bge-m3"
SILICONFLOW_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
SILICONFLOW_MULTIMODAL_MODEL = "Qwen/Qwen2.5-VL-72B-Instruct"

# 默认 LLM 价格（每 1K tokens，单位与 distill.llm_cost_budget_per_session 一致）。
# 用户可通过 llm.provider_prices 覆盖；未命中时成本按 0 计算，预算控制不生效。
DEFAULT_PROVIDER_PRICES: Dict[str, Dict[str, Dict[str, float]]] = {
    "siliconflow": {
        "default": {"input": 0.0005, "output": 0.001},
        "deepseek-ai/DeepSeek-V4-Flash": {"input": 0.0005, "output": 0.001},
    },
    "openai": {
        "default": {"input": 0.005, "output": 0.015},
        "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
        "gpt-4o": {"input": 0.005, "output": 0.015},
    },
    "dmxapi": {
        "default": {"input": 0.0, "output": 0.0},
        "kimi-k2.5-free": {"input": 0.0, "output": 0.0},
        "MiniMax-M2.7-free": {"input": 0.0, "output": 0.0},
    },
    "anthropic": {
        "default": {"input": 0.008, "output": 0.024},
    },
    "deepseek": {
        "default": {"input": 0.0005, "output": 0.001},
    },
}

logger = logging.getLogger(__name__)


def _lookup_price(prices: Dict[str, Any], model: str) -> Optional[Dict[str, float]]:
    """按小写模型名查找价格，支持大小写不敏感匹配。"""
    for key, price in prices.items():
        if str(key).lower() == model and isinstance(price, dict):
            return {
                "input": float(price.get("input", 0.0) or 0.0),
                "output": float(price.get("output", 0.0) or 0.0),
            }
    return None


def get_provider_price(provider: str, model: str, config: Optional[Any] = None) -> Dict[str, float]:
    """返回指定 provider + model 的 input/output 单价（每 1K tokens）。

    优先级：config.llm.provider_prices > DEFAULT_PROVIDER_PRICES。
    模型名比较大小写不敏感。
    未命中时返回 {"input": 0.0, "output": 0.0}。
    """
    provider = (provider or "").lower()
    model = (model or "").lower()
    if config is None:
        try:
            from core.config import get_config as _get_config

            config = _get_config()
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            config = None
    user_prices = (_cfg_get(config, "llm.provider_prices", {}) or {}) if config is not None else {}
    user_provider = (user_prices.get(provider) or {}) if isinstance(user_prices, dict) else {}
    if isinstance(user_provider, dict):
        price = _lookup_price(user_provider, model)
        if price is not None:
            return price
        default_price = user_provider.get("default")
        if isinstance(default_price, dict):
            return {
                "input": float(default_price.get("input", 0.0) or 0.0),
                "output": float(default_price.get("output", 0.0) or 0.0),
            }

    defaults = DEFAULT_PROVIDER_PRICES.get(provider, {})
    price = _lookup_price(defaults, model)
    if price is not None:
        return price
    if "default" in defaults:
        return dict(defaults["default"])
    return {"input": 0.0, "output": 0.0}


def estimate_cost(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    config: Optional[Any] = None,
) -> float:
    """估算一次 LLM 调用的成本。"""
    price = get_provider_price(provider, model, config)
    return (prompt_tokens / 1000.0) * price["input"] + (completion_tokens / 1000.0) * price[
        "output"
    ]


@dataclass(frozen=True)
class LLMApiConfig:
    provider: str
    api_key: str
    base_url: str
    model: str
    source: str
    timeout: Optional[int] = None
    cost_level: Optional[str] = None
    pool: Optional[KeyPool] = None

    @property
    def configured(self) -> bool:
        if self.pool is not None:
            return self.pool.pick() is not None
        return bool(self.api_key)

    def active(self) -> "LLMApiConfig":
        """Return the currently active key config from the pool, or self."""
        if self.pool is None:
            return self
        picked = self.pool.pick()
        if picked is None:
            # Return self so callers see an unconfigured key and skip/fail normally.
            return self
        return LLMApiConfig(
            provider=self.provider,
            api_key=picked.api_key,
            base_url=self.base_url,
            model=self.model,
            source=picked.source,
            timeout=self.timeout,
            cost_level=self.cost_level,
        )

    def report_success(self, key: "LLMApiConfig") -> None:
        """Report a successful call for a key returned by ``active()``.

        No-op when this config has no pool.
        """
        if self.pool is not None:
            self.pool.report_success(key)

    def report_failure(self, key: "LLMApiConfig", error: str = "unknown") -> None:
        """Report a failed call for a key returned by ``active()``.

        No-op when this config has no pool.
        """
        if self.pool is not None:
            self.pool.report_failure(key, error)

    def masked_key(self) -> str:
        if not self.api_key:
            return ""
        if len(self.api_key) <= 8:
            return "***"
        return f"{self.api_key[:4]}...{self.api_key[-4:]}"


@dataclass(frozen=True)
class ModelApiConfig:
    """Generic model endpoint config for embedding/reranker style APIs."""

    kind: str
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


@dataclass(frozen=True)
class LLMApiChain:
    """LLM API 主备链配置。

    按优先级尝试：primary → same_provider_backup → cross_provider_backup
    → additional_backups。前三个字段保留旧接口兼容；额外后备节点用于保留
    ``llm.chain`` 中同属跨 provider 的多个后备模型。
    全部失败时暂停蒸馏，等待 API 恢复。
    """

    primary: LLMApiConfig
    same_provider_backup: Optional[LLMApiConfig] = None
    cross_provider_backup: Optional[LLMApiConfig] = None
    additional_backups: Tuple[LLMApiConfig, ...] = ()

    @property
    def all_configs(self) -> List[LLMApiConfig]:
        """按优先级返回所有已配置的配置项。"""
        result = [self.primary]
        if self.same_provider_backup and self.same_provider_backup.configured:
            result.append(self.same_provider_backup)
        if self.cross_provider_backup and self.cross_provider_backup.configured:
            result.append(self.cross_provider_backup)
        for cfg in self.additional_backups:
            if cfg.configured:
                result.append(cfg)
        return result

    def describe(self) -> str:
        """返回人类可读的主备链描述（用于日志/报告）。"""
        lines = [
            f"{idx}. {cfg.provider} / {cfg.model}"
            for idx, cfg in enumerate(self.all_configs, start=1)
        ]
        return " → ".join(lines)


class ProviderRateLimiter:
    """按 provider + model 限制 LLM API 调用频率。

    支持 RPM（每分钟请求数）与 TPM（每分钟 Token 数）双层限流。
    可独立配置 provider 级限制与 per-model 限制，取交集生效。

    配置格式：
        llm.rate_limits = {
            "siliconflow": {"rpm": 500, "tpm": 2000000},
            "dmxapi": {
                "rpm": 10,
                "models": {
                    "kimi-k2.5-free": {"rpm": 5},
                    "MiniMax-M2.7-free": {"rpm": 5},
                },
            },
        }
    在调用前执行 acquire()，如果当前窗口内调用次数或 Token 数已满，
    则 sleep 到下一个可用时间点。
    """

    # provider -> 每分钟最大调用次数
    # dmxapi 免费模型共享 5 RPM 平台限制，per-model 限制用于模型间路由。
    DEFAULT_CALLS_PER_MINUTE: Dict[str, float] = {
        "dmxapi": 5.0,
    }
    WINDOW_SECONDS = 60.0

    def __init__(self, config: Optional[Any] = None):
        self._lock = threading.Lock()
        # provider 级记录
        self._timestamps: Dict[str, List[float]] = defaultdict(list)
        self._token_records: Dict[str, List[Tuple[float, int]]] = defaultdict(list)
        # model 级记录，key = (provider, model)
        self._model_timestamps: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        self._model_token_records: Dict[Tuple[str, str], List[Tuple[float, int]]] = defaultdict(list)

        # provider 级限制
        self._rpm_limits: Dict[str, float] = dict(self.DEFAULT_CALLS_PER_MINUTE)
        self._tpm_limits: Dict[str, int] = {}
        # model 级限制，key = (provider, model)
        self._model_rpm_limits: Dict[Tuple[str, str], float] = {}
        self._model_tpm_limits: Dict[Tuple[str, str], int] = {}

        if config is not None:
            try:
                rate_limits = config.get("llm.rate_limits") or {}
                if isinstance(rate_limits, dict):
                    for provider, value in rate_limits.items():
                        provider = str(provider).lower()
                        if not isinstance(value, dict):
                            continue
                        rpm = value.get("rpm")
                        tpm = value.get("tpm")
                        if isinstance(rpm, (int, float)) and rpm > 0:
                            self._rpm_limits[provider] = float(rpm)
                        if isinstance(tpm, (int, float)) and tpm > 0:
                            self._tpm_limits[provider] = int(tpm)

                        # per-model 限制
                        models = value.get("models")
                        if isinstance(models, dict):
                            for model, model_value in models.items():
                                model_key = (provider, str(model).lower())
                                if not isinstance(model_value, dict):
                                    continue
                                model_rpm = model_value.get("rpm")
                                model_tpm = model_value.get("tpm")
                                if isinstance(model_rpm, (int, float)) and model_rpm > 0:
                                    self._model_rpm_limits[model_key] = float(model_rpm)
                                if isinstance(model_tpm, (int, float)) and model_tpm > 0:
                                    self._model_tpm_limits[model_key] = int(model_tpm)
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                logger.debug("[ProviderRateLimiter] 读取配置失败，使用默认限制", exc_info=True)

    def _clean_window(self, provider: str, now: float):
        """清理并返回 provider 级当前滑动窗口内的记录。"""
        window_start = now - self.WINDOW_SECONDS
        timestamps = [t for t in self._timestamps[provider] if t > window_start]
        self._timestamps[provider] = timestamps
        token_records = [r for r in self._token_records[provider] if r[0] > window_start]
        self._token_records[provider] = token_records
        return timestamps, token_records

    def _clean_model_window(self, model_key: Tuple[str, str], now: float):
        """清理并返回 model 级当前滑动窗口内的记录。"""
        window_start = now - self.WINDOW_SECONDS
        timestamps = [t for t in self._model_timestamps[model_key] if t > window_start]
        self._model_timestamps[model_key] = timestamps
        token_records = [r for r in self._model_token_records[model_key] if r[0] > window_start]
        self._model_token_records[model_key] = token_records
        return timestamps, token_records

    def _wait_until(self, deadline: float) -> float:
        """阻塞等待直到 deadline，返回等待后的当前时间。"""
        now = time.monotonic()
        sleep_seconds = deadline - now
        if sleep_seconds > 0:
            # 先释放锁再 sleep，避免阻塞其它 provider
            self._lock.release()
            try:
                time.sleep(sleep_seconds)
            finally:
                self._lock.acquire()
        return time.monotonic()

    def _check_and_wait_rpm(
        self,
        timestamps: List[float],
        rpm_limit: Optional[float],
        label: str,
    ) -> float:
        """若 RPM 超限则等待最旧记录滑出；返回当前时间。"""
        now = time.monotonic()
        if rpm_limit and rpm_limit > 0 and len(timestamps) >= rpm_limit:
            deadline = timestamps[0] + self.WINDOW_SECONDS
            logger.info(
                "[ProviderRateLimiter] %s 已达 %d 次/分钟限制，等待 %.1fs",
                label,
                int(rpm_limit),
                max(0.0, deadline - now),
            )
            now = self._wait_until(deadline)
        return now

    def _check_and_wait_tpm(
        self,
        token_records: List[Tuple[float, int]],
        tpm_limit: Optional[int],
        estimated_tokens: int,
        label: str,
    ) -> float:
        """若 TPM 超限则等待最旧 Token 记录滑出；返回当前时间。"""
        now = time.monotonic()
        if (
            tpm_limit
            and tpm_limit > 0
            and estimated_tokens
            and token_records
        ):
            current_tpm = sum(tokens for _, tokens in token_records)
            if current_tpm + estimated_tokens > tpm_limit:
                deadline = token_records[0][0] + self.WINDOW_SECONDS
                logger.info(
                    "[ProviderRateLimiter] %s 已达 %d tokens/分钟限制，等待 %.1fs",
                    label,
                    int(tpm_limit),
                    max(0.0, deadline - now),
                )
                now = self._wait_until(deadline)
        return now

    def _get_effective_limits(
        self,
        provider: str,
        model_key: Optional[Tuple[str, str]],
    ) -> tuple[Optional[float], Optional[int], Optional[float], Optional[int]]:
        """返回 provider/model 的有效 (rpm_limit, tpm_limit, model_rpm_limit, model_tpm_limit)。"""
        rpm_limit = self._rpm_limits.get(provider)
        tpm_limit = self._tpm_limits.get(provider)
        model_rpm_limit = self._model_rpm_limits.get(model_key) if model_key else None
        model_tpm_limit = self._model_tpm_limits.get(model_key) if model_key else None
        return rpm_limit, tpm_limit, model_rpm_limit, model_tpm_limit

    @staticmethod
    def _would_exceed_limit(
        timestamps: List[float],
        token_records: List[Tuple[float, int]],
        rpm_limit: Optional[float],
        tpm_limit: Optional[int],
        estimated_tokens: int,
    ) -> bool:
        """共享滑动窗口检查：任一限制超限返回 True。"""
        if rpm_limit and len(timestamps) >= rpm_limit:
            return True
        if (
            tpm_limit
            and tpm_limit > 0
            and estimated_tokens
            and token_records
            and sum(tokens for _, tokens in token_records) + estimated_tokens > tpm_limit
        ):
            return True
        return False

    def can_acquire(
        self,
        provider: str,
        model: Optional[str] = None,
        estimated_tokens: int = 0,
    ) -> bool:
        """非阻塞检查当前是否可获取调用许可。

        Args:
            provider: 服务提供商名称。
            model: 模型名称；None 时只检查 provider 级限制。
            estimated_tokens: 本次请求预估 Token 数。
        """
        provider = str(provider or "").lower()
        model_key: Optional[Tuple[str, str]] = None
        if model:
            model_key = (provider, str(model).lower())

        rpm_limit, tpm_limit, model_rpm_limit, model_tpm_limit = self._get_effective_limits(
            provider, model_key
        )

        if not any([rpm_limit, tpm_limit, model_rpm_limit, model_tpm_limit]):
            return True

        estimated_tokens = max(0, int(estimated_tokens or 0))

        with self._lock:
            now = time.monotonic()
            timestamps, token_records = self._clean_window(provider, now)
            if self._would_exceed_limit(
                timestamps, token_records, rpm_limit, tpm_limit, estimated_tokens
            ):
                return False

            if model_key:
                model_timestamps, model_token_records = self._clean_model_window(model_key, now)
                if self._would_exceed_limit(
                    model_timestamps,
                    model_token_records,
                    model_rpm_limit,
                    model_tpm_limit,
                    estimated_tokens,
                ):
                    return False

            return True

    def acquire(
        self,
        provider: str,
        model: Optional[str] = None,
        estimated_tokens: int = 0,
    ):
        """获取一次调用许可，必要时阻塞等待。

        Args:
            provider: 服务提供商名称。
            model: 模型名称；None 时只检查 provider 级限制。
            estimated_tokens: 本次请求预估 Token 数。大于 0 时触发 TPM 检查。
        """
        provider = str(provider or "").lower()
        model_key: Optional[Tuple[str, str]] = None
        if model:
            model_key = (provider, str(model).lower())

        rpm_limit = self._rpm_limits.get(provider)
        tpm_limit = self._tpm_limits.get(provider)
        model_rpm_limit = self._model_rpm_limits.get(model_key) if model_key else None
        model_tpm_limit = self._model_tpm_limits.get(model_key) if model_key else None

        if not any([rpm_limit, tpm_limit, model_rpm_limit, model_tpm_limit]):
            return

        estimated_tokens = max(0, int(estimated_tokens or 0))

        with self._lock:
            now = time.monotonic()
            timestamps, token_records = self._clean_window(provider, now)

            # Provider 级 RPM/TPM 等待
            now = self._check_and_wait_rpm(timestamps, rpm_limit, provider)
            timestamps, token_records = self._clean_window(provider, now)
            now = self._check_and_wait_tpm(token_records, tpm_limit, estimated_tokens, provider)
            timestamps, token_records = self._clean_window(provider, now)

            # Model 级 RPM/TPM 等待
            if model_key:
                model_timestamps, model_token_records = self._clean_model_window(model_key, now)
                now = self._check_and_wait_rpm(
                    model_timestamps, model_rpm_limit, f"{provider}/{model}"
                )
                model_timestamps, model_token_records = self._clean_model_window(model_key, now)
                now = self._check_and_wait_tpm(
                    model_token_records,
                    model_tpm_limit,
                    estimated_tokens,
                    f"{provider}/{model}",
                )
                model_timestamps, model_token_records = self._clean_model_window(model_key, now)

            now = time.monotonic()
            timestamps.append(now)
            self._timestamps[provider] = timestamps
            if model_key:
                model_timestamps.append(now)
                self._model_timestamps[model_key] = model_timestamps
            if estimated_tokens:
                self._token_records[provider].append((now, estimated_tokens))
                if model_key:
                    self._model_token_records[model_key].append((now, estimated_tokens))


_MISSING = object()


def _cfg_get(config: Any, key: str, default: Any = _MISSING) -> Any:
    """Read canonical Config values without masking registry violations.

    Plain mappings remain supported for isolated callers/tests.  The runtime
    Config object owns defaults and validation, so its ``get`` method is called
    without a caller-selected fallback.
    """
    if config is None:
        return None if default is _MISSING else default
    if isinstance(config, Mapping):
        if default is _MISSING:
            return config.get(key)
        return config.get(key, default)
    return config.get(key)


def _rstrip_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _cfg_string(config: Any, key: str, default: str = "") -> str:
    value = _cfg_get(config, key, default)
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return default


def _env_first(*names: str) -> tuple[str, str]:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value, f"env:{name}"
    return "", ""


def _provider_defaults(provider: str) -> tuple[str, str]:
    """返回给定 provider 的默认 base_url 和 model。"""
    provider = (provider or "").lower()
    defaults = {
        "siliconflow": (SILICONFLOW_BASE_URL, SILICONFLOW_MODEL),
        "openai": (OPENAI_BASE_URL, OPENAI_MODEL),
        "dmxapi": (DMXAPI_BASE_URL, DMXAPI_MODEL),
        "anthropic": ("https://api.anthropic.com/v1", "claude-sonnet-4-6"),
        "google": ("https://generativelanguage.googleapis.com/v1", "gemini-2.5-pro"),
        "azure": ("https://api.azure.com/v1", "gpt-4o"),
        "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
        "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b"),
        "together": ("https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
        "openrouter": ("https://openrouter.ai/api/v1", "openai/gpt-4o"),
        "xai": ("https://api.x.ai/v1", "grok-2"),
        "cohere": ("https://api.cohere.com/v1", "command-r-plus"),
        "mistral": ("https://api.mistral.ai/v1", "mistral-large-latest"),
    }
    return defaults.get(provider, (OPENAI_BASE_URL, OPENAI_MODEL))


def _is_default_global_llm_chain(chain_cfg: List[Any]) -> bool:
    """Return True for the built-in provider-env fallback chain.

    Setup/user input is stored in top-level ``llm.*`` first.  Older and default
    configs may still carry the built-in SiliconFlow/DMX chain; that chain is a
    global fallback and must not mask a complete user-entered top-level config.
    Custom chains still keep their explicit priority.
    """
    expected = (
        ("siliconflow", SILICONFLOW_BASE_URL, SILICONFLOW_MODEL, "env:SILICONFLOW_API_KEY"),
        ("dmxapi", DMXAPI_BASE_URL, DMXAPI_MODEL, "env:DMXAPI_API_KEY"),
        ("dmxapi", DMXAPI_BASE_URL, "MiniMax-M2.7-free", "env:DMXAPI_API_KEY"),
    )
    if len(chain_cfg) != len(expected):
        return False
    for item, (provider, base_url, model, key_source) in zip(chain_cfg, expected):
        if not isinstance(item, dict):
            return False
        if str(item.get("provider", "") or "").lower() != provider:
            return False
        if _rstrip_url(item.get("base_url")) != base_url:
            return False
        if str(item.get("model", "") or "") != model:
            return False
        if str(item.get("api_key_source", "") or "") != key_source:
            return False
        if item.get("api_key_sources"):
            return False
    return True


def _provider_from_base_url(base_url: str, fallback: str = "openai") -> str:
    url = (base_url or "").lower()
    patterns = [
        ("siliconflow", "siliconflow"),
        ("dmxapi", "dmxapi"),
        ("dmxapi.cn", "dmxapi"),
        ("anthropic", "anthropic"),
        ("googleapis", "google"),
        ("aistudio.google", "google"),
        ("azure", "azure"),
        ("deepseek", "deepseek"),
        ("groq", "groq"),
        ("together", "together"),
        ("openrouter", "openrouter"),
        ("x.ai", "xai"),
        ("cohere", "cohere"),
        ("mistral", "mistral"),
    ]
    for keyword, provider in patterns:
        if keyword in url:
            return provider
    return fallback or "openai"


def _resolve_mnemos_llm_env() -> Optional[LLMApiConfig]:
    """Priority 1: dedicated ``MNEMOS_LLM_*`` environment variables."""
    key = os.environ.get("MNEMOS_LLM_API_KEY") or ""
    provider = str(os.environ.get("MNEMOS_LLM_PROVIDER") or "").lower()
    base_url = os.environ.get("MNEMOS_LLM_BASE_URL")
    if not (key or provider or base_url):
        return None
    provider = provider or "siliconflow"
    base_default, model_default = _provider_defaults(provider)
    return LLMApiConfig(
        provider,
        key,
        _rstrip_url(base_url or base_default),
        os.environ.get("MNEMOS_LLM_MODEL") or model_default,
        "env:MNEMOS_LLM_API_KEY" if key else "missing",
    )


def _resolve_siliconflow_env_config(key: str) -> LLMApiConfig:
    return LLMApiConfig(
        "siliconflow",
        key,
        os.environ.get("SILICONFLOW_BASE_URL", SILICONFLOW_BASE_URL).rstrip("/"),
        os.environ.get("SILICONFLOW_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or SILICONFLOW_MODEL,
        "env:SILICONFLOW_API_KEY",
    )


def _resolve_dmxapi_env_config(key: str, source: str) -> LLMApiConfig:
    return LLMApiConfig(
        "dmxapi",
        key,
        _rstrip_url(
            os.environ.get("DMXAPI_BASE_URL")
            or os.environ.get("DMX_API_BASE_URL")
            or DMXAPI_BASE_URL
        ),
        os.environ.get("DMXAPI_MODEL") or os.environ.get("DMX_API_MODEL") or DMXAPI_MODEL,
        source,
    )


def _resolve_openai_env_config(key: str) -> LLMApiConfig:
    return LLMApiConfig(
        "openai",
        key,
        OPENAI_BASE_URL,
        os.environ.get("OPENAI_MODEL") or OPENAI_MODEL,
        "env:OPENAI_API_KEY",
    )


def _resolve_from_openai_base_url(
    env_base: str,
    sf_key: str,
    dmx_key: str,
    dmx_source: str,
    openai_key: str,
) -> LLMApiConfig:
    """``OPENAI_BASE_URL`` 存在时根据 URL 推断 provider。"""
    provider = _provider_from_base_url(env_base, fallback="openai")
    if provider == "siliconflow":
        api_key = sf_key or openai_key
        model = os.environ.get("OPENAI_MODEL") or SILICONFLOW_MODEL
        source = "env:SILICONFLOW_API_KEY" if sf_key else "env:OPENAI_API_KEY"
    elif provider == "dmxapi":
        api_key = dmx_key or openai_key or sf_key
        model = (
            os.environ.get("OPENAI_MODEL")
            or os.environ.get("DMXAPI_MODEL")
            or DMXAPI_MODEL
        )
        source = dmx_source or (
            "env:OPENAI_API_KEY" if openai_key else "env:SILICONFLOW_API_KEY"
        )
    else:
        api_key = openai_key or sf_key
        model = os.environ.get("OPENAI_MODEL") or OPENAI_MODEL
        source = "env:OPENAI_API_KEY" if openai_key else "env:SILICONFLOW_API_KEY"
    return LLMApiConfig(provider, api_key, env_base, model, source if api_key else "missing")


def _resolve_provider_env() -> Optional[LLMApiConfig]:
    """Priority 2: provider-specific environment variables."""
    env_base = _rstrip_url(os.environ.get("OPENAI_BASE_URL"))
    sf_key = os.environ.get("SILICONFLOW_API_KEY") or ""
    dmx_key, dmx_source = _env_first("DMXAPI_API_KEY", "DMX_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY") or ""

    if env_base:
        return _resolve_from_openai_base_url(env_base, sf_key, dmx_key, dmx_source, openai_key)
    if sf_key:
        return _resolve_siliconflow_env_config(sf_key)
    if dmx_key:
        return _resolve_dmxapi_env_config(dmx_key, dmx_source)
    if openai_key:
        return _resolve_openai_env_config(openai_key)
    return None


def _load_config(config: Optional[Any]) -> Any:
    """Return config object or None when lazy import fails."""
    if config is not None:
        return config
    try:
        from core.config import get_config

        return get_config()
    except ImportError:
        return None


def _resolve_provider_config(
    config: Any, provider: str, base_default: str, model_default: str
) -> Optional[LLMApiConfig]:
    """Provider-scoped ``llm.providers.<provider>`` configuration."""
    providers = _cfg_get(config, "llm.providers", {}) or {}
    provider_cfg = providers.get(provider, {}) if isinstance(providers, dict) else {}
    if not isinstance(provider_cfg, dict):
        return None

    base_url = _rstrip_url(provider_cfg.get("base_url") or base_default)
    model = provider_cfg.get("model") or model_default

    sources = provider_cfg.get("api_key_sources") or []
    if isinstance(sources, (list, tuple)) and sources:
        pool = _build_key_pool(
            provider=provider,
            base_url=base_url,
            model=model,
            sources=[str(s) for s in sources],
            strategy=str(provider_cfg.get("key_strategy", "weighted")),
        )
        if pool is None:
            return None
        return LLMApiConfig(
            provider=provider,
            api_key="",
            base_url=base_url,
            model=model,
            source="pool",
            pool=pool,
        )

    key_source = provider_cfg.get("api_key_source") or ""
    key_env = provider_cfg.get("api_key_env") or ""
    if key_env and not key_source:
        key_source = f"env:{key_env}"
    api_key, source_desc = _resolve_api_key(provider_cfg.get("api_key") or "", key_source)
    if not api_key:
        return None

    source = (
        source_desc
        if source_desc.startswith("env:")
        else f"config:llm.providers.{provider}.api_key"
    )
    return LLMApiConfig(provider, api_key, base_url, model, source)


def _resolve_top_level_llm_config(
    config: Any, provider: str, base_default: str, model_default: str
) -> Optional[LLMApiConfig]:
    """User-entered top-level ``llm.*`` fields."""
    base_url = _rstrip_url(_cfg_string(config, "llm.base_url", base_default) or base_default)
    model = _cfg_string(config, "llm.model", model_default) or model_default

    sources = _cfg_get(config, "llm.api_key_sources") or []
    if isinstance(sources, (list, tuple)) and sources:
        pool = _build_key_pool(
            provider=provider,
            base_url=base_url,
            model=model,
            sources=[str(s) for s in sources],
            strategy=str(_cfg_get(config, "llm.key_strategy") or "weighted"),
        )
        if pool is None:
            return None
        return LLMApiConfig(
            provider=provider,
            api_key="",
            base_url=base_url,
            model=model,
            source="pool",
            pool=pool,
        )

    key_source = _cfg_string(config, "llm.api_key_source", "")
    key_env = _cfg_string(config, "llm.api_key_env", "")
    if key_env and not key_source:
        key_source = f"env:{key_env}"
    api_key, source_desc = _resolve_api_key(
        _cfg_string(config, "llm.api_key", ""), key_source
    )
    if not api_key:
        return None

    source = source_desc if source_desc.startswith("env:") else "config:llm.api_key"
    return LLMApiConfig(provider, api_key, base_url, model, source)


def resolve_llm_api_config(config: Optional[Any] = None) -> LLMApiConfig:
    """Resolve LLM API config with a stable priority order.

    Priority:
    1. Dedicated ``MNEMOS_LLM_*`` environment variables.
    2. User-entered top-level ``llm.*`` fields in main.json.
    3. ``llm.providers.<provider>`` in main.json.
    4. Provider environment variables (SiliconFlow / DMXAPI / OpenAI).
    """
    cfg = _resolve_mnemos_llm_env()
    if cfg is not None:
        return cfg

    config = _load_config(config)
    provider = (_cfg_string(config, "llm.provider", "siliconflow") or "siliconflow").lower()
    base_default, model_default = _provider_defaults(provider)

    cfg = _resolve_top_level_llm_config(config, provider, base_default, model_default)
    if cfg is not None:
        return cfg

    cfg = _resolve_provider_config(config, provider, base_default, model_default)
    if cfg is not None:
        return cfg

    cfg = _resolve_provider_env()
    if cfg is not None:
        return cfg

    return LLMApiConfig(provider, "", base_default, model_default, "missing")


def _has_dedicated_env_vars(dedicated_prefix: str) -> bool:
    return bool(
        os.environ.get(f"MNEMOS_{dedicated_prefix}_API_KEY")
        or os.environ.get(f"MNEMOS_{dedicated_prefix}_PROVIDER")
        or os.environ.get(f"MNEMOS_{dedicated_prefix}_BASE_URL")
        or os.environ.get(f"MNEMOS_{dedicated_prefix}_MODEL")
    )


def _resolve_dedicated_env_config(
    kind: str,
    dedicated_prefix: str,
    default_provider: str,
    default_model: str,
) -> ModelApiConfig | None:
    if not _has_dedicated_env_vars(dedicated_prefix):
        return None

    env_key = os.environ.get(f"MNEMOS_{dedicated_prefix}_API_KEY") or ""
    env_provider = str(os.environ.get(f"MNEMOS_{dedicated_prefix}_PROVIDER") or "").lower()
    provider = env_provider or default_provider
    base_default, _ = _provider_defaults(provider)
    return ModelApiConfig(
        kind,
        provider,
        env_key,
        _rstrip_url(os.environ.get(f"MNEMOS_{dedicated_prefix}_BASE_URL") or base_default),
        os.environ.get(f"MNEMOS_{dedicated_prefix}_MODEL") or default_model,
        f"env:MNEMOS_{dedicated_prefix}_API_KEY" if env_key else "missing",
    )


def _resolve_siliconflow_model_config(
    kind: str,
    dedicated_prefix: str,
    default_model: str,
    default_provider: str,
) -> ModelApiConfig | None:
    sf_key = os.environ.get("SILICONFLOW_API_KEY") or ""
    if not sf_key or default_provider != "siliconflow":
        return None
    model_env = os.environ.get(f"SILICONFLOW_{dedicated_prefix}_MODEL") or ""
    return ModelApiConfig(
        kind,
        "siliconflow",
        sf_key,
        _rstrip_url(os.environ.get("SILICONFLOW_BASE_URL") or SILICONFLOW_BASE_URL),
        model_env or default_model,
        "env:SILICONFLOW_API_KEY",
    )


def _resolve_model_from_config(
    config: Any,
    config_section: str,
    model_keys: tuple[str, ...],
) -> str:
    model = ""
    for key in model_keys:
        model = _cfg_string(config, f"{config_section}.{key}", "").strip()
        if model:
            break
    return model


def _resolve_config_model_api_config(
    config: Any,
    kind: str,
    config_section: str,
    default_provider: str,
    default_model: str,
    model_keys: tuple[str, ...],
) -> ModelApiConfig:
    provider = (
        _cfg_string(config, f"{config_section}.provider", default_provider) or default_provider
    ).lower()
    base_default, _ = _provider_defaults(provider)
    api_key_source = _cfg_string(config, f"{config_section}.api_key_source", "")
    api_key_env = _cfg_string(config, f"{config_section}.api_key_env", "")
    if api_key_env and not api_key_source:
        api_key_source = f"env:{api_key_env}"
    api_key, source_desc = _resolve_api_key(
        _cfg_string(config, f"{config_section}.api_key", ""),
        api_key_source,
    )
    model = _resolve_model_from_config(config, config_section, model_keys)

    return ModelApiConfig(
        kind,
        provider,
        api_key,
        _rstrip_url(_cfg_string(config, f"{config_section}.base_url", base_default) or base_default),
        model or default_model,
        source_desc if api_key else "missing",
    )


def _resolve_model_api_config(
    *,
    kind: str,
    config_section: str,
    dedicated_prefix: str,
    default_provider: str,
    default_model: str,
    model_keys: tuple[str, ...],
    config: Optional[Any] = None,
) -> ModelApiConfig:
    """Resolve embedding/reranker endpoint config without cross-kind key reuse."""

    cfg = _resolve_dedicated_env_config(kind, dedicated_prefix, default_provider, default_model)
    if cfg is not None:
        return cfg

    if config is None:
        try:
            from core.config import get_config

            config = get_config()
        except ImportError:
            config = None

    config_cfg = _resolve_config_model_api_config(
        config, kind, config_section, default_provider, default_model, model_keys
    )
    if config_cfg.configured:
        return config_cfg

    cfg = _resolve_siliconflow_model_config(kind, dedicated_prefix, default_model, default_provider)
    if cfg is not None:
        return cfg

    return config_cfg


def resolve_embedding_api_config(config: Optional[Any] = None) -> ModelApiConfig:
    """Resolve configurable embedding endpoint settings."""

    return _resolve_model_api_config(
        kind="embedding",
        config_section="embedding",
        dedicated_prefix="EMBEDDING",
        default_provider="siliconflow",
        default_model=SILICONFLOW_EMBEDDING_MODEL,
        model_keys=("model", "embedding_model"),
        config=config,
    )


def resolve_reranker_api_config(config: Optional[Any] = None) -> ModelApiConfig:
    """Resolve configurable reranker endpoint settings."""

    return _resolve_model_api_config(
        kind="reranker",
        config_section="reranker",
        dedicated_prefix="RERANKER",
        default_provider="siliconflow",
        default_model=SILICONFLOW_RERANKER_MODEL,
        model_keys=("model",),
        config=config,
    )


def resolve_multimodal_api_config(config: Optional[Any] = None) -> ModelApiConfig:
    """Resolve optional multimodal/vision endpoint settings.

    Unlike LLM/embedding/reranker, this endpoint is optional: absence or
    ``multimodal.enabled=false`` returns an unconfigured config without falling
    back to a global provider key. Dedicated ``MNEMOS_MULTIMODAL_*`` environment
    variables always opt in.
    """

    cfg = _resolve_dedicated_env_config(
        "multimodal",
        "MULTIMODAL",
        "siliconflow",
        SILICONFLOW_MULTIMODAL_MODEL,
    )
    if cfg is not None:
        return cfg

    config = _load_config(config)
    enabled_value = _cfg_get(config, "multimodal.enabled", False)
    enabled = enabled_value is True or str(enabled_value).lower() in {"1", "true", "yes", "on"}
    config_cfg = _resolve_config_model_api_config(
        config,
        "multimodal",
        "multimodal",
        "siliconflow",
        SILICONFLOW_MULTIMODAL_MODEL,
        ("model", "vision_model"),
    )
    if enabled and config_cfg.configured:
        return config_cfg
    if enabled:
        sf_cfg = _resolve_siliconflow_model_config(
            "multimodal",
            "MULTIMODAL",
            SILICONFLOW_MULTIMODAL_MODEL,
            "siliconflow",
        )
        if sf_cfg is not None:
            return sf_cfg
    return ModelApiConfig(
        "multimodal",
        config_cfg.provider,
        "",
        config_cfg.base_url,
        config_cfg.model,
        "missing",
    )


def _resolve_api_key(api_key: str, api_key_source: str) -> tuple[str, str]:
    """解析 API key 及其来源。

    - api_key_source 以 "env:" 开头时，从对应环境变量读取。
    - api_key_source 以 "keyring:" 开头时，从系统密钥环读取。
    - 明文 api_key / 直接 api_key_source 不再作为真实 key 使用。

    Returns:
        (resolved_key, source_description)
    """
    src = (api_key_source or "").strip()
    if src.startswith("env:"):
        env_var = src[4:].strip()
        val = os.environ.get(env_var, "")
        if not val and env_var == "DMXAPI_API_KEY":
            val = os.environ.get("DMX_API_KEY", "")
            if val:
                return val, "env:DMX_API_KEY"
        return val, f"env:{env_var}"
    if src.startswith("keyring:"):
        ref = src[8:].strip()
        try:
            import keyring  # type: ignore

            return keyring.get_password("mnemos.llm", ref) or "", f"keyring:{ref}"
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.warning("[LLM Config] keyring source unavailable: %s", ref, exc_info=True)
            return "", f"keyring:{ref}"
    if src:
        logger.warning("[LLM Config] Refusing plaintext api_key_source; use env:VAR or keyring:REF")
        return "", "invalid:plaintext_api_key_source"
    if api_key:
        logger.warning("[LLM Config] Refusing plaintext api_key; use api_key_env/api_key_source")
        return "", "invalid:plaintext_api_key"
    return "", "missing"


def _build_key_pool(
    provider: str,
    base_url: str,
    model: str,
    sources: List[str],
    timeout: Optional[int] = None,
    cost_level: Optional[str] = None,
    strategy: str = "weighted",
) -> Optional[KeyPool]:
    """Build a KeyPool from a list of api_key_source strings.

    Only sources that resolve to a non-empty key are included. Returns None if
    no key is available, preserving the existing "unconfigured" behavior.
    """
    keys: List[LLMApiConfig] = []
    for src in sources:
        api_key, source_desc = _resolve_api_key("", src)
        if not api_key:
            continue
        keys.append(
            LLMApiConfig(
                provider=provider,
                api_key=api_key,
                base_url=base_url,
                model=model,
                source=source_desc,
                timeout=timeout,
                cost_level=cost_level,
            )
        )
    if not keys:
        return None
    return KeyPool(keys, strategy=strategy)


def _load_chain_config(config: Optional[Any]) -> List[Any]:
    """加载 ``llm.chain`` 并归一化空/非列表情况。"""
    chain_cfg = _cfg_get(config, "llm.chain")
    if isinstance(chain_cfg, list):
        return chain_cfg
    return []


def _parse_chain_node(item: Any) -> Optional[LLMApiConfig]:
    """解析单个 chain 节点；不完整时记录日志并返回 None。"""
    if not isinstance(item, dict):
        return None
    provider = str(item.get("provider", "") or "").strip()
    base_url = str(item.get("base_url", "") or "").strip().rstrip("/")
    model = str(item.get("model", "") or "").strip()
    raw_key = str(item.get("api_key", "") or "")
    key_source = str(item.get("api_key_source", "") or "")
    key_sources = item.get("api_key_sources") or []

    if not provider or not base_url or not model:
        logger.warning(
            "[LLM Chain] 跳过不完整配置: provider=%s, base_url=%s, model=%s",
            provider,
            base_url,
            model,
        )
        return None

    timeout = item.get("timeout")
    if timeout is not None:
        try:
            timeout = int(timeout)
            if timeout <= 0:
                timeout = None
        except (TypeError, ValueError):
            timeout = None

    cost_level = str(item.get("cost_level") or "").strip() or None

    if isinstance(key_sources, (list, tuple)) and key_sources:
        pool = _build_key_pool(
            provider=provider,
            base_url=base_url,
            model=model,
            sources=[str(s) for s in key_sources],
            timeout=timeout,
            cost_level=cost_level,
            strategy=str(item.get("key_strategy", "weighted")),
        )
        if pool is None:
            return None
        return LLMApiConfig(
            provider=provider,
            api_key="",
            base_url=base_url,
            model=model,
            source="pool",
            timeout=timeout,
            cost_level=cost_level,
            pool=pool,
        )

    resolved_key, source_desc = _resolve_api_key(raw_key, key_source)
    return LLMApiConfig(
        provider, resolved_key, base_url, model, source_desc, timeout, cost_level
    )


def _build_llm_api_chain(configs: List[LLMApiConfig]) -> LLMApiChain:
    """按列表顺序分配 primary / same-provider backup / cross-provider backups。"""
    primary = configs[0]
    same_provider: Optional[LLMApiConfig] = None
    cross_provider: Optional[LLMApiConfig] = None
    additional_backups: List[LLMApiConfig] = []

    for cfg in configs[1:]:
        if cfg.provider == primary.provider and same_provider is None:
            same_provider = cfg
        elif cross_provider is None:
            cross_provider = cfg
        else:
            additional_backups.append(cfg)

    chain = LLMApiChain(
        primary=primary,
        same_provider_backup=same_provider,
        cross_provider_backup=cross_provider,
        additional_backups=tuple(additional_backups),
    )
    logger.info("LLM API chain from config: %s", chain.describe())
    return chain


def resolve_llm_api_chain(config: Optional[Any] = None) -> LLMApiChain:
    """解析 LLM API 主备链。

    从配置文件的 ``llm.chain`` 读取主备链定义，每个节点通过
    ``api_key_source`` 解析真实 key（支持 ``env:VAR_NAME`` 或 ``keyring:REF``）。

    若 ``llm.chain`` 未配置或为空，回退到 ``resolve_llm_api_config()``
    返回的单一 provider 配置。
    """
    if config is None:
        try:
            from core.config import get_config

            config = get_config()
        except ImportError:
            config = None

    chain_cfg = _load_chain_config(config)
    if chain_cfg:
        if _is_default_global_llm_chain(chain_cfg):
            provider = (
                _cfg_string(config, "llm.provider", "siliconflow") or "siliconflow"
            ).lower()
            base_default, model_default = _provider_defaults(provider)
            top_level_cfg = _resolve_top_level_llm_config(
                config, provider, base_default, model_default
            )
            if top_level_cfg is not None:
                logger.info(
                    "LLM API chain uses user-entered top-level config over default global chain: %s / %s",
                    top_level_cfg.provider,
                    top_level_cfg.model,
                )
                return LLMApiChain(primary=top_level_cfg)

        configs = []
        for item in chain_cfg:
            cfg = _parse_chain_node(item)
            if cfg is not None:
                configs.append(cfg)

        configured_configs = [cfg for cfg in configs if cfg.configured]
        if configured_configs:
            if len(configured_configs) != len(configs):
                logger.warning("[LLM Chain] 已跳过未配置 API key 的 chain 节点")
            return _build_llm_api_chain(configured_configs)
        if configs:
            logger.warning("[LLM Chain] 所有 llm.chain 节点均未配置 API key，回退到单一配置")
        logger.warning("[LLM Chain] llm.chain 配置为空或无效")

    # 回退到标准单一配置
    default = resolve_llm_api_config(config)
    logger.info(
        "LLM API chain fallback to single provider: %s / %s", default.provider, default.model
    )
    return LLMApiChain(primary=default)


def resolve_effective_llm_api_config(config: Optional[Any] = None) -> LLMApiConfig:
    """Resolve the LLM endpoint that runtime code will actually try first.

    Runtime LLM callers use ``resolve_llm_api_chain()`` so user-facing status
    checks must report that same primary endpoint instead of the older
    single-provider resolver. If the primary node is a key pool, return the
    currently active concrete key so smoke tests can call it directly.
    """
    return resolve_llm_api_chain(config).primary.active()
