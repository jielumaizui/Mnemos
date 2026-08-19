# -*- coding: utf-8 -*-
"""Tests for dynamic distillation response token budgets."""

from core.config import DEFAULT_CONFIG
from core.hephaestus.response_budget import resolve_response_token_limits


class _Config:
    def __init__(self, values=None):
        self._values = values or {}

    def get(self, key, default=None):
        return self._values.get(key, default)


def test_default_config_declares_expanded_four_tier_response_caps():
    distill_config = DEFAULT_CONFIG["distill"]

    assert distill_config["response_tokens"] == 6000
    assert distill_config["response_tokens_default"] == 6000
    assert distill_config["response_tokens_medium"] == 8000
    assert distill_config["response_tokens_long"] == 12000
    assert distill_config["response_tokens_retry_max"] == 16000


def test_short_sessions_use_expanded_default_response_cap():
    limits = resolve_response_token_limits(
        _Config(),
        input_tokens=1200,
        analysis_type="standard",
    )

    assert limits.initial == 6000
    assert limits.retry_max == 16000
    assert limits.tier == "default"


def test_medium_sessions_use_medium_cap():
    limits = resolve_response_token_limits(
        _Config(),
        input_tokens=9000,
        analysis_type="standard",
    )

    assert limits.initial == 8000
    assert limits.retry_max == 16000
    assert limits.tier == "medium"


def test_long_and_chunked_sessions_use_long_cap():
    long_limits = resolve_response_token_limits(
        _Config(),
        input_tokens=24000,
        analysis_type="standard",
    )
    chunked_limits = resolve_response_token_limits(
        _Config(),
        input_tokens=2000,
        analysis_type="chunked",
    )

    assert long_limits.initial == 12000
    assert long_limits.tier == "long"
    assert chunked_limits.initial == 12000
    assert chunked_limits.tier == "long"


def test_dynamic_budget_can_be_disabled_for_legacy_static_cap():
    limits = resolve_response_token_limits(
        _Config(
            {
                "distill.dynamic_response_tokens_enabled": False,
                "distill.response_tokens": 5000,
            }
        ),
        input_tokens=100000,
        analysis_type="chunked",
    )

    assert limits.initial == 5000
    assert limits.retry_max == 5000
    assert limits.tier == "static"


def test_dynamic_budget_normalizes_stale_legacy_four_tier_config():
    limits = resolve_response_token_limits(
        _Config(
            {
                "distill.response_tokens": 4000,
                "distill.response_tokens_default": 4000,
                "distill.response_tokens_medium": 6000,
                "distill.response_tokens_long": 8000,
                "distill.response_tokens_retry_max": 12000,
            }
        ),
        input_tokens=24000,
        analysis_type="standard",
    )

    assert limits.initial == 12000
    assert limits.retry_max == 16000
    assert limits.tier == "long"


def test_explicit_length_retry_uses_retry_max_cap():
    limits = resolve_response_token_limits(
        _Config(),
        input_tokens=1200,
        analysis_type="standard",
        previous_finish_reason="length",
    )

    assert limits.initial == 16000
    assert limits.retry_max == 16000
    assert limits.tier == "retry"
