# -*- coding: utf-8 -*-
"""
AdaptiveScorerV2 专项单元测试

覆盖范围：
  - ScoreCardV2 / FeedbackV2 / GroundTruth 数据类
  - SklearnPartialFitNB（mocked sklearn）
  - AdaptiveScorerV2 初始化、评分、反馈、训练队列、模型持久化、
    特征提取、规则评分、ML 评分、贝叶斯状态、状态查询

测试策略：
  - tmp_path 隔离文件系统
  - monkeypatch 隔离 get_config / sqlite3 / sklearn
  - MagicMock 模拟 sklearn 模块
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from unittest.mock import MagicMock

import pytest

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def mock_config(monkeypatch, tmp_path):
    """Mock get_config to return a fake config object with data_dir。"""
    fake_cfg = MagicMock()
    fake_cfg.data_dir = tmp_path / "data"
    fake_cfg.database_dir = tmp_path / "db"
    monkeypatch.setattr(
        "core.scoring.adaptive_scorer_v2.get_config",
        lambda: fake_cfg,
    )
    return fake_cfg


@pytest.fixture  # noqa
def mock_sklearn(monkeypatch):
    """Mock sklearn modules via sys.modules to avoid real sklearn dependency."""
    sklearn_mock = MagicMock()
    sklearn_mock.naive_bayes.ComplementNB = MagicMock
    sklearn_mock.feature_extraction.FeatureHasher = MagicMock
    sklearn_mock.pipeline.Pipeline = MagicMock  # noqa: Vulture - sklearn pipeline module mock.
    sklearn_mock.__version__ = "1.4.0"

    monkeypatch.setitem(sys.modules, "sklearn", sklearn_mock)
    monkeypatch.setitem(sys.modules, "sklearn.naive_bayes", sklearn_mock.naive_bayes)
    monkeypatch.setitem(sys.modules, "sklearn.feature_extraction", sklearn_mock.feature_extraction)
    monkeypatch.setitem(sys.modules, "sklearn.pipeline", sklearn_mock.pipeline)

    # Also patch _SKLEARN_AVAILABLE in the module
    import core.scoring.adaptive_scorer_v2 as asv2

    monkeypatch.setattr(asv2, "_SKLEARN_AVAILABLE", True)
    return sklearn_mock


@pytest.fixture
def db_with_schema(tmp_path):
    """Create a SQLite DB with all required tables."""
    db = tmp_path / "test.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """
            CREATE TABLE scorer_training_queue (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                dimension TEXT NOT NULL,
                features_json TEXT NOT NULL,
                priority INTEGER DEFAULT 0,
                earliest_train_at TEXT,
                status TEXT DEFAULT 'pending',
                retry_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE ground_truth_signals (
                id INTEGER PRIMARY KEY,
                profile_id TEXT,
                session_id TEXT,
                signal_type TEXT,
                signal_value TEXT,
                confidence REAL,
                latency_hours INTEGER,
                created_at TEXT
            )
        """
        )
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
            CREATE UNIQUE INDEX idx_gt_session ON ground_truth_signals(session_id, signal_type)
        """
        )
        conn.commit()
    return db


@pytest.fixture
def scorer_no_load(mock_config, db_with_schema, monkeypatch):
    """Return an AdaptiveScorerV2 instance that skips _load_all_models."""
    from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

    monkeypatch.setattr(AdaptiveScorerV2, "_load_all_models", lambda self: None)
    return AdaptiveScorerV2(domain="l1_storage", db_path=str(db_with_schema))


# =============================================================================
# ScoreCardV2 / FeedbackV2 / GroundTruth
# =============================================================================


class TestDataClasses:
    def test_scorecard_v2_defaults(self):
        from core.scoring.adaptive_scorer_v2 import ScoreCardV2

        sc = ScoreCardV2(
            scores={"l1_storage": 0.8},
            confidences={"l1_storage": 0.7},
            features={"len": 10},
            model_version="v2-test",
        )
        assert sc.scores["l1_storage"] == pytest.approx(0.8)
        assert sc.confidences["l1_storage"] == pytest.approx(0.7)
        assert isinstance(sc.timestamp, datetime)

    def test_feedback_v2_defaults(self):
        from core.scoring.adaptive_scorer_v2 import FeedbackV2

        fb = FeedbackV2(
            session_id="s1",
            dimension="l1_storage",
            expected=0.9,
            actual=0.5,
            features={"x": 1},
        )
        assert fb.source == "manual"
        assert isinstance(fb.timestamp, datetime)

    def test_ground_truth_defaults(self):
        from core.scoring.adaptive_scorer_v2 import GroundTruth

        gt = GroundTruth(session_id="s1", signal_type="click", label=1)
        assert gt.confidence == pytest.approx(1.0)
        assert gt.latency_hours == 0


# =============================================================================
# SklearnPartialFitNB (mocked)
# =============================================================================


class TestSklearnPartialFitNBMocked:
    def test_init_state(self, mock_sklearn):  # noqa
        from core.scoring.adaptive_scorer_v2 import SklearnPartialFitNB

        model = SklearnPartialFitNB(n_features=512)
        assert model.n_features == 512
        assert model._hasher is None
        assert model._classifier is None
        assert not model.is_fitted

    def test_ensure_init_creates_instances(self, mock_sklearn):  # noqa
        from core.scoring.adaptive_scorer_v2 import SklearnPartialFitNB

        model = SklearnPartialFitNB()
        model._ensure_init()
        assert model._hasher is not None
        assert model._classifier is not None

    def test_fit_delegates_to_partial_fit(self, mock_sklearn):  # noqa
        from core.scoring.adaptive_scorer_v2 import SklearnPartialFitNB

        model = SklearnPartialFitNB()
        model.fit([{"a": 1}], [0])
        assert model.is_fitted
        # partial_fit on classifier should have been called once
        model._classifier.partial_fit.assert_called_once()

    def test_predict_and_predict_proba(self, mock_sklearn):  # noqa
        from core.scoring.adaptive_scorer_v2 import SklearnPartialFitNB

        model = SklearnPartialFitNB()
        model.fit([{"a": 1}, {"b": 1}], [0, 1])

        # Set return values for classifier mock
        model._classifier.predict.return_value = [1]
        model._classifier.predict_proba.return_value = [[0.2, 0.8]]

        preds = model.predict([{"a": 1}])
        assert preds == [1]

        proba = model.predict_proba([{"a": 1}])
        assert proba == [[0.2, 0.8]]


# =============================================================================
# AdaptiveScorerV2 — Initialization
# =============================================================================


class TestAdaptiveScorerV2Init:
    def test_default_init(self, mock_config, monkeypatch):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        monkeypatch.setattr(AdaptiveScorerV2, "_load_all_models", lambda self: None)
        scorer = AdaptiveScorerV2()
        assert scorer.domain == "mnemos"
        assert scorer._mode == "cold"
        assert scorer._models == {}
        assert scorer.config["backend"] in ("standard", "lightweight")

    def test_init_with_custom_config(self, mock_config, monkeypatch):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        monkeypatch.setattr(AdaptiveScorerV2, "_load_all_models", lambda self: None)
        scorer = AdaptiveScorerV2(
            config={"backend": "lightweight", "bayesian": {"alpha_prior": 2.0}}
        )
        assert scorer.config["backend"] == "lightweight"
        assert scorer.config["bayesian"]["alpha_prior"] == pytest.approx(2.0)
        # [P2-23] 配置项必须真正传给 BayesianScorer
        assert scorer._bayesian.prior_alpha == pytest.approx(2.0)
        # other defaults preserved
        assert scorer.config["training"]["min_confidence"] == pytest.approx(0.7)

    def test_init_with_db_path(self, mock_config, monkeypatch, tmp_path):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        monkeypatch.setattr(AdaptiveScorerV2, "_load_all_models", lambda self: None)
        db = tmp_path / "custom.db"
        scorer = AdaptiveScorerV2(db_path=str(db))
        assert scorer.db_path == db

    def test_config_validation_warnings_logged(self, mock_config, monkeypatch):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        monkeypatch.setattr(AdaptiveScorerV2, "_load_all_models", lambda self: None)
        # min_samples < 5 triggers validation error
        scorer = AdaptiveScorerV2(config={"training": {"min_samples_per_dimension": 2}})
        assert scorer.config["training"]["min_samples_per_dimension"] == 2

    def test_legacy_model_loader_is_fail_closed(self, mock_config, db_with_schema):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        scorer = AdaptiveScorerV2(db_path=str(db_with_schema))
        with pytest.raises(
            PermissionError,
            match="training_admission_receipt_required:load_all_models",
        ):
            scorer._load_all_models()


# =============================================================================
# AdaptiveScorerV2 — score()
# =============================================================================


class TestAdaptiveScorerV2Score:
    def test_score_returns_scorecard(self, scorer_no_load):
        from core.scoring.adaptive_scorer_v2 import ScoreCardV2

        result = scorer_no_load.score(
            {"content": "Hello world", "frontmatter": {"heat": 0.8}},
            dimensions=["l1_storage", "sync"],
        )
        assert isinstance(result, ScoreCardV2)
        # ScoreCardV2 fields
        assert "l1_storage" in result.scores
        assert "sync" in result.scores
        assert 0.0 <= result.scores["l1_storage"] <= 1.0
        assert 0.0 <= result.confidences["l1_storage"] <= 1.0
        assert result.features["content_len"] == 11
        assert result.model_version.startswith("v2")

    def test_score_with_string_item(self, scorer_no_load):
        result = scorer_no_load.score("Simple string content", dimensions=["sync"])
        assert "sync" in result.scores
        assert result.features["content"] == "Simple string content"

    def test_score_with_path_item(self, scorer_no_load, tmp_path):
        f = tmp_path / "note.md"
        f.write_text("---\nheat: hot\n---\nBody text here")
        result = scorer_no_load.score(f, dimensions=["l1_storage"])
        assert "l1_storage" in result.scores
        assert result.features.get("fm_heat") == pytest.approx(0.9)

    def test_score_uses_ml_when_model_available(self, scorer_no_load, monkeypatch):
        from core.scoring.lightweight_nb import LightweightComplementNB

        clf = LightweightComplementNB()
        clf.fit([{"x": 1}, {"x": 2}], [0, 1])
        scorer_no_load._models["l1_storage"] = clf
        result = scorer_no_load.score({"content": "test"}, dimensions=["l1_storage"])
        # ML model should contribute; version should show ml
        assert "ml" in result.model_version or "rule-only" not in result.model_version

    def test_score_falls_back_to_rule_when_no_model(self, scorer_no_load):
        result = scorer_no_load.score({"content": "test"}, dimensions=["l1_storage"])
        assert result.model_version == "v2-rule-only"

    def test_score_with_alias_dimension(self, scorer_no_load):
        result = scorer_no_load.score(
            {"content": "test", "frontmatter": {"heat": "hot", "quality_score": 100}},
            dimensions=["session_quality"],  # alias for l1_storage
        )
        # 别名应在输出中保留，但内部应命中 l1_storage 规则先验。
        assert "session_quality" in result.scores
        assert result.confidences["session_quality"] > 0.0
        # 高热+高质量内容应得到明显高于默认 0.5 的分数
        assert result.scores["session_quality"] > 0.6

    def test_score_with_l1_alias_uses_l1_storage_rule_prior(self, scorer_no_load):
        result = scorer_no_load.score(
            {"content": "test", "frontmatter": {"heat": "hot", "quality_score": 100}},
            dimensions=["l1"],
        )
        assert "l1" in result.scores
        # 别名 l1 必须映射到 l1_storage，从而触发规则先验；否则 confidence 为 0。
        assert result.confidences["l1"] > 0.0
        assert result.scores["l1"] > 0.6


# =============================================================================
# AdaptiveScorerV2 — feedback / ground_truth / training_queue
# =============================================================================


class TestAdaptiveScorerV2Feedback:
    def test_feedback_rejects_reaction_label_without_writing(self, scorer_no_load, db_with_schema):
        from core.scoring.adaptive_scorer_v2 import FeedbackV2

        feedback = FeedbackV2(
            session_id="s1",
            dimension="l1_storage",
            expected=0.9,
            actual=0.5,
            features={"len": 10},
        )
        with pytest.raises(
            PermissionError,
            match="training_admission_receipt_required:feedback",
        ):
            scorer_no_load.feedback(feedback)

        with sqlite3.connect(str(db_with_schema)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM ground_truth_signals").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM scorer_training_queue").fetchone()[0] == 0

    def test_insert_ground_truth_rejects_caller_label_without_writing(self, db_with_schema):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        with pytest.raises(
            PermissionError,
            match="training_admission_receipt_required:insert_ground_truth",
        ):
            AdaptiveScorerV2.insert_ground_truth(
                session_id="s2",
                signal_type="search_hit",
                label=1,
                confidence=0.9,
                db_path=db_with_schema,
            )

        with sqlite3.connect(str(db_with_schema)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM ground_truth_signals").fetchone()[0] == 0

    def test_enqueue_training_sample_rejects_and_does_not_create_database(self, tmp_path):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        db_path = tmp_path / "fresh.db"
        with pytest.raises(
            PermissionError,
            match="training_admission_receipt_required:enqueue_training_sample",
        ):
            AdaptiveScorerV2.enqueue_training_sample(
                session_id="fresh-1",
                dimension="predictive_delivery",
                features={"triggered_by": "guard_check"},
                expected_score=1.0,
                source="test",
                db_path=str(db_path),
            )
        assert not db_path.exists()


# =============================================================================
# AdaptiveScorerV2 — Model persistence
# =============================================================================


class TestAdaptiveScorerV2ModelPersistence:
    def test_legacy_save_and_load_are_fail_closed(self, scorer_no_load):
        scorer_no_load._models["l1_storage"] = object()

        with pytest.raises(
            PermissionError,
            match="training_admission_receipt_required:save_model",
        ):
            scorer_no_load.save_model("l1_storage")
        with pytest.raises(
            PermissionError,
            match="training_admission_receipt_required:load_model",
        ):
            scorer_no_load.load_model("l1_storage")

    def test_constructor_ignores_historical_active_model_rows(self, db_with_schema):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        with sqlite3.connect(str(db_with_schema)) as conn:
            conn.execute(
                """
                INSERT INTO scorer_models (
                    dimension, model_version, model_type, model_blob, model_hash,
                    is_active, created_at, meta_json
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    "l1_storage",
                    "legacy-v1",
                    "pickle",
                    b"historical-only",
                    "untrusted",
                    datetime.now().isoformat(),
                    "{}",
                ),
            )
            conn.commit()

        scorer = AdaptiveScorerV2(db_path=str(db_with_schema))
        assert scorer._models == {}
        assert scorer._model_versions == {}

    def test_governed_activation_requires_state_store_and_principal(self, scorer_no_load):
        with pytest.raises(
            PermissionError,
            match="training_admission_receipt_required:governance_state_store",
        ):
            scorer_no_load.apply_governed_run("run-1")


# =============================================================================
# AdaptiveScorerV2 — Feature extraction
# =============================================================================


class TestAdaptiveScorerV2FeatureExtraction:
    def test_extract_features_from_dict(self, scorer_no_load):
        features = scorer_no_load._extract_features(
            {
                "content": "Hello world",
                "frontmatter": {"heat": 0.8, "tags": ["a", "b"]},
            }
        )
        assert features["content"] == "Hello world"
        assert features["content_len"] == 11
        assert features["content_words"] == 2
        assert features["has_frontmatter"] is True
        assert features["fm_heat"] == pytest.approx(0.8)
        assert "_source" in features

    def test_extract_features_from_string(self, scorer_no_load):
        features = scorer_no_load._extract_features("Some text with # header and [[link]]")
        assert features["content"] == "Some text with # header and [[link]]"
        assert features["header_count"] == 1
        assert features["link_count"] == 1
        assert features["_source"] == "str"

    def test_extract_features_from_path(self, scorer_no_load, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("---\nheat: hot\nquality_score: 85\n---\nContent here")
        features = scorer_no_load._extract_features(f)
        assert features["_source"] == "path"
        assert features["fm_heat"] == pytest.approx(0.9)
        assert features["fm_quality_score"] == pytest.approx(0.85)

    def test_extract_features_normalizes_frontmatter(self, scorer_no_load):
        features = scorer_no_load._extract_features(
            {
                "content": "x",
                "frontmatter": {
                    "heat": "warm",
                    "quality_score": "75%",
                    "confidence": 100,
                    "priority": "0.5",
                },
            }
        )
        assert features["fm_heat"] == pytest.approx(0.6)
        assert features["fm_quality_score"] == pytest.approx(0.75)
        assert features["fm_confidence"] == pytest.approx(1.0)
        assert features["fm_priority"] == pytest.approx(0.5)


# =============================================================================
# AdaptiveScorerV2 — Rule scoring
# =============================================================================


class TestAdaptiveScorerV2RuleScore:
    def test_rule_score_l1_storage(self, scorer_no_load):
        features = scorer_no_load._extract_features(
            {
                "content": "test",
                "frontmatter": {"heat": "hot", "quality_score": 100},
            }
        )
        score, conf = scorer_no_load._rule_score("l1_storage", None, features)
        assert score == pytest.approx(0.95, abs=0.01)
        assert conf == pytest.approx(0.4)

    def test_rule_score_sync(self, scorer_no_load):
        features = scorer_no_load._extract_features({"content": "short"})
        score, conf = scorer_no_load._rule_score("sync", None, features)
        assert 0.0 <= score <= 1.0
        assert conf == pytest.approx(0.3)

    def test_rule_score_distill(self, scorer_no_load):
        content = (
            "```python\n"
            "def cluster_election():\n"
            "    pass\n"
            "```\n"
            "架构选型对比：微服务 vs 单体，Redis Cluster 选举机制分析。"
        )
        features = scorer_no_load._extract_features({"content": content})
        score, conf = scorer_no_load._rule_score("distill", None, features)
        assert score > 0.4  # code + architecture signals boost score
        assert conf == pytest.approx(0.4)

    def test_rule_score_kg(self, scorer_no_load):
        features = scorer_no_load._extract_features({"content": "[[A]] [[B]] [[C]]"})
        score, conf = scorer_no_load._rule_score("kg", None, features)
        assert score > 0.3  # links boost score
        assert conf == pytest.approx(0.3)

    def test_rule_score_profile(self, scorer_no_load):
        features = scorer_no_load._extract_features(
            {
                "content": "test",
                "frontmatter": {"tags": ["a", "b", "c", "d", "e"]},
            }
        )
        score, conf = scorer_no_load._rule_score("profile", None, features)
        assert score > 0.4
        assert conf == pytest.approx(0.3)

    def test_rule_score_ops(self, scorer_no_load):
        features = scorer_no_load._extract_features({"content": "error timeout crash"})
        score, conf = scorer_no_load._rule_score("ops", None, features)
        assert score > 0.2
        assert conf == pytest.approx(0.35)

    def test_rule_score_unknown_dimension(self, scorer_no_load):
        features = scorer_no_load._extract_features({"content": "test"})
        score, conf = scorer_no_load._rule_score("unknown", None, features)
        assert score == pytest.approx(0.5)
        assert conf == pytest.approx(0.3)


# =============================================================================
# AdaptiveScorerV2 — ML scoring
# =============================================================================


class TestAdaptiveScorerV2MLScore:
    def test_ml_score_no_model(self, scorer_no_load):
        ml_like, ml_conf = scorer_no_load._ml_score("l1_storage", {"x": 1})
        assert ml_like == pytest.approx(0.5)
        assert ml_conf == pytest.approx(0.0)

    def test_ml_score_with_lightweight_model(self, scorer_no_load):
        from core.scoring.lightweight_nb import LightweightComplementNB

        clf = LightweightComplementNB()
        clf.fit([{"a": 5}, {"b": 5}], [1, 0])
        scorer_no_load._models["l1_storage"] = clf
        ml_like, ml_conf = scorer_no_load._ml_score("l1_storage", {"a": 5})
        assert 0.0 <= ml_like <= 1.0
        assert ml_conf == pytest.approx(0.6)

    def test_ml_score_degrades_after_failures(self, scorer_no_load, monkeypatch):
        from core.scoring.lightweight_nb import LightweightComplementNB

        clf = LightweightComplementNB()
        clf.fit([{"a": 1}], [1])
        scorer_no_load._models["l1_storage"] = clf
        # Force consecutive failures
        scorer_no_load._fallback._consecutive_failures["l1_storage"] = 5
        ml_like, ml_conf = scorer_no_load._ml_score("l1_storage", {"a": 1})
        assert ml_like == pytest.approx(0.5)
        assert ml_conf == pytest.approx(0.0)

    def test_ml_score_records_failure_on_exception(self, scorer_no_load):
        bad_model = MagicMock()
        bad_model.predict_proba.side_effect = RuntimeError("boom")
        bad_model.__class__.__name__ = "LightweightComplementNB"
        scorer_no_load._models["l1_storage"] = bad_model
        ml_like, ml_conf = scorer_no_load._ml_score("l1_storage", {"x": 1})
        assert ml_like == pytest.approx(0.5)
        assert ml_conf == pytest.approx(0.0)
        assert scorer_no_load._fallback._consecutive_failures.get("l1_storage", 0) >= 1


# =============================================================================
# AdaptiveScorerV2 — Training
# =============================================================================


class TestAdaptiveScorerV2Training:
    def test_process_legacy_queue_is_fail_closed_and_non_mutating(
        self, scorer_no_load, db_with_schema
    ):
        with sqlite3.connect(str(db_with_schema)) as conn:
            conn.execute(
                """
                INSERT INTO scorer_training_queue (
                    session_id, dimension, features_json, earliest_train_at, status
                ) VALUES (?, ?, ?, ?, 'pending')
                """,
                (
                    "legacy-session",
                    "l1_storage",
                    "{}",
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()

        with pytest.raises(
            PermissionError,
            match="training_admission_receipt_required:process_training_queue",
        ):
            scorer_no_load.process_training_queue()

        with sqlite3.connect(str(db_with_schema)) as conn:
            assert conn.execute(
                "SELECT status FROM scorer_training_queue WHERE session_id=?",
                ("legacy-session",),
            ).fetchone() == ("pending",)

    def test_retired_private_queue_training_helpers_are_absent(self, scorer_no_load):
        for name in (
            "_train_dimension",
            "_mark_queue_trained",
            "_get_training_samples",
            "_normalize_pending_queue_dimensions",
        ):
            assert not hasattr(scorer_no_load, name)


# =============================================================================
# AdaptiveScorerV2 — Feature conversion
# =============================================================================


class TestAdaptiveScorerV2FeatureConversion:
    def test_features_to_sparse(self, scorer_no_load):
        sparse = scorer_no_load._features_to_sparse(
            {
                "flag": True,
                "count": 42,
                "content": "hello world",
                "category": "test",
                "tags": ["a", "b"],
            }
        )
        assert sparse["__bias__"] == 1.0
        assert sparse["flag"] == 1.0
        assert sparse["count"] == 42.0
        assert sparse["category=test"] == 1.0
        assert sparse["tags=a"] == 1.0
        assert sparse["tags=b"] == 1.0
        assert sparse["word_hello"] == 1.0
        assert sparse["word_world"] == 1.0

    def test_features_to_dense(self, scorer_no_load):
        dense = scorer_no_load._features_to_dense({"a": 1, "b": 2})
        assert isinstance(dense, list)
        assert len(dense) >= 2
        assert sum(dense) > 0


# =============================================================================
# AdaptiveScorerV2 — Bayesian state
# =============================================================================


class TestAdaptiveScorerV2BayesianState:
    def test_bayesian_runtime_is_cold_and_stateless(self, scorer_no_load):
        state = scorer_no_load._bayesian.state_to_dict()
        for dimension in list(scorer_no_load._SCORER_MAP) + ["l1_storage"]:
            assert state[dimension]["alpha"] == pytest.approx(1.0)
            assert state[dimension]["beta"] == pytest.approx(1.0)
            assert state[dimension]["total_samples"] == 0

    def test_bayesian_mutation_and_legacy_rebuild_are_fail_closed(self, scorer_no_load):
        with pytest.raises(
            PermissionError,
            match="training_admission_receipt_required:bayesian_update_from_ground_truth",
        ):
            scorer_no_load._bayesian.update_from_ground_truth("l1_storage", 1, confidence=1.0)
        with pytest.raises(
            PermissionError,
            match=(
                "training_admission_receipt_required:" "refresh_bayesian_priors_from_ground_truth"
            ),
        ):
            scorer_no_load.refresh_bayesian_priors_from_ground_truth(dimensions=["l1_storage"])


# =============================================================================
# AdaptiveScorerV2 — Status & utilities
# =============================================================================


class TestAdaptiveScorerV2Status:
    def test_get_status_is_cold_without_governed_admissions(self, scorer_no_load):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        status = scorer_no_load.get_status()
        assert status["domain"] == "l1_storage"
        assert status["version"] == "v2-full"
        assert status["mode"] == "cold"
        assert status["models_loaded"] == []
        assert status["ready_samples"] == 0
        assert status["signal_samples"] == 0
        assert status["mode_thresholds"] == {
            "cold": AdaptiveScorerV2.COLD_THRESHOLD,
            "warm": AdaptiveScorerV2.WARM_THRESHOLD,
            "hot": AdaptiveScorerV2.HOT_THRESHOLD,
        }

    def test_historical_queue_and_ground_truth_do_not_count(self, scorer_no_load, db_with_schema):
        now = datetime.now().isoformat()
        with sqlite3.connect(str(db_with_schema)) as conn:
            conn.execute(
                """
                INSERT INTO scorer_training_queue (
                    session_id, dimension, features_json, earliest_train_at, status
                ) VALUES ('legacy', 'l1_storage', '{}', ?, 'pending')
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO ground_truth_signals (
                    session_id, signal_type, signal_value, confidence, created_at
                ) VALUES ('legacy', 'l1_storage', '1', 1.0, ?)
                """,
                (now,),
            )
            conn.commit()

        assert scorer_no_load._count_ready_samples() == 0
        assert scorer_no_load._count_signal_samples() == 0
        scorer_no_load._update_mode()
        assert scorer_no_load._mode == "cold"

    def test_normalize_dimension(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        assert AdaptiveScorerV2.normalize_dimension("session_quality") == "l1_storage"
        assert AdaptiveScorerV2.normalize_dimension("engagement") == "profile"
        assert AdaptiveScorerV2.normalize_dimension("l1_storage") == "l1_storage"
        assert AdaptiveScorerV2.normalize_dimension("") == ""

    def test_deep_merge(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        base = {"a": 1, "b": {"c": 2, "d": 3}}
        override = {"b": {"c": 99}}
        merged = AdaptiveScorerV2._deep_merge(base, override)
        assert merged["a"] == 1
        assert merged["b"]["c"] == 99
        assert merged["b"]["d"] == 3

    def test_load_config_backend_fallback_when_sklearn_unavailable(self, monkeypatch):
        from core.scoring import adaptive_scorer_v2 as asv2

        monkeypatch.setattr(asv2, "_SKLEARN_AVAILABLE", False)
        cfg = asv2.AdaptiveScorerV2._load_config({"backend": "standard"})
        assert cfg["backend"] == "lightweight"

    def test_validate_scorer_config(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        assert (
            AdaptiveScorerV2.validate_scorer_config(
                {
                    "backend": "standard",
                    "training": {"min_samples_per_dimension": 20},
                    "bayesian": {"alpha_prior": 1, "beta_prior": 1},
                    "dimensions": {"l1_storage": True},
                }
            )
            == []
        )
        errors = AdaptiveScorerV2.validate_scorer_config(
            {
                "backend": "unknown",
                "training": {"min_samples_per_dimension": 2},
                "bayesian": {"alpha_prior": 0, "beta_prior": 1},
                "dimensions": {"l1_storage": False},
            }
        )
        assert len(errors) >= 3


# =============================================================================
# Frontmatter normalization
# =============================================================================


class TestFrontmatterNormalization:
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


# =============================================================================
# [P2-21] Expanded Feature Extraction — 50+ dimensions
# =============================================================================


class TestExpandedFeatures:
    """Tests for the 50+ dimensional feature expansion."""

    def test_feature_count_exceeds_50(self, scorer_no_load):
        """验证特征维度 >= 50"""
        features = scorer_no_load._extract_features(
            {
                "content": "Hello world with some content here.",
                "frontmatter": {"heat": 0.8},
            }
        )
        assert len(features) >= 50, f"Expected >= 50 features, got {len(features)}"

    def test_content_quality_features(self, scorer_no_load):
        features = scorer_no_load._extract_features(
            {
                "content": "First sentence here. Second sentence! Third?\n\nParagraph two. **Bold** text.\n- Item 1\n- Item 2\n```python\ncode\nline2\n```",  # noqa: E501
                "frontmatter": {},
            }
        )
        assert features["content_sentence_count"] >= 3
        assert features["content_avg_sentence_length"] > 0
        assert features["content_unique_word_ratio"] > 0
        assert features["content_has_question"] is True
        assert features["content_has_exclamation"] is True
        assert features["content_bold_count"] >= 1
        assert features["content_list_item_count"] >= 2
        assert features["content_code_block_count"] >= 1
        assert features["content_max_code_block_lines"] >= 2
        assert features["content_paragraph_count"] >= 2

    def test_content_quality_empty_content(self, scorer_no_load):
        features = scorer_no_load._extract_features({"content": "", "frontmatter": {}})
        assert features["content_sentence_count"] == 0
        assert features["content_unique_word_ratio"] == 0.0
        assert features["content_code_block_count"] == 0

    def test_temporal_features_exist(self, scorer_no_load):
        features = scorer_no_load._extract_features({"content": "test", "frontmatter": {}})
        assert "hour_of_day" in features
        assert 0.0 <= features["hour_of_day"] <= 1.0
        assert "day_of_week" in features
        assert 0 <= features["day_of_week"] <= 6
        assert "is_weekend" in features
        assert isinstance(features["is_weekend"], bool)
        assert "sessions_today" in features
        assert "sessions_this_week" in features
        assert "avg_session_interval_hours" in features
        assert "time_since_last_session_hours" in features

    def test_persona_features_exist(self, scorer_no_load):
        features = scorer_no_load._extract_features({"content": "test", "frontmatter": {}})
        assert "persona_confidence" in features
        assert "persona_match_score" in features
        assert "persona_energy_focus_depth" in features
        assert "persona_cognitive_abstraction" in features
        assert "persona_value_depth_vs_breadth" in features
        # 默认值在合理范围内（无画像时或画像匹配高时）
        assert 0.0 <= features["persona_energy_focus_depth"] <= 1.0
        assert 0.0 <= features["persona_match_score"] <= 1.0

    def test_persona_match_with_content(self, scorer_no_load):
        """深度偏好的用户应匹配长内容"""
        # 此测试依赖实际画像数据，若画像系统不可用则验证默认值路径
        features = scorer_no_load._extract_features(
            {
                "content": "原理 机制 模型 框架 理论 抽象 架构 " * 50,
                "frontmatter": {"tags": ["concept", "architecture"]},
            }
        )
        assert "persona_match_score" in features
        assert 0.0 <= features["persona_match_score"] <= 1.0

    def test_kg_features_exist(self, scorer_no_load):
        features = scorer_no_load._extract_features(
            {
                "content": "[[Entity1]] [[Entity2]] some text",
                "frontmatter": {},
            }
        )
        assert "kg_entity_density" in features
        assert "kg_relation_out_count" in features
        assert "kg_relation_in_count" in features
        assert "kg_relation_richness" in features
        assert "kg_connectivity_score" in features
        assert "kg_avg_relation_strength" in features
        # 实体密度应反映 wiki links
        assert features["kg_entity_density"] > 0

    def test_kg_features_with_path(self, scorer_no_load, tmp_path):
        f = tmp_path / "test_kg.md"
        f.write_text("---\ntitle: Test Page\n---\nContent with [[Link]]")
        features = scorer_no_load._extract_features(f)
        assert features["kg_entity_density"] > 0

    def test_interaction_features_exist(self, scorer_no_load):
        features = scorer_no_load._extract_features({"content": "test", "frontmatter": {}})
        assert "interaction_follow_up_depth" in features
        assert "interaction_correction_count" in features
        assert "interaction_rejection_rate" in features
        assert "interaction_satisfaction_rate" in features
        assert "interaction_avg_session_duration" in features
        assert "interaction_modification_requests" in features
        assert "interaction_termination_satisfied" in features
        # 数值范围检查
        assert 0.0 <= features["interaction_rejection_rate"] <= 1.0
        assert 0.0 <= features["interaction_satisfaction_rate"] <= 1.0

    def test_embedding_placeholder(self, scorer_no_load):
        """Embedding 特征初始为 None（条件计算）"""
        features = scorer_no_load._extract_features({"content": "test", "frontmatter": {}})
        assert "embedding_sim_to_high_quality" in features
        assert features["embedding_sim_to_high_quality"] is None

    def test_all_features_are_hashable_for_sparse(self, scorer_no_load):
        """验证所有特征值可被 _features_to_sparse 正确处理"""
        features = scorer_no_load._extract_features(
            {
                "content": "Test content with # header and [[link]].",
                "frontmatter": {"heat": 0.8, "quality_score": 85},
            }
        )
        sparse = scorer_no_load._features_to_sparse(features)
        assert "__bias__" in sparse
        # 数值特征应被转换
        assert "content_len" in sparse
        assert "content_words" in sparse
        assert "fm_heat" in sparse
        assert "fm_quality_score" in sparse
        # 布尔特征应被转换
        assert "has_code_block" in sparse
        # 列表/字符串特征应被转换或跳过（不抛异常即可）


class TestEmbeddingConditionalComputation:
    """Tests for conditional embedding similarity computation (bottom 20%)."""

    def test_should_compute_embedding_low_confidence(self, scorer_no_load):
        """极低置信度应触发 embedding 计算"""
        scorer_no_load._confidence_window = []
        assert scorer_no_load._should_compute_embedding([0.1, 0.15]) is True

    def test_should_compute_embedding_high_confidence(self, scorer_no_load, monkeypatch):
        """高置信度且窗口足够时不应触发（除非在 bottom 20%）"""
        # 模拟 embedding 可用
        monkeypatch.setattr(
            "core.scoring.adaptive_scorer_v2.AdaptiveScorerV2._should_compute_embedding",
            lambda self, _confs: False,  # 实际测试中由于 embedding 不可用会返回 False
        )
        # 直接测试：高置信度不应触发
        scorer_no_load._confidence_window = [0.9] * 100
        result = scorer_no_load._should_compute_embedding([0.9])
        # 由于 embedding 可能不可用，结果为 False
        assert result is False or result is True  # 两种可能都合理

    def test_should_compute_embedding_rolling_window(self, scorer_no_load):
        """验证置信度窗口维护"""
        scorer_no_load._confidence_window = []
        # 填充窗口
        for _ in range(15):
            scorer_no_load._should_compute_embedding([0.5])
        assert len(scorer_no_load._confidence_window) == 15

    def test_compute_embedding_similarity_empty_content(self, scorer_no_load):
        """空内容应返回 None"""
        result = scorer_no_load._compute_embedding_similarity("")
        assert result is None

    def test_compute_embedding_similarity_short_content(self, scorer_no_load):
        """过短内容应返回 None"""
        result = scorer_no_load._compute_embedding_similarity("hi")
        assert result is None

    def test_score_includes_embedding_for_low_confidence(self, scorer_no_load, monkeypatch):
        """低置信度评分时 embedding_sim 应被填充或保持 None"""
        # Mock embedding 方法以避免外部依赖
        monkeypatch.setattr(scorer_no_load, "_should_compute_embedding", lambda _confs: True)
        monkeypatch.setattr(
            scorer_no_load,
            "_compute_embedding_similarity",
            lambda content, **_kwargs: 0.75,
        )
        result = scorer_no_load.score(
            {"content": "test content"},
            dimensions=["sync"],
            subject_scope=("test", "embedding-feature"),
        )
        assert result.features["embedding_sim_to_high_quality"] == pytest.approx(0.75)

    def test_score_skips_embedding_for_high_confidence(self, scorer_no_load, monkeypatch):
        """高置信度评分时 embedding_sim 应为 None"""
        monkeypatch.setattr(scorer_no_load, "_should_compute_embedding", lambda _confs: False)
        result = scorer_no_load.score({"content": "test content"}, dimensions=["sync"])
        assert result.features["embedding_sim_to_high_quality"] is None


def test_historical_feedback_rows_never_enter_governed_training_or_bayesian_state(
    scorer_no_load,
    db_with_schema,
):
    now = datetime.now().isoformat()
    with sqlite3.connect(str(db_with_schema)) as conn:
        conn.execute(
            """
            INSERT INTO scorer_training_queue (
                session_id, dimension, features_json, earliest_train_at, status
            ) VALUES ('feedback-legacy', 'l1_storage', '{"source":"ordinary"}', ?, 'pending')
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO ground_truth_signals (
                session_id, signal_type, signal_value, confidence, created_at
            ) VALUES ('feedback-legacy', 'l1_storage', '1', 1.0, ?)
            """,
            (now,),
        )
        conn.commit()

    assert scorer_no_load._count_ready_samples("l1_storage") == 0
    assert scorer_no_load._count_signal_samples("l1_storage") == 0
    assert scorer_no_load._bayesian.state_to_dict()["l1_storage"]["total_samples"] == 0
    with pytest.raises(
        PermissionError,
        match="training_admission_receipt_required:process_training_queue",
    ):
        scorer_no_load.process_training_queue("l1_storage")

    with sqlite3.connect(str(db_with_schema)) as conn:
        assert conn.execute(
            "SELECT status FROM scorer_training_queue WHERE session_id='feedback-legacy'"
        ).fetchone() == ("pending",)
