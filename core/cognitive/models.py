"""
Observation Layer — 数据模型

Observation = 从长期行为中抽出的客观观察（事实统计）
特点：可增量更新、可缓存、可快速检索

7 个通用维度：
  1. Attention — 用户长期关注什么
  2. Decisions — 用户如何做选择
  3. Actions — 用户如何执行
  4. Time — 估算偏差、延期模式
  5. Stress — 压力下的变化
  6. Relationships — 与人的互动
  7. Growth — 长期身份变化
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
import json
import uuid

from core.cognitive.sources import ContentSource, UserIntent


class Dimension(str, Enum):
    """7 个通用观察维度"""

    ATTENTION = "attention"  # 关注分布
    DECISIONS = "decisions"  # 决策模式
    ACTIONS = "actions"  # 行动模式
    TIME = "time"  # 时间模式
    STRESS = "stress"  # 压力信号
    RELATIONSHIPS = "relationships"  # 关系模式
    GROWTH = "growth"  # 成长轨迹


class ObservationType(str, Enum):
    """观察类型"""

    FREQUENCY = "frequency"  # 频次统计（如某词出现N次）
    PATTERN = "pattern"  # 模式识别（如反复出现的序列）
    TREND = "trend"  # 趋势变化（如逐渐增多/减少）
    DEVIATION = "deviation"  # 偏离（如估算偏差）
    CONTRAST = "contrast"  # 对比（如预期 vs 实际）
    RATIO = "ratio"  # 比率（如完成率）


class SourceType(str, Enum):
    """数据来源类型"""

    RAW = "raw"  # L1: 原始对话记录
    WIKI = "wiki"  # L2: 蒸馏后的知识


@dataclass
class Observation:
    """
    单个观察记录

    设计原则：
    - 只包含客观事实，不包含洞察/解释
    - 每条 Observation 必须能回溯到具体来源
    - value 可以是任意可 JSON 序列化的结构
    """

    dimension: Dimension  # 所属维度
    observation_type: ObservationType  # 观察类型

    # 数值
    value: Any  # 观察值（数字、字符串、dict、list）
    unit: str = ""  # 单位（次、倍、小时、% 等）
    confidence: float = 1.0  # 置信度 0-1
    # The extractor's measurement is immutable calibration input.  ``confidence``
    # may expose the posterior only when the exact committed calibration
    # revision below is bound; the prior is never overwritten or guessed.
    base_confidence: Optional[float] = None
    base_measurement_status: str = "verified"
    calibration_revision_id: str = ""
    calibration_input_hash: str = ""
    calibration_spec_hash: str = ""
    calibration_record_hash: str = ""
    # In-memory identity captured before persistence redaction.  The digest is
    # persisted inside CalibrationRecord input, but never in observations.db;
    # no private literal is retained here or in the canonical state payload.
    calibration_measurement_hash: str = field(default="", repr=False, compare=False)
    calibration_peer_hash: str = field(default="", repr=False, compare=False)

    # 来源追踪
    source_type: SourceType = SourceType.WIKI
    source_path: str = ""  # 来源文件路径
    source_id: str = ""  # session_id / wiki page id
    evidence: List[str] = field(default_factory=list)  # 证据片段（原文引用）
    source_span_ids: List[str] = field(default_factory=list)
    # Object-level ACL inherited from canonical source provenance.  It is
    # deliberately persisted with the measurement rather than inferred from a
    # caller or from a later prompt path.  Missing envelopes are converted to
    # restricted-unknown by ObservationStore, never public.
    access_control: Dict[str, Any] = field(default_factory=dict)

    # 时间
    observed_at: Optional[datetime] = None  # 观察发生时间
    period_start: Optional[datetime] = None  # 统计周期开始
    period_end: Optional[datetime] = None  # 统计周期结束

    # 内容来源与意图信号（用于行为信号分层）
    content_source: ContentSource = ContentSource.UNKNOWN
    user_intent_signal: UserIntent = UserIntent.UNKNOWN

    # 用户备注（系统只读，不参与观察值计算；来自 Wiki 只读投影的用户纠错/补充）
    user_notes: str = ""

    # 元数据
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    version: int = 1  # 版本号（用于增量更新）

    def __post_init__(self) -> None:
        if self.base_confidence is None:
            self.base_confidence = float(self.confidence)
        self.base_confidence = float(self.base_confidence)
        self.confidence = float(self.confidence)
        if not 0.0 <= self.base_confidence <= 1.0:
            raise ValueError("base_confidence must be between 0 and 1")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if self.base_measurement_status not in {"verified", "historical_unverified"}:
            raise ValueError("base_measurement_status is invalid")
        pointer_fields = (
            self.calibration_revision_id,
            self.calibration_input_hash,
            self.calibration_spec_hash,
            self.calibration_record_hash,
        )
        if any(pointer_fields) and not all(pointer_fields):
            raise ValueError("calibration pointer fields must be all present or all empty")
        if not isinstance(self.source_span_ids, (list, tuple)):
            raise ValueError("source_span_ids must be a sequence")
        normalized_spans = [str(value).strip() for value in self.source_span_ids]
        if any(not value for value in normalized_spans):
            raise ValueError("source_span_ids contains a blank identity")
        self.source_span_ids = sorted(set(normalized_spans))
        if not isinstance(self.access_control, dict):
            raise ValueError("access_control must be an object")
        if not self.calibration_revision_id:
            self.confidence = float(self.base_confidence)

    def base_confidence_value(self) -> float:
        """Return the initialized immutable measurement prior."""

        if self.base_confidence is None:
            raise RuntimeError("Observation base confidence is uninitialized")
        return float(self.base_confidence)

    def calibration_measurement_payload(self) -> Dict[str, Any]:
        """Return every base-measurement field consumed by calibration."""

        return {
            "observation_id": self.id,
            "dimension": self.dimension.value,
            "observation_type": self.observation_type.value,
            "value": self.value,
            "unit": self.unit,
            "base_confidence": self.base_confidence_value(),
            "base_measurement_status": self.base_measurement_status,
            "source_type": self.source_type.value,
            "source_path": self.source_path,
            "source_id": self.source_id,
            "evidence": list(self.evidence or []),
            "source_span_ids": list(self.source_span_ids),
            "content_source": self.content_source.value,
            "user_intent_signal": self.user_intent_signal.value,
            "period_start": self.period_start.isoformat() if self.period_start else "",
            "period_end": self.period_end.isoformat() if self.period_end else "",
        }

    def calibration_peer_payload(self) -> Dict[str, Any]:
        """Return validator-visible peer facts without transient generated IDs."""

        return {
            "peer_identity": {
                "dimension": self.dimension.value,
                "observation_type": self.observation_type.value,
                "source_type": self.source_type.value,
                "source_id": self.source_id,
            },
            "dimension": self.dimension.value,
            "observation_type": self.observation_type.value,
            "value": self.value,
            "source_type": self.source_type.value,
            "source_id": self.source_id,
            "content_source": self.content_source.value,
        }

    def to_dict(self) -> Dict:
        """序列化为 dict（用于数据库存储）"""
        return {
            "id": self.id,
            "dimension": self.dimension.value,
            "observation_type": self.observation_type.value,
            "value": self.value,
            "unit": self.unit,
            "confidence": self.confidence,
            "base_confidence": self.base_confidence,
            "base_measurement_status": self.base_measurement_status,
            "calibration_revision_id": self.calibration_revision_id,
            "calibration_input_hash": self.calibration_input_hash,
            "calibration_spec_hash": self.calibration_spec_hash,
            "calibration_record_hash": self.calibration_record_hash,
            "source_type": self.source_type.value,
            "source_path": self.source_path,
            "source_id": self.source_id,
            "evidence": json.dumps(self.evidence, ensure_ascii=False),
            "source_span_ids": json.dumps(self.source_span_ids, ensure_ascii=False),
            "access_control": self.access_control,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "content_source": self.content_source.value,
            "user_intent_signal": self.user_intent_signal.value,
            "user_notes": self.user_notes,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "Observation":
        """从 dict 反序列化"""
        import json

        return cls(
            id=d["id"],
            dimension=Dimension(d["dimension"]),
            observation_type=ObservationType(d["observation_type"]),
            value=d["value"],
            unit=d.get("unit", ""),
            confidence=d.get("confidence", 1.0),
            base_confidence=d.get("base_confidence"),
            base_measurement_status=d.get("base_measurement_status", "verified"),
            calibration_revision_id=d.get("calibration_revision_id", ""),
            calibration_input_hash=d.get("calibration_input_hash", ""),
            calibration_spec_hash=d.get("calibration_spec_hash", ""),
            calibration_record_hash=d.get("calibration_record_hash", ""),
            source_type=SourceType(d["source_type"]),
            source_path=d.get("source_path", ""),
            source_id=d.get("source_id", ""),
            evidence=json.loads(d.get("evidence", "[]")),
            source_span_ids=json.loads(d.get("source_span_ids", "[]")),
            access_control=dict(d.get("access_control") or {}),
            observed_at=datetime.fromisoformat(d["observed_at"]) if d.get("observed_at") else None,
            period_start=(
                datetime.fromisoformat(d["period_start"]) if d.get("period_start") else None
            ),
            period_end=datetime.fromisoformat(d["period_end"]) if d.get("period_end") else None,
            content_source=ContentSource(d.get("content_source", "unknown")),
            user_intent_signal=UserIntent(d.get("user_intent_signal", "unknown")),
            user_notes=d.get("user_notes", ""),
            created_at=datetime.fromisoformat(d["created_at"]),
            updated_at=datetime.fromisoformat(d["updated_at"]),
            version=d.get("version", 1),
        )


@dataclass
class ObservationBatch:
    """一批观察结果"""

    observations: List[Observation] = field(default_factory=list)
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    source_count: int = 0  # 扫描了多少个来源文件
    dimension_counts: Dict[str, int] = field(default_factory=dict)
    extraction_status: str = "not_started"
    extraction_reason: str = ""
    persist_stats: Dict[str, Any] = field(default_factory=dict)
    # A paginated full replay intentionally retains only a bounded detail
    # sample in ``observations``.  Keep the exact total separately so callers
    # never mistake a memory-safe summary for zero output.
    observation_total: int = 0
    observations_truncated: bool = False

    def add(self, obs: Observation):
        self.observations.append(obs)
        self.observation_total += 1
        dim = obs.dimension.value
        self.dimension_counts[dim] = self.dimension_counts.get(dim, 0) + 1

    @property
    def total_observations(self) -> int:
        """Return the exact observation count, including paginated summaries."""
        return max(int(self.observation_total), len(self.observations))

    def by_dimension(self, dim: Dimension) -> List[Observation]:
        return [o for o in self.observations if o.dimension == dim]

    def by_type(self, obs_type: ObservationType) -> List[Observation]:
        return [o for o in self.observations if o.observation_type == obs_type]
