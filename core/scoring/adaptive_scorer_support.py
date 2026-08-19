# -*- coding: utf-8 -*-
"""Data models and feature helpers for AdaptiveScorerV2."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, List, Mapping, Optional, Tuple

from core.db_utils import delete_older_than, sqlite_conn
from core.scoring.lightweight_nb import LightweightComplementNB
from core.scoring.model_call_boundary import (
    SubjectScope,
    require_adaptive_score_subject_scope,
)

logger = logging.getLogger(__name__)

_MODEL_SERIALIZATION = "json"
ADAPTIVE_SCORER_V2_DURATION_BUCKET_WEEK_DAYS = 7


class SklearnPartialFitNB:
    """[P1-3] sklearn ComplementNB + FeatureHasher 增量学习包装器。

    FeatureHasher 是 stateless 的（固定特征空间），因此可以安全地
    partial_fit 而不丢失旧样本的特征信息。
    """

    def __init__(self, n_features: int = 2**10):
        self.n_features = n_features
        self._hasher = None
        self._classifier = None
        self._classes = None
        self.is_fitted = False

    def _ensure_init(self):
        if self._hasher is None:
            from sklearn.feature_extraction import FeatureHasher
            from sklearn.naive_bayes import ComplementNB

            self._hasher = FeatureHasher(n_features=self.n_features, alternate_sign=False)
            self._classifier = ComplementNB()

    def fit(self, X: List[Dict], y: List[int]) -> "SklearnPartialFitNB":
        """批量训练（首次训练时等同于 partial_fit + classes）"""
        return self.partial_fit(X, y, classes=[0, 1])

    def partial_fit(
        self, X: List[Dict], y: List[int], classes: Optional[List[int]] = None
    ) -> "SklearnPartialFitNB":
        """增量训练"""
        self._ensure_init()
        X_hashed = self._hasher.transform(X)  # type: ignore[attr-defined]
        if self.is_fitted:
            self._classifier.partial_fit(X_hashed, y)  # type: ignore[attr-defined]
        else:
            self._classifier.partial_fit(  # type: ignore[attr-defined]
                X_hashed, y, classes=classes or [0, 1]
            )  # type: ignore[attr-defined]
            self.is_fitted = True
        return self

    def predict_proba(self, X: List[Dict]):
        self._ensure_init()
        X_hashed = self._hasher.transform(X)  # type: ignore[attr-defined]
        return self._classifier.predict_proba(X_hashed)  # type: ignore[attr-defined]

    def predict(self, X: List[Dict]):
        self._ensure_init()
        X_hashed = self._hasher.transform(X)  # type: ignore[attr-defined]
        return self._classifier.predict(X_hashed)  # type: ignore[attr-defined]


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
    subject_provenance: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class GroundTruth:
    """外部真实信号"""

    session_id: str
    signal_type: str
    label: int
    confidence: float = 1.0
    latency_hours: int = 0


class ScorerFeatureMixin:
    """Feature extraction, safe model serialization, and cleanup helpers."""

    # Structural contract supplied by ``AdaptiveScorerV2``.
    _DIMENSION_ALIASES: ClassVar[Mapping[str, str]]
    db_path: Path
    domain: str
    _parse_frontmatter_yaml: Callable[[str], Dict[str, Any]]
    _extract_kg_features: Callable[[str, Optional[Path], Dict[str, Any]], None]
    _confidence_window: List[float]

    @classmethod
    def normalize_dimension(cls, dimension: str) -> str:
        """将历史/外部信号维度归一到六域评分器维度。"""
        dim = (dimension or "").strip()
        return cls._DIMENSION_ALIASES.get(dim, dim)

    @staticmethod
    def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """深度合并两个字典：override 递归覆盖 base 的同名键。"""
        result = base.copy()
        for key, val in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(val, dict):
                result[key] = ScorerFeatureMixin._deep_merge(result[key], val)
            else:
                result[key] = val
        return result

    @staticmethod
    def _resolve_embedding_subject_scope(
        item: Any,
        subject_scope: SubjectScope | None,
    ) -> SubjectScope | None:
        """Return only an explicit owner for an optional billable embedding.

        A filesystem ``Path`` is an exact, durable asset identity.  Other
        representations can contain user text without provenance, so they
        must be accompanied by an explicit typed subject supplied by their
        owning call path.  Returning ``None`` is intentional fail-closed
        behavior: normal local scoring remains available, but no provider
        call is allowed.
        """
        if subject_scope is not None:
            return require_adaptive_score_subject_scope(subject_scope)
        if isinstance(item, Path):
            return "path", str(item.expanduser().resolve(strict=False))
        return None

    def cleanup_older_than(self, days: int, dry_run: bool = False) -> int:
        """Clean operational search sessions; historical training rows stay immutable."""

        with sqlite_conn(str(self.db_path)) as conn:
            return delete_older_than(
                conn,
                "search_sessions",
                "created_at",
                days,
                dry_run=dry_run,
            )

    @staticmethod
    def _serialize_model(model: Any) -> Tuple[bytes, str, Dict[str, str]]:
        """Serialize supported models without pickle."""
        if not isinstance(model, LightweightComplementNB):
            raise ValueError(f"Unsupported model type for safe persistence: {type(model).__name__}")

        payload = {
            "model_class": "LightweightComplementNB",
            "state": model.to_dict(),
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return (
            blob,
            "lightweight_nb_json",
            {
                "model_class": "LightweightComplementNB",
                "serialization": _MODEL_SERIALIZATION,
            },
        )

    @staticmethod
    def _deserialize_model(blob: bytes, model_type: str, meta: Dict[str, Any]) -> Any:
        """Deserialize only explicitly supported safe JSON model formats."""
        if meta.get("serialization") != _MODEL_SERIALIZATION:
            logger.warning(
                "[ScorerV2] Refusing legacy/non-json model serialization: %s",
                meta.get("serialization") or "missing",
            )
            return None

        if model_type not in ("lightweight_nb_json", "lightweight_nb"):
            logger.warning("[ScorerV2] Unsupported model_type for safe load: %s", model_type)
            return None

        data = json.loads(blob.decode("utf-8") if isinstance(blob, bytes) else blob)
        if data.get("model_class") != "LightweightComplementNB":
            logger.warning("[ScorerV2] Unsupported JSON model class: %s", data.get("model_class"))
            return None
        return LightweightComplementNB.from_dict(data.get("state", {}))

    @staticmethod
    def _sklearn_version() -> str:
        """返回当前 sklearn 版本，未安装返回 'none'。"""
        try:
            import sklearn

            return sklearn.__version__  # type: ignore[no-any-return]
        except ImportError:
            return "none"

    def rollback_model(self, dimension: str, version: str) -> None:
        """Reject pre-cutover version rollback after governed model activation."""

        del dimension, version
        raise PermissionError("training_admission_receipt_required:rollback_model")

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
            ENUM_MAP = {
                "hot": 0.9,
                "warm": 0.6,
                "cold": 0.3,
                "high": 0.85,
                "medium": 0.55,
                "low": 0.25,
                "critical": 0.95,
                "normal": 0.5,
            }
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
        """从 item 提取特征字典（frontmatter 数值已归一化到 [0,1]）

        [P2-21] 特征维度从 ~15 维扩展到 50+ 维，新增：
          - 内容质量特征：句子结构、词汇多样性、格式丰富度（~12维）
          - 时序特征：会话模式、时间间隔（~6维）
          - 画像特征：energy/cognitive/value 三层匹配（~17维）
          - 知识图谱特征：实体密度、关系丰富度、关联度（~6维）
          - 交互特征：追问深度、纠正次数、拒绝率（~7维）
          - Embedding 特征：与历史高质量内容相似度（条件计算，1维）
        """
        features: Dict[str, Any] = {"_domain": self.domain}

        # 统一提取 content 和 frontmatter
        content = ""
        frontmatter: Dict[str, Any] = {}
        item_path: Optional[Path] = None
        if isinstance(item, Mapping):
            content = item.get("content", "")
            frontmatter = item.get("frontmatter", {})
            path_str = item.get("path", "")
            if path_str:
                item_path = Path(path_str)
            features["_source"] = "dict"
            if item.get("prediction_kind") == "predictive_delivery_usefulness":
                from core.cognitive.training_contract import derive_feature_snapshot

                governed_snapshot = derive_feature_snapshot(item)
                features.update(dict(governed_snapshot["values"]))
                features["_governed_prediction_feature_snapshot_hash"] = str(
                    governed_snapshot["snapshot_hash"]
                )
        elif isinstance(item, str):
            content = item
            features["_source"] = "str"
        elif isinstance(item, Path):
            try:
                text = item.read_text(encoding="utf-8", errors="ignore")
                item_path = item
                if text.startswith("---"):
                    parts = text.split("---", 2)
                    if len(parts) >= 3:
                        try:
                            frontmatter = self._parse_frontmatter_yaml(parts[1])
                            content = parts[2]
                        except ImportError:
                            content = text
                    else:
                        content = text
                else:
                    content = text
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
                content = ""
            features["_source"] = "path"

        features["content"] = content
        features["content_len"] = len(content)
        features["content_words"] = len(content.split())
        features["has_frontmatter"] = bool(frontmatter)
        features["frontmatter_keys"] = list(frontmatter.keys())
        features["_frontmatter"] = frontmatter

        # 简单元数据特征（保留原有）
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

        # ── [P2-21] 扩展特征 ──
        self._extract_content_quality_features(content, features)
        self._extract_temporal_features(features)
        self._extract_persona_features(content, frontmatter, features)
        self._extract_kg_features(content, item_path, features)
        self._extract_interaction_features(features)

        # Embedding 特征占位（条件计算，见 _should_compute_embedding）
        features["embedding_sim_to_high_quality"] = None

        return features

    def _extract_content_quality_features(self, content: str, features: Dict[str, Any]) -> None:
        """提取深层内容质量特征（~12维）"""
        if not content:
            features["content_sentence_count"] = 0
            features["content_avg_sentence_length"] = 0.0
            features["content_unique_word_ratio"] = 0.0
            features["content_has_question"] = False
            features["content_has_exclamation"] = False
            features["content_image_count"] = 0
            features["content_bold_count"] = 0
            features["content_list_item_count"] = 0
            features["content_external_link_count"] = 0
            features["content_code_block_count"] = 0
            features["content_max_code_block_lines"] = 0
            features["content_paragraph_count"] = 0
            return

        words = content.split()
        sentences = [
            s.strip() for s in content.replace("!", ".").replace("?", ".").split(".") if s.strip()
        ]

        # 句子结构
        features["content_sentence_count"] = len(sentences)
        features["content_avg_sentence_length"] = sum(len(s.split()) for s in sentences) / max(
            len(sentences), 1
        )

        # 词汇多样性
        unique_words = set(w.lower() for w in words if w.strip())
        features["content_unique_word_ratio"] = len(unique_words) / max(len(words), 1)

        # 格式丰富度
        features["content_has_question"] = "?" in content
        features["content_has_exclamation"] = "!" in content
        features["content_image_count"] = content.count("![")
        features["content_bold_count"] = content.count("**") // 2
        features["content_list_item_count"] = len(
            [
                line
                for line in content.split("\n")
                if line.strip().startswith(("- ", "* ", "1. ", "2. ", "3. ", "4. ", "5. "))
            ]
        )
        features["content_external_link_count"] = content.count("](")
        features["content_paragraph_count"] = len([p for p in content.split("\n\n") if p.strip()])

        # 代码块深度
        code_blocks = content.split("```")
        features["content_code_block_count"] = max(0, (len(code_blocks) - 1)) // 2
        max_cb_lines = 0
        for i in range(1, len(code_blocks), 2):
            lines = code_blocks[i].count("\n")
            if lines > max_cb_lines:
                max_cb_lines = lines
        features["content_max_code_block_lines"] = max_cb_lines

    def _extract_temporal_features(self, features: Dict[str, Any]) -> None:
        """提取时序特征（~6维）——基于 signal store"""
        now = datetime.now()
        features["hour_of_day"] = now.hour / 23.0
        features["day_of_week"] = now.weekday()
        features["is_weekend"] = now.weekday() >= 5

        # 默认值
        features["sessions_today"] = 0
        features["sessions_this_week"] = 0
        features["avg_session_interval_hours"] = 0.0
        features["time_since_last_session_hours"] = 0.0

        try:
            from core.persona.psyche import get_signal_store

            store = get_signal_store()
            recent = store.get_recent_session_signals(
                days=ADAPTIVE_SCORER_V2_DURATION_BUCKET_WEEK_DAYS
            )

            if not recent:
                return

            today_str = now.strftime("%Y-%m-%d")
            features["sessions_today"] = sum(
                1 for s in recent if s.get("created_at", "").startswith(today_str)
            )
            features["sessions_this_week"] = len(recent)

            # 计算平均会话间隔
            timestamps = []
            for s in recent:
                ts = s.get("created_at", "")
                if ts:
                    try:
                        timestamps.append(datetime.fromisoformat(ts))
                    except ValueError:
                        logger.warning("[adaptive_scorer_v2] ValueError suppressed", exc_info=True)
            timestamps.sort()
            if len(timestamps) >= 2:
                intervals = [
                    (timestamps[i] - timestamps[i - 1]).total_seconds() / 3600.0
                    for i in range(1, len(timestamps))
                ]
                features["avg_session_interval_hours"] = sum(intervals) / len(intervals)
                features["time_since_last_session_hours"] = (
                    now - timestamps[-1]
                ).total_seconds() / 3600.0
        except (ImportError, OSError, RuntimeError, ValueError, sqlite3.Error):
            logger.debug(
                "[adaptive_scorer_v2] optional temporal signal store unavailable",
                exc_info=True,
            )

    def _get_profile_classes(self) -> Dict[str, Any]:
        """动态加载画像 profile 类；不可用则返回空字典。"""
        try:
            from core.persona.pythia import (
                EnergyProfile,
                CognitiveProfile,
                ValueProfile,
            )

            return {
                "energy": EnergyProfile,
                "cognitive": CognitiveProfile,
                "value": ValueProfile,
            }
        except ImportError:
            return {}

    def _build_persona_dimensions(self, profile_classes: Dict[str, Any]) -> List[str]:
        """从 dataclass 字段动态构建维度列表，新增维度自动识别。"""
        from dataclasses import fields

        if profile_classes:
            dims = []
            for layer_name, cls in profile_classes.items():
                for f in fields(cls):
                    if f.name in ("confidence", "insufficient_dimensions"):
                        continue
                    dims.append(f"persona_{layer_name}_{f.name}")
            return dims

        return [
            "persona_energy_focus_depth",
            "persona_energy_startup_difficulty",
            "persona_energy_endurance_mode",
            "persona_energy_switching_flexibility",
            "persona_energy_recovery_cycle",
            "persona_cognitive_abstraction",
            "persona_cognitive_system_view",
            "persona_cognitive_skepticism",
            "persona_cognitive_creativity",
            "persona_cognitive_deduction",
            "persona_value_correctness_vs_efficiency",
            "persona_value_depth_vs_breadth",
            "persona_value_perfection_vs_completion",
            "persona_value_innovation_vs_safety",
            "persona_value_autonomy_vs_collaboration",
            "persona_value_action_vs_analysis",
        ]

    def _load_persona_profile(self) -> Optional[Any]:
        """加载当前用户的 persona profile；不可用或不存在时返回 None。"""
        try:
            from core.persona.delphi import get_persona_store

            store = get_persona_store()
            profile, _ = store.load_persona()
            return profile
        except (ImportError, OSError, RuntimeError, ValueError, sqlite3.Error):
            # Persona enrichment is optional.  In particular, test/process
            # lifecycles can retire a persona SQLite root while a cache still
            # points at it; that must degrade to base scoring instead of
            # making an otherwise valid score request fail.
            logger.debug("[adaptive_scorer_v2] persona unavailable", exc_info=True)
            return None

    def _apply_profile_dimensions(
        self,
        features: Dict[str, Any],
        profile: Any,
        profile_classes: Dict[str, Any],
    ) -> None:
        """将 profile 各层维度值写入 features。"""
        from dataclasses import fields

        for layer_name, cls in profile_classes.items():
            layer_obj = getattr(profile, layer_name, None)
            if not layer_obj:
                continue
            for f in fields(cls):
                if f.name in ("confidence", "insufficient_dimensions"):
                    continue
                dim_key = f"persona_{layer_name}_{f.name}"
                features[dim_key] = getattr(layer_obj, f.name, 0.5)

    def _match_tags(self, tags: List[str]) -> Tuple[float, float]:
        """标签匹配：维度越丰富得分越高。"""
        if tags:
            return 0.3 * min(1.0, len(tags) / 5.0), 0.3
        return 0.0, 0.0

    def _match_depth_preference(self, profile: Any, content: str) -> Tuple[float, float]:
        """深度偏好匹配：长内容匹配深度偏好，短内容匹配广度偏好。"""
        if hasattr(profile, "value") and profile.value:
            depth_pref = getattr(profile.value, "depth_vs_breadth", 0.5)
            word_count = len(content.split())
            if depth_pref > 0.6 and word_count > 200:
                return 0.2 * depth_pref, 0.2
            elif depth_pref < 0.4 and word_count < 100:
                return 0.2 * (1 - depth_pref), 0.2
        return 0.0, 0.0

    def _match_abstraction_preference(
        self, profile: Any, content_lower: str
    ) -> Tuple[float, float]:
        """抽象偏好匹配：概念词命中反映抽象思维偏好。"""
        if hasattr(profile, "cognitive") and profile.cognitive:
            abs_pref = getattr(profile.cognitive, "abstraction", 0.5)
            concept_words = [
                "原理",
                "机制",
                "模型",
                "框架",
                "理论",
                "抽象",
                "架构",
                "principle",
                "mechanism",
                "model",
                "framework",
                "theory",
                "architecture",
            ]
            concept_hits = sum(1 for w in concept_words if w in content_lower)
            if abs_pref > 0.6 and concept_hits > 0:
                return 0.2 * min(1.0, concept_hits / 3.0), 0.2
        return 0.0, 0.0

    def _match_innovation_preference(self, profile: Any, content_lower: str) -> Tuple[float, float]:
        """创新偏好匹配：创新/突破类词汇命中反映创新偏好。"""
        if hasattr(profile, "value") and profile.value:
            innov_pref = getattr(profile.value, "innovation_vs_safety", 0.5)
            innov_words = [
                "创新",
                "突破",
                "新方案",
                "改进",
                "优化",
                "实验",
                "探索",
                "innovation",
                "breakthrough",
                "experiment",
                "explore",
                "optimize",
            ]
            innov_hits = sum(1 for w in innov_words if w in content_lower)
            if innov_pref > 0.6 and innov_hits > 0:
                return 0.2 * min(1.0, innov_hits / 3.0), 0.2
        return 0.0, 0.0

    def _compute_persona_match_score(
        self,
        content: str,
        frontmatter: Dict[str, Any],
        profile: Any,
    ) -> float:
        """基于内容标签与画像偏好计算匹配分数。"""
        content_lower = content.lower()
        match_indicators = 0.0
        total_weight = 0.0

        tags = frontmatter.get("tags", []) if frontmatter else []
        dimension_matches = [
            self._match_tags(tags),
            self._match_depth_preference(profile, content),
            self._match_abstraction_preference(profile, content_lower),
            self._match_innovation_preference(profile, content_lower),
        ]
        for indicators, weight in dimension_matches:
            match_indicators += indicators
            total_weight += weight

        if total_weight > 0:
            return min(1.0, match_indicators / total_weight)
        return 0.5

    def _extract_persona_features(
        self, content: str, frontmatter: Dict[str, Any], features: Dict[str, Any]
    ) -> None:
        """提取画像匹配特征（动态反射 profile 维度）"""
        profile_classes = self._get_profile_classes()
        persona_dims = self._build_persona_dimensions(profile_classes)

        for d in persona_dims:
            features[d] = 0.5  # 中性默认值

        features["persona_confidence"] = 0.0
        features["persona_match_score"] = 0.5

        profile = self._load_persona_profile()
        if profile is None:
            return

        self._apply_profile_dimensions(features, profile, profile_classes)
        features["persona_confidence"] = getattr(profile, "confidence", 0.0)
        features["persona_match_score"] = self._compute_persona_match_score(
            content, frontmatter, profile
        )

    def _extract_interaction_features(self, features: Dict[str, Any]) -> None:
        """提取用户交互特征（~7维）——基于 signal store"""
        features["interaction_follow_up_depth"] = 0.0
        features["interaction_correction_count"] = 0
        features["interaction_rejection_rate"] = 0.0
        features["interaction_satisfaction_rate"] = 0.5
        features["interaction_avg_session_duration"] = 0.0
        features["interaction_modification_requests"] = 0
        features["interaction_termination_satisfied"] = 0.5

        try:
            from core.persona.psyche import get_signal_store

            store = get_signal_store()
            recent = store.get_recent_session_signals(
                days=ADAPTIVE_SCORER_V2_DURATION_BUCKET_WEEK_DAYS
            )

            if not recent:
                return

            follow_ups = []
            corrections = []
            durations = []
            satisfied = 0
            total = len(recent)

            for s in recent:
                fud = s.get("follow_up_depth", 0)
                if fud:
                    follow_ups.append(fud)

                cc = s.get("correction_count", 0)
                if cc:
                    corrections.append(cc)

                dur = s.get("duration_seconds", 0)
                if dur:
                    durations.append(dur)

                term = s.get("termination_type", "")
                if term == "satisfied":
                    satisfied += 1

            if follow_ups:
                features["interaction_follow_up_depth"] = sum(follow_ups) / len(follow_ups)
            if corrections:
                features["interaction_correction_count"] = sum(corrections)
                features["interaction_modification_requests"] = sum(corrections)
                features["interaction_rejection_rate"] = min(1.0, sum(corrections) / max(total, 1))
            if durations:
                features["interaction_avg_session_duration"] = (
                    sum(durations) / len(durations) / 3600.0
                )
            if total > 0:
                features["interaction_satisfaction_rate"] = satisfied / total
                features["interaction_termination_satisfied"] = satisfied / total

        except (ImportError, OSError, RuntimeError, ValueError, sqlite3.Error):
            logger.debug(
                "[adaptive_scorer_v2] optional interaction signal store unavailable",
                exc_info=True,
            )

    def _should_compute_embedding(self, rule_confs: List[float]) -> bool:
        """判断是否需要计算 embedding 相似度特征（bottom 20% 低置信度）"""
        # [P1-34] 避免同步 health_check 阻塞评分；仅检查客户端是否存在
        try:
            from core.embeddings.siliconflow_client import get_embedding_client

            if get_embedding_client() is None:
                return False
        except ImportError:
            return False

        avg_conf = sum(rule_confs) / len(rule_confs) if rule_confs else 0.5

        # 维护置信度滚动窗口
        self._confidence_window.append(avg_conf)
        if len(self._confidence_window) > 1000:
            self._confidence_window = self._confidence_window[-1000:]

        # 窗口足够时，计算 bottom 20% 阈值
        if len(self._confidence_window) >= 10:
            sorted_confs = sorted(self._confidence_window)
            threshold_idx = max(0, int(len(sorted_confs) * 0.2))
            bottom_20_threshold = sorted_confs[threshold_idx]
            if avg_conf <= bottom_20_threshold:
                return True

        # 保底：置信度极低时直接计算
        return avg_conf < 0.25
