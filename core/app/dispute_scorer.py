# -*- coding: utf-8 -*-
"""
DisputeScorer — 争议仲裁综合评分器

为 DisputeResolver.scan() 提供多维综合评分与自动裁决决策。
支持可配置权重，并提供可选的自适应权重学习（默认关闭）。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core import config as _config_module
from core.jsonl_rotation import iter_jsonl_lines, rotate_jsonl

# Constants extracted from magic numbers
DISPUTE_SCORER_DURATION_BUCKET_MONTH_DAYS = 30

logger = logging.getLogger(__name__)

# 来源方法可信度映射（可配置覆盖）
SOURCE_METHOD_TRUST = {
    "manual": 1.0,
    "user_annotation": 1.0,
    "link_parse": 0.9,
    "anti_pattern_match": 0.85,
    "llm_inference": 0.75,
    "keyword_overlap": 0.6,
    "similarity": 0.65,
    "auto": 0.5,
}

# 知识阶段核心度映射
STAGE_CORE_SCORE = {
    "P0": 1.0,
    "P1": 0.9,
    "P2": 0.7,
    "P3": 0.5,
}


@dataclass
class RelationFeatures:
    """一条关系/断言在裁决时使用的特征"""

    confidence: float = 0.5
    freshness: float = 0.5
    citation: float = 0.0
    quality: float = 0.0
    source: float = 0.5
    core: float = 0.5

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RelationFeatures":
        return cls(**{k: float(data.get(k, 0.5)) for k in cls.__dataclass_fields__})


class AdaptiveWeightLearner:
    """
    在线自适应权重学习器（默认关闭）。

    记录每一次系统裁决与后续用户/实际反馈，通过简单梯度更新微调权重，
    使综合评分越来越接近用户的真实偏好。学习结果保存在独立 JSON 文件中，
    不修改用户配置文件里的默认值。
    """

    def __init__(self, config: Dict[str, Any], state_path: Path):
        self.enabled = bool(config.get("adaptive_learning", {}).get("enabled", False))
        self.lr = float(config.get("adaptive_learning", {}).get("learning_rate", 0.05))
        self.min_samples = int(
            config.get("adaptive_learning", {}).get("min_samples_before_update", 5)
        )
        self.max_weight = float(config.get("adaptive_learning", {}).get("max_weight", 0.60))
        self.min_weight = float(config.get("adaptive_learning", {}).get("min_weight", 0.05))
        self._state_path = state_path
        self._feedback_path = state_path.with_suffix(".feedback.jsonl")
        feedback_cfg = config.get("adaptive_learning", {}).get("feedback", {})
        self._feedback_max_size_bytes = int(
            feedback_cfg.get("max_size_mb", 10)
        ) * 1024 * 1024
        self._feedback_max_archives = int(feedback_cfg.get("max_archives", 5))
        self._weights: Dict[str, float] = self._load_weights()

    def _load_weights(self) -> Dict[str, float]:
        if self._state_path.exists():
            try:
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                weights = data.get("weights")
                if weights and set(weights) == set(_DIMENSIONS):
                    return {k: float(v) for k, v in weights.items()}
            except (OSError, UnicodeError, ValueError, TypeError, KeyError) as e:
                logger.warning("加载自适应权重失败: %s", e)
        return {}

    def _save_weights(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps(
                {
                    "updated_at": datetime.now().isoformat(),
                    "weights": self._weights,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def current_weights(self, fallback: Dict[str, float]) -> Dict[str, float]:
        if not self.enabled or not self._weights:
            return dict(fallback)
        return dict(self._weights)

    def record_feedback(
        self,
        pair_key: str,
        features_a: RelationFeatures,
        features_b: RelationFeatures,
        system_decision: str,
        actual_winner: str,
        user_overridden: bool = False,
    ) -> None:
        """
        记录一次裁决反馈。

        actual_winner: "a" | "b" | "both" | "none"
            - a/b: 用户最终选择了某一方
            - both: 用户选择合并
            - none: 争议被撤销或无需处理
        """
        record = {
            "timestamp": datetime.now().isoformat(),
            "pair_key": pair_key,
            "features_a": features_a.to_dict(),
            "features_b": features_b.to_dict(),
            "system_decision": system_decision,
            "actual_winner": actual_winner,
            "user_overridden": user_overridden,
            "weights": self._weights or {},
        }
        self._feedback_path.parent.mkdir(parents=True, exist_ok=True)
        with self._feedback_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        rotate_jsonl(
            self._feedback_path,
            max_size_bytes=self._feedback_max_size_bytes,
            max_archives=self._feedback_max_archives,
        )

    def learn(self) -> Optional[Dict[str, float]]:
        """
        从反馈记录中学习并更新权重。
        默认关闭；开启后也需要攒够 min_samples 才会真正更新。
        """
        if not self.enabled:
            logger.debug("自适应权重学习未启用")
            return None
        if not self._feedback_path.exists():
            logger.debug("无权重反馈记录，跳过学习")
            return None

        records: List[Dict[str, Any]] = []
        try:
            for line in iter_jsonl_lines(self._feedback_path):
                line = line.strip()
                if not line:
                    continue
                records.append(json.loads(line))
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as e:
            logger.warning("读取权重反馈记录失败: %s", e)
            return None

        if len(records) < self.min_samples:
            logger.debug("反馈样本不足: %d < %d，暂不更新权重", len(records), self.min_samples)
            return None

        # 简单在线梯度更新：对每一条反馈，如果系统选错了赢家，
        # 就沿“正确方特征 - 错误方特征”方向调整权重。
        weights = self.current_weights(_DEFAULT_WEIGHTS())
        total_update: Dict[str, float] = {k: 0.0 for k in weights}
        update_count = 0

        for record in records:
            winner = record.get("actual_winner")
            if winner not in ("a", "b"):
                continue

            fa = RelationFeatures.from_dict(record.get("features_a", {}))
            fb = RelationFeatures.from_dict(record.get("features_b", {}))
            diff = {
                "confidence": fa.confidence - fb.confidence,
                "freshness": fa.freshness - fb.freshness,
                "citation": fa.citation - fb.citation,
                "quality": fa.quality - fb.quality,
                "source": fa.source - fb.source,
                "core": fa.core - fb.core,
            }

            # 预测赢家
            pred_score_a = sum(weights[k] * getattr(fa, k) for k in weights)
            pred_score_b = sum(weights[k] * getattr(fb, k) for k in weights)
            pred_winner = "a" if pred_score_a >= pred_score_b else "b"

            if pred_winner == winner:
                continue

            # 更新方向：让正确方分数更高
            sign = 1.0 if winner == "a" else -1.0
            for k in weights:
                total_update[k] += sign * self.lr * diff[k]
            update_count += 1

        if update_count == 0:
            logger.debug("无需更新权重：所有预测均正确")
            return None

        new_weights: Dict[str, float] = {}
        for k, w in weights.items():
            new_weights[k] = max(
                self.min_weight, min(self.max_weight, w + total_update[k] / update_count)
            )

        # 投影到概率单形（权重和为 1）
        total = sum(new_weights.values())
        if total > 0:
            new_weights = {k: v / total for k, v in new_weights.items()}
        else:
            new_weights = _DEFAULT_WEIGHTS()

        self._weights = new_weights
        self._save_weights()
        logger.info("自适应权重已更新: %s", self._weights)
        return dict(self._weights)


# 维度和默认权重（与 core/config.py 中的 dispute_scan.weights 保持一致）
_DIMENSIONS = ["confidence", "freshness", "citation", "quality", "source", "core"]


def _DEFAULT_WEIGHTS() -> Dict[str, float]:
    return {
        "confidence": 0.25,
        "freshness": 0.25,
        "citation": 0.20,
        "quality": 0.15,
        "source": 0.10,
        "core": 0.05,
    }


class DisputeScorer:
    """争议综合评分与决策器"""

    def __init__(
        self,
        wiki_dir: Optional[Path] = None,
        config: Optional[Dict[str, Any]] = None,
        state_dir: Optional[Path] = None,
    ):
        self.wiki_dir = (
            Path(wiki_dir).expanduser() if wiki_dir else _config_module.get_config().wiki_dir
        )
        self.db_path: Optional[Path] = None
        raw_cfg = config or _config_module.get_config().get("dispute_scan", {})
        self.cfg = dict(raw_cfg)
        self.weights = self._load_weights()
        self._weight_source = "config" if self.cfg.get("weights") else "default"
        self.half_life = float(
            self.cfg.get("freshness_half_life_days", DISPUTE_SCORER_DURATION_BUCKET_MONTH_DAYS)
        )
        self.citation_max = max(1, int(self.cfg.get("citation_max_reference", 20)))
        self.auto_gap = float(self.cfg.get("auto_resolve_min_gap", 0.30))
        self.merge_gap = float(self.cfg.get("merge_min_gap", 0.15))
        self.min_conflict_strength = float(self.cfg.get("min_conflict_strength", 0.5))

        if state_dir is not None:
            self._state_dir = Path(state_dir).expanduser()
        else:
            raw_mnemos_dir = _config_module.get_config().get("mnemos_dir")
            self._state_dir = (
                Path(raw_mnemos_dir if raw_mnemos_dir else Path.home() / ".mnemos") / "state"
            )
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self._state_dir / "dispute_weights.json"

        # 加载 state 权重（CLI --set 写入），覆盖 config/default
        state_weights = self.load_weights_from_state()
        if state_weights:
            self.weights = state_weights
            self._weight_source = "state"

        self._learner = AdaptiveWeightLearner(
            self.cfg, self._state_dir / "dispute_adaptive_weights.json"
        )
        # 如果已存在学习后的权重，覆盖配置默认值
        learned = self._learner.current_weights(self.weights)
        if learned != self.weights:
            self.weights = learned
            self._weight_source = "learner"

    def _load_weights(self) -> Dict[str, float]:
        weights = self.cfg.get("weights", {})
        if not weights:
            return _DEFAULT_WEIGHTS()
        result = {}
        for k in _DIMENSIONS:
            result[k] = float(weights.get(k, _DEFAULT_WEIGHTS()[k]))
        # 归一化
        total = sum(result.values())
        if total > 0:
            result = {k: v / total for k, v in result.items()}
        return result

    def current_weights(self) -> Dict[str, float]:
        """返回当前生效的权重（可能被自适应 learner 覆盖）"""
        return dict(self.weights)

    def weight_source(self) -> str:
        """返回当前权重的来源：default / config / state / learner"""
        return self._weight_source

    def save_weights(self, weights: Dict[str, float]) -> None:
        """保存权重到 state 文件，覆盖 config 和默认值"""
        return self.save_weights_to_state(weights)

    def save_weights_to_state(self, weights: Dict[str, float]) -> None:
        """保存权重到 state 文件，覆盖 config 和默认值"""
        normalized = self._normalize_weights(weights)
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps(
                {
                    "updated_at": datetime.now().isoformat(),
                    "weights": normalized,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.weights = normalized
        self._weight_source = "state"

    def load_weights_from_state(self) -> Optional[Dict[str, float]]:
        """从 state 文件加载权重，支持完整或部分覆盖，不存在或损坏返回 None"""
        if not self._state_path.exists():
            return None
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            weights = data.get("weights") if isinstance(data, dict) and "weights" in data else data
            if not weights or not isinstance(weights, dict):
                return None
            # 与当前 config/default 权重合并，缺失维度保持原值
            merged = dict(self.weights)
            for k, v in weights.items():
                if k in merged:
                    merged[k] = float(v)
            return self._normalize_weights(merged)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as e:
            logger.warning("加载 state 权重失败: %s", e)
            return None

    def reset_weights(self) -> None:
        """清除 state 文件中的权重，回退到 config/默认值"""
        try:
            if self._state_path.exists():
                self._state_path.unlink()
        except OSError as e:
            logger.warning("删除 state 权重文件失败: %s", e)
        self.weights = self._load_weights()
        self._weight_source = "config" if self.cfg.get("weights") else "default"

    @staticmethod
    def _normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
        """归一化权重，确保和为 1 且维度完整"""
        result = {}
        for k in _DIMENSIONS:
            result[k] = float(weights.get(k, _DEFAULT_WEIGHTS()[k]))
        total = sum(result.values())
        if total > 0:
            result = {k: v / total for k, v in result.items()}
        else:
            result = _DEFAULT_WEIGHTS()
        return result

    def _wiki_metrics(self):
        # 延迟导入避免循环依赖
        from core.wiki_metrics import WikiMetrics

        return WikiMetrics(wiki_dir=str(self.wiki_dir))

    def _find_page_metrics(self, rel_path: str):
        """根据关系中的路径查找页面指标，支持多种路径形式"""
        metrics = self._wiki_metrics()
        candidates = [rel_path]
        p = Path(rel_path)
        if p.suffix != ".md":
            candidates.append(str(p.with_suffix(".md")))
        candidates.append(p.name)
        candidates.append(p.stem)
        for candidate in candidates:
            pm = metrics.get_page(candidate)
            if pm is not None:
                return pm
        logger.debug("未找到页面指标: %s", rel_path)
        return None

    def _page_title(self, rel_path: str) -> str:
        p = Path(rel_path)
        return p.stem

    def _freshness_score(self, days: int) -> float:
        if days <= 0:
            return 1.0
        return math.exp(-days / self.half_life)

    def extract_features(self, rel) -> RelationFeatures:
        """从 Relation 对象提取裁决特征"""
        pm = self._find_page_metrics(rel.source)

        raw_confidence = getattr(rel, "confidence", None)
        confidence = max(
            0.0, min(1.0, float(raw_confidence if raw_confidence is not None else 0.5))
        )

        if pm is not None:
            freshness = self._freshness_score(pm.freshness_days)
            citation = min(
                1.0,
                (pm.source_count + pm.backlink_count + len(getattr(rel, "evidence", []) or []))
                / self.citation_max,
            )
            quality = max(0.0, min(1.0, (pm.quality_score or 0.0) / 100.0))
            core = STAGE_CORE_SCORE.get(pm.knowledge_stage or "P3", 0.5)
        else:
            freshness = 0.5
            citation = min(1.0, len(getattr(rel, "evidence", []) or []) / self.citation_max)
            quality = 0.0
            core = 0.5

        source_method = getattr(rel, "source_method", "auto") or "auto"
        source = SOURCE_METHOD_TRUST.get(source_method, 0.5)

        return RelationFeatures(
            confidence=confidence,
            freshness=freshness,
            citation=citation,
            quality=quality,
            source=source,
            core=core,
        )

    def composite_score(self, features: RelationFeatures) -> float:
        """计算综合得分（0-1）"""
        total = 0.0
        for k, w in self.weights.items():
            total += w * getattr(features, k, 0.0)
        return round(max(0.0, min(1.0, total)), 4)

    def decide(
        self,
        features_a: RelationFeatures,
        features_b: RelationFeatures,
        conflict_strength: float,
        pair_key: str = "",
    ) -> Tuple[str, Dict[str, Any]]:
        """
        返回 (action, context)

        action:
            - skip: 冲突太弱，忽略
            - auto_resolve: 综合分差距大，自动采纳高分方
            - merge: 有差距但不大，给双方加边界说明
            - create_dispute: 拿不准，生成争议页
        """
        if conflict_strength < self.min_conflict_strength:
            return "skip", {"reason": "冲突强度低于阈值"}

        score_a = self.composite_score(features_a)
        score_b = self.composite_score(features_b)
        gap = abs(score_a - score_b)

        context = {
            "score_a": score_a,
            "score_b": score_b,
            "gap": gap,
            "conflict_strength": conflict_strength,
            "weights": dict(self.weights),
        }

        if gap >= self.auto_gap:
            winner = "a" if score_a > score_b else "b"
            return "auto_resolve", {**context, "winner": winner, "reason": "综合分差距大，自动裁决"}

        if gap >= self.merge_gap:
            return "merge", {**context, "reason": "综合分有差距但不够大，自动合并边界"}

        return "create_dispute", {**context, "reason": "综合分接近，需人工裁决"}

    def record_outcome(
        self,
        pair_key: str,
        features_a: RelationFeatures,
        features_b: RelationFeatures,
        system_decision: str,
        actual_winner: str,
        user_overridden: bool = False,
    ) -> None:
        """记录反馈并触发学习（如果开启）"""
        self._learner.record_feedback(
            pair_key, features_a, features_b, system_decision, actual_winner, user_overridden
        )
        if self._learner.enabled:
            learned = self._learner.learn()
            if learned:
                self.weights = learned

    def learn(self) -> Optional[Dict[str, float]]:
        """手动触发一次学习"""
        learned = self._learner.learn()
        if learned:
            self.weights = learned
        return learned
