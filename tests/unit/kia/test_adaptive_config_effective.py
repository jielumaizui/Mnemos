# -*- coding: utf-8 -*-
"""AdaptiveConfig + EffectivePolicy 集成测试。"""

import pytest

from core.kia.adaptive_config import AdaptiveConfig
from core.kia.policy import EffectivePolicy


class TestAdaptiveConfigEffective:
    @staticmethod
    def _make_config(tmp_path):
        policy = EffectivePolicy(db_path=tmp_path / "policy.db")
        return AdaptiveConfig(
            base_config={
                "scoring": {"min_samples_per_dimension": 20},
                "distill": {
                    "trigger_threshold": 0.4,
                    "min_session_fragment_pass_ratio": 0.5,
                },
                "app": {"push_max_items": 3},
                "quality_gate": {"base_threshold": 0.55, "review_margin": 0.15},
                "knowledge_graph": {"freshness_decay_half_life_days": 30},
                "raw_event_store": {"retention_days": 30},
                "document_process": {"max_file_size_mb": 100},
                "intent_router": {"llm_fallback_threshold": 0.65},
                "trust": {"min_delivery_score": 0.55},
            },
            policy=policy,
            db_path=tmp_path / "ac.db",
        )

    def test_high_false_positive_rate_creates_distill_threshold_shadow(self, tmp_path):
        ac = self._make_config(tmp_path)
        # 把 false_positive_rate EWMA 推到高位
        for _ in range(10):
            ac.record_usage("distill", "false_positive_rate", 1.0)

        suggestions = ac.suggest_adjustments()
        assert "distill.trigger_threshold" in suggestions

        applied = ac.apply_adjustments(suggestions)
        assert "distill.trigger_threshold" in applied
        assert ac.policy.get("distill.trigger_threshold") == pytest.approx(
            applied["distill.trigger_threshold"]
        )

    def test_high_push_ignore_rate_creates_push_max_items_shadow(self, tmp_path):
        ac = self._make_config(tmp_path)
        for _ in range(10):
            ac.record_usage("app", "push_ignore_rate", 1.0)

        suggestions = ac.suggest_adjustments()
        assert "app.push_max_items" in suggestions

        applied = ac.apply_adjustments(suggestions)
        assert "app.push_max_items" in applied
        assert ac.policy.get("app.push_max_items") == pytest.approx(applied["app.push_max_items"])

    def test_quality_gate_rejection_rate_adjusts_threshold(self, tmp_path):
        ac = self._make_config(tmp_path)
        for _ in range(10):
            ac.record_usage("quality_gate", "rejection_rate", 1.0)

        suggestions = ac.suggest_adjustments()
        assert "quality_gate.base_threshold" in suggestions
        assert suggestions["quality_gate.base_threshold"]["suggested"] < 0.55

    def test_base_config_loads_from_config_get_paths(self, tmp_path, monkeypatch):
        class FakeConfig:
            values = {
                "scoring.min_samples_per_dimension": 17,
                "distill.trigger_threshold": 0.62,
                "distill.min_session_fragment_pass_ratio": 0.7,
                "app.push_max_items": 4,
                "quality_gate.base_threshold": 0.6,
                "quality_gate.review_margin": 0.2,
                "knowledge_graph.freshness_decay_half_life_days": 45,
                "raw_event_store.retention_days": 60,
                "document_process.max_file_size_mb": 150,
                "intent_router.llm_fallback_threshold": 0.7,
                "trust.min_delivery_score": 0.6,
            }

            def get(self, key, default=None):
                return self.values.get(key, default)

        monkeypatch.setattr("core.kia.adaptive_config.get_config", lambda: FakeConfig())

        policy = EffectivePolicy(db_path=tmp_path / "policy.db")
        ac = AdaptiveConfig(policy=policy, db_path=tmp_path / "ac.db")

        assert ac.base_config["scoring"]["min_samples_per_dimension"] == 17
        assert ac.base_config["distill"]["trigger_threshold"] == pytest.approx(0.62)
        assert ac.base_config["distill"]["min_session_fragment_pass_ratio"] == pytest.approx(0.7)
        assert ac.base_config["app"]["push_max_items"] == 4
        assert ac.base_config["quality_gate"]["base_threshold"] == pytest.approx(0.6)
        assert ac.base_config["quality_gate"]["review_margin"] == pytest.approx(0.2)
        assert ac.base_config["knowledge_graph"]["freshness_decay_half_life_days"] == 45
        assert ac.base_config["raw_event_store"]["retention_days"] == 60
        assert ac.base_config["document_process"]["max_file_size_mb"] == 150
        assert ac.base_config["intent_router"]["llm_fallback_threshold"] == pytest.approx(0.7)
        assert ac.base_config["trust"]["min_delivery_score"] == pytest.approx(0.6)

    def test_policy_summary_reports_active_shadow(self, tmp_path):
        ac = self._make_config(tmp_path)
        ac.policy.set_shadow("quality_gate.base_threshold", 0.5, experiment_id="qg", metric_before=0.4)

        summary = ac.get_policy_summary()

        assert summary["ok"] is True
        assert summary["coverage_count"] == summary["rule_count"]
        assert summary["active_shadow_count"] == 1
        assert summary["active_shadows"][0]["config_key"] == "quality_gate.base_threshold"

    def test_add_rule_custom_metric_participates_in_suggestions(self, tmp_path):
        ac = self._make_config(tmp_path)
        ac.add_rule(
            config_key="app.push_max_items",
            metric="app.experimental_ignore_rate",
            threshold_high=0.5,
            threshold_low=0.1,
            adjust_up=1,
            adjust_down=-1,
            min_value=1,
            max_value=10,
        )

        for _ in range(10):
            ac.record_usage("app", "experimental_ignore_rate", 1.0)

        suggestions = ac.suggest_adjustments()
        assert "app.push_max_items" in suggestions
        assert suggestions["app.push_max_items"]["metric"] == "app.experimental_ignore_rate"

    def test_custom_rules_load_from_runtime_config(self, tmp_path, monkeypatch):
        class FakeConfig:
            values = {
                "scoring.min_samples_per_dimension": 17,
                "distill.trigger_threshold": 0.62,
                "app.push_max_items": 4,
                "knowledge_graph.freshness_decay_half_life_days": 45,
                "adaptive_config.rules": [
                    {
                        "config_key": "app.push_max_items",
                        "metric": "app.custom_ignore_rate",
                        "threshold_high": 0.5,
                        "threshold_low": 0.1,
                        "adjust_up": 1,
                        "adjust_down": -1,
                        "min_value": 1,
                        "max_value": 10,
                    }
                ],
            }

            def get(self, key, default=None):
                return self.values.get(key, default)

        monkeypatch.setattr("core.kia.adaptive_config.get_config", lambda: FakeConfig())

        policy = EffectivePolicy(db_path=tmp_path / "policy.db")
        ac = AdaptiveConfig(policy=policy, db_path=tmp_path / "ac.db")
        assert any(rule["metric"] == "app.custom_ignore_rate" for rule in ac.rules)

        for _ in range(10):
            ac.record_usage("app", "custom_ignore_rate", 1.0)

        suggestions = ac.suggest_adjustments()
        assert "app.push_max_items" in suggestions
        assert suggestions["app.push_max_items"]["metric"] == "app.custom_ignore_rate"

    def test_runtime_config_without_get_skips_custom_rules(self, tmp_path, monkeypatch):
        """精简配置对象可只提供 database_dir，不应破坏自定义规则加载降级。"""

        class FakeConfig:
            database_dir = tmp_path

        monkeypatch.setattr("core.kia.adaptive_config.get_config", lambda: FakeConfig())

        policy = EffectivePolicy(db_path=tmp_path / "policy.db")
        ac = AdaptiveConfig(policy=policy, db_path=tmp_path / "ac.db")

        assert ac.rules == list(AdaptiveConfig.DEFAULT_RULES)

    def test_add_rule_rejects_invalid_ranges(self, tmp_path):
        ac = self._make_config(tmp_path)

        with pytest.raises(ValueError, match="threshold_low"):
            ac.add_rule("app.push_max_items", "app.bad", 0.1, 0.5, 1, -1, 1, 10)

        with pytest.raises(ValueError, match="min_value"):
            ac.add_rule("app.push_max_items", "app.bad", 0.5, 0.1, 1, -1, 10, 1)

    def test_base_config_and_db_path_do_not_read_runtime_config(self, tmp_path, monkeypatch):
        def fail_get_config():
            raise AssertionError("get_config should not be called")

        monkeypatch.setattr("core.kia.adaptive_config.get_config", fail_get_config)

        policy = EffectivePolicy(db_path=tmp_path / "policy.db")
        ac = AdaptiveConfig(
            base_config={"app": {"push_max_items": 4}},
            policy=policy,
            db_path=tmp_path / "ac.db",
        )

        assert ac.base_config["app"]["push_max_items"] == 4
