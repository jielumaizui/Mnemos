# -*- coding: utf-8 -*-
"""Tests for core.llm_key_pool."""

from datetime import datetime, timedelta, timezone

from core.llm_key_pool import KeyPool, MAX_CONSECUTIVE_FAILURES
from core.llm_config import LLMApiConfig


def _key(source: str, api_key: str = "key") -> LLMApiConfig:
    return LLMApiConfig(
        provider="sf",
        api_key=api_key,
        base_url="https://api.test/v1",
        model="m",
        source=source,
    )


class TestKeyPoolPick:
    def test_weighted_prefers_higher_success_rate(self):
        good = _key("env:GOOD", api_key="good")
        bad = _key("env:BAD", api_key="bad")
        pool = KeyPool([good, bad], strategy="weighted")

        pool.report_success(good)
        pool.report_success(good)
        pool.report_failure(bad, "error")

        # Weighted strategy should pick the key with better success rate.
        assert pool.pick() is good

    def test_round_robin_rotates(self):
        a = _key("env:A", api_key="DUMMY_CREDENTIAL_A")
        b = _key("env:B", api_key="DUMMY_CREDENTIAL_B")
        pool = KeyPool([a, b], strategy="round_robin")

        assert pool.pick() is a
        assert pool.pick() is b
        assert pool.pick() is a

    def test_random_returns_available(self):
        a = _key("env:A", api_key="DUMMY_CREDENTIAL_A")
        b = _key("env:B", api_key="DUMMY_CREDENTIAL_B")
        pool = KeyPool([a, b], strategy="random")

        picked = pool.pick()
        assert picked in (a, b)


class TestKeyPoolFailureCooldown:
    def test_failure_applies_cooldown(self):
        key = _key("env:K", api_key="DUMMY_CREDENTIAL_VALUE_FOR_REDACTION_TEST")
        pool = KeyPool([key], strategy="weighted")

        pool.report_failure(key, "rate limit hit")
        assert pool.pick() is None

    def test_cooldown_expires(self):
        key = _key("env:K", api_key="DUMMY_CREDENTIAL_VALUE_FOR_REDACTION_TEST")
        pool = KeyPool([key], strategy="weighted")

        pool.report_failure(key, "rate limit hit")
        state = pool._state[pool._key_id(key)]
        # Manually backdate cooldown so it has expired.
        state.cooldown_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        state.status = "cooling"

        assert pool.pick() is key

    def test_max_failures_marks_expired(self):
        key = _key("env:K", api_key="DUMMY_CREDENTIAL_VALUE_FOR_REDACTION_TEST")
        pool = KeyPool([key], strategy="weighted")

        for _ in range(MAX_CONSECUTIVE_FAILURES):
            pool.report_failure(key, "error")

        assert pool.pick() is None
        assert pool._state[pool._key_id(key)].status == "expired"


class TestKeyPoolHealth:
    def test_health_counts(self):
        a = _key("env:A", api_key="DUMMY_CREDENTIAL_A")
        b = _key("env:B", api_key="DUMMY_CREDENTIAL_B")
        pool = KeyPool([a, b], strategy="weighted")

        pool.report_failure(a, "error")
        pool.report_success(b)

        health = pool.health()
        assert health["total"] == 2
        assert health["active"] == 1
        assert health["cooling"] == 1

    def test_health_details_exposes_cooldown_reason(self):
        a = _key("env:A", api_key="dummy")
        pool = KeyPool([a], strategy="weighted")

        pool.report_failure(a, "429 rate limit")

        health = pool.health(details=True)
        assert health["total"] == 1
        assert health["cooling"] == 1
        assert health["keys"] == [
            {
                "id": "env:A",
                "source": "env:A",
                "provider": "sf",
                "model": "m",
                "status": "cooling",
                "total_calls": 1,
                "success_calls": 0,
                "consecutive_failures": 1,
                "cooldown_until": health["keys"][0]["cooldown_until"],
                "cooldown_reason": "rate_limit",
            }
        ]
        assert health["keys"][0]["cooldown_until"] is not None


class TestKeyPoolReset:
    def test_reset_restores_key(self):
        key = _key("env:K", api_key="DUMMY_CREDENTIAL_VALUE_FOR_REDACTION_TEST")
        pool = KeyPool([key], strategy="weighted")

        for _ in range(MAX_CONSECUTIVE_FAILURES):
            pool.report_failure(key, "error")

        assert pool.pick() is None
        assert pool.reset(key) is True
        assert pool.pick() is key

    def test_reset_unknown_returns_false(self):
        key = _key("env:K", api_key="DUMMY_CREDENTIAL_VALUE_FOR_REDACTION_TEST")
        pool = KeyPool(
            [_key("env:OTHER", api_key="DUMMY_CREDENTIAL_VALUE_FOR_REDACTION_TEST")],
            strategy="weighted",
        )
        assert pool.reset(key) is False
