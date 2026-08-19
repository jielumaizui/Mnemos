# -*- coding: utf-8 -*-
"""Rule-first scorer with a COG-048 governed-model activation boundary."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Mapping, NoReturn, Optional, Tuple

from core.config import get_config
from core.scoring.model_call_boundary import (
    SubjectScope,
    embed_for_adaptive_score,
)
from core.db_utils import sqlite_conn
from core.kia.policy import get_effective_policy
from core.scoring.adaptive_scorer_support import (  # noqa: F401
    FeedbackV2,
    GroundTruth,
    ScoreCardV2,
    ScorerFeatureMixin,
    SklearnPartialFitNB,
)

logger = logging.getLogger(__name__)

LEGACY_TRAINING_ERROR = "training_admission_receipt_required"


def _reject_retired_training(operation: str) -> NoReturn:
    raise PermissionError(f"{LEGACY_TRAINING_ERROR}:{operation}")


_MODEL_SERIALIZATION = "json"

# V2 子模块
from core.scoring.bayesian_scorer import BayesianScorer  # noqa: E402
from core.scoring.fallback import ScorerFallback  # noqa: E402
from core.scoring.subject_provenance import ensure_scoring_subject_provenance_schema  # noqa: E402

# sklearn 可选导入（标准环境）
try:
    pass

    _SKLEARN_AVAILABLE = True
except ImportError:
    _SKLEARN_AVAILABLE = False

# 从 V1 迁移过来的规则辅助函数（不再依赖 V1 scorer 类）
from core.scoring import rule_helpers  # noqa: E402

# Constants extracted from magic numbers
ADAPTIVE_SCORER_V2_WARM_THRESHOLD = 30
ADAPTIVE_SCORER_V2_DURATION_BUCKET_MONTH_DAYS = 30
ADAPTIVE_SCORER_V2_DURATION_BUCKET_WEEK_DAYS = 7


class GovernedBinaryCentroid:
    """Runtime adapter for one repeatedly verified governed model artifact."""

    def __init__(self, governance: Any, principal: Any, snapshot: Any):
        self._governance = governance
        self._principal = principal
        self._run_revision_id = str(snapshot.run_revision_id)
        self._model_id = str(snapshot.model_id)
        self._model_blob_hash = str(snapshot.model_blob_hash)
        blob = dict(snapshot.model_blob)
        self._feature_names = tuple(str(value) for value in blob["feature_names"])
        self._negative = tuple(float(value) for value in blob["negative_centroid"])
        self._positive = tuple(float(value) for value in blob["positive_centroid"])

    def predict_proba(self, rows: List[Mapping[str, Any]]) -> List[Dict[int, float]]:
        """Score rows only while the exact governed artifact remains current."""

        current = self._governance.load_applied_model(
            self._run_revision_id,
            self._principal,
        )
        if current.model_id != self._model_id or current.model_blob_hash != self._model_blob_hash:
            raise RuntimeError("governed scorer model changed after activation")
        result: List[Dict[int, float]] = []
        for row in rows:
            vector = tuple(float(row.get(name, 0.0)) for name in self._feature_names)
            negative_distance = sum(
                (value - center) ** 2 for value, center in zip(vector, self._negative)
            )
            positive_distance = sum(
                (value - center) ** 2 for value, center in zip(vector, self._positive)
            )
            total = negative_distance + positive_distance
            positive_probability = 0.5 if total == 0 else negative_distance / total
            result.append({0: 1.0 - positive_probability, 1: positive_probability})
        return result


def _parse_feature_frontmatter_yaml(source: str) -> Dict[str, Any]:
    import yaml

    return yaml.safe_load(source) or {}


# ==================== AdaptiveScorerV2（完整实现） ====================


class AdaptiveScorerV2(ScorerFeatureMixin):
    """自适应评分引擎 V2"""

    _parse_frontmatter_yaml = staticmethod(_parse_feature_frontmatter_yaml)

    # 三阶段阈值
    COLD_THRESHOLD = 0
    WARM_THRESHOLD = ADAPTIVE_SCORER_V2_WARM_THRESHOLD
    HOT_THRESHOLD = 200

    # 维度注册表的 value 已停用，只保留当前调用方所需的 key。
    _SCORER_MAP = {
        "sync": None,
        "distill": None,
        "kg": None,
        "profile": None,
        "ops": None,
        "falsify": None,
        "evolve": None,
        "heat": None,
        "predictive_delivery": None,
    }

    _DIMENSION_ALIASES = {
        "l1": "l1_storage",
        "session_quality": "l1_storage",
        "memory_quality": "l1_storage",
        "capture_quality": "l1_storage",
        "engagement": "profile",
        "persona": "profile",
        "user_profile": "profile",
        "correction_pattern": "ops",
        "error_pattern": "ops",
        "health": "ops",
        "distill_complete": "distill",
        "distill_skip": "distill",
        "knowledge_distilled": "distill",
        "knowledge_graph": "kg",
        "relation_quality": "kg",
    }

    def __init__(
        self,
        domain: str = "mnemos",
        config: Dict[str, Any] | None = None,
        db_path: Optional[str] = None,
        *,
        governance_state_store: Any = None,
        governance_principal: Any = None,
    ):
        self.domain = domain
        self.config = self._load_config(config)

        # 真正调用配置校验，仅记录警告不阻塞初始化
        cfg_errors = self.validate_scorer_config(self.config)
        if cfg_errors:
            logger.warning("[ScorerV2] Config validation warnings: %s", cfg_errors)

        self.db_path = Path(db_path) if db_path else (get_config().database_dir / "mnemos.db")
        self._governance_state_store = governance_state_store
        self._governance_principal = governance_principal

        self._mode = "cold"
        self._models: Dict[str, Any] = {}  # dimension → model
        self._model_versions: Dict[str, str] = {}
        self._governed_rule_weights: Dict[str, Dict[str, Any]] = {}
        self._governed_effect_bindings: Dict[str, Tuple[Any, Any, str]] = {}
        # l1_storage 有独立规则先验，必须注册到贝叶斯融合器中，否则会被当作未知维度忽略规则先验。
        bayesian_cfg = self.config.get("bayesian", {})
        self._bayesian = BayesianScorer(
            dimensions=list(self._SCORER_MAP.keys()) + ["l1_storage"],
            db_path=self.db_path,
            prior_alpha=bayesian_cfg.get("alpha_prior", 1.0),
            prior_beta=bayesian_cfg.get("beta_prior", 1.0),
            neg_likelihood=bayesian_cfg.get("explicit_neg_likelihood", 0.3),
            rule_weight_cold=bayesian_cfg.get("rule_weight_cold", 3.0),
            rule_weight_hot=bayesian_cfg.get("rule_weight_hot", 0.5),
            persistent=False,
        )
        self._fallback = ScorerFallback()
        self._confidence_window: List[float] = []  # [P2-21] embedding 条件计算的置信度窗口
        self._embedding_index_manager = None  # [P1-31] 懒加载缓存

        # Pre-COG-048 scorer/Bayesian rows are historical-only after cutover.

    @classmethod
    def ensure_tables(cls, db_path: Optional[str] = None) -> None:
        """Initialize only the operational search-session schema.

        COG-048 intentionally does not create any pre-cutover training, model, or
        Bayesian table.  Historical instances are migration inputs only.
        """
        db = Path(db_path) if db_path else (get_config().database_dir / "mnemos.db")
        try:
            with sqlite_conn(str(db)) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                # search_sessions：搜索会话追踪（点击/忽略信号采集）
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_sessions (
                        id INTEGER PRIMARY KEY,
                        session_id TEXT NOT NULL UNIQUE,
                        query TEXT,
                        result_paths TEXT,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        clicked_path TEXT,
                        clicked_at TEXT,
                        opened_path TEXT,
                        opened_at TEXT,
                        ignored_at TEXT,
                        outcome_status TEXT DEFAULT '',
                        outcome_at TEXT
                    )
                """
                )
                search_columns = {
                    str(row[1]) for row in conn.execute("PRAGMA table_info(search_sessions)")
                }
                if "opened_path" not in search_columns:
                    conn.execute("ALTER TABLE search_sessions ADD COLUMN opened_path TEXT")
                if "opened_at" not in search_columns:
                    conn.execute("ALTER TABLE search_sessions ADD COLUMN opened_at TEXT")
                if "ignored_at" not in search_columns:
                    conn.execute("ALTER TABLE search_sessions ADD COLUMN ignored_at TEXT")
                if "outcome_status" not in search_columns:
                    conn.execute(
                        "ALTER TABLE search_sessions ADD COLUMN outcome_status TEXT DEFAULT ''"
                    )
                if "outcome_at" not in search_columns:
                    conn.execute("ALTER TABLE search_sessions ADD COLUMN outcome_at TEXT")
                ensure_scoring_subject_provenance_schema(conn)
                conn.commit()
        except sqlite3.Error as e:
            logger.warning("[ScorerV2] ensure_tables failed: %s", e, exc_info=True)

    @staticmethod
    def _load_config(user_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """加载并合并配置：默认值 < config/scorer.yaml < 用户传入（深度合并）"""
        defaults = {
            "backend": "standard" if _SKLEARN_AVAILABLE else "lightweight",
            "training": {
                "min_samples_per_dimension": get_effective_policy().get(
                    "scoring.min_samples_per_dimension", 20
                ),
                "min_confidence": 0.7,
                "max_queue_size": 500,
                "retention_days": ADAPTIVE_SCORER_V2_DURATION_BUCKET_MONTH_DAYS,
            },
            "bayesian": {
                "alpha_prior": 1.0,
                "beta_prior": 1.0,
                "explicit_neg_likelihood": 0.3,
                # [P2-23] 规则权重使用 cold→hot 的指数衰减曲线，
                # 不再使用阶梯式的 warm 阈值。
                "rule_weight_cold": 3.0,
                "rule_weight_hot": 0.5,
            },
            "fallback": {
                "max_consecutive_failures": 3,
                "degrade_to_rule": True,
            },
            "persistence": {
                "format": _MODEL_SERIALIZATION,
                "max_versions": 5,
                "auto_save_after_training": True,
            },
            "dimensions": {
                "sync": True,
                "distill": True,
                "kg": True,
                "profile": True,
                "ops": True,
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
        except ImportError:
            logger.debug("[adaptive_scorer_v2] ImportError suppressed", exc_info=True)

        # 三层深度合并：defaults < yaml < user
        merged = AdaptiveScorerV2._deep_merge(defaults, yaml_config)
        if user_config:
            merged = AdaptiveScorerV2._deep_merge(merged, user_config)
        if merged.get("backend") == "standard" and not _SKLEARN_AVAILABLE:
            merged["backend"] = "lightweight"
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

    def score(
        self,
        item: Any,
        dimensions: List[str],
        *,
        subject_scope: SubjectScope | None = None,
    ) -> ScoreCardV2:
        """
        多维度评分：特征提取 → 规则先验 → [P2-21] embedding 条件计算 → ML 似然 → 贝叶斯后验。

        ``subject_scope`` is required before visible user-derived content may
        reach the optional embedding provider.  A direct ``Path`` input is
        itself a durable asset identity and may therefore derive a ``path``
        scope; bare text and dicts deliberately do not receive a generic
        scorer fallback.  System-owned content must likewise be declared by
        its owning caller with an explicit ``("source", owner)`` scope.
        """
        features = self._extract_features(item)
        embedding_subject_scope = self._resolve_embedding_subject_scope(item, subject_scope)
        scores: Dict[str, float] = {}
        confidences: Dict[str, float] = {}
        rule_results: Dict[str, Tuple[str, float, float]] = {}

        # 将外部/历史别名统一为内部维度名，避免规则先验和 ML 模型找不到分支。
        dim_norm_map = {dim: self.normalize_dimension(dim) for dim in dimensions}

        # 1. 规则先验（全维度预计算，用于判断是否需要 embedding）
        for dim in dimensions:
            norm_dim = dim_norm_map[dim]
            rule_prior, rule_conf = self._rule_score(norm_dim, item, features)
            rule_results[dim] = (norm_dim, rule_prior, rule_conf)

        # [P2-21] 对低置信度样本（bottom 20%）计算 embedding 相似度
        rule_confs = [rc for _, _, rc in rule_results.values()]
        if self._should_compute_embedding(rule_confs) and embedding_subject_scope is not None:
            emb_sim = self._compute_embedding_similarity(
                features.get("content", ""),
                subject_scope=embedding_subject_scope,
            )
            if emb_sim is not None:
                features["embedding_sim_to_high_quality"] = emb_sim
                # embedding 特征可能影响规则先验，重新计算
                for dim in dimensions:
                    norm_dim = dim_norm_map[dim]
                    rule_prior, rule_conf = self._rule_score(norm_dim, item, features)
                    rule_results[dim] = (norm_dim, rule_prior, rule_conf)

        for dim in dimensions:
            norm_dim, rule_prior, rule_conf = rule_results[dim]

            # 2. ML 似然（带降级保护）
            ml_like, ml_conf = self._ml_score(norm_dim, features)

            # 3. 贝叶斯融合
            post, post_conf = self._bayesian.fuse(
                dimension=norm_dim,
                rule_prior=rule_prior,
                ml_likelihood=ml_like,
                ml_confidence=ml_conf,
            )
            scores[dim] = post
            confidences[dim] = post_conf

        if self._models:
            dim_versions = []
            for dim in dimensions:
                if dim in self._models:
                    dim_versions.append(f"{dim}=ml")
                else:
                    dim_versions.append(f"{dim}=rule")
            version = "v2:" + ",".join(dim_versions)
        else:
            version = "v2-rule-only"
        return ScoreCardV2(
            scores=scores,
            confidences=confidences,
            features=features,
            model_version=version,
        )

    def feedback(self, fb: FeedbackV2) -> Dict[str, Any]:
        """Reject reaction or caller-score promotion into training."""

        del fb
        _reject_retired_training("feedback")

    def apply_governed_run(self, run_revision_id: str) -> str:
        """Activate one exact current governed run after full receipt validation."""

        from core.cognitive.state_store import CognitiveStateStore
        from core.cognitive.training_governance import TrainingGovernanceStore

        if not isinstance(self._governance_state_store, CognitiveStateStore):
            raise PermissionError("training_admission_receipt_required:governance_state_store")
        if self._governance_principal is None:
            raise PermissionError("training_admission_receipt_required:governance_principal")
        governance = TrainingGovernanceStore(
            self._governance_state_store,
            database_dir=self.db_path.parent,
        )
        snapshot = governance.load_applied_model(
            str(run_revision_id or ""),
            self._governance_principal,
        )
        if snapshot.dimension != "predictive_delivery":
            raise ValueError("unknown governed scorer dimension")
        self._models[snapshot.dimension] = GovernedBinaryCentroid(
            governance,
            self._governance_principal,
            snapshot,
        )
        prior = self._bayesian.priors[snapshot.dimension]
        prior.alpha = float(snapshot.bayesian_prior["alpha"])
        prior.beta = float(snapshot.bayesian_prior["beta"])
        prior.total_samples = int(snapshot.bayesian_prior["total_samples"])
        prior.last_updated = str(snapshot.bayesian_prior.get("artifact_hash") or "")
        self._governed_rule_weights[snapshot.dimension] = dict(snapshot.rule_optimizer)
        self._governed_effect_bindings[snapshot.dimension] = (
            governance,
            self._governance_principal,
            snapshot.run_revision_id,
        )
        self._model_versions[snapshot.dimension] = snapshot.model_id
        self._mode = "hot"
        return snapshot.model_id

    @classmethod
    def enqueue_training_sample(
        cls,
        session_id: str,
        dimension: str,
        features: Dict[str, Any],
        expected_score: float,
        source: str,
        db_path: Optional[str] = None,
        subject_provenance: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Reject the retired caller-labelled queue and weak-ground-truth writer."""

        del cls, session_id, dimension, features, expected_score
        del source, db_path, subject_provenance
        _reject_retired_training("enqueue_training_sample")

    # ── 批量训练接口 ──

    def process_training_queue(self, dimension: Optional[str] = None) -> int:
        """Reject the retired pre-COG-048 queue trainer."""

        del dimension
        _reject_retired_training("process_training_queue")

    # ── 模型管理 ──

    def save_model(
        self,
        dimension: str,
        note: Optional[str] = None,
        train_samples: Optional[int] = None,
        source_refs: Tuple[Tuple[str, str], ...] = (),
    ) -> str:
        """Reject arbitrary or pre-COG-048 model persistence."""

        del dimension, note, train_samples, source_refs
        _reject_retired_training("save_model")

    def load_model(self, dimension: str, version: Optional[str] = None) -> Any:
        """Reject pre-cutover model identity; use apply_governed_run(revision_id)."""

        del dimension, version
        _reject_retired_training("load_model")

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
        subject_provenance: Mapping[str, Any] | None = None,
    ) -> None:
        """Reject caller-provided labels without a canonical admission receipt."""

        del cls, session_id, signal_type, label, confidence
        del latency_hours, db_path, subject_provenance
        _reject_retired_training("insert_ground_truth")

    # ── 内部方法 ──

    # ── [P2-21] 特征提取 helper 方法 ──

    def _extract_kg_features(
        self, content: str, item_path: Optional[Path], features: Dict[str, Any]
    ) -> None:
        """提取知识图谱特征（~6维）"""
        features["kg_entity_density"] = 0.0
        features["kg_relation_out_count"] = 0
        features["kg_relation_in_count"] = 0
        features["kg_relation_richness"] = 0.0
        features["kg_connectivity_score"] = 0.0
        features["kg_avg_relation_strength"] = 0.0

        try:
            from core.kia.knowledge_graph import KnowledgeGraph

            page_id = None
            if item_path:
                try:
                    page_id = str(
                        item_path.expanduser()
                        .resolve(strict=False)
                        .relative_to(Path(get_config().wiki_dir).expanduser().resolve(strict=False))
                    )
                except ValueError:
                    page_id = None

            if page_id:
                kg = KnowledgeGraph(initialize=False, read_only=True)
                out_rels = kg.get_relations(page=page_id, min_confidence=0.0)
                in_rels = kg.get_incoming_relations(page=page_id, min_confidence=0.0)

                features["kg_relation_out_count"] = len(out_rels)
                features["kg_relation_in_count"] = len(in_rels)

                all_rels = list(out_rels) + list(in_rels)
                if all_rels:
                    unique_types = set(getattr(r, "relation_type", None) for r in all_rels)
                    features["kg_relation_richness"] = len(
                        [t for t in unique_types if t is not None]
                    ) / max(len(all_rels), 1)
                    strengths = [
                        getattr(r, "strength", 0.0) for r in all_rels if hasattr(r, "strength")
                    ]
                    if strengths:
                        features["kg_avg_relation_strength"] = sum(strengths) / len(strengths)

                # 关联度：邻居数量
                cluster = kg.get_related_cluster(page=page_id, depth=1, min_strength=0.3)
                features["kg_connectivity_score"] = min(1.0, len(cluster) / 20.0)

            # 实体密度：从内容中的 [[link]] 推断
            wiki_links = content.count("[[")
            words = len(content.split())
            if words > 0:
                features["kg_entity_density"] = min(1.0, wiki_links * 10.0 / words)

        except ImportError:
            logger.debug("[adaptive_scorer_v2] ImportError suppressed", exc_info=True)

    def _compute_embedding_similarity(
        self,
        content: str,
        *,
        subject_scope: SubjectScope | None = None,
    ) -> Optional[float]:
        """计算内容与历史高质量内容的平均 embedding 余弦相似度"""
        if not content or len(content) < 20 or subject_scope is None:
            return None

        try:
            from core.embeddings.siliconflow_client import get_embedding_client
            from core.embeddings.index_manager import EmbeddingIndexManager

            client = get_embedding_client()

            # 获取当前内容 embedding（截断控制成本）
            current_vec = embed_for_adaptive_score(
                client,
                content[:2000],
                get_config(),
                subject_scope=subject_scope,
            )
            if not current_vec:
                return None

            # 从索引中获取相似的高质量内容（复用缓存的索引管理器）
            if self._embedding_index_manager is None:
                self._embedding_index_manager = EmbeddingIndexManager()  # type: ignore[assignment]
            idx = self._embedding_index_manager
            # type: ignore[attr-defined]
            similar = idx.search(  # type: ignore[attr-defined]
                content[:200],
                top_k=5,
                similarity_threshold=0.5,
                subject_scope=subject_scope,
            )

            if not similar:
                return None

            similarities = []
            for _path, sim in similar:
                if sim > 0.3:
                    similarities.append(sim)

            if similarities:
                return sum(similarities) / len(similarities)  # type: ignore[no-any-return]

            return None

        # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
            sqlite3.Error,
        ):
            return None

    def _rule_score(self, dim: str, item: Any, features: Dict[str, Any]) -> Tuple[float, float]:
        """
        基于 frontmatter/内容特征的启发式规则先验。

        V1 五域 scorer 中真正有价值的规则已迁移到 core.scoring.rule_helpers，
        这里直接调用；不再依赖 V1 scorer 类，避免循环依赖和 EventBus 阻塞。
        所有 frontmatter 数值已通过 _extract_features 归一化到 [0,1]。
        """
        dim = self.normalize_dimension(dim)
        fm = features.get("_frontmatter", {})
        content = features.get("content", "")
        features.get("content_words", 0)

        if dim == "predictive_delivery":
            artifact = self._refresh_governed_aux_effects(dim)
            if artifact is not None:
                weights = artifact["weights"]
                bias = float(artifact["bias"])
                adjustment = sum(
                    float(weights[name]) * (float(features.get(name, 0.5)) - 0.5)
                    for name in artifact["feature_names"]
                )
                score = max(0.0, min(1.0, bias + adjustment * 0.5))
                confidence = min(1.0, 0.5 + int(artifact["sample_count"]) / 200.0)
                return score, confidence

        if dim == "sync":
            # 同步紧迫度：V1 SyncScorer 的 urgency 规则（崩溃/异常/故障关键词）
            score = rule_helpers.sync_urgency_score(content)
            return score, 0.3

        elif dim == "distill":
            # 蒸馏价值：V1 DistillScorer 的完整 RuleScorer 评分
            score = rule_helpers.distill_value_score(content)
            return score, 0.4

        elif dim == "falsify":
            # 可证伪性：V1 DistillScorer._falsify_rule
            score = rule_helpers.falsifiability_score(content)
            return score, 0.35

        elif dim == "evolve":
            # 进化潜力：V1 DistillScorer._evolve_rule
            score = rule_helpers.evolution_score(content)
            return score, 0.35

        elif dim == "heat":
            # 热度预测：V1 DistillScorer._heat_rule
            score = rule_helpers.heat_score(content, has_code=features.get("has_code_block", False))
            return score, 0.35

        elif dim == "kg":
            # 知识图谱关联度：链接数 + V1 relation_confidence 规则
            links = features.get("link_count", 0)
            link_score = min(1.0, 0.3 + links * 0.1)
            relation_score = rule_helpers.relation_confidence_score(content)
            score = 0.6 * link_score + 0.4 * relation_score
            return score, 0.3

        elif dim == "profile":
            # 画像匹配：行为模式 + 标签丰富度
            behavior = rule_helpers.profile_behavior_score(
                content, has_code=features.get("has_code_block", False)
            )
            tags = fm.get("tags", [])
            tag_bonus = min(0.3, len(tags) * 0.05)
            score = min(1.0, behavior + tag_bonus)
            return score, 0.3

        elif dim == "ops":
            # 运维异常：V1 OpsScorer._anomaly_rule 的 richer keyword set
            score = rule_helpers.ops_anomaly_score(content)
            return score, 0.35

        elif dim == "l1_storage":
            # L1 存储质量：基于 frontmatter heat 和 quality_score
            heat = features.get("fm_heat", 0.5)
            qscore = features.get("fm_quality_score", 0.5)
            score = min(1.0, 0.5 + heat * 0.22 + qscore * 0.25)
            return score, 0.4

        return 0.5, 0.3  # 默认先验

    def _refresh_governed_aux_effects(self, dimension: str) -> Dict[str, Any] | None:
        binding = self._governed_effect_bindings.get(dimension)
        if binding is None:
            return None
        governance, principal, run_revision_id = binding
        try:
            snapshot = governance.load_applied_model(run_revision_id, principal)
        except (OSError, PermissionError, RuntimeError, TypeError, ValueError):
            self._models.pop(dimension, None)
            self._model_versions.pop(dimension, None)
            self._governed_rule_weights.pop(dimension, None)
            self._governed_effect_bindings.pop(dimension, None)
            self._bayesian.priors[dimension] = self._bayesian._fresh_prior()
            return None
        prior = self._bayesian.priors[dimension]
        prior.alpha = float(snapshot.bayesian_prior["alpha"])
        prior.beta = float(snapshot.bayesian_prior["beta"])
        prior.total_samples = int(snapshot.bayesian_prior["total_samples"])
        prior.last_updated = str(snapshot.bayesian_prior.get("artifact_hash") or "")
        artifact = dict(snapshot.rule_optimizer)
        self._governed_rule_weights[dimension] = artifact
        return artifact

    def _ml_score(self, dim: str, features: Dict[str, Any]) -> Tuple[float, float]:
        """调用 ML 模型获取似然（带降级保护）"""
        dim = self.normalize_dimension(dim)
        model = self._models.get(dim)
        if model is None:
            return 0.5, 0.0  # 未训练

        if self._fallback.should_degrade(dim):
            return 0.5, 0.0

        try:
            # 将特征字典扁平化为 sparse 特征
            sparse_feat = self._features_to_sparse(features)
            if type(model).__name__ in ("SklearnPartialFitNB", "Pipeline") and hasattr(
                model, "predict_proba"
            ):
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
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, ArithmeticError) as e:
            self._fallback._record_failure(dim)
            if isinstance(model, GovernedBinaryCentroid):
                self._models.pop(dim, None)
                self._model_versions.pop(dim, None)
                self._governed_rule_weights.pop(dim, None)
                self._governed_effect_bindings.pop(dim, None)
                self._bayesian.priors[dim] = self._bayesian._fresh_prior()
            logger.debug("[ScorerV2] ML scoring failed for %s: %s", dim, e, exc_info=True)
            return 0.5, 0.0

    def _load_all_models(self) -> None:
        """Pre-cutover model loading is retired; governed runs use an exact API."""

        _reject_retired_training("load_all_models")

    def _features_to_sparse(self, features: Dict[str, Any]) -> Dict[str, float]:
        """将特征字典转为 sparse 数值特征（用于预测）"""
        sparse: Dict[str, float] = {"__bias__": 1.0}
        for k, v in features.items():
            if isinstance(v, bool):
                sparse[k] = 1.0 if v else 0.0
            elif isinstance(v, (int, float)):
                sparse[k] = float(v)
            elif isinstance(v, str) and k == "content":
                # 简单词频特征
                words = v.lower().split()
                for w in words:
                    sparse[f"word_{w}"] = sparse.get(f"word_{w}", 0.0) + 1.0
            elif isinstance(v, str) and v:
                sparse[f"{k}={v[:80]}"] = 1.0
            elif isinstance(v, (list, tuple, set)):
                for item in v:
                    if isinstance(item, (str, int, float, bool)):
                        sparse[f"{k}={str(item)[:80]}"] = 1.0
        return sparse

    def _features_to_dense(self, features: Dict[str, Any]) -> List[float]:
        """将特征字典转为 dense list（用于 sklearn）"""
        # 获取所有可能的特征键（从当前模型或默认值）
        sparse = self._features_to_sparse(features)
        keys = sorted(sparse.keys())
        return [sparse.get(k, 0.0) for k in keys]

    def refresh_bayesian_priors_from_ground_truth(
        self,
        dimensions: Optional[List[str]] = None,
    ) -> Dict[str, int]:
        """Reject Bayesian reconstruction from historical caller labels."""

        del dimensions
        _reject_retired_training("refresh_bayesian_priors_from_ground_truth")

    # ── 已有内部方法（保留） ──

    def _count_ready_samples(self, dimension: Optional[str] = None) -> int:
        """Count current admitted governed samples without pre-cutover rows."""

        normalized = self.normalize_dimension(dimension or "predictive_delivery")
        if normalized != "predictive_delivery" or not self.db_path.is_file():
            return 0
        try:
            with sqlite3.connect(
                f"file:{self.db_path.resolve(strict=True)}?mode=ro",
                uri=True,
            ) as conn:
                from core.scoring.training_schema import inspect_training_schema

                if not inspect_training_schema(conn).ok:
                    return 0
                row = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM governed_training_samples AS sample
                    WHERE sample.dimension='predictive_delivery'
                      AND (
                        SELECT action.action_type
                        FROM governed_training_sample_actions AS action
                        WHERE action.sample_id=sample.sample_id
                        ORDER BY action.created_at DESC, action.action_id DESC
                        LIMIT 1
                      )='admit'
                    """
                ).fetchone()
                return int(row[0]) if row else 0
        except (OSError, RuntimeError, sqlite3.Error):
            return 0

    def _count_signal_samples(self, domain: Optional[str] = None) -> int:
        """Count only governed samples for the first supported dimension."""

        dim = self.normalize_dimension(domain or self.domain)
        if dim != "predictive_delivery":
            return 0
        return self._count_ready_samples(dim)

    def _update_mode(self) -> None:
        """根据样本数更新冷启动阶段"""
        total = self._count_signal_samples()
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
            "mode_thresholds": {
                "cold": self.COLD_THRESHOLD,
                "warm": self.WARM_THRESHOLD,
                "hot": self.HOT_THRESHOLD,
            },
            "models_loaded": list(self._models.keys()),
            "ready_samples": self._count_ready_samples(),
            "signal_samples": self._count_signal_samples(),
            "db_path": str(self.db_path),
            "version": "v2-full",
            "sklearn": _SKLEARN_AVAILABLE,
        }
