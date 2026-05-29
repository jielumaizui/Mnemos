# -*- coding: utf-8 -*-
"""
评分层 V2 单元测试 — beta_bayesian / lightweight_nb / fallback / training_scheduler / adaptive_scorer_v2
"""

import json
import pickle
import sqlite3
import sys

import pytest
from datetime import datetime, timedelta


# ==================== beta_bayesian ====================

class TestBetaBayesianFusion:
    def test_prior_mean_after_positive_update(self):
        from core.scoring.beta_bayesian import BetaBayesianFusion

        bb = BetaBayesianFusion(["memos"])
        bb.update_from_ground_truth("memos", 1, confidence=1.0)
        # prior: alpha=1+1=2, beta=1 => mean=2/3
        assert bb.priors["memos"].mean == pytest.approx(2 / 3, abs=0.01)

    def test_prior_mean_after_negative_update(self):
        from core.scoring.beta_bayesian import BetaBayesianFusion

        bb = BetaBayesianFusion(["memos"])
        bb.update_from_ground_truth("memos", 0, confidence=1.0)
        # alpha=1, beta=1+1=2 => mean=1/3
        assert bb.priors["memos"].mean == pytest.approx(1 / 3, abs=0.01)

    def test_fuse_returns_score_between_0_and_1(self):
        from core.scoring.beta_bayesian import BetaBayesianFusion

        bb = BetaBayesianFusion(["memos"])
        score, conf = bb.fuse("memos", rule_prior=0.6, ml_likelihood=0.7, ml_confidence=0.8)
        assert 0.0 <= score <= 1.0
        assert 0.0 <= conf <= 1.0

    def test_batch_update(self):
        from core.scoring.beta_bayesian import BetaBayesianFusion

        bb = BetaBayesianFusion(["kg"])
        bb.batch_update("kg", [1, 1, 0, 1])
        assert bb.priors["kg"].total_samples == 4
        assert bb.priors["kg"].mean > 0.5

    def test_dimension_status(self):
        from core.scoring.beta_bayesian import BetaBayesianFusion

        bb = BetaBayesianFusion(["distill"])
        bb.update_from_ground_truth("distill", 1)
        status = bb.get_dimension_status("distill")
        assert "mean" in status
        assert "samples" in status


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

        with fb.guard("memos", rule_fn) as try_ml:
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


# ==================== training_scheduler ====================

class TestScorerTrainingScheduler:
    def test_on_buffer_full_triggers_training(self):
        from core.scoring.training_scheduler import ScorerTrainingScheduler

        calls = []

        def mock_train(dim):
            calls.append(dim)
            return {"success": True, "version": "v1", "samples": 42}

        sched = ScorerTrainingScheduler(train_fn=mock_train)
        job = sched.on_buffer_full("memos")

        assert job.status == "completed"
        assert job.samples_used == 42
        assert "memos" in calls

    def test_manual_trigger(self):
        from core.scoring.training_scheduler import ScorerTrainingScheduler

        def mock_train(dim):
            return {"success": True, "version": "v2", "samples": 10}

        sched = ScorerTrainingScheduler(train_fn=mock_train)
        job = sched.trigger_manual("kg")

        assert job.triggered_by == "manual"
        assert job.status == "completed"

    def test_training_failure_recorded(self):
        from core.scoring.training_scheduler import ScorerTrainingScheduler

        def mock_train(dim):
            raise RuntimeError("OOM")

        sched = ScorerTrainingScheduler(train_fn=mock_train)
        job = sched.on_buffer_full("distill")

        assert job.status == "failed"
        assert "OOM" in job.error_msg


# ==================== adaptive_scorer_v2 ====================

class TestAdaptiveScorerV2:
    def test_score_returns_scorecard(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        scorer = AdaptiveScorerV2()
        result = scorer.score(
            {"content": "Hello world", "frontmatter": {"heat": 0.8}},
            dimensions=["memos", "sync"],
        )

        assert "memos" in result.scores
        assert "sync" in result.scores
        assert 0.0 <= result.scores["memos"] <= 1.0
        assert result.features["content_len"] == 11

    def test_extract_features_from_string(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        scorer = AdaptiveScorerV2()
        features = scorer._extract_features("Test content with # header")
        assert features["content"] == "Test content with # header"
        assert features["header_count"] == 1

    def test_insert_ground_truth(self, tmp_path):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2
        import sqlite3

        db = tmp_path / "test.db"
        # 创建最小表结构
        with sqlite3.connect(str(db)) as conn:
            conn.execute("""
                CREATE TABLE ground_truth_signals (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT,
                    signal_type TEXT,
                    signal_value INTEGER,
                    confidence REAL,
                    latency_hours INTEGER,
                    created_at TEXT
                )
            """)
            conn.execute("""
                CREATE UNIQUE INDEX idx_gt_session ON ground_truth_signals(session_id, signal_type)
            """)

        AdaptiveScorerV2.insert_ground_truth(
            session_id="s1",
            signal_type="search_hit",
            label=1,
            db_path=db,
        )

        with sqlite3.connect(str(db)) as conn:
            row = conn.execute("SELECT signal_value FROM ground_truth_signals WHERE session_id='s1'").fetchone()
            assert row[0] == 1

    def test_get_status(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        scorer = AdaptiveScorerV2()
        status = scorer.get_status()
        assert status["domain"] == "mnemos"
        assert status["version"] == "v2-full"


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
        features = scorer._extract_features({
            "content": "test",
            "frontmatter": {
                "heat": "hot",
                "quality_score": 85,
                "confidence": "75%",
                "priority": "0.9",
            },
        })
        assert features["fm_heat"] == pytest.approx(0.9)
        assert features["fm_quality_score"] == pytest.approx(0.85)
        assert features["fm_confidence"] == pytest.approx(0.75)
        assert features["fm_priority"] == pytest.approx(0.9)

    def test_rule_score_uses_normalized_values(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        scorer = AdaptiveScorerV2()
        # heat=hot(0.9), quality_score=100(1.0) => (0.9*0.5 + 1.0*0.5) = 0.95
        features = scorer._extract_features({
            "content": "test",
            "frontmatter": {"heat": "hot", "quality_score": 100},
        })
        score, conf = scorer._rule_score("memos", None, features)
        assert score == pytest.approx(0.95, abs=0.01)

        # heat=cold(0.3), quality_score=0(0.0) => (0.3*0.5 + 0.0*0.5) = 0.15
        features2 = scorer._extract_features({
            "content": "test",
            "frontmatter": {"heat": "cold", "quality_score": 0},
        })
        score2, _ = scorer._rule_score("memos", None, features2)
        assert score2 == pytest.approx(0.15, abs=0.01)


# ==================== 新增：配置深合并 ====================

class TestConfigDeepMerge:
    def test_deep_merge_preserves_untouched_nested_keys(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        # 用户只覆盖 training.min_samples_per_dimension，training 其他键保留
        user = {"training": {"min_samples_per_dimension": 50}}
        cfg = AdaptiveScorerV2._load_config(user)

        assert cfg["training"]["min_samples_per_dimension"] == 50
        assert cfg["training"]["min_confidence"] == 0.7  # 默认值保留
        assert cfg["training"]["max_queue_size"] == 500   # 默认值保留

    def test_user_overrides_yaml(self, monkeypatch):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        def _patched_load(user_config):
            defaults = {
                "backend": "standard",
                "training": {"min_samples_per_dimension": 20, "min_confidence": 0.7},
                "bayesian": {"rule_weight_cold": 3.0, "rule_weight_warm": 1.5},
                "fallback": {"max_consecutive_failures": 3},
                "persistence": {"format": "joblib"},
                "dimensions": {"memos": True},
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
        orig_validate = AdaptiveScorerV2.validate_scorer_config

        def mock_validate(cfg):
            calls.append(cfg)
            return ["mock_error"]  # 返回非空，验证仅记录警告

        monkeypatch.setattr(AdaptiveScorerV2, "validate_scorer_config", staticmethod(mock_validate))
        # 同时 mock _load_all_models 避免 DB 依赖
        monkeypatch.setattr(AdaptiveScorerV2, "_load_all_models", lambda self: None)

        scorer = AdaptiveScorerV2()
        assert len(calls) == 1
        assert calls[0]["backend"] in ("standard", "lightweight")


# ==================== 新增：模型加载安全护栏 ====================

class TestModelLoadSecurity:
    """测试 save_model / load_model 的安全元数据校验。"""

    @pytest.fixture
    def db_with_schema(self, tmp_path):
        db = tmp_path / "secure.db"
        with sqlite3.connect(str(db)) as conn:
            conn.execute("""
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
            """)
        return db

    def _insert_model(self, db, dimension, blob, model_hash, meta):
        with sqlite3.connect(str(db)) as conn:
            conn.execute("""
                INSERT INTO scorer_models
                (dimension, model_version, model_type, model_blob, model_hash, is_active, created_at, meta_json)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """, (dimension, "v1", "lightweight_nb", blob, model_hash,
                  datetime.now().isoformat(), json.dumps(meta)))
            conn.commit()

    def test_load_model_success(self, db_with_schema, monkeypatch):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2, _SCHEMA_VERSION

        model = {"dummy": "model"}
        blob = pickle.dumps(model)
        blob_hash = __import__("hashlib").sha256(blob).hexdigest()
        meta = {
            "schema_version": _SCHEMA_VERSION,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.0",
            "sklearn_version": "none",
            "model_class": "dict",
        }
        self._insert_model(db_with_schema, "memos", blob, blob_hash, meta)

        scorer = AdaptiveScorerV2(db_path=str(db_with_schema))
        loaded = scorer.load_model("memos")
        assert loaded == {"dummy": "model"}
        assert scorer._model_versions["memos"] == "latest"

    def test_load_model_hash_mismatch_returns_none(self, db_with_schema, monkeypatch):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2, _SCHEMA_VERSION

        model = {"dummy": "model"}
        blob = pickle.dumps(model)
        meta = {
            "schema_version": _SCHEMA_VERSION,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.0",
            "sklearn_version": "none",
        }
        # hash 故意写错
        self._insert_model(db_with_schema, "memos", blob, "wrong_hash", meta)

        scorer = AdaptiveScorerV2(db_path=str(db_with_schema))
        loaded = scorer.load_model("memos")
        assert loaded is None
        assert "memos" not in scorer._models

    def test_load_model_schema_mismatch_returns_none(self, db_with_schema, monkeypatch):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        model = {"dummy": "model"}
        blob = pickle.dumps(model)
        blob_hash = __import__("hashlib").sha256(blob).hexdigest()
        meta = {
            "schema_version": "v0.0",  # 不匹配
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.0",
        }
        self._insert_model(db_with_schema, "memos", blob, blob_hash, meta)

        scorer = AdaptiveScorerV2(db_path=str(db_with_schema))
        loaded = scorer.load_model("memos")
        assert loaded is None

    def test_load_model_python_version_mismatch_returns_none(self, db_with_schema, monkeypatch):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2, _SCHEMA_VERSION

        model = {"dummy": "model"}
        blob = pickle.dumps(model)
        blob_hash = __import__("hashlib").sha256(blob).hexdigest()
        meta = {
            "schema_version": _SCHEMA_VERSION,
            "python_version": "2.7",  # 不可能匹配当前 Python
            "sklearn_version": "none",
        }
        self._insert_model(db_with_schema, "memos", blob, blob_hash, meta)

        scorer = AdaptiveScorerV2(db_path=str(db_with_schema))
        loaded = scorer.load_model("memos")
        assert loaded is None

    def test_load_model_no_meta_still_loads(self, db_with_schema, monkeypatch):
        """旧模型没有 meta_json 时，跳过校验直接加载（向后兼容）。"""
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        model = {"legacy": True}
        blob = pickle.dumps(model)
        blob_hash = __import__("hashlib").sha256(blob).hexdigest()
        with sqlite3.connect(str(db_with_schema)) as conn:
            conn.execute("""
                INSERT INTO scorer_models
                (dimension, model_version, model_type, model_blob, model_hash, is_active, created_at, meta_json)
                VALUES (?, ?, ?, ?, ?, 1, ?, NULL)
            """, ("memos", "v1", "lightweight_nb", blob, blob_hash, datetime.now().isoformat()))
            conn.commit()

        scorer = AdaptiveScorerV2(db_path=str(db_with_schema))
        loaded = scorer.load_model("memos")
        assert loaded == {"legacy": True}

    def test_init_load_failure_isolated(self, db_with_schema, monkeypatch):
        """初始化时某维度模型加载失败，不影响其他维度继续尝试。"""
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2, _SCHEMA_VERSION

        # memos 放一个有效的模型
        model_ok = {"ok": True}
        blob_ok = pickle.dumps(model_ok)
        hash_ok = __import__("hashlib").sha256(blob_ok).hexdigest()
        meta_ok = {
            "schema_version": _SCHEMA_VERSION,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.0",
            "sklearn_version": "none",
        }
        self._insert_model(db_with_schema, "memos", blob_ok, hash_ok, meta_ok)

        # sync 放一个 hash 错误的模型（会失败）
        model_bad = {"bad": True}
        blob_bad = pickle.dumps(model_bad)
        meta_bad = {
            "schema_version": _SCHEMA_VERSION,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.0",
            "sklearn_version": "none",
        }
        self._insert_model(db_with_schema, "sync", blob_bad, "bad_hash", meta_bad)

        scorer = AdaptiveScorerV2(db_path=str(db_with_schema))
        # memos 应该加载成功
        assert "memos" in scorer._models
        # sync 应该加载失败但不抛异常
        assert "sync" not in scorer._models


# ==================== 新增：强制 sklearn 不可用时的纯 Python fallback ====================

class TestSklearnUnavailableFallback:
    def test_lightweight_backend_trains_and_predicts(self, monkeypatch, tmp_path):
        """模拟 sklearn 不可用，验证 LightweightComplementNB 完整路径。"""
        from core.scoring import adaptive_scorer_v2 as asv2

        # 强制 sklearn 不可用
        monkeypatch.setattr(asv2, "_SKLEARN_AVAILABLE", False)

        db = tmp_path / "lightweight.db"
        with sqlite3.connect(str(db)) as conn:
            conn.execute("""
                CREATE TABLE scorer_models (
                    id INTEGER PRIMARY KEY,
                    dimension TEXT, model_version TEXT, model_type TEXT,
                    model_blob BLOB, model_hash TEXT, train_samples INTEGER,
                    is_active INTEGER, created_at TEXT, meta_json TEXT
                )
            """)

        # 显式指定 lightweight 后端（因为 _load_config 中的 _SKLEARN_AVAILABLE
        # 在模块加载时解析，monkeypatch 不会 retroactive 影响已编译的 staticmethod）
        scorer = asv2.AdaptiveScorerV2(db_path=str(db), config={"backend": "lightweight"})
        assert scorer.config["backend"] == "lightweight"

        # 直接构造一个 LightweightComplementNB 训练（使用明显区分的特征）
        from core.scoring.lightweight_nb import LightweightComplementNB
        clf = LightweightComplementNB()
        X = [
            {"python": 5, "hello": 0},
            {"python": 4, "hello": 1},
            {"python": 0, "hello": 5},
            {"python": 1, "hello": 4},
        ]
        y = [1, 1, 0, 0]
        clf.fit(X, y)

        # 预测——不要求硬编码类别值，但同类应一致、异类应不同
        preds = clf.predict(X)
        assert preds[0] == preds[1]
        assert preds[2] == preds[3]
        assert preds[0] != preds[2]

        # predict_proba
        probs = clf.predict_proba([{"python": 5, "hello": 0}])[0]
        assert pytest.approx(sum(probs.values()), abs=0.01) == 1.0

        # 保存到 scorer_models
        scorer._models["memos"] = clf
        version = scorer.save_model("memos", note="lightweight_test")
        assert version.startswith("20")  # 时间戳格式

        # 重新实例化并加载
        scorer2 = asv2.AdaptiveScorerV2(db_path=str(db))
        loaded = scorer2.load_model("memos")
        assert loaded is not None
        # 加载后应能继续预测
        preds2 = loaded.predict([{"python": 5, "hello": 0}])
        assert preds2[0] == preds[0]

    def test_rule_score_works_without_sklearn(self, monkeypatch):
        """即使 sklearn 不可用，rule_score 仍应正常工作。"""
        from core.scoring import adaptive_scorer_v2 as asv2

        monkeypatch.setattr(asv2, "_SKLEARN_AVAILABLE", False)

        scorer = asv2.AdaptiveScorerV2()
        result = scorer.score(
            {"content": "test content", "frontmatter": {"heat": "warm"}},
            dimensions=["memos"],
        )
        assert 0.0 <= result.scores["memos"] <= 1.0
        assert result.model_version.startswith("v2")
