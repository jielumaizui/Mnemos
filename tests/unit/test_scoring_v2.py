# -*- coding: utf-8 -*-
"""
评分层 V2 单元测试 — bayesian_scorer / lightweight_nb / fallback / adaptive_scorer_v2
"""

import sqlite3

import pytest
from datetime import datetime


@pytest.fixture(autouse=True)  # noqa
def _patch_get_config_for_scoring_v2_tests(patched_get_config):
    """确保本模块所有测试使用隔离的 fake config，不写入真实 ~/.mnemos。"""
    return patched_get_config


# ==================== bayesian_scorer ====================


class TestBayesianScorerFusion:
    @pytest.fixture
    def bb(self, tmp_path):
        from core.scoring.bayesian_scorer import BayesianScorer

        return BayesianScorer(
            dimensions=["profile", "kg", "distill"],
            db_path=tmp_path / "bb.db",
        )

    def test_fuse_returns_score_between_0_and_1(self, bb):
        score, confidence = bb.fuse(
            "profile",
            rule_prior=0.6,
            ml_likelihood=0.7,
            ml_confidence=0.8,
        )
        assert 0.0 <= score <= 1.0
        assert 0.0 <= confidence <= 1.0

    @pytest.mark.parametrize(
        ("operation", "expected_suffix"),
        (
            (
                lambda scorer: scorer.update_from_ground_truth("profile", 1, confidence=1.0),
                "bayesian_update_from_ground_truth",
            ),
            (
                lambda scorer: scorer.batch_update("kg", [1, 1, 0, 1]),
                "bayesian_batch_update",
            ),
        ),
    )
    def test_legacy_mutations_are_fail_closed(self, bb, operation, expected_suffix):
        before = bb.state_to_dict()
        with pytest.raises(
            PermissionError,
            match=f"training_admission_receipt_required:{expected_suffix}",
        ):
            operation(bb)
        assert bb.state_to_dict() == before

    def test_dimension_status_remains_cold(self, bb):
        status = bb.get_dimension_status("distill")
        assert status["samples"] == 0
        assert status["mean"] == pytest.approx(0.5)


# ==================== lightweight_nb ====================


class TestLightweightComplementNB:
    def test_fit_and_predict(self):
        from core.scoring.lightweight_nb import LightweightComplementNB

        clf = LightweightComplementNB()
        # 使用更明显的区分特征
        X = [
            {"python": 5, "hello": 0},
            {"python": 4, "hello": 1},
            {"python": 0, "hello": 5},
            {"python": 1, "hello": 4},
        ]
        y = [1, 1, 0, 0]
        clf.fit(X, y)

        preds = clf.predict(X)
        # 不要求 100% 准确，但相同类别应大部分一致
        assert preds[0] == preds[1]  # 前两个同类
        assert preds[2] == preds[3]  # 后两个同类
        assert preds[0] != preds[2]  # 两类不同

    def test_partial_fit_incremental(self):
        from core.scoring.lightweight_nb import LightweightComplementNB

        clf = LightweightComplementNB()
        X1 = [{"a": 1}, {"b": 1}]
        y1 = [1, 0]
        clf.partial_fit(X1, y1, classes=[0, 1])

        X2 = [{"a": 2}, {"b": 2}]
        y2 = [1, 0]
        clf.partial_fit(X2, y2)

        assert clf.is_fitted
        assert clf._class_count[1] == 2.0

    def test_predict_proba_sum_to_one(self):
        from core.scoring.lightweight_nb import LightweightComplementNB

        clf = LightweightComplementNB()
        X = [{"x": 1}, {"y": 1}]
        y = [1, 0]
        clf.fit(X, y)

        probs = clf.predict_proba([{"x": 1}])[0]
        assert pytest.approx(sum(probs.values()), abs=0.01) == 1.0

    def test_unfitted_returns_uniform(self):
        from core.scoring.lightweight_nb import LightweightComplementNB

        clf = LightweightComplementNB()
        probs = clf.predict_proba([{"x": 1}])[0]
        assert probs[0] == pytest.approx(0.5)
        assert probs[1] == pytest.approx(0.5)

    def test_serialize_roundtrip(self):
        from core.scoring.lightweight_nb import LightweightComplementNB

        clf = LightweightComplementNB()
        X = [{"a": 5}, {"b": 5}]
        y = [1, 0]
        clf.fit(X, y)

        # roundtrip 前预测
        pred_before = clf.predict([{"a": 1}])

        data = clf.to_dict()
        clf2 = LightweightComplementNB.from_dict(data)
        assert clf2.is_fitted

        # roundtrip 后预测应一致
        pred_after = clf2.predict([{"a": 1}])
        assert pred_before == pred_after


# ==================== fallback ====================


class TestScorerFallback:
    def test_guard_catches_exception_and_returns_rule_score(self):
        from core.scoring.fallback import ScorerFallback

        fb = ScorerFallback()

        def rule_fn():
            return 0.75

        with fb.guard("profile", rule_fn) as try_ml:
            result = try_ml(lambda: (_ for _ in ()).throw(ValueError("ml fail")))

        assert result == 0.75
        assert len(fb.get_events()) == 1

    def test_consecutive_failure_lock(self):
        from core.scoring.fallback import ScorerFallback

        fb = ScorerFallback()
        for _ in range(3):
            fb._record_failure("kg")
        assert fb.should_degrade("kg")

    def test_reset_failure(self):
        from core.scoring.fallback import ScorerFallback

        fb = ScorerFallback()
        fb._record_failure("sync")
        fb.reset_failure("sync")
        assert not fb.should_degrade("sync")


# ==================== adaptive_scorer_v2 ====================


class TestAdaptiveScorerV2:
    def test_score_returns_scorecard(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        scorer = AdaptiveScorerV2()
        result = scorer.score(
            {"content": "Hello world", "frontmatter": {"heat": 0.8}},
            dimensions=["profile", "sync"],
        )

        assert "profile" in result.scores
        assert "sync" in result.scores
        assert 0.0 <= result.scores["profile"] <= 1.0
        assert result.features["content_len"] == 11

    def test_extract_features_from_string(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        scorer = AdaptiveScorerV2()
        features = scorer._extract_features("Test content with # header")
        assert features["content"] == "Test content with # header"
        assert features["header_count"] == 1

    def test_get_status_is_cold_without_governed_evidence(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        status = AdaptiveScorerV2().get_status()
        assert status["domain"] == "mnemos"
        assert status["version"] == "v2-full"
        assert status["mode"] == "cold"
        assert status["ready_samples"] == 0

    def test_legacy_writers_are_fail_closed(self, tmp_path):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        db = tmp_path / "scorer.db"
        with pytest.raises(
            PermissionError,
            match="training_admission_receipt_required:insert_ground_truth",
        ):
            AdaptiveScorerV2.insert_ground_truth(
                session_id="s1",
                signal_type="search_hit",
                label=1,
                db_path=db,
            )
        with pytest.raises(
            PermissionError,
            match="training_admission_receipt_required:enqueue_training_sample",
        ):
            AdaptiveScorerV2.enqueue_training_sample(
                session_id="push-1",
                dimension="engagement",
                features={"topic": "redis"},
                expected_score=0.8,
                source="test",
                db_path=str(db),
            )
        assert not db.exists()


# ==================== 新增：frontmatter 数值归一化 ====================


class TestFrontmatterNormalization:
    """测试 _normalize_frontmatter_value 对各种输入形态的归一化。"""

    def test_enum_strings(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        n = AdaptiveScorerV2._normalize_frontmatter_value
        assert n("hot", "heat") == pytest.approx(0.9)
        assert n("warm", "heat") == pytest.approx(0.6)
        assert n("cold", "heat") == pytest.approx(0.3)
        assert n("high", "quality_score") == pytest.approx(0.85)
        assert n("medium", "quality_score") == pytest.approx(0.55)
        assert n("low", "quality_score") == pytest.approx(0.25)
        assert n("critical", "heat") == pytest.approx(0.95)
        assert n("normal", "heat") == pytest.approx(0.5)

    def test_numeric_strings(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        n = AdaptiveScorerV2._normalize_frontmatter_value
        assert n("0.8", "heat") == pytest.approx(0.8)
        assert n("1.0", "heat") == pytest.approx(1.0)
        assert n("0", "heat") == pytest.approx(0.0)

    def test_percentage_strings(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        n = AdaptiveScorerV2._normalize_frontmatter_value
        assert n("80%", "heat") == pytest.approx(0.8)
        assert n("100%", "heat") == pytest.approx(1.0)
        assert n("0%", "heat") == pytest.approx(0.0)

    def test_0_to_100_values(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        n = AdaptiveScorerV2._normalize_frontmatter_value
        # 0-100 分值自动 /100
        assert n(100, "heat") == pytest.approx(1.0)
        assert n(80, "quality_score") == pytest.approx(0.8)
        assert n(50, "confidence") == pytest.approx(0.5)
        assert n(0, "heat") == pytest.approx(0.0)

    def test_0_to_1_values_preserved(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        n = AdaptiveScorerV2._normalize_frontmatter_value
        assert n(0.8, "heat") == pytest.approx(0.8)
        assert n(0.25, "quality_score") == pytest.approx(0.25)

    def test_booleans(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        n = AdaptiveScorerV2._normalize_frontmatter_value
        assert n(True, "heat") == pytest.approx(1.0)
        assert n(False, "heat") == pytest.approx(0.0)

    def test_invalid_returns_none(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        n = AdaptiveScorerV2._normalize_frontmatter_value
        assert n("unknown", "heat") is None
        assert n(None, "heat") is None
        assert n([1, 2, 3], "heat") is None
        assert n("", "heat") is None

    def test_extract_features_applies_normalization(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        scorer = AdaptiveScorerV2()
        features = scorer._extract_features(
            {
                "content": "test",
                "frontmatter": {
                    "heat": "hot",
                    "quality_score": 85,
                    "confidence": "75%",
                    "priority": "0.9",
                },
            }
        )
        assert features["fm_heat"] == pytest.approx(0.9)
        assert features["fm_quality_score"] == pytest.approx(0.85)
        assert features["fm_confidence"] == pytest.approx(0.75)
        assert features["fm_priority"] == pytest.approx(0.9)

    def test_rule_score_uses_normalized_values(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        scorer = AdaptiveScorerV2()
        # heat=hot(0.9), quality_score=100(1.0) => (0.9*0.5 + 1.0*0.5) = 0.95
        features = scorer._extract_features(
            {
                "content": "test",
                "frontmatter": {"heat": "hot", "quality_score": 100},
            }
        )
        score, conf = scorer._rule_score("l1_storage", None, features)
        assert score == pytest.approx(0.95, abs=0.01)

        # heat=cold(0.3), quality_score=0(0.0) => 0.5 + 0.3*0.22 + 0.0*0.25 = 0.566
        features2 = scorer._extract_features(
            {
                "content": "test",
                "frontmatter": {"heat": "cold", "quality_score": 0},
            }
        )
        score2, _ = scorer._rule_score("l1_storage", None, features2)
        assert score2 == pytest.approx(0.566, abs=0.01)


# ==================== 新增：配置深合并 ====================


class TestConfigDeepMerge:
    def test_deep_merge_preserves_untouched_nested_keys(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        # 用户只覆盖 training.min_samples_per_dimension，training 其他键保留
        user = {"training": {"min_samples_per_dimension": 50}}
        cfg = AdaptiveScorerV2._load_config(user)

        assert cfg["training"]["min_samples_per_dimension"] == 50
        assert cfg["training"]["min_confidence"] == 0.7  # 默认值保留
        assert cfg["training"]["max_queue_size"] == 500  # 默认值保留

    def test_user_overrides_yaml(self, monkeypatch):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        def _patched_load(user_config):
            defaults = {
                "backend": "standard",
                "training": {"min_samples_per_dimension": 20, "min_confidence": 0.7},
                "bayesian": {"rule_weight_cold": 3.0, "rule_weight_warm": 1.5},
                "fallback": {"max_consecutive_failures": 3},
                "persistence": {"format": "joblib"},
                "dimensions": {"profile": True},
            }
            yaml_cfg = {"bayesian": {"rule_weight_cold": 5.0}}
            merged = AdaptiveScorerV2._deep_merge(defaults, yaml_cfg)
            if user_config:
                merged = AdaptiveScorerV2._deep_merge(merged, user_config)
            return merged

        monkeypatch.setattr(AdaptiveScorerV2, "_load_config", staticmethod(_patched_load))

        # 用户再覆盖 rule_weight_cold = 1.0
        cfg = AdaptiveScorerV2._load_config({"bayesian": {"rule_weight_cold": 1.0}})
        assert cfg["bayesian"]["rule_weight_cold"] == 1.0
        assert cfg["bayesian"]["rule_weight_warm"] == 1.5  # yaml 未覆盖，保留

    def test_validation_called_on_init(self, monkeypatch):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        calls = []
        AdaptiveScorerV2.validate_scorer_config

        def mock_validate(cfg):
            calls.append(cfg)
            return ["mock_error"]  # 返回非空，验证仅记录警告

        monkeypatch.setattr(AdaptiveScorerV2, "validate_scorer_config", staticmethod(mock_validate))
        # 同时 mock _load_all_models 避免 DB 依赖
        monkeypatch.setattr(AdaptiveScorerV2, "_load_all_models", lambda self: None)

        AdaptiveScorerV2()
        assert len(calls) == 1
        assert calls[0]["backend"] in ("standard", "lightweight")


# ==================== 新增：模型加载安全护栏 ====================


class TestModelLoadSecurity:
    """Legacy model blobs are historical migration inputs, never runtime models."""

    @pytest.fixture
    def db_with_schema(self, tmp_path):
        db = tmp_path / "secure.db"
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                """
                CREATE TABLE scorer_models (
                    id INTEGER PRIMARY KEY,
                    dimension TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    model_type TEXT,
                    model_blob BLOB,
                    model_hash TEXT,
                    train_samples INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT,
                    meta_json TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO scorer_models (
                    dimension, model_version, model_type, model_blob, model_hash,
                    is_active, created_at, meta_json
                ) VALUES ('profile', 'v1', 'pickle', X'8004', 'legacy', 1, ?, '{}')
                """,
                (datetime.now().isoformat(),),
            )
            conn.commit()
        return db

    def test_constructor_does_not_deserialize_or_activate_legacy_model(self, db_with_schema):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        scorer = AdaptiveScorerV2(db_path=str(db_with_schema))
        assert scorer._models == {}
        assert scorer._model_versions == {}

    def test_legacy_load_and_save_are_fail_closed(self, db_with_schema):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        scorer = AdaptiveScorerV2(db_path=str(db_with_schema))
        with pytest.raises(
            PermissionError,
            match="training_admission_receipt_required:load_model",
        ):
            scorer.load_model("profile")
        with pytest.raises(
            PermissionError,
            match="training_admission_receipt_required:save_model",
        ):
            scorer.save_model("profile")


# ==================== 新增：强制 sklearn 不可用时的纯 Python fallback ====================


class TestSklearnUnavailableFallback:
    def test_lightweight_backend_trains_and_predicts(self, monkeypatch):
        """Pure-Python scoring remains available without legacy persistence."""
        from core.scoring import adaptive_scorer_v2 as asv2
        from core.scoring.lightweight_nb import LightweightComplementNB

        monkeypatch.setattr(asv2, "_SKLEARN_AVAILABLE", False)
        scorer = asv2.AdaptiveScorerV2(config={"backend": "lightweight"})
        assert scorer.config["backend"] == "lightweight"

        classifier = LightweightComplementNB()
        features = [
            {"python": 5, "hello": 0},
            {"python": 4, "hello": 1},
            {"python": 0, "hello": 5},
            {"python": 1, "hello": 4},
        ]
        labels = [1, 1, 0, 0]
        classifier.fit(features, labels)

        predictions = classifier.predict(features)
        assert predictions[0] == predictions[1]
        assert predictions[2] == predictions[3]
        assert predictions[0] != predictions[2]
        probabilities = classifier.predict_proba([{"python": 5, "hello": 0}])[0]
        assert pytest.approx(sum(probabilities.values()), abs=0.01) == 1.0

        scorer._models["profile"] = classifier
        with pytest.raises(
            PermissionError,
            match="training_admission_receipt_required:save_model",
        ):
            scorer.save_model("profile", note="legacy-persistence-is-retired")

    def test_rule_score_works_without_sklearn(self, monkeypatch):
        """即使 sklearn 不可用，rule_score 仍应正常工作。"""
        from core.scoring import adaptive_scorer_v2 as asv2

        monkeypatch.setattr(asv2, "_SKLEARN_AVAILABLE", False)

        scorer = asv2.AdaptiveScorerV2()
        result = scorer.score(
            {"content": "test content", "frontmatter": {"heat": "warm"}},
            dimensions=["profile"],
        )
        assert 0.0 <= result.scores["profile"] <= 1.0
        assert result.model_version.startswith("v2")


class TestSklearnPartialFit:
    """[P1-3] 验证 standard backend 的 SklearnPartialFitNB 真正支持增量学习。"""

    def test_sklearn_partial_fit_incremental(self, monkeypatch):
        pytest.importorskip("sklearn")
        from core.scoring.adaptive_scorer_v2 import SklearnPartialFitNB

        # 第一批训练
        model = SklearnPartialFitNB()
        X1 = [{"a": 1, "b": 0}, {"a": 0, "b": 1}]
        y1 = [0, 1]
        model.fit(X1, y1)
        preds1 = [model.predict([{"a": 1, "b": 0}])[0], model.predict([{"a": 0, "b": 1}])[0]]

        # 第二批增量训练（加入新特征 c，验证旧特征不影响）
        X2 = [{"a": 1, "b": 0, "c": 5}, {"a": 0, "b": 1, "c": 0}]
        y2 = [0, 1]
        model.partial_fit(X2, y2)

        # 对旧样本的预测不应大幅退化
        preds_after = [model.predict([{"a": 1, "b": 0}])[0], model.predict([{"a": 0, "b": 1}])[0]]
        assert preds_after == preds1

        # predict_proba 输出有效概率
        proba = model.predict_proba([{"a": 1, "b": 0}])[0]
        assert len(proba) == 2
        assert pytest.approx(sum(proba), abs=0.01) == 1.0

    def test_sklearn_partial_fit_survives_pickle(self, tmp_path):
        pytest.importorskip("sklearn")
        import pickle
        from core.scoring.adaptive_scorer_v2 import SklearnPartialFitNB

        model = SklearnPartialFitNB()
        model.fit([{"x": 1}, {"x": 2}], [0, 1])

        blob = pickle.dumps(model)
        restored = pickle.loads(blob)

        assert restored.is_fitted
        restored.partial_fit([{"x": 3}], [1])
        pred = restored.predict([{"x": 3}])
        assert pred[0] == 1
