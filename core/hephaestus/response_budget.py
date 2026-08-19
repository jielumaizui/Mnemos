# -*- coding: utf-8 -*-
"""Dynamic response token budgets for distillation LLM calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_RESPONSE_TOKENS = 6000
DEFAULT_RESPONSE_TOKENS_MEDIUM = 8000
DEFAULT_RESPONSE_TOKENS_LONG = 12000
DEFAULT_RESPONSE_TOKENS_RETRY_MAX = 16000
DEFAULT_SHORT_INPUT_THRESHOLD = 6000
DEFAULT_MEDIUM_INPUT_THRESHOLD = 16000
DEFAULT_MERGE_FRAGMENT_THRESHOLD = 2


@dataclass(frozen=True)
class ResponseTokenLimits:
    """Initial and retry response caps for one distillation LLM call."""

    initial: int
    retry_max: int
    tier: str


def _cfg_get(cfg: Any, key: str, default: Any) -> Any:
    try:
        return cfg.get(key, default)
    except (AttributeError, TypeError, ValueError):
        return default


def _cfg_int(cfg: Any, key: str, default: int) -> int:
    value = _cfg_get(cfg, key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _cfg_bool(cfg: Any, key: str, default: bool) -> bool:
    value = _cfg_get(cfg, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _normalize_limits(
    default_tokens: int,
    medium_tokens: int,
    long_tokens: int,
    retry_max_tokens: int,
) -> tuple[int, int, int, int]:
    default_tokens = max(DEFAULT_RESPONSE_TOKENS, default_tokens)
    medium_tokens = max(DEFAULT_RESPONSE_TOKENS_MEDIUM, default_tokens, medium_tokens)
    long_tokens = max(DEFAULT_RESPONSE_TOKENS_LONG, medium_tokens, long_tokens)
    retry_max_tokens = max(DEFAULT_RESPONSE_TOKENS_RETRY_MAX, long_tokens, retry_max_tokens)
    return default_tokens, medium_tokens, long_tokens, retry_max_tokens


def resolve_response_token_limits(
    cfg: Any,
    *,
    input_tokens: int,
    analysis_type: str = "standard",
    fragment_count: int = 0,
    previous_finish_reason: str | None = None,
) -> ResponseTokenLimits:
    """Resolve a bounded output budget for a distillation-related LLM call.

    The cap is intentionally tiered: short sessions use the default 6000-token
    limit, while long/chunked work receives more headroom and length-truncated
    retries can use the 16000-token emergency cap.
    """

    legacy_static = _cfg_int(cfg, "distill.response_tokens", DEFAULT_RESPONSE_TOKENS)
    if not _cfg_bool(cfg, "distill.dynamic_response_tokens_enabled", True):
        static_tokens = max(1, legacy_static)
        return ResponseTokenLimits(static_tokens, static_tokens, "static")

    default_tokens = _cfg_int(
        cfg, "distill.response_tokens_default", legacy_static or DEFAULT_RESPONSE_TOKENS
    )
    medium_tokens = _cfg_int(
        cfg, "distill.response_tokens_medium", DEFAULT_RESPONSE_TOKENS_MEDIUM
    )
    long_tokens = _cfg_int(cfg, "distill.response_tokens_long", DEFAULT_RESPONSE_TOKENS_LONG)
    retry_max_tokens = _cfg_int(
        cfg, "distill.response_tokens_retry_max", DEFAULT_RESPONSE_TOKENS_RETRY_MAX
    )
    default_tokens, medium_tokens, long_tokens, retry_max_tokens = _normalize_limits(
        default_tokens, medium_tokens, long_tokens, retry_max_tokens
    )

    if previous_finish_reason == "length":
        return ResponseTokenLimits(retry_max_tokens, retry_max_tokens, "retry")

    short_threshold = max(
        0,
        _cfg_int(
            cfg,
            "distill.response_tokens_short_input_threshold",
            DEFAULT_SHORT_INPUT_THRESHOLD,
        ),
    )
    medium_threshold = max(
        short_threshold,
        _cfg_int(
            cfg,
            "distill.response_tokens_medium_input_threshold",
            DEFAULT_MEDIUM_INPUT_THRESHOLD,
        ),
    )
    merge_fragment_threshold = max(
        1,
        _cfg_int(
            cfg,
            "distill.response_tokens_merge_fragment_threshold",
            DEFAULT_MERGE_FRAGMENT_THRESHOLD,
        ),
    )

    normalized_type = str(analysis_type or "standard").lower()
    if (
        previous_finish_reason == "length"
        or normalized_type in {"chunked", "merge", "fragment_merge"}
        or fragment_count >= merge_fragment_threshold
        or input_tokens > medium_threshold
    ):
        return ResponseTokenLimits(long_tokens, retry_max_tokens, "long")
    if input_tokens > short_threshold:
        return ResponseTokenLimits(medium_tokens, retry_max_tokens, "medium")
    return ResponseTokenLimits(default_tokens, retry_max_tokens, "default")
