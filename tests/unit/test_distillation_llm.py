# -*- coding: utf-8 -*-
"""Tests for core.hephaestus.distillation_llm routing strategies."""

import pytest

from core.hephaestus.distillation_llm import HttpApiHostAgentCaller
from core.hephaestus.distillation_errors import DistillationAPIError
from core.hephaestus.distill_response import DistillBackendResponse
from core.llm_config import LLMApiChain, LLMApiConfig


def _make_chain() -> LLMApiChain:
    return LLMApiChain(
        primary=LLMApiConfig(
            provider="dmxapi",
            api_key="dmx-key",
            base_url="https://www.dmxapi.cn/v1",
            model="kimi-k2.5-free",
            source="env:DMXAPI_API_KEY",
            cost_level="free",
        ),
        same_provider_backup=LLMApiConfig(
            provider="dmxapi",
            api_key="dmx-key",
            base_url="https://www.dmxapi.cn/v1",
            model="MiniMax-M2.7-free",
            source="env:DMXAPI_API_KEY",
            cost_level="free",
        ),
        cross_provider_backup=LLMApiConfig(
            provider="siliconflow",
            api_key="sf-key",
            base_url="https://api.siliconflow.cn/v1",
            model="deepseek-ai/DeepSeek-V4-Flash",
            source="env:SILICONFLOW_API_KEY",
            cost_level="paid",
        ),
    )


def _enable_priority_race(caller: HttpApiHostAgentCaller) -> HttpApiHostAgentCaller:
    caller._routing_strategy = "priority_race"
    return caller


def _sensitive_provider_error_marker() -> str:
    """Build a realistic failure payload without a committed secret literal."""
    return "|".join(
        (
            "api" + "_key" + "=" + "DUMMY_CREDENTIAL_VALUE",
            "pass" + "word" + "=" + "DUMMY_CREDENTIAL_VALUE",
            "bank" + "_card" + "=" + "DUMMY_CREDENTIAL_VALUE",
            "prompt" + "=" + "PRIVATE_PROMPT_BODY",
            "response" + "=" + "PRIVATE_RESPONSE_BODY",
        )
    )


def test_call_with_evidence_returns_raw_provider_and_parse_metadata(monkeypatch):
    caller = HttpApiHostAgentCaller(api_chain=_make_chain())
    caller.reset_session_cost_budget(10.0)

    monkeypatch.setattr(
        caller,
        "_try_api_config",
        lambda prompt, timeout, cfg: (
            '{"result":"ok"}',
            {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "cost": 0.0,
                "request_id": "request-evidence",
                "finish_reason": "stop",
            },
        ),
    )

    response = caller.call_with_evidence("prompt", expect_json=True, max_retries=0)

    assert isinstance(response, DistillBackendResponse)
    assert response.parsed == {"result": "ok"}
    assert response.raw_text == '{"result":"ok"}'
    assert response.provider == "dmxapi"
    assert response.model == "kimi-k2.5-free"
    assert response.request_id == "request-evidence"
    assert response.finish_reason == "stop"
    assert response.parse_path == "direct_json"
    assert response.attempt_history[-1]["status"] == "success"


def test_non_json_failure_keeps_raw_response_evidence_on_error(monkeypatch):
    caller = HttpApiHostAgentCaller(api_chain=_make_chain())
    caller.reset_session_cost_budget(10.0)
    monkeypatch.setattr(
        caller,
        "_try_api_config",
        lambda prompt, timeout, cfg: (
            "not-json-response",
            {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "cost": 0.0,
                "request_id": "request-invalid-json",
                "finish_reason": "stop",
            },
        ),
    )

    with pytest.raises(DistillationAPIError) as raised:
        caller.call_with_evidence("prompt", expect_json=True, max_retries=0)

    evidence = raised.value.response_evidence
    assert isinstance(evidence, DistillBackendResponse)
    assert evidence.raw_text == "not-json-response"
    assert evidence.parsed is None
    assert evidence.parse_path == "failed"
    assert evidence.attempt_history[-1]["status"] == "parse_failed"


def test_transport_empty_failure_records_explicit_empty_evidence(monkeypatch):
    caller = HttpApiHostAgentCaller(api_chain=_make_chain())
    caller.reset_session_cost_budget(10.0)
    monkeypatch.setattr(
        caller,
        "_try_api_config",
        lambda prompt, timeout, cfg: (
            None,
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost": 0.0,
                "request_id": "",
                "finish_reason": "",
            },
        ),
    )

    with pytest.raises(DistillationAPIError) as raised:
        caller.call_with_evidence("prompt", expect_json=True, max_retries=0)

    evidence = raised.value.response_evidence
    assert isinstance(evidence, DistillBackendResponse)
    assert evidence.raw_text == ""
    assert evidence.parse_path == "transport_empty"
    assert evidence.to_failure_metadata()["transport_empty"] is True


class TestGroupByCost:
    def test_groups_free_and_paid_by_cost_level(self):
        caller = HttpApiHostAgentCaller(api_chain=_make_chain())
        free, paid = caller._group_by_cost(_make_chain().all_configs)
        assert [c.model for c in free] == ["kimi-k2.5-free", "MiniMax-M2.7-free"]
        assert [c.model for c in paid] == ["deepseek-ai/DeepSeek-V4-Flash"]

    def test_infers_free_from_zero_price(self):
        chain = LLMApiChain(
            primary=LLMApiConfig(
                provider="dmxapi",
                api_key="DUMMY_CREDENTIAL_VALUE_FOR_REDACTION_TEST",
                base_url="https://www.dmxapi.cn/v1",
                model="unknown-free-model",
                source="test",
            ),
        )
        caller = HttpApiHostAgentCaller(api_chain=chain)
        free, paid = caller._group_by_cost(chain.all_configs)
        assert len(free) == 1
        assert len(paid) == 0


class TestPriorityRace:
    def test_uses_only_free_when_available(self, monkeypatch):
        """不忙时只调用 dmxapi，不触碰 siliconflow。"""
        caller = _enable_priority_race(HttpApiHostAgentCaller(api_chain=_make_chain()))
        caller.reset_session_cost_budget(10.0)

        called_models = []

        def fake_try(prompt, timeout, cfg):
            called_models.append(cfg.model)
            return '{"result": "ok"}', {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0}

        monkeypatch.setattr(caller, "_try_api_config", fake_try)

        result = caller.call("prompt", expect_json=True)
        assert result == {"result": "ok"}
        assert "deepseek-ai/DeepSeek-V4-Flash" not in called_models
        assert "kimi-k2.5-free" in called_models

    def test_switches_to_backup_free_model_on_failure(self, monkeypatch):
        """主免费模型失败时切换到另一个免费模型。"""
        caller = _enable_priority_race(HttpApiHostAgentCaller(api_chain=_make_chain()))
        caller.reset_session_cost_budget(10.0)

        def fake_try(prompt, timeout, cfg):
            if cfg.model == "kimi-k2.5-free":
                return None, {"cost": 0.0}
            return '{"result": "backup"}', {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0}

        monkeypatch.setattr(caller, "_try_api_config", fake_try)

        result = caller.call("prompt", expect_json=True)
        assert result == {"result": "backup"}

    def test_preserves_free_failure_history_before_race_success(self, monkeypatch):
        caller = _enable_priority_race(HttpApiHostAgentCaller(api_chain=_make_chain()))
        chain = _make_chain()
        configs = [chain.primary, chain.cross_provider_backup]
        failure = DistillBackendResponse.transport_empty(
            usage={"cost": 0.0},
            provider="dmxapi",
            model="kimi-k2.5-free",
            attempt_history=({"attempt": 0, "status": "transport_empty"},),
        )
        success = DistillBackendResponse.create(
            raw_text='{"result":"paid"}',
            parsed={"result": "paid"},
            usage={"cost": 0.001},
            provider="siliconflow",
            model="deepseek-ai/DeepSeek-V4-Flash",
            parse_path="direct_json",
            attempt_history=({"attempt": 0, "status": "success"},),
        )
        monkeypatch.setattr(
            caller._rate_limiter,
            "can_acquire",
            lambda provider, model=None, estimated_tokens=0: provider == "dmxapi",
        )
        monkeypatch.setattr(
            caller,
            "_call_one_config",
            lambda *args, **kwargs: (failure, {"cost": 0.0}),
        )
        monkeypatch.setattr(
            caller,
            "_race_configs",
            lambda *args, **kwargs: (success, {"cost": 0.001}),
        )

        response, _usage = caller._call_priority_race(
            configs,
            "prompt",
            True,
            0,
            1,
        )

        assert [item["status"] for item in response.attempt_history] == [
            "transport_empty",
            "success",
        ]

    def test_races_with_paid_when_free_rate_limited(self, monkeypatch):
        """免费模型被限流时，并行调用付费模型兜底。"""
        chain = _make_chain()
        caller = _enable_priority_race(HttpApiHostAgentCaller(api_chain=chain))
        caller.reset_session_cost_budget(10.0)

        # 让 dmxapi 两个模型都不可获取，siliconflow 可获取
        def fake_can_acquire(provider, model=None, estimated_tokens=0):
            return provider != "dmxapi"

        monkeypatch.setattr(caller._rate_limiter, "can_acquire", fake_can_acquire)

        called_models = []
        call_order = []

        def fake_try(prompt, timeout, cfg):
            called_models.append(cfg.model)
            call_order.append(cfg.model)
            # siliconflow 立即返回
            if cfg.provider == "siliconflow":
                return '{"result": "paid"}', {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.001}
            # dmxapi 永远返回 None，模拟限流后仍然失败
            return None, {"cost": 0.0}

        monkeypatch.setattr(caller, "_try_api_config", fake_try)

        result = caller.call("prompt", expect_json=True)
        assert result == {"result": "paid"}
        assert "deepseek-ai/DeepSeek-V4-Flash" in called_models

    def test_race_returns_first_success(self, monkeypatch):
        """并行竞争中先返回的成功结果获胜。"""
        chain = _make_chain()
        caller = _enable_priority_race(HttpApiHostAgentCaller(api_chain=chain))
        caller.reset_session_cost_budget(10.0)
        caller._race_timeout = 5

        # 免费层被限流，触发与付费层的并行竞争
        monkeypatch.setattr(
            caller._rate_limiter, "can_acquire", lambda provider, model=None, estimated_tokens=0: provider != "dmxapi"
        )

        def fake_try(prompt, timeout, cfg):
            # 模拟 siliconflow 更快返回
            if cfg.provider == "siliconflow":
                return '{"result": "fast"}', {"cost": 0.001}
            # dmxapi 慢但也会成功
            import time
            time.sleep(0.05)
            return '{"result": "slow"}', {"cost": 0.0}

        monkeypatch.setattr(caller, "_try_api_config", fake_try)

        result = caller.call("prompt", expect_json=True)
        assert result == {"result": "fast"}


class TestForceProvider:
    def test_api_alias_keeps_default_chain(self, monkeypatch):
        caller = HttpApiHostAgentCaller(api_chain=_make_chain(), force_provider="api")
        caller.reset_session_cost_budget(10.0)

        called_providers = []

        def fake_try(prompt, timeout, cfg):
            called_providers.append(cfg.provider)
            return '{"result": "api-chain"}', {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0}

        monkeypatch.setattr(caller, "_try_api_config", fake_try)

        result = caller.call("prompt", expect_json=True)

        assert result == {"result": "api-chain"}
        assert called_providers == ["dmxapi"]

    def test_force_provider_filters_sequential_chain(self, monkeypatch):
        caller = HttpApiHostAgentCaller(api_chain=_make_chain(), force_provider="siliconflow")
        caller.reset_session_cost_budget(10.0)
        caller._routing_strategy = "sequential"

        called_providers = []

        def fake_try(prompt, timeout, cfg):
            called_providers.append(cfg.provider)
            return '{"result": "forced"}', {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.001}

        monkeypatch.setattr(caller, "_try_api_config", fake_try)

        result = caller.call("prompt", expect_json=True)

        assert result == {"result": "forced"}
        assert called_providers == ["siliconflow"]

    def test_force_provider_filters_priority_race_candidates(self, monkeypatch):
        caller = _enable_priority_race(
            HttpApiHostAgentCaller(api_chain=_make_chain(), force_provider="dmxapi")
        )
        caller.reset_session_cost_budget(10.0)

        called_providers = []

        def fake_try(prompt, timeout, cfg):
            called_providers.append(cfg.provider)
            return '{"result": "forced-free"}', {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "cost": 0.0,
            }

        monkeypatch.setattr(caller, "_try_api_config", fake_try)

        result = caller.call("prompt", expect_json=True)

        assert result == {"result": "forced-free"}
        assert called_providers == ["dmxapi"]

    def test_force_provider_raises_when_no_config_matches(self):
        caller = HttpApiHostAgentCaller(api_chain=_make_chain(), force_provider="cli")
        caller.reset_session_cost_budget(10.0)

        from core.hephaestus.distillation_errors import DistillationAPIError

        with pytest.raises(DistillationAPIError, match="force_provider='cli'"):
            caller.call("prompt", expect_json=True)


class TestSequential:
    def test_default_strategy_is_sequential(self):
        class EmptyConfig:
            def get(self, _key, default=None):
                return default

        caller = HttpApiHostAgentCaller(api_chain=_make_chain(), config_getter=EmptyConfig)
        assert caller._routing_strategy == "sequential"

    def test_call_uses_shared_tokenizer_estimate_for_rate_budget(self, monkeypatch):
        import core.hephaestus.distillation_llm as distillation_llm

        chain = _make_chain()
        caller = HttpApiHostAgentCaller(api_chain=chain)
        caller.reset_session_cost_budget(10.0)
        caller._routing_strategy = "sequential"
        seen_tokens = []

        monkeypatch.setattr(
            distillation_llm,
            "estimate_tokens",
            lambda text: 123 if text == "prompt" else 0,
            raising=False,
        )

        def fake_call_sequential(configs, prompt, expect_json, retries, estimated_tokens):
            seen_tokens.append(estimated_tokens)
            usage = {"cost": 0.0}
            return DistillBackendResponse.create(
                raw_text='{"result":"ok"}',
                parsed={"result": "ok"},
                usage=usage,
                provider="test",
                model="test",
                parse_path="direct_json",
                attempt_history=({"attempt": 0, "status": "success"},),
            ), usage

        monkeypatch.setattr(caller, "_call_sequential", fake_call_sequential)

        result = caller.call("prompt", expect_json=True)

        assert result == {"result": "ok"}
        assert seen_tokens == [123]

    def test_sequential_strategy_tries_chain_in_order(self, monkeypatch):
        chain = _make_chain()
        caller = HttpApiHostAgentCaller(api_chain=chain)
        caller.reset_session_cost_budget(10.0)
        caller._routing_strategy = "sequential"

        called_models = []

        def fake_try(prompt, timeout, cfg):
            called_models.append(cfg.model)
            if cfg.model == "MiniMax-M2.7-free":
                return '{"result": "minimax"}', {"cost": 0.0}
            return None, {"cost": 0.0}

        monkeypatch.setattr(caller, "_try_api_config", fake_try)

        result = caller.call("prompt", expect_json=True)
        assert result == {"result": "minimax"}
        assert called_models[0] == "kimi-k2.5-free"
        assert "MiniMax-M2.7-free" in called_models
        assert "deepseek-ai/DeepSeek-V4-Flash" not in called_models

    def test_sequential_falls_back_to_cross_provider(self, monkeypatch):
        chain = _make_chain()
        caller = HttpApiHostAgentCaller(api_chain=chain)
        caller.reset_session_cost_budget(10.0)
        caller._routing_strategy = "sequential"

        def fake_try(prompt, timeout, cfg):
            if cfg.provider == "siliconflow":
                return '{"result": "fallback"}', {"cost": 0.001}
            return None, {"cost": 0.0}

        monkeypatch.setattr(caller, "_try_api_config", fake_try)

        result = caller.call("prompt", expect_json=True)
        assert result == {"result": "fallback"}


def test_call_raises_when_all_providers_fail(monkeypatch):
    chain = _make_chain()
    caller = HttpApiHostAgentCaller(api_chain=chain)
    caller.reset_session_cost_budget(10.0)

    monkeypatch.setattr(caller, "_try_api_config", lambda prompt, timeout, cfg: (None, {"cost": 0.0}))

    from core.hephaestus.distillation_errors import DistillationAPIError

    with pytest.raises(DistillationAPIError):
        caller.call("prompt", expect_json=True)


def test_budget_error_does_not_expose_ledger_exception_text(monkeypatch):
    """The caller-visible distillation error remains safe if the ledger changes."""
    from core.hephaestus.distillation_errors import DistillationAPIError
    from core.telemetry.prompt_call_log import ModelCallBudgetExceeded

    marker = _sensitive_provider_error_marker()

    class Config:
        def get(self, _key, default=None):
            return default

    class RejectingLedger:
        @staticmethod
        def reserve(**_kwargs):
            raise ModelCallBudgetExceeded(marker)

    caller = HttpApiHostAgentCaller(api_chain=_make_chain(), config_getter=Config)
    monkeypatch.setattr(caller, "_ledger_for_call", lambda: (RejectingLedger(), "safe-run"))

    with pytest.raises(DistillationAPIError) as exc_info:
        caller._try_api_config("private prompt " + marker, 1, _make_chain().primary)

    assert str(exc_info.value) == "模型调用预算已耗尽"
    assert marker not in str(exc_info.value)


class _MockDelta:
    def __init__(self, content=None):
        self.content = content


class _MockChoice:
    def __init__(self, content=None, finish_reason=None):
        self.delta = _MockDelta(content)
        self.finish_reason = finish_reason


class _MockChunk:
    def __init__(self, content=None, finish_reason=None):
        self.choices = [_MockChoice(content, finish_reason)]


def test_try_api_config_uses_streaming_and_concatenates_chunks(monkeypatch, tmp_path):
    """流式输出：验证多个 chunk 被正确拼接，并估算 token 用量。"""
    chain = _make_chain()

    class ExplicitFreePriceConfig:
        data_dir = tmp_path
        database_dir = tmp_path

        _data = {
            "model_call_ledger": {
                "daily_cost_cap": 10.0,
                "allow_explicit_zero_price": True,
            },
            "llm": {
                "provider_prices": {
                    "dmxapi": {
                        "kimi-k2.5-free": {"input": 0.0, "output": 0.0},
                    }
                }
            },
        }

        def get(self, key, default=None):
            value = self._data
            for part in key.split("."):
                if not isinstance(value, dict) or part not in value:
                    return default
                value = value[part]
            return value

    caller = HttpApiHostAgentCaller(
        api_chain=chain,
        config_getter=ExplicitFreePriceConfig,
    )

    def _fake_create(**kwargs):
        assert kwargs.get("stream") is True
        return iter(
            [
                _MockChunk('{"result": '),
                _MockChunk('"ok"}'),
                _MockChunk("", finish_reason="stop"),
            ]
        )

    class _MockCompletions:
        create = staticmethod(_fake_create)

    class _MockChat:
        completions = _MockCompletions()

    class _MockClient:
        def __init__(self, **kwargs):
            pass

        chat = _MockChat()

    import openai

    monkeypatch.setattr(openai, "OpenAI", _MockClient)

    raw, usage = caller._try_api_config('{"prompt"}', 120, chain.primary)
    assert raw == '{"result": "ok"}'
    assert usage["finish_reason"] == "stop"
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    caller._settle_provider_usage(usage, latency_ms=1)


def test_call_one_config_escalates_response_cap_after_length_finish(monkeypatch):
    """结构化输出被 length 截断时，下一次重试应升到兜底上限。"""
    chain = _make_chain()
    caller = HttpApiHostAgentCaller(api_chain=chain)
    seen_max_tokens = []

    def fake_try(prompt, timeout, cfg, max_tokens=None):
        seen_max_tokens.append(max_tokens)
        if len(seen_max_tokens) == 1:
            return '{"result":', {"cost": 0.0, "finish_reason": "length"}
        return '{"result": "ok"}', {"cost": 0.0, "finish_reason": "stop"}

    monkeypatch.setattr(caller, "_try_api_config", fake_try)

    result, usage = caller._call_one_config(
        chain.primary,
        "prompt",
        expect_json=True,
        retries=1,
        response_max_tokens=12000,
        response_retry_max_tokens=16000,
    )

    assert result.parsed == {"result": "ok"}
    assert usage["finish_reason"] == "stop"
    assert seen_max_tokens == [12000, 16000]


def test_call_one_config_retries_parseable_length_finish(monkeypatch):
    """即使 length 响应可解析，也应优先用兜底上限重试避免接受截断结果。"""
    chain = _make_chain()
    caller = HttpApiHostAgentCaller(api_chain=chain)
    seen_max_tokens = []

    def fake_try(prompt, timeout, cfg, max_tokens=None):
        seen_max_tokens.append(max_tokens)
        if len(seen_max_tokens) == 1:
            return '{"result": "partial"}', {"cost": 0.0, "finish_reason": "length"}
        return '{"result": "complete"}', {"cost": 0.0, "finish_reason": "stop"}

    monkeypatch.setattr(caller, "_try_api_config", fake_try)

    result, usage = caller._call_one_config(
        chain.primary,
        "prompt",
        expect_json=True,
        retries=1,
        response_max_tokens=12000,
        response_retry_max_tokens=16000,
    )

    assert result.parsed == {"result": "complete"}
    assert usage["finish_reason"] == "stop"
    assert seen_max_tokens == [12000, 16000]
