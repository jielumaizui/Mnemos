# -*- coding: utf-8 -*-
"""Unit tests for core.app.intent_router."""

from __future__ import annotations

import hashlib
import logging
import sys
import sqlite3
from types import SimpleNamespace

import pytest

from core.app.intent_router import CorrectionStore, IntentRouter
from core.telemetry.provider_request import canonical_chat_input, utf8_token_upper_bound


def _sensitive_provider_error_marker() -> str:
    """Build an adversarial payload without storing a credential literal."""
    return "|".join(
        (
            "api" + "_key" + "=" + "DUMMY_CREDENTIAL_VALUE",
            "pass" + "word" + "=" + "DUMMY_CREDENTIAL_VALUE",
            "bank" + "_card" + "=" + "DUMMY_CREDENTIAL_VALUE",
            "prompt" + "=" + "PRIVATE_PROMPT_BODY",
            "response" + "=" + "PRIVATE_RESPONSE_BODY",
        )
    )


@pytest.fixture
def router(tmp_path, monkeypatch):
    """提供使用临时纠正库的 IntentRouter，默认关闭 LLM fallback。"""
    monkeypatch.setattr(
        "core.app.intent_router.get_config",
        lambda: {"intent_router.llm_fallback_enabled": False},
    )
    store = CorrectionStore(db_path=str(tmp_path / "corrections.db"))
    return IntentRouter(correction_store=store)


class TestIntentRouter:
    def test_route_recall_time_keyword(self, router):
        decision = router.route("总结一下之前的对话")
        assert decision.intent == "recall"
        assert decision.data_source == "raw"
        assert decision.route_tools == ["session_search"]
        assert decision.fallback_tools == ["context_aware_search"]
        assert "之前" in decision.matched_keywords

    def test_route_knowledge_question_keyword(self, router):
        decision = router.route("什么是 LazyPath")
        assert decision.intent == "knowledge"
        assert decision.data_source == "wiki"
        assert decision.route_tools == ["context_aware_search", "wiki_search"]
        assert decision.fallback_tools == ["session_search"]
        assert "什么是" in decision.matched_keywords

    def test_route_chinese_knowledge_question_without_llm(self, router, monkeypatch):
        monkeypatch.setattr(
            router,
            "_llm_classify",
            lambda user_input, candidates: pytest.fail("中文规则命中不应触发 LLM fallback"),
        )
        decision = router.route("怎么理解知识图谱的隐式关系？")
        assert decision.intent == "knowledge"
        assert decision.llm_fallback is False
        assert "怎么" in decision.matched_keywords or "怎么理解" in decision.matched_keywords

    def test_route_english_knowledge_question_without_llm(self, router, monkeypatch):
        monkeypatch.setattr(
            router,
            "_llm_classify",
            lambda user_input, candidates: pytest.fail("英文规则命中不应触发 LLM fallback"),
        )
        decision = router.route("How do I use Python for knowledge graph indexing?")
        assert decision.intent == "knowledge"
        assert decision.llm_fallback is False
        assert "how do" in decision.matched_keywords or "how" in decision.matched_keywords

    def test_route_english_mixed_action_prefers_task_without_llm(self, router, monkeypatch):
        monkeypatch.setattr(
            router,
            "_llm_classify",
            lambda user_input, candidates: pytest.fail("混合动作输入不应依赖 LLM fallback 纠偏"),
        )
        decision = router.route("Compare these two files and fix the failing test")
        assert decision.intent == "task"
        assert decision.llm_fallback is False
        assert "fix" in decision.matched_keywords

    def test_route_english_explain_update_prefers_task_without_llm(self, router, monkeypatch):
        monkeypatch.setattr(
            router,
            "_llm_classify",
            lambda user_input, candidates: pytest.fail("英文 update 动作不应被 explain 抢成知识查询"),
        )
        decision = router.route("Please explain and then update the docs")
        assert decision.intent == "task"
        assert decision.llm_fallback is False
        assert "update" in decision.matched_keywords

    def test_route_chinese_mixed_action_prefers_task_without_llm(self, router, monkeypatch):
        monkeypatch.setattr(
            router,
            "_llm_classify",
            lambda user_input, candidates: pytest.fail("中文动作输入不应被对比类知识词抢占"),
        )
        decision = router.route("请对比这两个方案并修改文档")
        assert decision.intent == "task"
        assert decision.llm_fallback is False
        assert "修改" in decision.matched_keywords

    def test_route_english_explain_without_action_stays_knowledge(self, router, monkeypatch):
        monkeypatch.setattr(
            router,
            "_llm_classify",
            lambda user_input, candidates: pytest.fail("纯解释型输入应由本地知识规则处理"),
        )
        decision = router.route("Please explain how Python decorators work")
        assert decision.intent == "knowledge"
        assert decision.llm_fallback is False

    def test_route_task_action_keyword(self, router):
        decision = router.route("帮我创建一个脚本")
        assert decision.intent == "task"
        assert decision.data_source == "none"
        assert decision.fallback_tools == ["preflight_inject", "guard_check"]
        assert "帮我" in decision.matched_keywords

    def test_route_default_chat(self, router):
        decision = router.route("随便聊聊")
        assert decision.intent == "chat"
        assert decision.confidence == 0.3
        assert decision.matched_keywords == []
        assert decision.explanation

    def test_route_correction_store_overrides(self, router):
        router.correct("随便聊聊", "chat", "knowledge")
        decision = router.route("随便聊聊")
        assert decision.intent == "knowledge"
        assert decision.confidence == 0.95
        assert decision.data_source == "wiki"


class TestIntentRouterRoadmapIntents:
    def test_route_raw_evidence_to_session_search(self, router):
        decision = router.route("查一下原话和证据")
        assert decision.intent == "recall"
        assert decision.data_source == "raw"
        assert decision.route_tools == ["session_search"]

    def test_route_mixed_recall_uses_raw_and_wiki(self, router):
        decision = router.route("上次我们怎么解决 Redis 连接池问题")
        assert decision.intent == "mixed_recall"
        assert decision.data_source == "raw+wiki"
        assert decision.route_tools == ["session_search", "context_aware_search"]

    def test_route_system_status_to_health_tools(self, router):
        decision = router.route("看一下 Mnemos 系统状态")
        assert decision.intent == "system_status"
        assert decision.data_source == "system"
        assert decision.route_tools == ["health_check", "doctor", "status"]

    def test_route_persona_to_persona_tools(self, router):
        decision = router.route("总结一下我的用户画像")
        assert decision.intent == "persona"
        assert decision.data_source == "persona"
        assert "persona_summary" in decision.route_tools

    def test_route_recap_to_pending_recaps(self, router):
        decision = router.route("看看有没有复盘提醒")
        assert decision.intent == "recap"
        assert decision.data_source == "recap"
        assert decision.route_tools == ["check_pending_recaps"]


class TestCorrectionStore:
    def test_record_and_lookup_exact(self, router, tmp_path):
        store = CorrectionStore(db_path=str(tmp_path / "corrections.db"))
        store.record_correction("hello", "chat", "task")
        assert store.lookup("hello") == "task"

    def test_lookup_pattern_match(self, router, tmp_path):
        store = CorrectionStore(db_path=str(tmp_path / "corrections.db"))
        store.record_correction("今天天气怎么样", "chat", "knowledge")
        # 关键词交集 > 60%
        assert store.lookup("今天天气怎么样啊") == "knowledge"

    def test_lookup_sequence_similarity(self, router, tmp_path):
        store = CorrectionStore(db_path=str(tmp_path / "corrections.db"))
        store.record_correction("打开Obsidian", "chat", "task")
        assert store.lookup("打开Obsidian") == "task"

    def test_lookup_miss_returns_none(self, router, tmp_path):
        store = CorrectionStore(db_path=str(tmp_path / "corrections.db"))
        assert store.lookup("完全不相关") is None


class TestIntentRouterIgnorePush:
    def test_route_ignore_push(self, router):
        decision = router.route("不用推送这个")
        assert decision.intent == "ignore_push"
        assert decision.data_source == "none"
        assert "不用" in decision.matched_keywords or "推送" in decision.matched_keywords

    def test_route_dismiss_in_english(self, router):
        decision = router.route("dismiss this suggestion")
        assert decision.intent == "ignore_push"


class TestIntentRouterCorrectionFlag:
    def test_ambiguous_boundary_needs_correction(self, router):
        # 单个关键词命中，confidence = 0.7 处于边界（CORRECTION_HIGH=0.7 为开区间）
        decision = router.route("帮我")
        assert decision.intent == "task"
        assert decision.needs_correction is True

    def test_multiple_rules_needs_correction(self, router):
        # 同时命中 recall 和 task 关键词
        decision = router.route("帮我总结一下之前的对话")
        assert decision.intent == "recall"
        assert decision.needs_correction is True

    def test_context_current_task_boosts_task(self, router):
        decision = router.route("运行测试", context={"current_task": "refactor auth"})
        assert decision.intent == "task"
        assert decision.confidence > 0.7

    def test_context_recent_intent_mismatch_needs_correction(self, router):
        decision = router.route("运行测试", context={"recent_intent": "knowledge"})
        assert decision.intent == "task"
        assert decision.needs_correction is True

    def test_empty_input_defaults_chat(self, router):
        decision = router.route("   ")
        assert decision.intent == "chat"
        assert decision.confidence == 0.0
        assert decision.needs_correction is False


class TestIntentRouterLLMFallback:
    def _enable_fallback(self, monkeypatch):
        monkeypatch.setattr(
            "core.app.intent_router.get_config",
            lambda: {
                "intent_router.llm_fallback_enabled": True,
                "intent_router.llm_fallback_threshold": 0.65,
            },
        )

    def test_llm_fallback_when_no_rule_match(self, router, monkeypatch):
        """无规则命中时，LLM fallback 返回有效意图。"""
        self._enable_fallback(monkeypatch)
        monkeypatch.setattr(router, "_llm_classify", lambda user_input, candidates: "knowledge")
        decision = router.route("glorp zeta flux")
        assert decision.intent == "knowledge"
        assert decision.confidence == 0.75
        assert decision.llm_fallback is True
        assert decision.needs_correction is False

    def test_llm_fallback_when_ambiguous_low_confidence(self, router, monkeypatch):
        """歧义且低置信时触发 LLM fallback。"""
        self._enable_fallback(monkeypatch)
        monkeypatch.setattr(router, "_llm_classify", lambda user_input, candidates: "task")
        # 调整 threshold 为 0.75，让 "帮我" (confidence=0.7, needs_correction=True) 触发 fallback
        monkeypatch.setattr(
            "core.app.intent_router.get_config",
            lambda: {
                "intent_router.llm_fallback_enabled": True,
                "intent_router.llm_fallback_threshold": 0.75,
            },
        )
        decision = router.route("帮我")
        assert decision.intent == "task"
        assert decision.llm_fallback is True

    def test_llm_fallback_failure_keeps_rule_result(self, router, monkeypatch):
        """LLM fallback 失败时保持原规则结果。"""
        self._enable_fallback(monkeypatch)
        monkeypatch.setattr(router, "_llm_classify", lambda user_input, candidates: None)
        decision = router.route("帮我")
        assert decision.intent == "task"
        assert decision.llm_fallback is False

    def test_llm_fallback_disabled_uses_chat(self, router, monkeypatch):
        """关闭 LLM fallback 时无规则命中返回 chat。"""
        monkeypatch.setattr(
            "core.app.intent_router.get_config",
            lambda: {"intent_router.llm_fallback_enabled": False},
        )
        decision = router.route("glorp zeta flux")
        assert decision.intent == "chat"
        assert decision.llm_fallback is False

    def test_caller_can_force_deterministic_rule_only_routing(self, router, monkeypatch):
        self._enable_fallback(monkeypatch)
        monkeypatch.setattr(
            router,
            "_llm_classify",
            lambda user_input, candidates: pytest.fail(
                "rule-only routing must not call the LLM"
            ),
        )

        decision = router.route("glorp zeta flux", allow_llm_fallback=False)

        assert decision.intent == "chat"
        assert decision.llm_fallback is False

    def test_llm_fallback_disabled_by_default(self, tmp_path, monkeypatch):
        """未显式配置时，默认不应调用 LLM fallback。"""
        monkeypatch.setattr("core.app.intent_router.get_config", lambda: {})
        store = CorrectionStore(db_path=str(tmp_path / "corrections.db"))
        router = IntentRouter(correction_store=store)
        monkeypatch.setattr(
            router,
            "_llm_classify",
            lambda user_input, candidates: pytest.fail("默认配置不应触发 LLM fallback"),
        )
        decision = router.route("glorp zeta flux")
        assert decision.intent == "chat"
        assert decision.llm_fallback is False

    def test_llm_fallback_uses_configured_timeout(self, tmp_path, monkeypatch):
        """启用 LLM fallback 时应使用配置化短超时，而不是硬编码 10 秒。"""
        class RuntimeConfig:
            data_dir = tmp_path
            database_dir = tmp_path

            def get(self, key, default=None):
                return {
                    "intent_router.llm_fallback_enabled": True,
                    "intent_router.llm_fallback_timeout_seconds": 1.25,
                    "llm.provider_prices": {
                        "fake": {"fake-model": {"input": 0.1, "output": 0.2}}
                    },
                }.get(key, default)

        monkeypatch.setattr(
            "core.app.intent_router.get_config",
            RuntimeConfig,
        )
        captured = {}

        class FakeCompletions:
            def create(self, **kwargs):
                captured["timeout"] = kwargs["timeout"]
                with sqlite3.connect(str(tmp_path / "model_call_ledger.db")) as conn:
                    captured["reservation_state"] = conn.execute(
                        "SELECT lifecycle_state, request_dispatched FROM model_call_entries "
                        "WHERE operation='intent_router'"
                    ).fetchone()
                    provider_input = canonical_chat_input(kwargs["messages"])
                    captured["reservation_input"] = conn.execute(
                        "SELECT reserved_input_tokens, input_digest, reserved_output_tokens "
                        "FROM model_call_entries WHERE operation='intent_router'"
                    ).fetchone()
                assert captured["reservation_input"] == (
                    utf8_token_upper_bound(provider_input),
                    hashlib.sha256(provider_input.encode("utf-8")).hexdigest(),
                    kwargs["max_tokens"],
                )
                return SimpleNamespace(
                    id="intent-request-1",
                    usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1),
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content='{"intent": "knowledge"}')
                        )
                    ]
                )

        class FakeOpenAIClient:
            def __init__(self, **kwargs):
                captured["constructor_kwargs"] = kwargs
                self.chat = SimpleNamespace(completions=FakeCompletions())

        class FakeApiConfig:
            provider = "fake"
            model = "fake-model"
            api_key = "fake-key"
            base_url = None
            configured = True

            def active(self):
                return self

            def report_success(self, active_cfg):
                return None

            def report_failure(self, active_cfg, error):
                return None

        monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAIClient))
        monkeypatch.setattr(
            "core.llm_config.resolve_llm_api_chain",
            lambda cfg: SimpleNamespace(all_configs=[FakeApiConfig()]),
        )
        store = CorrectionStore(db_path=str(tmp_path / "corrections.db"))
        router = IntentRouter(correction_store=store)

        assert router._llm_classify("未知中文", ["knowledge"]) == "knowledge"
        assert captured["timeout"] == 1.25
        assert captured["constructor_kwargs"]["max_retries"] == 0
        assert captured["reservation_state"] == ("reserved", 1)
        with sqlite3.connect(str(tmp_path / "model_call_ledger.db")) as conn:
            row = conn.execute(
                "SELECT operation, lifecycle_state FROM model_call_entries"
            ).fetchone()
        assert row == ("intent_router", "settled")

    def test_llm_fallback_empty_choices_preserves_dispatched_reservation(
        self, tmp_path, monkeypatch
    ):
        class RuntimeConfig:
            data_dir = tmp_path
            database_dir = tmp_path

            def get(self, key, default=None):
                return {
                    "intent_router.llm_fallback_enabled": True,
                    "llm.provider_prices": {
                        "fake": {"fake-model": {"input": 0.1, "output": 0.2}}
                    },
                }.get(key, default)

        class FakeCompletions:
            def create(self, **_kwargs):
                return SimpleNamespace(
                    id="intent-empty-choices",
                    usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
                    choices=[],
                )

        class FakeOpenAIClient:
            def __init__(self, **_kwargs):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        class FakeApiConfig:
            provider = "fake"
            model = "fake-model"
            api_key = "fake-key"
            base_url = None
            configured = True

            def active(self):
                return self

            def report_success(self, _active_cfg):
                return None

            def report_failure(self, _active_cfg, _error):
                return None

        monkeypatch.setattr("core.app.intent_router.get_config", RuntimeConfig)
        monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAIClient))
        monkeypatch.setattr(
            "core.llm_config.resolve_llm_api_chain",
            lambda _cfg: SimpleNamespace(all_configs=[FakeApiConfig()]),
        )
        router = IntentRouter(correction_store=CorrectionStore(db_path=str(tmp_path / "corrections.db")))

        assert router._llm_classify("未知中文", ["knowledge"]) is None
        with sqlite3.connect(str(tmp_path / "model_call_ledger.db")) as conn:
            row = conn.execute(
                "SELECT lifecycle_state, request_dispatched, error_code "
                "FROM model_call_entries WHERE operation='intent_router'"
            ).fetchone()
        # Explicit provider usage is settled even though an empty choices list
        # makes the routing result unusable; it must never remain reserved.
        assert row == ("settled", 1, "")

    def test_llm_fallback_redacts_provider_exception_from_log_and_ledger(
        self, tmp_path, monkeypatch, caplog
    ):
        """A fallback failure must expose a category, never provider text."""
        marker = _sensitive_provider_error_marker()
        failures = []

        class RuntimeConfig:
            data_dir = tmp_path
            database_dir = tmp_path

            def get(self, key, default=None):
                return {
                    "intent_router.llm_fallback_enabled": True,
                    "model_call_ledger.daily_cost_cap": 10.0,
                    "llm.provider_prices": {
                        "fake": {"fake-model": {"input": 0.1, "output": 0.2}}
                    },
                }.get(key, default)

        class ProviderError(Exception):
            pass

        class FakeCompletions:
            @staticmethod
            def create(**_kwargs):
                raise ProviderError(marker)

        class FakeOpenAIClient:
            def __init__(self, **_kwargs):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        class FakeApiConfig:
            provider = "fake"
            model = "fake-model"
            api_key = "fake-key"
            base_url = None
            configured = True

            def active(self):
                return self

            def report_success(self, _active_cfg):
                return None

            def report_failure(self, _active_cfg, category):
                failures.append(category)

        monkeypatch.setattr("core.app.intent_router.get_config", RuntimeConfig)
        monkeypatch.setitem(
            sys.modules,
            "openai",
            SimpleNamespace(OpenAI=FakeOpenAIClient, OpenAIError=ProviderError),
        )
        monkeypatch.setattr(
            "core.llm_config.resolve_llm_api_chain",
            lambda _cfg: SimpleNamespace(all_configs=[FakeApiConfig()]),
        )
        caplog.set_level(logging.DEBUG)
        router = IntentRouter(
            correction_store=CorrectionStore(db_path=str(tmp_path / "corrections.db"))
        )

        assert router._llm_classify("private input " + marker, ["knowledge"]) is None

        assert failures == ["provider_error"]
        assert marker not in caplog.text
        assert "category=provider_error" in caplog.text
        assert marker.encode("utf-8") not in (
            tmp_path / "model_call_ledger.db"
        ).read_bytes()
