# -*- coding: utf-8 -*-
"""Tests for core.app.dispute_scorer."""

from __future__ import annotations


import json

import pytest

from core.app.dispute_scorer import (
    AdaptiveWeightLearner,
    DisputeScorer,
    RelationFeatures,
    _DEFAULT_WEIGHTS,
)


@pytest.fixture
def scorer_config():
    return {
        "enabled": True,
        "max_daily_disputes": 10,
        "min_conflict_strength": 0.5,
        "auto_resolve_min_gap": 0.30,
        "merge_min_gap": 0.15,
        "freshness_half_life_days": 30,
        "citation_max_reference": 20,
        "weights": {
            "confidence": 0.25,
            "freshness": 0.25,
            "citation": 0.20,
            "quality": 0.15,
            "source": 0.10,
            "core": 0.05,
        },
        "adaptive_learning": {
            "enabled": False,
        },
    }


@pytest.fixture
def scorer(tmp_path, scorer_config):
    return DisputeScorer(wiki_dir=tmp_path, config=scorer_config)


class TestRelationFeatures:
    def test_composite_score_with_default_weights(self, scorer):
        features = RelationFeatures(
            confidence=1.0,
            freshness=1.0,
            citation=1.0,
            quality=1.0,
            source=1.0,
            core=1.0,
        )
        assert scorer.composite_score(features) == 1.0

    def test_composite_score_zero(self, scorer):
        features = RelationFeatures(
            confidence=0.0,
            freshness=0.0,
            citation=0.0,
            quality=0.0,
            source=0.0,
            core=0.0,
        )
        assert scorer.composite_score(features) == 0.0


class TestDisputeScorerDecision:
    def test_skip_when_conflict_strength_below_threshold(self, scorer):
        fa = RelationFeatures(confidence=0.9, freshness=0.9)
        fb = RelationFeatures(confidence=0.1, freshness=0.1)
        action, ctx = scorer.decide(fa, fb, conflict_strength=0.2, pair_key="k")
        assert action == "skip"

    def test_auto_resolve_when_gap_large(self, scorer):
        fa = RelationFeatures(confidence=0.9, freshness=0.9, quality=0.9)
        fb = RelationFeatures(confidence=0.1, freshness=0.1, quality=0.1)
        action, ctx = scorer.decide(fa, fb, conflict_strength=0.8, pair_key="k")
        assert action == "auto_resolve"
        assert ctx["winner"] == "a"

    def test_merge_when_gap_medium(self, scorer):
        fa = RelationFeatures(confidence=0.8, freshness=0.8, quality=0.8)
        fb = RelationFeatures(confidence=0.5, freshness=0.5, quality=0.5)
        action, ctx = scorer.decide(fa, fb, conflict_strength=0.8, pair_key="k")
        assert action == "merge"

    def test_create_dispute_when_close(self, scorer):
        fa = RelationFeatures(confidence=0.55, freshness=0.55)
        fb = RelationFeatures(confidence=0.50, freshness=0.50)
        action, ctx = scorer.decide(fa, fb, conflict_strength=0.8, pair_key="k")
        assert action == "create_dispute"


class TestDisputeScorerFreshness:
    def test_freshness_score_one_for_recent(self, scorer):
        assert scorer._freshness_score(0) == pytest.approx(1.0)

    def test_freshness_score_decays_over_time(self, scorer):
        assert scorer._freshness_score(30) == pytest.approx(0.3679, abs=0.01)


class TestAdaptiveWeightLearner:
    def test_disabled_by_default(self, tmp_path):
        learner = AdaptiveWeightLearner(
            {"adaptive_learning": {"enabled": False}},
            tmp_path / "weights.json",
        )
        assert not learner.enabled
        assert learner.current_weights(_DEFAULT_WEIGHTS()) == _DEFAULT_WEIGHTS()

    def test_record_feedback_creates_jsonl(self, tmp_path):
        learner = AdaptiveWeightLearner(
            {"adaptive_learning": {"enabled": True}},
            tmp_path / "weights.json",
        )
        fa = RelationFeatures(confidence=0.9)
        fb = RelationFeatures(confidence=0.1)
        learner.record_feedback("pair", fa, fb, "auto_resolve", "a")
        feedback_path = tmp_path / "weights.feedback.jsonl"
        assert feedback_path.exists()
        assert "pair" in feedback_path.read_text(encoding="utf-8")

    def test_learning_updates_weights(self, tmp_path):
        cfg = {
            "adaptive_learning": {
                "enabled": True,
                "min_samples_before_update": 1,
                "learning_rate": 0.5,
                "max_weight": 0.60,
                "min_weight": 0.05,
            }
        }
        learner = AdaptiveWeightLearner(cfg, tmp_path / "weights.json")
        fa = RelationFeatures(confidence=1.0, freshness=0.0)
        fb = RelationFeatures(confidence=0.0, freshness=1.0)
        # 构造系统选 a、实际为 b 的反馈，让学习器降低 confidence、提升 freshness
        for _ in range(3):
            learner.record_feedback("pair", fa, fb, "auto_resolve", "b")
        new_weights = learner.learn()
        assert new_weights is not None
        # confidence 应该下降，freshness 应该上升
        assert new_weights["confidence"] < new_weights["freshness"]

    def test_learning_disabled_returns_none(self, tmp_path):
        learner = AdaptiveWeightLearner(
            {"adaptive_learning": {"enabled": False}}, tmp_path / "weights.json"
        )
        assert learner.learn() is None

    def test_learning_insufficient_samples_returns_none(self, tmp_path):
        cfg = {
            "adaptive_learning": {
                "enabled": True,
                "min_samples_before_update": 5,
            }
        }
        learner = AdaptiveWeightLearner(cfg, tmp_path / "weights.json")
        fa = RelationFeatures(confidence=1.0)
        fb = RelationFeatures(confidence=0.0)
        learner.record_feedback("pair", fa, fb, "auto_resolve", "a")
        assert learner.learn() is None

    def test_find_page_metrics_missing_returns_none(self, scorer):
        assert scorer._find_page_metrics("nonexistent/page.md") is None


class TestStateWeights:
    def test_state_weights_override_config_weights(self, tmp_path, scorer_config):
        """state 文件中的权重要覆盖 config 权重。"""
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_path = state_dir / "dispute_weights.json"
        state_path.write_text(
            '{"weights": {"confidence": 0.01, "freshness": 0.50}}', encoding="utf-8"
        )
        scorer = DisputeScorer(wiki_dir=tmp_path, config=scorer_config, state_dir=state_dir)
        # state 权重与 config 合并后归一化
        assert scorer.weights["confidence"] == pytest.approx(0.01 / 1.01, abs=1e-6)
        assert scorer.weights["freshness"] == pytest.approx(0.50 / 1.01, abs=1e-6)

    def test_state_weights_partial_merge_with_defaults(self, tmp_path, scorer_config):
        """state 文件部分覆盖时，其余维度保持 config 默认值。"""
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_path = state_dir / "dispute_weights.json"
        state_path.write_text('{"weights": {"citation": 0.40}}', encoding="utf-8")
        scorer = DisputeScorer(wiki_dir=tmp_path, config=scorer_config, state_dir=state_dir)
        # 0.4 与默认值合并后归一化 (总和 1.2)
        assert scorer.weights["citation"] == pytest.approx(0.40 / 1.2, abs=1e-6)
        assert scorer.weights["confidence"] == pytest.approx(0.25 / 1.2, abs=1e-6)

    def test_save_weights_creates_state_file(self, tmp_path, scorer_config):
        """save_weights() 应将权重持久化到 state 文件。"""
        state_dir = tmp_path / "state"
        scorer = DisputeScorer(wiki_dir=tmp_path, config=scorer_config, state_dir=state_dir)
        scorer.save_weights({"confidence": 0.40, "freshness": 0.30})
        state_path = state_dir / "dispute_weights.json"
        assert state_path.exists()
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        weights = saved["weights"]
        # 保存时自动补齐并归一化
        assert weights["confidence"] == pytest.approx(0.40 / 1.2, abs=1e-6)
        assert weights["freshness"] == pytest.approx(0.30 / 1.2, abs=1e-6)

    def test_reset_weights_removes_state_file(self, tmp_path, scorer_config):
        """reset_weights() 应删除 state 文件并回退到 config 权重。"""
        state_dir = tmp_path / "state"
        scorer = DisputeScorer(wiki_dir=tmp_path, config=scorer_config, state_dir=state_dir)
        scorer.save_weights({"confidence": 0.99})
        assert (state_dir / "dispute_weights.json").exists()
        scorer.reset_weights()
        assert not (state_dir / "dispute_weights.json").exists()
        assert scorer.weights["confidence"] == pytest.approx(0.25)
