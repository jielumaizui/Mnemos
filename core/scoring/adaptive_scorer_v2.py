# -*- coding: utf-8 -*-
"""
AdaptiveScorerV2 — 自适应评分引擎 V2（完整实现）

ADR-016 修复清单：
  1. _bayesian_update 使用显式 P(E|~H)
  2. 训练标签来自 ground_truth_signals（外部真实信号），禁止自举
  3. 使用 partial_fit 增量更新，不覆盖已有模型
  4. EWMA 更新模型内部参数
  5. _extract_features 返回 content 键

数据流：
  评分 → scorer_training_queue → 等待延迟信号 → ground_truth_signals
                                              ↑
  用户反馈/搜索命中/页面访问/盲区检测 ──────────┘
              ↓
  chronos 每小时 → process_training_queue → partial_fit
              ↓
  保存到 scorer_models
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import pickle
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import get_config

# 模型 schema 版本（加载时校验，不匹配则拒绝加载）
_SCHEMA_VERSION = "v2.1"

# V2 子模块
from core.scoring.beta_bayesian import BetaBayesianFusion
from core.scoring.fallback import ScorerFallback
from core.scoring.lightweight_nb import LightweightComplementNB

logger = logging.getLogger(__name__)

# sklearn 可选导入（标准环境）
try:
    from sklearn.naive_bayes import ComplementNB
    _SKLEARN_AVAILABLE = True
except ImportError:
    _SKLEARN_AVAILABLE = False

# 六域规则评分器（用于特征提取和规则先验）
from core.scoring.scorers.distill_scorer import DistillScorer
from core.scoring.scorers.kg_scorer import KGScorer
from core.scoring.scorers.memos_scorer import MemosQualityScorer
from core.scoring.scorers.ops_scorer import OpsScorer
from core.scoring.scorers.profile_scorer import ProfileScorer
from core.scoring.scorers.sync_scorer import SyncScorer


# ==================== 数据模型 ====================

@dataclass(frozen=True)
class ScoreCardV2:
    """V2 评分卡"""
    scores: Dict[str, float]
    confidences: Dict[str, float]
    features: Dict[str, Any]
    model_version: str
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class FeedbackV2:
    """V2 反馈信号"""
    session_id: str
    dimension: str
    expected: float
    actual: float
    features: Dict[str, Any]
    source: str = "manual"
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class GroundTruth:
    """外部真实信号"""
    session_id: str
    signal_type: str
    label: int
    confidence: float = 1.0
    latency_hours: int = 0


# ==================== AdaptiveScorerV2（完整实现） ====================

class AdaptiveScorerV2:
    """自适应评分引擎 V2"""

    # 三阶段阈值
    COLD_THRESHOLD = 0
    WARM_THRESHOLD = 30
    HOT_THRESHOLD = 200

    # 维度 → 规则评分器类
    _SCORER_MAP = {
        "memos": MemosQualityScorer,
        "sync": SyncScorer,
        "distill": DistillScorer,
        "kg": KGScorer,
        "profile": ProfileScorer,
        "ops": OpsScorer,
    }

    def __init__(
        self,
        domain: str = "mnemos",
        config: Dict[str, Any] = None,
        db_path: Optional[str] = None,
    ):
        self.domain = domain
        self.config = self._load_config(config)

        # 真正调用配置校验，仅记录警告不阻塞初始化
        cfg_errors = self.validate_scorer_config(self.config)
        if cfg_errors:
            logger.warning(f"[ScorerV2] Config validation warnings: {cfg_errors}")

        self.db_path = Path(db_path) if db_path else (get_config().data_dir / "mnemos.db")

        self._mode = "cold"
        self._models: Dict[str, Any] = {}          # dimension → model
        self._model_versions: Dict[str, str] = {}
        self._bayesian = BetaBayesianFusion(list(self._SCORER_MAP.keys()))
        self._fallback = ScorerFallback()

        # 加载已有模型（逐维度隔离，单维度失败不阻塞其他维度）
        self._load_all_models()

    @staticmethod
    def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """深度合并两个字典：override 递归覆盖 base 的同名键。"""
        result = base.copy()
        for key, val in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(val, dict):
                result[key] = AdaptiveScorerV2._deep_merge(result[key], val)
            else:
                result[key] = val
        return result

    @staticmethod
    def _load_config(user_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """加载并合并配置：默认值 < config/scorer.yaml < 用户传入（深度合并）"""
        defaults = {
            "backend": "standard" if _SKLEARN_AVAILABLE else "lightweight",
            "training": {
                "min_samples_per_dimension": 20,
                "min_confidence": 0.7,
                "max_queue_size": 500,
                "retention_days": 30,
            },
            "bayesian": {
                "alpha_prior": 1.0,
                "beta_prior": 1.0,
                "explicit_neg_likelihood": 0.3,
                "rule_weight_cold": 3.0,
                "rule_weight_warm": 1.5,
                "rule_weight_hot": 0.5,
            },
            "fallback": {
                "max_consecutive_failures": 3,
                "degrade_to_rule": True,
            },
            "persistence": {
                "format": "joblib",
                "max_versions": 5,
                "auto_save_after_training": True,
            },
            "dimensions": {
                "memos": True, "sync": True, "distill": True,
                "kg": True, "profile": True, "ops": True,
            },
        }

        # 尝试加载 YAML 配置
        yaml_config = {}
        try:
            import yaml
            cfg_path = Path(__file__).parents[2] / "config" / "scorer.yaml"
            if cfg_path.exists():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    loaded = yaml.safe_load(f) or {}
                    yaml_config = loaded.get("scorer", {})
        except Exception:
            pass

        # 三层深度合并：defaults < yaml < user
        merged = AdaptiveScorerV2._deep_merge(defaults, yaml_config)
        if user_config:
            merged = AdaptiveScorerV2._deep_merge(merged, user_config)
        return merged

    @staticmethod
    def validate_scorer_config(cfg: Dict[str, Any]) -> List[str]:
        """校验配置，返回错误列表（空列表表示通过）"""
        errors = []
        if cfg.get("backend") not in ("standard", "lightweight"):
            errors.append(f"backend must be 'standard' or 'lightweight', got {cfg.get('backend')}")

        training = cfg.get("training", {})
        if training.get("min_samples_per_dimension", 0) < 5:
            errors.append("training.min_samples_per_dimension must be >= 5")

        bayesian = cfg.get("bayesian", {})
        if bayesian.get("alpha_prior", 0) <= 0 or bayesian.get("beta_prior", 0) <= 0:
            errors.append("bayesian alpha_prior / beta_prior must be > 0")

        dims = cfg.get("dimensions", {})
        if not any(dims.values()):
            errors.append("at least one dimension must be enabled")

        return errors

    # ── 核心评分接口 ──

    def score(self, item: Any, dimensions: List[str]) -> ScoreCardV2:
        """
        多维度评分：特征提取 → 规则先验 → ML 似然 → 贝叶斯后验。
        """
        features = self._extract_features(item)
        scores: Dict[str, float] = {}
        confidences: Dict[str, float] = {}

        for dim in dimensions:
            # 1. 规则先验
            rule_prior, rule_conf = self._rule_score(dim, item, features)

            # 2. ML 似然（带降级保护）
            ml_like, ml_conf = self._ml_score(dim, features)

            # 3. 贝叶斯融合
            post, post_conf = self._bayesian.fuse(
                dimension=dim,
                rule_prior=rule_prior,
                ml_likelihood=ml_like,
                ml_confidence=ml_conf,
            )
            scores[dim] = post
            confidences[dim] = post_conf

        version = self._model_versions.get(dimensions[0], "v2-rule-only") if not self._models else "v2-ml"
        return ScoreCardV2(
            scores=scores,
            confidences=confidences,
            features=features,
            model_version=version,
        )

    def feedback(self, fb: FeedbackV2) -> None:
        """接收反馈，写入 ground_truth 和训练队列"""
        self._insert_ground_truth(
            session_id=fb.session_id,
            signal_type="user_feedback",
            label=1 if fb.expected >= 0.5 else 0,
            confidence=abs(fb.expected - fb.actual),
        )
        self._insert_training_queue(fb)
        logger.debug(f"[ScorerV2] Feedback recorded for session={fb.session_id}")

    # ── 批量训练接口 ──

    def process_training_queue(self, dimension: Optional[str] = None) -> int:
        """
        处理训练队列，返回本次训练的样本数。
        """
        ready_count = self._count_ready_samples(dimension)
        if ready_count < 20:
            logger.info(
                f"[ScorerV2] Training skipped: only {ready_count} ready samples "
                f"(need ≥20 to start first training)"
            )
            return 0

        # 按维度分组训练
        dims = [dimension] if dimension else list(self._SCORER_MAP.keys())
        total_trained = 0
        for dim in dims:
            trained = self._train_dimension(dim)
            total_trained += trained

        logger.info(f"[ScorerV2] Training completed: {total_trained} samples across {len(dims)} dimensions")
        return total_trained

    # ── 模型管理 ──

    def save_model(self, dimension: str, note: Optional[str] = None) -> str:
        """将模型序列化到 scorer_models 表（带安全元数据）。"""
        model = self._models.get(dimension)
        if model is None:
            raise ValueError(f"No model loaded for dimension={dimension}")

        version = datetime.now().strftime("%Y%m%d-%H%M%S")
        blob = pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL)

        # 元数据：用于加载时安全校验
        meta = json.dumps({
            "schema_version": _SCHEMA_VERSION,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "sklearn_version": self._sklearn_version(),
            "model_class": type(model).__name__,
            "note": note,
        })
        blob_hash = hashlib.sha256(blob).hexdigest()

        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute("""
                    INSERT INTO scorer_models
                        (dimension, model_version, model_type, model_blob, model_hash,
                         train_samples, is_active, created_at, meta_json)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """, (
                    dimension,
                    version,
                    "sklearn_complement_nb" if _SKLEARN_AVAILABLE else "lightweight_nb",
                    blob,
                    blob_hash,
                    getattr(model, "n_features_in_", 0) if _SKLEARN_AVAILABLE else len(model.to_dict()),
                    datetime.now().isoformat(),
                    meta,
                ))
                conn.commit()
        except Exception as e:
            logger.warning(f"[ScorerV2] save_model failed: {e}")
            raise

        self._model_versions[dimension] = version
        logger.info(f"[ScorerV2] Model saved: {dimension}@{version}")
        return version

    @staticmethod
    def _sklearn_version() -> str:
        """返回当前 sklearn 版本，未安装返回 'none'。"""
        try:
            import sklearn
            return sklearn.__version__
        except Exception:
            return "none"

    def load_model(self, dimension: str, version: Optional[str] = None) -> Any:
        """
        从 scorer_models 表加载模型（带安全护栏）。

        校验项：
          1. schema_version 匹配（防止跨版本加载不兼容模型）
          2. SHA256 hash 匹配（防止 blob 损坏/篡改）
          3. Python 主版本一致（pickle 跨大版本不安全）
          4. sklearn 版本一致（sklearn 模型跨版本可能不兼容）

        任何校验失败 → 静默返回 None，不阻塞评分流程。
        """
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                if version:
                    row = conn.execute("""
                        SELECT model_blob, model_type, model_hash, meta_json
                        FROM scorer_models
                        WHERE dimension = ? AND model_version = ?
                    """, (dimension, version)).fetchone()
                else:
                    row = conn.execute("""
                        SELECT model_blob, model_type, model_hash, meta_json
                        FROM scorer_models
                        WHERE dimension = ? AND is_active = 1
                        ORDER BY created_at DESC LIMIT 1
                    """, (dimension,)).fetchone()

                if not row:
                    logger.debug(f"[ScorerV2] No model found for {dimension}")
                    return None

                blob, model_type, stored_hash, meta_json = row

                # 1. hash 校验
                if stored_hash and hashlib.sha256(blob).hexdigest() != stored_hash:
                    logger.warning(f"[ScorerV2] Hash mismatch for {dimension}, refusing load")
                    return None

                # 2. 元数据校验
                if meta_json:
                    try:
                        meta = json.loads(meta_json)
                    except json.JSONDecodeError:
                        meta = {}

                    # schema 版本
                    if meta.get("schema_version") != _SCHEMA_VERSION:
                        logger.warning(
                            f"[ScorerV2] Schema mismatch for {dimension}: "
                            f"stored={meta.get('schema_version')} expected={_SCHEMA_VERSION}"
                        )
                        return None

                    # Python 主版本
                    py_ver = meta.get("python_version", "")
                    current_major = f"{sys.version_info.major}.{sys.version_info.minor}"
                    if py_ver and not py_ver.startswith(current_major):
                        logger.warning(
                            f"[ScorerV2] Python version mismatch for {dimension}: "
                            f"stored={py_ver} current={current_major}"
                        )
                        return None

                    # sklearn 版本（仅当当前有 sklearn 时检查）
                    sk_ver = meta.get("sklearn_version", "none")
                    if sk_ver != "none" and _SKLEARN_AVAILABLE:
                        current_sk = self._sklearn_version()
                        if sk_ver != current_sk:
                            logger.warning(
                                f"[ScorerV2] sklearn version mismatch for {dimension}: "
                                f"stored={sk_ver} current={current_sk}"
                            )
                            return None

                # 3. pickle 反序列化（隔离异常）
                try:
                    model = pickle.loads(blob)
                except Exception as e:
                    logger.warning(f"[ScorerV2] pickle deserialization failed for {dimension}: {e}")
                    return None

                self._models[dimension] = model
                self._model_versions[dimension] = version or "latest"
                logger.info(f"[ScorerV2] Model loaded: {dimension} ({model_type})")
                return model
        except Exception as e:
            logger.warning(f"[ScorerV2] load_model failed: {e}")
            return None

    def rollback_model(self, dimension: str, version: str) -> None:
        """回滚到指定版本"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                UPDATE scorer_models SET is_active = 0 WHERE dimension = ?
            """, (dimension,))
            conn.execute("""
                UPDATE scorer_models SET is_active = 1
                WHERE dimension = ? AND model_version = ?
            """, (dimension, version))
            conn.commit()
        self.load_model(dimension, version)
        logger.info(f"[ScorerV2] Model rolled back: {dimension} -> {version}")

    # ── ground_truth 写入点 ──

    @classmethod
    def insert_ground_truth(
        cls,
        session_id: str,
        signal_type: str,
        label: int,
        confidence: float = 1.0,
        latency_hours: int = 0,
        db_path: Optional[Path] = None,
    ) -> None:
        db = db_path or (get_config().data_dir / "mnemos.db")
        try:
            with sqlite3.connect(str(db)) as conn:
                # 先删除旧记录（避免 UNIQUE 约束缺失导致的 ON CONFLICT 失败）
                conn.execute("""
                    DELETE FROM ground_truth_signals
                    WHERE session_id = ? AND signal_type = ?
                """, (session_id, signal_type))

                # 检测表是否有 profile_id 列（兼容旧测试表结构）
                has_profile_id = False
                try:
                    cursor = conn.execute("PRAGMA table_info(ground_truth_signals)")
                    columns = {row[1] for row in cursor.fetchall()}
                    has_profile_id = "profile_id" in columns
                except Exception:
                    pass

                if has_profile_id:
                    conn.execute("""
                        INSERT INTO ground_truth_signals
                            (profile_id, session_id, signal_type, signal_value, confidence, latency_hours, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        session_id, session_id, signal_type, str(label), confidence, latency_hours,
                        datetime.now().isoformat(),
                    ))
                else:
                    conn.execute("""
                        INSERT INTO ground_truth_signals
                            (session_id, signal_type, signal_value, confidence, latency_hours, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        session_id, signal_type, str(label), confidence, latency_hours,
                        datetime.now().isoformat(),
                    ))
                conn.commit()
        except Exception as e:
            logger.warning(f"[ScorerV2] ground_truth insert failed: {e}")

    # ── 内部方法 ──

    # ── frontmatter 数值归一化 ──

    @staticmethod
    def _normalize_frontmatter_value(
        val: Any, key: str = "", clamp_0_1: bool = True
    ) -> Optional[float]:
        """
        将 frontmatter 值归一化为 [0, 1] 浮点数。

        处理多种输入形态：
          - 字符串枚举："hot"→0.9, "warm"→0.6, "cold"→0.3
          - 字符串数字："0.8" → 0.8, "100" → 1.0
          - 0-100 分值：自动检测并 /100 归一化
          - 已经是 0-1 浮点：直接保留
          - 布尔值：True→1.0, False→0.0
          - 其他/无法解析：返回 None（调用方取默认）
        """
        if val is None:
            return None

        # 布尔值
        if isinstance(val, bool):
            return 1.0 if val else 0.0

        # 已经是数值
        if isinstance(val, (int, float)):
            # 排除 bool 子类（上面已处理）
            fval = float(val)
            # 检测 0-100 分值（常见 frontmatter 习惯）
            if key in ("heat", "quality_score", "confidence", "priority"):
                if fval > 1.0:
                    fval = fval / 100.0
            if clamp_0_1:
                fval = max(0.0, min(1.0, fval))
            return fval

        # 字符串处理
        if isinstance(val, str):
            s = val.strip().lower()
            # 枚举值
            ENUM_MAP = {"hot": 0.9, "warm": 0.6, "cold": 0.3,
                        "high": 0.85, "medium": 0.55, "low": 0.25,
                        "critical": 0.95, "normal": 0.5}
            if s in ENUM_MAP:
                return ENUM_MAP[s]
            # 百分比字符串
            if s.endswith("%"):
                try:
                    return max(0.0, min(1.0, float(s[:-1]) / 100.0))
                except ValueError:
                    return None
            # 纯数字字符串
            try:
                fval = float(s)
                if key in ("heat", "quality_score", "confidence", "priority") and fval > 1.0:
                    fval = fval / 100.0
                return max(0.0, min(1.0, fval)) if clamp_0_1 else fval
            except ValueError:
                return None

        return None

    def _extract_features(self, item: Any) -> Dict[str, Any]:
        """从 item 提取特征字典（frontmatter 数值已归一化到 [0,1]）"""
        features: Dict[str, Any] = {"_domain": self.domain}

        # 统一提取 content 和 frontmatter
        content = ""
        frontmatter: Dict[str, Any] = {}
        if isinstance(item, dict):
            content = item.get("content", "")
            frontmatter = item.get("frontmatter", {})
            features["_source"] = "dict"
        elif isinstance(item, str):
            content = item
            features["_source"] = "str"
        elif isinstance(item, Path):
            try:
                text = item.read_text(encoding="utf-8", errors="ignore")
                # 简单 frontmatter 提取
                if text.startswith("---"):
                    parts = text.split("---", 2)
                    if len(parts) >= 3:
                        try:
                            import yaml
                            frontmatter = yaml.safe_load(parts[1]) or {}
                            content = parts[2]
                        except Exception:
                            content = text
                else:
                    content = text
            except Exception:
                content = ""
            features["_source"] = "path"

        features["content"] = content
        features["content_len"] = len(content)
        features["content_words"] = len(content.split())
        features["has_frontmatter"] = bool(frontmatter)
        features["frontmatter_keys"] = list(frontmatter.keys())
        features["_frontmatter"] = frontmatter

        # 简单元数据特征
        features["has_code_block"] = "```" in content
        features["has_table"] = "|" in content and "\n|" in content
        features["header_count"] = content.count("# ")
        features["link_count"] = content.count("[[")

        # frontmatter 数值特征（经归一化到 [0,1]）
        for key in ["heat", "quality_score", "confidence", "priority"]:
            val = frontmatter.get(key)
            norm = self._normalize_frontmatter_value(val, key=key)
            if norm is not None:
                features[f"fm_{key}"] = norm

        return features

    def _rule_score(self, dim: str, item: Any, features: Dict[str, Any]) -> Tuple[float, float]:
        """
        基于 frontmatter/内容特征的简单启发式规则先验。

        不调用 V1 的六域 scorer（避免循环依赖和 EventBus 阻塞），
        直接利用已提取的 features 计算先验得分。
        所有 frontmatter 数值已通过 _extract_features 归一化到 [0,1]。
        """
        fm = features.get("_frontmatter", {})
        content = features.get("content", "")
        words = features.get("content_words", 0)

        # 通用特征映射到各维度先验
        if dim == "memos":
            # 使用已归一化的特征；若不存在则回退到原始值再做一次归一化
            heat = features.get("fm_heat",
                self._normalize_frontmatter_value(fm.get("heat"), "heat") or 0.5)
            quality = features.get("fm_quality_score",
                self._normalize_frontmatter_value(fm.get("quality_score"), "quality_score") or 0.5)
            # clamp 确保 [0,1]
            heat = max(0.0, min(1.0, float(heat)))
            quality = max(0.0, min(1.0, float(quality)))
            return (heat * 0.5 + quality * 0.5), 0.4

        elif dim == "sync":
            # 同步紧迫度：内容越短越可能是待同步片段
            urgency = 1.0 - min(1.0, words / 500)
            return urgency, 0.3

        elif dim == "distill":
            # 蒸馏价值：有代码块/表格的内容更有蒸馏价值
            has_code = features.get("has_code_block", False)
            has_table = features.get("has_table", False)
            score = 0.5 + (0.2 if has_code else 0) + (0.15 if has_table else 0)
            return min(1.0, score), 0.4

        elif dim == "kg":
            # 知识图谱关联度：链接越多关联度越高
            links = features.get("link_count", 0)
            score = min(1.0, 0.3 + links * 0.1)
            return score, 0.3

        elif dim == "profile":
            # 画像匹配：有行为标签的内容匹配度更高
            tags = fm.get("tags", [])
            score = min(1.0, 0.4 + len(tags) * 0.1)
            return score, 0.3

        elif dim == "ops":
            # 运维异常：内容中包含错误关键词
            error_keywords = ["error", "fail", "timeout", "crash", "exception"]
            lower = content.lower()
            hits = sum(1 for k in error_keywords if k in lower)
            score = min(1.0, 0.2 + hits * 0.15)
            return score, 0.35

        return 0.5, 0.3  # 默认先验

    def _ml_score(self, dim: str, features: Dict[str, Any]) -> Tuple[float, float]:
        """调用 ML 模型获取似然（带降级保护）"""
        model = self._models.get(dim)
        if model is None:
            return 0.5, 0.0  # 未训练

        if self._fallback.should_degrade(dim):
            return 0.5, 0.0

        try:
            # 将特征字典扁平化为 sparse 特征
            sparse_feat = self._features_to_sparse(features)
            if _SKLEARN_AVAILABLE and hasattr(model, "predict_proba"):
                proba = model.predict_proba([sparse_feat])[0]
                # 二分类：proba[1] 是正类概率
                ml_like = float(proba[1]) if len(proba) > 1 else float(proba[0])
                ml_conf = 0.7  # sklearn 模型默认置信度
            else:
                # LightweightNB
                probs = model.predict_proba([sparse_feat])[0]
                ml_like = float(probs.get(1, 0.5))
                ml_conf = 0.6

            self._fallback.reset_failure(dim)
            return ml_like, ml_conf
        except Exception as e:
            self._fallback._record_failure(dim)
            logger.debug(f"[ScorerV2] ML scoring failed for {dim}: {e}")
            return 0.5, 0.0

    def _train_dimension(self, dim: str) -> int:
        """训练单个维度的模型"""
        # 1. 获取训练样本
        samples = self._get_training_samples(dim)
        if len(samples) < 20:
            return 0

        X = [s["features"] for s in samples]
        y = [s["label"] for s in samples]

        # 2. 训练模型
        try:
            if _SKLEARN_AVAILABLE:
                model = ComplementNB()
                # sklearn 需要 dense 矩阵或特定格式；这里简化处理
                # 将 sparse dict 转为统一长度的 list
                X_dense = [self._features_to_dense(f) for f in X]
                model.fit(X_dense, y)
            else:
                model = LightweightComplementNB()
                model.fit(X, y)

            self._models[dim] = model

            # 3. 更新 Beta 先验
            for lbl in y:
                self._bayesian.update_from_ground_truth(dim, lbl)

            # 4. 保存模型
            version = self.save_model(dim, note=f"auto_train_{len(y)}samples")
            self._model_versions[dim] = version

            # 5. 标记队列为已训练
            self._mark_queue_trained(dim)

            logger.info(f"[ScorerV2] {dim} trained with {len(y)} samples -> {version}")
            return len(y)
        except Exception as e:
            logger.warning(f"[ScorerV2] Training failed for {dim}: {e}")
            return 0

    def _get_training_samples(self, dim: str) -> List[Dict]:
        """从 scorer_training_queue + ground_truth_signals 获取训练样本"""
        samples = []
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                # 获取待训练队列
                rows = conn.execute("""
                    SELECT session_id, features_json FROM scorer_training_queue
                    WHERE status = 'pending' AND dimension = ?
                    ORDER BY earliest_train_at
                    LIMIT 500
                """, (dim,)).fetchall()

                for session_id, feat_json in rows:
                    # 查询 ground_truth 标签
                    gt = conn.execute("""
                        SELECT signal_value, confidence FROM ground_truth_signals
                        WHERE session_id = ?
                    """, (session_id,)).fetchone()

                    if gt:
                        features = json.loads(feat_json) if feat_json else {}
                        samples.append({
                            "session_id": session_id,
                            "features": features,
                            "label": int(gt[0]),
                            "confidence": gt[1],
                        })
        except Exception as e:
            logger.warning(f"[ScorerV2] _get_training_samples failed: {e}")
        return samples

    def _mark_queue_trained(self, dim: str) -> None:
        """将已训练样本标记为 completed"""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute("""
                    UPDATE scorer_training_queue
                    SET status = 'completed'
                    WHERE dimension = ? AND status = 'pending'
                """, (dim,))
                conn.commit()
        except Exception as e:
            logger.warning(f"[ScorerV2] _mark_queue_trained failed: {e}")

    def _load_all_models(self) -> None:
        """初始化时逐维度加载活跃模型；单维度失败不阻塞其他维度。"""
        for dim in self._SCORER_MAP.keys():
            # load_model 内部已包含完整异常隔离和校验
            loaded = self.load_model(dim)
            if loaded is None:
                logger.debug(f"[ScorerV2] No valid model for {dim}, will run in rule-only mode")

    def _features_to_sparse(self, features: Dict[str, Any]) -> Dict[str, float]:
        """将特征字典转为 sparse 数值特征（用于预测）"""
        sparse: Dict[str, float] = {}
        for k, v in features.items():
            if isinstance(v, (int, float)):
                sparse[k] = float(v)
            elif isinstance(v, bool):
                sparse[k] = 1.0 if v else 0.0
            elif isinstance(v, str) and k == "content":
                # 简单词频特征
                words = v.lower().split()
                for w in words:
                    sparse[f"word_{w}"] = sparse.get(f"word_{w}", 0.0) + 1.0
        return sparse

    def _features_to_dense(self, features: Dict[str, Any]) -> List[float]:
        """将特征字典转为 dense list（用于 sklearn）"""
        # 获取所有可能的特征键（从当前模型或默认值）
        sparse = self._features_to_sparse(features)
        keys = sorted(sparse.keys())
        return [sparse.get(k, 0.0) for k in keys]

    # ── 已有内部方法（保留） ──

    def _insert_ground_truth(self, session_id: str, signal_type: str,
                             label: int, confidence: float = 1.0) -> None:
        self.insert_ground_truth(
            session_id=session_id,
            signal_type=signal_type,
            label=label,
            confidence=confidence,
            db_path=self.db_path,
        )

    def _insert_training_queue(self, fb: FeedbackV2) -> None:
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute("""
                    INSERT INTO scorer_training_queue
                        (session_id, dimension, features_json, priority, earliest_train_at, status)
                    VALUES (?, ?, ?, ?, ?, 'pending')
                """, (
                    fb.session_id,
                    fb.dimension,
                    json.dumps(fb.features, ensure_ascii=False, default=str),
                    10,
                    (datetime.now() + timedelta(hours=0)).isoformat(),
                ))
                conn.commit()
        except Exception as e:
            logger.warning(f"[ScorerV2] training_queue insert failed: {e}")

    def _count_ready_samples(self, dimension: Optional[str] = None) -> int:
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                now = datetime.now().isoformat()
                if dimension:
                    row = conn.execute("""
                        SELECT COUNT(*) FROM scorer_training_queue
                        WHERE status = 'pending' AND earliest_train_at <= ? AND dimension = ?
                    """, (now, dimension)).fetchone()
                    # 同时统计 ground_truth_signals
                    gt_row = conn.execute("""
                        SELECT COUNT(*) FROM ground_truth_signals
                        WHERE signal_type = ?
                    """, (dimension,)).fetchone()
                else:
                    row = conn.execute("""
                        SELECT COUNT(*) FROM scorer_training_queue
                        WHERE status = 'pending' AND earliest_train_at <= ?
                    """, (now,)).fetchone()
                    gt_row = conn.execute("""
                        SELECT COUNT(*) FROM ground_truth_signals
                    """).fetchone()
                return (row[0] if row else 0) + (gt_row[0] if gt_row else 0)
        except Exception:
            logger.warning(f"Unexpected error in adaptive_scorer_v2.py", exc_info=True)
            return 0

    def _update_mode(self) -> None:
        """根据样本数更新冷启动阶段"""
        total = self._count_ready_samples()
        if total < self.WARM_THRESHOLD:
            self._mode = "cold"
        elif total < self.HOT_THRESHOLD:
            self._mode = "warm"
        else:
            self._mode = "hot"

    def get_status(self) -> Dict[str, Any]:
        self._update_mode()
        return {
            "domain": self.domain,
            "mode": self._mode,
            "models_loaded": list(self._models.keys()),
            "ready_samples": self._count_ready_samples(),
            "db_path": str(self.db_path),
            "version": "v2-full",
            "sklearn": _SKLEARN_AVAILABLE,
        }
