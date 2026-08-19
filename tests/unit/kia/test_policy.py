# -*- coding: utf-8 -*-
"""EffectivePolicy 单元测试。"""

import pytest

from core.kia.policy import EffectivePolicy


class TestEffectivePolicy:
    @pytest.fixture
    def isolated_policy(self, tmp_path, monkeypatch):
        """使用独立 Config 与独立 DB 的 EffectivePolicy。"""
        from core.config import Config

        cfg = Config(config_path=tmp_path / "main.json")
        monkeypatch.setattr("core.kia.policy.get_config", lambda: cfg)
        policy = EffectivePolicy(db_path=tmp_path / "policy.db")
        return policy, cfg

    def test_shadow_overrides_config(self, isolated_policy):
        policy, _cfg = isolated_policy
        key = "distill.trigger_threshold"
        assert policy.get(key) == pytest.approx(0.4)
        policy.set_shadow(key, 0.9, experiment_id="exp-1")
        assert policy.get(key) == pytest.approx(0.9)

    def test_commit_writes_back_to_config(self, isolated_policy):
        policy, cfg = isolated_policy
        policy.set_shadow(
            "app.push_max_items", 5, experiment_id="app.push_max_items", metric_before=0.6
        )
        committed = policy.commit_or_rollback("app.push_max_items", metric_after=0.2)
        assert committed is True
        assert cfg.get("app.push_max_items") == 5
        assert policy.get("app.push_max_items") == 5
        assert policy.list_shadows() == {}

    def test_rollback_removes_shadow(self, isolated_policy):
        policy, cfg = isolated_policy
        policy.set_shadow(
            "app.push_max_items", 5, experiment_id="app.push_max_items", metric_before=0.1
        )
        committed = policy.commit_or_rollback("app.push_max_items", metric_after=0.9)
        assert committed is False
        assert policy.list_shadows() == {}
        # rollback 不应写入全局 Config
        assert cfg.get("app.push_max_items", 3) == 3

    def test_force_commit_writes_shadow_without_metric_decision(self, isolated_policy):
        policy, cfg = isolated_policy

        policy.set_shadow("app.push_max_items", 6, experiment_id="manual-commit")

        assert policy.force_commit("manual-commit") is True
        assert cfg.get("app.push_max_items") == 6
        assert policy.list_shadows() == {}
        assert policy.force_commit("manual-commit") is False

    def test_force_rollback_discards_shadow_without_config_write(self, isolated_policy):
        policy, cfg = isolated_policy

        policy.set_shadow("app.push_max_items", 6, experiment_id="manual-rollback")

        assert policy.force_rollback("manual-rollback") is True
        assert cfg.get("app.push_max_items", 3) == 3
        assert policy.list_shadows() == {}
        assert policy.force_rollback("manual-rollback") is False

    def test_shadows_survive_restart(self, tmp_path, monkeypatch):
        from core.config import Config

        cfg = Config(config_path=tmp_path / "main.json")
        monkeypatch.setattr("core.kia.policy.get_config", lambda: cfg)
        db = tmp_path / "policy.db"
        policy = EffectivePolicy(db_path=db)
        policy.set_shadow("distill.trigger_threshold", 0.77, experiment_id="exp-restart")

        policy2 = EffectivePolicy(db_path=db)
        assert policy2.get("distill.trigger_threshold", 0.5) == pytest.approx(0.77)
