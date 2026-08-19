"""
Observation 自动校准系统

核心原则：Calibration 应该是系统自动完成的，不依赖用户主观勾选。

校准方法：
1. 交叉来源验证 — L1(raw) 和 L2(wiki) 是否一致
2. 时间稳定性 — 同一 Observation 在不同时间窗口是否稳定
3. 对抗验证 — 主动找反例挑战 Observation
4. 矛盾检测 — 跨维度 Observation 是否自相矛盾
5. 行为验证 — Observation 是否能预测后续行为（长期）

每种验证器输出：
- score: 0-1（该验证维度的得分）
- verdict: "confirmed" | "questionable" | "refuted"
- reason: 判断理由
"""

import re
import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence

from core.cognitive.calibration_math import (
    canonical_hash,
    recompute_posterior,
    validator_input_hash,
)
from core.cognitive.calibration_lineage import build_calibration_lineage
from core.cognitive.models import Dimension, Observation, ObservationBatch
from core.cognitive.sources import SourceItem
from core.privacy.content_redaction import redact_persistence_value


CALIBRATION_RECORD_SCHEMA_VERSION = "mnemos.calibration_record.v1"
CALIBRATION_SPEC_VERSION = "mnemos.observation_calibration_spec.v1"
CALIBRATION_COMBINER = "weighted_evidence_shrinkage_v1"


def _sha256(value: Any) -> str:
    return canonical_hash(value)


def _source_code_hash(value: Any) -> str:
    """Hash exact executable source and fail closed when it is unavailable."""

    try:
        source = inspect.getsource(value)
    except (OSError, TypeError) as exc:
        identity = (
            f"{getattr(value, '__module__', '')}."
            f"{getattr(value, '__qualname__', getattr(value, '__name__', ''))}"
        ).strip(".")
        raise RuntimeError(
            f"calibration implementation source is unavailable: {identity or 'unknown'}"
        ) from exc
    return _sha256(source)


def _observation_input_snapshot(observation: Observation) -> Dict[str, Any]:
    """Capture every Observation field consumed by the calibration contract."""

    snapshot = observation.calibration_measurement_payload()
    # The persisted visible snapshot is redacted below.  This digest still
    # distinguishes exact private inputs without storing their literals.
    snapshot["measurement_hash"] = (
        observation.calibration_measurement_hash or _sha256(snapshot)
    )
    return snapshot


def _peer_observation_input_snapshot(observation: Observation) -> Dict[str, Any]:
    """Bind validator-visible peer facts without transient generated IDs."""

    snapshot = observation.calibration_peer_payload()
    snapshot["peer_identity"] = _sha256(snapshot["peer_identity"])
    snapshot["measurement_hash"] = (
        observation.calibration_peer_hash
        or _sha256(observation.calibration_peer_payload())
    )
    return snapshot


def _source_item_sort_key(item: SourceItem) -> str:
    """Canonicalize validator input order without persisting source literals."""

    return _sha256(
        {
            "source_type": item.source_type,
            "file_path": item.file_path,
            "content_hash": item.source_content_hash,
            "lineage_revision_ids": list(item.lineage_revision_ids),
            "lineage_root_hashes": [list(value) for value in item.lineage_root_hashes],
            "source_span_ids": list(item.source_span_ids),
            "content_source": item.content_source.value,
            "user_intent": item.user_intent.value,
        }
    )


@dataclass
class ValidationResult:
    """单个验证结果"""

    validator_name: str
    score: float  # 0-1
    verdict: str  # "confirmed" | "questionable" | "refuted" | "inconclusive"
    reason: str
    confidence_delta: float = 0.0  # 对原始置信度的调整值
    weight: float = 1.0
    supporting_cluster_ids: tuple[str, ...] = ()
    counter_cluster_ids: tuple[str, ...] = ()
    input_hash: str = ""


@dataclass
class CalibrationReport:
    """校准报告"""

    observation_id: str
    original_confidence: float
    calibrated_confidence: float
    overall_verdict: str  # "confirmed" | "questionable" | "refuted"
    validations: List[ValidationResult] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    schema_version: str = CALIBRATION_RECORD_SCHEMA_VERSION
    validator_spec_version: str = CALIBRATION_SPEC_VERSION
    validator_spec_hash: str = ""
    validator_code_hashes: Dict[str, str] = field(default_factory=dict)
    calculation_input_hash: str = ""
    input_snapshot: Dict[str, Any] = field(default_factory=dict)
    independent_evidence_clusters: List[Dict[str, Any]] = field(default_factory=list)
    supporting_evidence: List[str] = field(default_factory=list)
    counter_evidence: List[str] = field(default_factory=list)
    source_span_ids: List[str] = field(default_factory=list)
    valid_from: str = ""
    valid_until: str = ""
    omission_receipts: List[Dict[str, Any]] = field(default_factory=list)
    derived_source_double_count: int = 0
    derived_members_deduplicated: int = 0
    calibration_revision_id: str = ""
    calibration_record_hash: str = ""
    stale: bool = False

    def canonical_record_payload(self) -> Dict[str, Any]:
        """Return the durable typed payload, excluding store-assigned IDs."""

        payload = {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "prior": self.original_confidence,
            "posterior": self.calibrated_confidence,
            "overall_verdict": self.overall_verdict,
            "validator_version": self.validator_spec_version,
            "validator_spec_hash": self.validator_spec_hash,
            "validator_code_hashes": dict(sorted(self.validator_code_hashes.items())),
            "independent_evidence_clusters": list(self.independent_evidence_clusters),
            "supporting_evidence": list(self.supporting_evidence),
            "counter_evidence": list(self.counter_evidence),
            "calculation_input_hash": self.calculation_input_hash,
            "input_snapshot": self.input_snapshot,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "source_span_ids": list(self.source_span_ids),
            "omission_receipts": list(self.omission_receipts),
            "derived_source_double_count": self.derived_source_double_count,
            "derived_members_deduplicated": self.derived_members_deduplicated,
            "validations": [
                {
                    "validator_name": result.validator_name,
                    "score": result.score,
                    "verdict": result.verdict,
                    "reason": result.reason,
                    "weight": result.weight,
                    "supporting_cluster_ids": list(result.supporting_cluster_ids),
                    "counter_cluster_ids": list(result.counter_cluster_ids),
                    "input_hash": result.input_hash,
                }
                for result in self.validations
            ],
            "suggestions": list(self.suggestions),
        }
        return payload

    def finalize_hash(self) -> str:
        self.calibration_record_hash = _sha256(self.canonical_record_payload())
        return self.calibration_record_hash


# ───────────────────────────────────────────────
# 验证器基类
# ───────────────────────────────────────────────


class Validator(ABC):
    """验证器基类"""

    name: str
    spec_version = "1"

    def checkpoint_identity(self) -> Dict[str, str]:
        return {
            "name": self.name,
            "spec_version": self.spec_version,
            "code_hash": _source_code_hash(type(self)),
        }

    @abstractmethod
    def validate(
        self,
        obs: Observation,
        all_observations: List[Observation],
        source_items: List[SourceItem],
    ) -> ValidationResult:
        pass


# ───────────────────────────────────────────────
# 1. 交叉来源验证
# ───────────────────────────────────────────────


class CrossSourceValidator(Validator):
    """
    交叉来源验证

    原理：只有 canonical lineage 不同的证据簇才是独立支持。
    同一 Raw revision 及其派生 Wiki 永远只计一簇。
    """

    name = "cross_source"
    spec_version = "2"

    def validate(self, obs, all_observations, source_items) -> ValidationResult:
        lineage = build_calibration_lineage(source_items)
        independent = lineage.independent_clusters
        if not independent:
            return ValidationResult(
                validator_name=self.name,
                score=0.5,
                verdict="inconclusive",
                reason="没有可证明的独立 lineage cluster，不得交叉加权",
                input_hash=lineage.snapshot_hash,
            )

        # 从 observation value 中提取关键词
        keywords = self._extract_keywords(obs)
        if not keywords:
            return ValidationResult(
                validator_name=self.name,
                score=0.5,
                verdict="inconclusive",
                reason="无法从 Observation 中提取关键词",
                input_hash=lineage.snapshot_hash,
            )

        counts = {
            cluster.cluster_id: sum(
                len(re.findall(re.escape(keyword), cluster.canonical_text, re.IGNORECASE))
                for keyword in keywords
            )
            for cluster in independent
        }
        supporting = tuple(sorted(cluster_id for cluster_id, count in counts.items() if count))
        counter = tuple(sorted(cluster_id for cluster_id, count in counts.items() if not count))
        if not supporting:
            return ValidationResult(
                validator_name=self.name,
                score=0.3,
                verdict="questionable",
                reason="关键词在所有可证明的独立证据簇中均未出现",
                counter_cluster_ids=counter,
                input_hash=lineage.snapshot_hash,
            )

        if len(supporting) >= 2:
            return ValidationResult(
                validator_name=self.name,
                score=0.85,
                verdict="confirmed",
                reason=f"{len(supporting)} 个 canonical 独立证据簇支持；派生 L2 已按 Raw lineage 去重",
                supporting_cluster_ids=supporting,
                counter_cluster_ids=counter,
                input_hash=lineage.snapshot_hash,
            )
        return ValidationResult(
            validator_name=self.name,
            score=0.55,
            verdict="questionable",
            reason=(
                "仅 1 个 canonical lineage cluster 支持，不足以构成独立交叉验证"
                + (
                    f"；已去重 {lineage.derived_members_deduplicated} 个派生 L2 成员"
                    if lineage.derived_members_deduplicated
                    else ""
                )
            ),
            supporting_cluster_ids=supporting,
            counter_cluster_ids=counter,
            input_hash=lineage.snapshot_hash,
        )

    def _extract_keywords(self, obs: Observation) -> List[str]:
        """从 Observation value 中提取关键词"""
        keywords = []
        val = obs.value

        if isinstance(val, dict):
            # 从各种结构中尝试提取
            if "concepts" in val:
                keywords.extend(val["concepts"].keys())
            if "top_words" in val:
                keywords.extend(val["top_words"].keys())
            if "dominant" in val and val["dominant"]:
                keywords.append(val["dominant"])

        return [k for k in keywords if isinstance(k, str)]


# ───────────────────────────────────────────────
# 2. 对抗验证
# ───────────────────────────────────────────────


class AdversarialValidator(Validator):
    """
    对抗验证（主动找反例）

    原理：对每条 Observation，系统主动尝试反驳它。
    如果找不到有力的反例 → Observation 更可信。

    例如：
    - Observation: "AI 是 dominant topic（4312 次）"
    - 对抗搜索：非 AI 内容有多少？如果非 AI 内容极少 → 确认
    - 对抗搜索：如果非 AI 内容也非常丰富 → Observation 被反驳
    """

    name = "adversarial"

    def validate(self, obs, all_observations, source_items) -> ValidationResult:
        all_text = " ".join(i.content for i in source_items)

        # Attention 维度的对抗验证
        if obs.dimension == Dimension.ATTENTION and isinstance(obs.value, dict):
            return self._validate_attention(obs, all_text)

        # Actions 维度的对抗验证
        if obs.dimension == Dimension.ACTIONS and isinstance(obs.value, dict):
            return self._validate_actions(obs, all_text)

        # Time 维度的对抗验证
        if obs.dimension == Dimension.TIME and isinstance(obs.value, dict):
            return self._validate_time(obs, all_text)

        # 其他维度默认通过
        return ValidationResult(
            validator_name=self.name,
            score=0.5,
            verdict="inconclusive",
            reason="该维度暂无对抗验证规则",
        )

    def _validate_attention(self, obs, all_text) -> ValidationResult:
        """验证 Attention：dominant topic 是否真的 dominant"""
        val = obs.value
        if "concepts" not in val:
            return ValidationResult(
                validator_name=self.name,
                score=0.5,
                verdict="inconclusive",
                reason="无法提取概念列表进行对抗验证",
            )

        concepts = val.get("concepts", {})
        if not concepts:
            return ValidationResult(
                validator_name=self.name,
                score=0.5,
                verdict="inconclusive",
                reason="概念列表为空",
            )

        total_mentions = val.get("total_mentions", sum(concepts.values()))
        dominant = val.get("dominant")

        if not dominant or dominant not in concepts:
            return ValidationResult(
                validator_name=self.name,
                score=0.5,
                verdict="inconclusive",
                reason="无法确定 dominant topic",
            )

        dominant_count = concepts[dominant]
        dominant_ratio = dominant_count / max(total_mentions, 1)

        # 检查第二名与第一名的差距
        sorted_concepts = sorted(concepts.items(), key=lambda x: x[1], reverse=True)
        if len(sorted_concepts) >= 2:
            second_count = sorted_concepts[1][1]
            gap_ratio = dominant_count / max(second_count, 1)
        else:
            gap_ratio = float("inf")

        if dominant_ratio < 0.2:
            # dominant topic 占比不到 20% → 不能叫 dominant
            return ValidationResult(
                validator_name=self.name,
                score=0.3,
                verdict="refuted",
                reason=f"'{dominant}' 占比仅 {dominant_ratio:.1%}，达不到 dominant 标准（需 >30%）",
                confidence_delta=-0.3,
            )
        elif dominant_ratio < 0.3:
            # 占比 20-30% → 勉强
            return ValidationResult(
                validator_name=self.name,
                score=0.5,
                verdict="questionable",
                reason=f"'{dominant}' 占比 {dominant_ratio:.1%}，刚达到 dominant 阈值",
                confidence_delta=-0.15,
            )
        elif gap_ratio < 1.5:
            # 第一名和第二名差距不到 50% → 没有明显 dominant
            return ValidationResult(
                validator_name=self.name,
                score=0.6,
                verdict="questionable",
                reason=f"'{dominant}' 与第二名差距仅 {gap_ratio:.1f} 倍，dominant 优势不明显",
                confidence_delta=-0.1,
            )
        else:
            # dominant 明显
            return ValidationResult(
                validator_name=self.name,
                score=0.9,
                verdict="confirmed",
                reason=f"'{dominant}' 占比 {dominant_ratio:.1%}，领先第二名 {gap_ratio:.1f} 倍，dominant 地位稳固",  # noqa: E501
                confidence_delta=+0.1,
            )

    def _validate_actions(self, obs, all_text) -> ValidationResult:
        """验证 Actions：完成率计算是否合理"""
        val = obs.value
        started = val.get("started", 0)
        completed = val.get("completed", 0)
        blocked = val.get("blocked", 0)

        total = started + completed + blocked
        if total == 0:
            return ValidationResult(
                validator_name=self.name,
                score=0.3,
                verdict="refuted",
                reason="没有任何行动信号，Observation 基础数据为零",
                confidence_delta=-0.3,
            )

        if completed > started * 5:
            # 完成次数远大于启动次数 → 可能是计数方式问题
            return ValidationResult(
                validator_name=self.name,
                score=0.5,
                verdict="questionable",
                reason=f"完成({completed}) 远大于 启动({started})，可能「完成」一词被过度匹配（如「完善」「完全」）",
                confidence_delta=-0.15,
            )

        return ValidationResult(
            validator_name=self.name,
            score=0.8,
            verdict="confirmed",
            reason=f"行动数据分布合理（启动{started}/完成{completed}/阻塞{blocked}）",
            confidence_delta=+0.05,
        )

    def _validate_time(self, obs, all_text) -> ValidationResult:
        """验证 Time：延期率或时间提及模式是否合理"""
        val = obs.value
        estimates = val.get("estimates", 0)
        delays = val.get("delays", 0)
        time_mentions = val.get("time_mentions", 0)

        # 结构1: 估算/延期数据
        if estimates > 0 or delays > 0:
            if estimates < 5:
                return ValidationResult(
                    validator_name=self.name,
                    score=0.4,
                    verdict="questionable",
                    reason=f"时间估算样本仅 {estimates} 次，统计意义不足",
                    confidence_delta=-0.2,
                )
            return ValidationResult(
                validator_name=self.name,
                score=0.75,
                verdict="confirmed",
                reason=f"时间数据样本充足（估算{estimates}次/延期{delays}次）",
                confidence_delta=+0.05,
            )

        # 结构2: 时间提及模式
        if time_mentions > 0:
            examples = val.get("examples", [])
            if time_mentions < 10:
                return ValidationResult(
                    validator_name=self.name,
                    score=0.5,
                    verdict="questionable",
                    reason=f"时间提及仅 {time_mentions} 次，模式可能不稳定",
                    confidence_delta=-0.15,
                )
            return ValidationResult(
                validator_name=self.name,
                score=0.75,
                verdict="confirmed",
                reason=f"时间提及模式清晰（{time_mentions} 次，{len(examples)} 种表达）",
                confidence_delta=+0.05,
            )

        # 无任何时间数据
        return ValidationResult(
            validator_name=self.name,
            score=0.3,
            verdict="refuted",
            reason="无时间相关数据，Observation 无效",
            confidence_delta=-0.3,
        )


# ───────────────────────────────────────────────
# 3. 矛盾检测
# ───────────────────────────────────────────────


class ContradictionDetector(Validator):
    """
    矛盾检测

    原理：检查不同维度的 Observation 是否自相矛盾。
    矛盾本身不是 bug，可能是重要的洞察信号。

    例如：
    - Attention: "高度关注 AI" + Actions: "AI 项目完成率极低" → 矛盾（关注但不行动）
    - Decisions: "频繁讨论优先级" + Time: "延期率极高" → 矛盾（讨论多但执行差）
    """

    name = "contradiction"

    # 已知矛盾模式
    CONTRADICTION_PATTERNS = [
        {
            "name": "关注但不行动",
            "dims": [Dimension.ATTENTION, Dimension.ACTIONS],
            "check": lambda att, act: (
                att.get("dominant") == "ai" and act.get("completion_rate", 1) < 0.5
            ),
            "reason": "高度关注 AI 但完成率极低，可能存在「关注-行动」落差",
        },
        {
            "name": "计划多执行差",
            "dims": [Dimension.DECISIONS, Dimension.TIME],
            "check": lambda dec, tim: (
                dec.get("priority", 0) > 50 and tim.get("delay_ratio", 0) > 0.3
            ),
            "reason": "频繁讨论优先级但延期率很高，说明计划并未改善执行",
        },
        {
            "name": "高压力低成长",
            "dims": [Dimension.STRESS, Dimension.GROWTH],
            "check": lambda stress, growth: (
                stress.get("stress_signals", 0) > 50 and growth.get("growth_signals", 0) < 10
            ),
            "reason": "压力信号很多但成长信号很少，可能处于「忙碌但停滞」状态",
        },
    ]

    def validate(self, obs, all_observations, source_items) -> ValidationResult:
        # 找到与当前 Observation 可能矛盾的 Observation
        contradictions = []

        for pattern in self.CONTRADICTION_PATTERNS:
            if obs.dimension not in pattern["dims"]:  # type: ignore[operator]
                continue

            # 找到另一个维度的 Observation
            other_dim = [d for d in pattern["dims"] if d != obs.dimension][  # type: ignore[attr-defined]  # noqa: E501
                0
            ]  # type: ignore[attr-defined]
            other_obs_list = [o for o in all_observations if o.dimension == other_dim]

            if not other_obs_list:
                continue

            other_obs = other_obs_list[0]  # 取第一个

            # 检查是否满足矛盾条件
            try:
                if obs.dimension == pattern["dims"][0]:  # type: ignore[index]
                    is_contradiction = pattern["check"](
                        obs.value, other_obs.value
                    )  # type: ignore[operator]
                else:
                    is_contradiction = pattern["check"](
                        other_obs.value, obs.value
                    )  # type: ignore[operator]
            # DEBT(S8): 容错跳过，避免单条记录中断批量处理
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                continue

            if is_contradiction:
                contradictions.append(pattern["name"])

        if contradictions:
            return ValidationResult(
                validator_name=self.name,
                score=0.4,
                verdict="questionable",
                # type: ignore[arg-type]
                reason=f"检测到矛盾：{', '.join(contradictions)}。矛盾不一定是错误，可能是重要洞察",  # type: ignore[arg-type]  # noqa: E501
                confidence_delta=-0.1,
            )

        return ValidationResult(
            validator_name=self.name,
            score=0.8,
            verdict="confirmed",
            reason="未检测到与其他维度的明显矛盾",
            confidence_delta=+0.05,
        )


# ───────────────────────────────────────────────
# 4. 样本充足性验证
# ───────────────────────────────────────────────


class SampleSizeValidator(Validator):
    """
    样本充足性验证

    原理：Observation 的置信度必须和样本量挂钩。
    基于 1 个文件的 Observation 不可信，基于 100 个文件的才可信。
    """

    name = "sample_size"

    MIN_SAMPLES = {
        Dimension.ATTENTION: 50,  # 词频需要较多样本
        Dimension.DECISIONS: 10,  # 决策信号可以较少
        Dimension.ACTIONS: 20,
        Dimension.TIME: 10,
        Dimension.STRESS: 5,
        Dimension.RELATIONSHIPS: 10,
        Dimension.GROWTH: 20,
    }

    def validate(self, obs, all_observations, source_items) -> ValidationResult:
        # 估算该 Observation 的样本量
        lineage = build_calibration_lineage(source_items)
        total_sources = len(lineage.independent_clusters)
        min_required = self.MIN_SAMPLES.get(obs.dimension, 10)

        if total_sources < min_required:
            ratio = total_sources / min_required
            return ValidationResult(
                validator_name=self.name,
                score=ratio,
                verdict="questionable",
                reason=(
                    f"样本量不足：仅 {total_sources} 个独立 lineage cluster，"
                    f"该维度建议至少 {min_required} 个"
                ),
                confidence_delta=-0.2 * (1 - ratio),
            )

        return ValidationResult(
            validator_name=self.name,
            score=0.9,
            verdict="confirmed",
            reason=f"样本量充足：{total_sources} 个独立 lineage cluster",
            confidence_delta=+0.05,
        )


# ───────────────────────────────────────────────
# 校准引擎
# ───────────────────────────────────────────────

# ───────────────────────────────────────────────
# 5. 内容来源可靠性验证
# ───────────────────────────────────────────────


class ContentSourceValidator(Validator):
    """
    内容来源可靠性验证

    原理：根据 Observation 的内容来源标记判断其可靠性。

    - NATIVE_DIALOGUE / USER_NOTE → 最可靠（用户原话）
    - LIKELY_PASTED → 中等可靠（用户选择粘贴，但内容非原创）
    - EXTERNAL_QUOTED → 仅作为行为信号，认知内容置信度低
    - UNKNOWN → 不确定，保守处理
    """

    name = "content_source"

    def validate(self, obs, all_observations, source_items) -> ValidationResult:
        from core.cognitive.sources import ContentSource

        # 找到该 Observation 相关的 source_items（通过 source_path 关联）
        # 聚合型 Observation 的 source_path 格式为 "aggregated:wiki:N,raw:M"
        # 这里简化处理：统计所有 source_items 的来源分布

        if not source_items:
            return ValidationResult(
                validator_name=self.name,
                score=0.5,
                verdict="inconclusive",
                reason="无来源数据",
            )

        # 统计来源分布
        native_count = sum(
            1 for i in source_items if i.content_source == ContentSource.NATIVE_DIALOGUE
        )
        user_note_count = sum(
            1 for i in source_items if i.content_source == ContentSource.USER_NOTE
        )
        pasted_count = sum(
            1 for i in source_items if i.content_source == ContentSource.LIKELY_PASTED
        )
        external_count = sum(
            1 for i in source_items if i.content_source == ContentSource.EXTERNAL_FILE
        )
        _ = sum(1 for i in source_items if i.content_source == ContentSource.UNKNOWN)

        total = len(source_items)
        native_ratio = (native_count + user_note_count) / total
        pasted_ratio = pasted_count / total
        external_ratio = external_count / total

        # 如果该 Observation 本身带有 content_source 标记，直接用它
        if obs.content_source != ContentSource.UNKNOWN:
            if obs.content_source in (ContentSource.NATIVE_DIALOGUE, ContentSource.USER_NOTE):
                return ValidationResult(
                    validator_name=self.name,
                    score=0.9,
                    verdict="confirmed",
                    reason=f"来源可靠：{obs.content_source.value}",
                    confidence_delta=+0.05,
                )
            elif obs.content_source == ContentSource.LIKELY_PASTED:
                return ValidationResult(
                    validator_name=self.name,
                    score=0.6,
                    verdict="questionable",
                    reason="内容来自疑似复制粘贴，置信度降级",
                    confidence_delta=-0.15,
                )
            elif obs.content_source == ContentSource.EXTERNAL_FILE:
                return ValidationResult(
                    validator_name=self.name,
                    score=0.5,
                    verdict="questionable",
                    reason="内容来自外部文件，仅作为行为信号",
                    confidence_delta=-0.2,
                )

        # 基于整体来源分布判断
        if native_ratio >= 0.7:
            return ValidationResult(
                validator_name=self.name,
                score=0.85,
                verdict="confirmed",
                reason=f"{native_ratio:.0%} 来源为用户原生对话或笔记",
                confidence_delta=+0.05,
            )
        elif pasted_ratio >= 0.3:
            return ValidationResult(
                validator_name=self.name,
                score=0.6,
                verdict="questionable",
                reason=f"{pasted_ratio:.0%} 来源为疑似复制粘贴，内容可信度降低",
                confidence_delta=-0.15,
            )
        elif external_ratio >= 0.3:
            return ValidationResult(
                validator_name=self.name,
                score=0.5,
                verdict="questionable",
                reason=f"{external_ratio:.0%} 来源为外部引用，仅保留行为信号",
                confidence_delta=-0.1,
            )
        else:
            return ValidationResult(
                validator_name=self.name,
                score=0.7,
                verdict="questionable",
                reason=f"来源分布混合（原生 {native_ratio:.0%}/粘贴 {pasted_ratio:.0%}/外部 {external_ratio:.0%}）",  # noqa: E501
                confidence_delta=-0.05,
            )


ALL_VALIDATORS = [
    CrossSourceValidator(),
    AdversarialValidator(),
    ContradictionDetector(),
    SampleSizeValidator(),
    ContentSourceValidator(),
]


class CalibrationEngine:
    """校准引擎"""

    VALIDATOR_WEIGHTS = {
        "cross_source": 1.5,
        "adversarial": 1.0,
        "contradiction": 1.0,
        "sample_size": 0.75,
        "content_source": 0.75,
    }
    PRIOR_WEIGHT = 2.0

    def __init__(self, validators=None):
        self.validators = list(ALL_VALIDATORS if validators is None else validators)
        if not self.validators:
            raise ValueError("CalibrationEngine requires at least one validator")
        identities = [validator.checkpoint_identity() for validator in self.validators]
        validator_names = [str(identity["name"]) for identity in identities]
        if any(not name for name in validator_names):
            raise ValueError("CalibrationEngine validator names must be non-empty")
        if len(validator_names) != len(set(validator_names)):
            raise ValueError("CalibrationEngine validator names must be unique")
        self.validator_code_hashes = {
            str(identity["name"]): str(identity["code_hash"]) for identity in identities
        }
        self.spec_payload = {
            "schema_version": CALIBRATION_SPEC_VERSION,
            "combiner": CALIBRATION_COMBINER,
            "prior_weight": self.PRIOR_WEIGHT,
            "implementation_hashes": {
                "calibration_engine": _source_code_hash(CalibrationEngine),
                "calibration_math_module": _source_code_hash(
                    inspect.getmodule(recompute_posterior)
                ),
                "lineage_module": _source_code_hash(
                    inspect.getmodule(build_calibration_lineage)
                ),
            },
            "validators": [
                {
                    **identity,
                    "weight": self.VALIDATOR_WEIGHTS.get(str(identity["name"]), 1.0),
                }
                for identity in identities
            ],
        }
        self.spec_hash = _sha256(self.spec_payload)

    def calibrate(
        self,
        observation: Observation,
        all_observations: List[Observation],
        source_items: List[SourceItem],
    ) -> CalibrationReport:
        """
        对单条 Observation 进行自动校准
        """
        (
            ordered_source_items,
            lineage,
            observation_snapshot,
            ordered_peer_snapshots,
            ordered_peer_observations,
        ) = self._canonical_inputs(observation, all_observations, source_items)
        raw_input_snapshot = {
            "observation": observation_snapshot,
            "peer_observations": ordered_peer_snapshots,
            "lineage": lineage.canonical_payload(),
            "lineage_snapshot_hash": lineage.snapshot_hash,
            "validator_spec": self.spec_payload,
        }
        redacted = redact_persistence_value(raw_input_snapshot)
        if not isinstance(redacted.value, dict):
            raise ValueError("calibration input snapshot must remain an object")
        input_snapshot = dict(redacted.value)
        input_snapshot["lineage_snapshot_hash"] = _sha256(input_snapshot["lineage"])
        input_snapshot["privacy_redaction"] = {
            "policy": redacted.policy,
            "counts": [
                {"type": name, "count": count} for name, count in redacted.counts
            ],
        }
        return self._evaluate(
            observation,
            ordered_peer_observations,
            ordered_source_items,
            lineage=lineage,
            input_snapshot=input_snapshot,
            valid_from=(
                observation.period_start.isoformat()
                if observation.period_start
                else "unbounded"
            ),
            valid_until=(
                observation.period_end.isoformat()
                if observation.period_end
                else "unbounded"
            ),
            omission_receipts=self._omission_receipts(
                observation,
                ordered_source_items,
                sorted(
                    set(lineage.source_span_ids)
                    | set(getattr(observation, "source_span_ids", []) or [])
                ),
            ),
        )

    def recalibrate_frozen_snapshot(
        self,
        observation: Observation,
        all_observations: List[Observation],
        source_items: List[SourceItem],
        *,
        frozen_input_snapshot: Mapping[str, Any],
        expected_input_hash: str,
        valid_from: str,
        valid_until: str,
        omission_receipts: Sequence[Mapping[str, Any]],
    ) -> CalibrationReport:
        """Replay exact historical inputs under the current validator spec.

        The durable snapshot is already privacy-redacted.  A migration may
        replace only its executable validator specification after proving that
        reconstructed Observation, peer, and Raw-lineage inputs reproduce the
        frozen visible snapshot exactly.  Historical redaction counts and
        omission receipts remain immutable evidence rather than being guessed
        from an already-redacted projection.
        """

        if _sha256(frozen_input_snapshot) != expected_input_hash:
            raise ValueError("frozen calibration input hash mismatch")
        required_fields = {
            "observation",
            "peer_observations",
            "lineage",
            "lineage_snapshot_hash",
            "validator_spec",
            "privacy_redaction",
        }
        if set(frozen_input_snapshot) != required_fields:
            raise ValueError("frozen calibration input snapshot shape mismatch")
        (
            ordered_source_items,
            lineage,
            observation_snapshot,
            ordered_peer_snapshots,
            ordered_peer_observations,
        ) = self._canonical_inputs(observation, all_observations, source_items)
        reconstructed = redact_persistence_value(
            {
                "observation": observation_snapshot,
                "peer_observations": ordered_peer_snapshots,
                "lineage": lineage.canonical_payload(),
            }
        )
        if not isinstance(reconstructed.value, dict):
            raise ValueError("reconstructed calibration input must remain an object")
        for field_name in ("observation", "peer_observations", "lineage"):
            if reconstructed.value.get(field_name) != frozen_input_snapshot.get(field_name):
                raise ValueError(
                    f"frozen calibration {field_name} does not match reconstructed input"
                )
        if _sha256(frozen_input_snapshot["lineage"]) != frozen_input_snapshot.get(
            "lineage_snapshot_hash"
        ):
            raise ValueError("frozen calibration lineage snapshot hash mismatch")

        input_snapshot = dict(frozen_input_snapshot)
        input_snapshot["validator_spec"] = self.spec_payload
        return self._evaluate(
            observation,
            ordered_peer_observations,
            ordered_source_items,
            lineage=lineage,
            input_snapshot=input_snapshot,
            valid_from=valid_from,
            valid_until=valid_until,
            omission_receipts=[dict(value) for value in omission_receipts],
        )

    @staticmethod
    def _canonical_inputs(
        observation: Observation,
        all_observations: List[Observation],
        source_items: List[SourceItem],
    ) -> tuple[
        List[SourceItem],
        Any,
        Dict[str, Any],
        List[Dict[str, Any]],
        List[Observation],
    ]:
        if observation.base_measurement_status != "verified":
            raise ValueError(
                "calibration requires a verified base measurement; re-extract the Observation"
            )
        ordered_source_items = sorted(source_items, key=_source_item_sort_key)
        lineage = build_calibration_lineage(ordered_source_items)
        observation_snapshot = _observation_input_snapshot(observation)
        peer_pairs = [
            (_peer_observation_input_snapshot(candidate), candidate)
            for candidate in all_observations
        ]
        peer_pairs.sort(
            key=lambda value: (
                str(value[0]["measurement_hash"]),
                str(value[0]["peer_identity"]),
            )
        )
        return (
            ordered_source_items,
            lineage,
            observation_snapshot,
            [snapshot for snapshot, _ in peer_pairs],
            [candidate for _, candidate in peer_pairs],
        )

    def _evaluate(
        self,
        observation: Observation,
        ordered_peer_observations: List[Observation],
        ordered_source_items: List[SourceItem],
        *,
        lineage: Any,
        input_snapshot: Dict[str, Any],
        valid_from: str,
        valid_until: str,
        omission_receipts: List[Dict[str, Any]],
    ) -> CalibrationReport:
        """Run the current validators against one canonical input snapshot."""

        prior = observation.base_confidence_value()
        calculation_input_hash = _sha256(input_snapshot)
        validations = []

        for validator in self.validators:
            code_hash = self.validator_code_hashes[validator.name]
            result_input_hash = validator_input_hash(
                calculation_input_hash=calculation_input_hash,
                validator_name=validator.name,
                validator_code_hash=code_hash,
            )
            result = validator.validate(
                observation,
                ordered_peer_observations,
                ordered_source_items,
            )
            result.weight = self.VALIDATOR_WEIGHTS.get(validator.name, 1.0)
            result.input_hash = result_input_hash
            validations.append(result)
        calibrated = recompute_posterior(
            prior,
            (
                {
                    "score": result.score,
                    "weight": result.weight,
                    "verdict": result.verdict,
                }
                for result in validations
            ),
            prior_weight=self.PRIOR_WEIGHT,
        )

        # 总体判断
        refuted = sum(1 for v in validations if v.verdict == "refuted")
        questionable = sum(1 for v in validations if v.verdict == "questionable")
        confirmed = sum(1 for v in validations if v.verdict == "confirmed")

        if refuted >= 2:
            overall = "refuted"
        elif refuted == 1 or questionable >= 2:
            overall = "questionable"
        elif confirmed >= 2:
            overall = "confirmed"
        else:
            overall = "questionable"

        # 生成建议
        suggestions = []
        for v in validations:
            if v.verdict in ("refuted", "questionable"):
                suggestions.append(f"[{v.validator_name}] {v.reason}")

        supporting = sorted(
            {cluster for result in validations for cluster in result.supporting_cluster_ids}
        )
        counter = sorted(
            {cluster for result in validations for cluster in result.counter_cluster_ids}
        )
        source_span_ids = sorted(
            set(lineage.source_span_ids)
            | set(getattr(observation, "source_span_ids", []) or [])
        )
        report = CalibrationReport(
            observation_id=observation.id,
            original_confidence=round(prior, 6),
            calibrated_confidence=calibrated,
            overall_verdict=overall,
            validations=validations,
            suggestions=suggestions,
            validator_spec_hash=self.spec_hash,
            validator_code_hashes=dict(self.validator_code_hashes),
            calculation_input_hash=calculation_input_hash,
            input_snapshot=input_snapshot,
            independent_evidence_clusters=[
                cluster.canonical_payload() for cluster in lineage.independent_clusters
            ],
            supporting_evidence=supporting,
            counter_evidence=counter,
            source_span_ids=source_span_ids,
            valid_from=valid_from,
            valid_until=valid_until,
            omission_receipts=omission_receipts,
            derived_source_double_count=0,
            derived_members_deduplicated=lineage.derived_members_deduplicated,
        )
        report.finalize_hash()
        return report

    @staticmethod
    def _omission_receipts(
        observation: Observation,
        source_items: List[SourceItem],
        source_span_ids: List[str],
    ) -> List[Dict[str, Any]]:
        receipts: List[Dict[str, Any]] = []
        contracts = (
            ("evidence_snippets", list(observation.evidence or []), 5),
            ("source_paths", sorted({item.file_path for item in source_items}), 20),
            ("source_span_ids", list(source_span_ids), 20),
        )
        for target, values, display_limit in contracts:
            omitted = values[display_limit:]
            payload = {
                "observation_id": observation.id,
                "target": target,
                "total_count": len(values),
                "displayed_count": min(len(values), display_limit),
                "omitted_count": len(omitted),
                "omitted_hash": _sha256(omitted),
            }
            payload["receipt_id"] = "omission:" + _sha256(payload).split(":", 1)[1][:32]
            receipts.append(payload)
        return receipts

    def calibrate_batch(
        self,
        batch: "ObservationBatch",
        source_items: List[SourceItem],
    ) -> Dict[str, CalibrationReport]:
        """批量校准"""
        reports = {}
        for obs in batch.observations:
            report = self.calibrate(obs, batch.observations, source_items)
            reports[obs.id] = report
        return reports
