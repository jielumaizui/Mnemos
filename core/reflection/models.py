"""
Reflection 数据模型

核心设计原则：
- ReflectionRecord: 记录一次完整的 Reflection 生成过程（不存 Insight 全文，存元数据）
- CognitiveTrajectory: 认知变迁轨迹（用户在某维度上的认知变化历史）
- UserFeedback: 用户对 Insight 的反馈（👍 准 / 👎 不准 / 🤔 有启发）
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
import uuid


class ReflectionTrigger(str, Enum):
    """Reflection 触发类型 — 决策触发为主"""

    NEW_PROJECT = "new_project"  # 启动新项目
    ABANDON_PROJECT = "abandon_project"  # 放弃项目
    LONG_TERM_PLAN = "long_term_plan"  # 长期规划
    MAJOR_DECISION = "major_decision"  # 重大决策
    ROLE_SHIFT = "role_shift"  # 角色/身份变化
    RELATIONSHIP_CHANGE = "relationship_change"  # 重大关系变化
    MANUAL = "manual"  # 手动触发
    SCHEDULED = "scheduled"  # 定时触发（低优先级）
    OBSERVATION_UPDATED = "observation_updated"  # L3 观察更新驱动


class FeedbackType(str, Enum):
    """用户反馈类型"""

    ACCURATE = "accurate"  # 👍 准
    INACCURATE = "inaccurate"  # 👎 不准
    INSIGHTFUL = "insightful"  # 🤔 有启发
    IRRELEVANT = "irrelevant"  # 🚫 无关


@dataclass
class MirrorSnapshot:
    """
    Mirror 证据链快照

    不存 Observation 全文，只存 ID + 当时的权重 + 摘要
    因为 Observation 会随时间变化（增量更新）
    """

    observation_id: str
    dimension: str
    value_summary: str  # value 的摘要（如 "决策信号 1512 次"）
    evidence_summary: str  # evidence 的第一条（如 "典型情境: xxx"）
    confidence: float
    recency_weight: float  # 当时的时间衰减权重
    period_end: Optional[datetime] = None


@dataclass
class InsightSnapshot:
    """
    Insight 快照

    不存 Insight 全文（运行时生成），只存摘要 + 关键结论
    这样可以在不存储全文的情况下，回溯"当时系统说了什么"
    """

    summary: str  # 一句话摘要
    key_points: List[str]  # 关键结论列表
    dimensions_involved: List[str]  # 涉及哪些维度


@dataclass
class ReflectionRecord:
    """
    一次完整的 Reflection 生成记录

    这是 Reflection 层的"长期资产"——不是 Insight 本身，
    而是"何时、为何、基于什么证据、生成了什么结论"的元数据
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    created_at: datetime = field(default_factory=datetime.now)

    # 触发信息
    trigger: ReflectionTrigger = ReflectionTrigger.MANUAL
    trigger_event: str = ""  # 触发事件描述（如 "用户启动项目 X"）
    user_query: str = ""  # 用户的原始输入（如果有）

    # Mirror 证据链快照
    mirror_snapshots: List[MirrorSnapshot] = field(default_factory=list)
    mirror_dimensions: List[str] = field(default_factory=list)

    # Insight 快照
    insight: Optional[InsightSnapshot] = None

    # 时间上下文
    temporal_context: Optional[Dict] = None  # 时间节律信息

    # 用户反馈（显式）
    user_feedback: Optional["UserFeedback"] = None

    # 隐式反馈（从会话结构自动推断）
    implicit_feedback: Optional["ImplicitFeedbackRecord"] = None

    # 内部校验结果（系统自检）
    internal_validation: Optional[Dict] = None

    # 反哺标记
    fed_back_to_observations: bool = False
    fed_back_to_knowledge: bool = False

    # Object-level ACL.  A reflection contains a user query, inferred patterns,
    # and feedback; it must carry the strictest provenance envelope instead of
    # inheriting visibility from whichever caller happens to retrieve it.
    access_control: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat(),
            "trigger": self.trigger.value,
            "trigger_event": self.trigger_event,
            "user_query": self.user_query,
            "mirror_snapshots": [
                {
                    "observation_id": s.observation_id,
                    "dimension": s.dimension,
                    "value_summary": s.value_summary,
                    "evidence_summary": s.evidence_summary,
                    "confidence": s.confidence,
                    "recency_weight": s.recency_weight,
                    "period_end": s.period_end.isoformat() if s.period_end else None,
                }
                for s in self.mirror_snapshots
            ],
            "mirror_dimensions": self.mirror_dimensions,
            "insight": (
                {
                    "summary": self.insight.summary,
                    "key_points": self.insight.key_points,
                    "dimensions_involved": self.insight.dimensions_involved,
                }
                if self.insight
                else None
            ),
            "temporal_context": self.temporal_context,
            "user_feedback": (
                {
                    "feedback_type": self.user_feedback.feedback_type.value,
                    "comment": self.user_feedback.comment,
                    "given_at": self.user_feedback.given_at.isoformat(),
                }
                if self.user_feedback
                else None
            ),
            "fed_back_to_observations": self.fed_back_to_observations,
            "fed_back_to_knowledge": self.fed_back_to_knowledge,
            "access_control": self.access_control,
        }


@dataclass
class UserFeedback:
    """用户对 Insight 的反馈"""

    feedback_type: FeedbackType
    comment: str = ""  # 用户可选的评论
    given_at: datetime = field(default_factory=datetime.now)


@dataclass
class ImplicitFeedbackRecord:
    """隐式反馈记录（系统从会话结构自动推断）"""

    inferred_type: FeedbackType
    confidence: float  # 推断置信度（低于显式反馈）
    signals: List[str]  # 触发推断的结构信号
    inferred_at: datetime = field(default_factory=datetime.now)


@dataclass
class CognitiveShift:
    """
    认知变迁事件

    记录用户在某个维度上的认知发生了显著变化
    这是反哺 Layer 3 的核心数据
    """

    dimension: str  # 哪个维度发生了变化
    shift_type: str  # 变化类型（如 "role_change", "focus_shift", "style_evolution"）
    from_state: str  # 之前的状态
    to_state: str  # 现在的状态
    confidence: float  # 变迁的置信度
    evidence: List[str]  # 支撑变迁的证据
    first_seen_at: Optional[datetime]  # 旧数据可能没有首次出现时间
    shift_detected_at: datetime = field(default_factory=datetime.now)
    access_control: Dict[str, Any] = field(default_factory=dict)
    related_reflection_id: str = ""

    def to_dict(self) -> Dict:
        return {
            "dimension": self.dimension,
            "shift_type": self.shift_type,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "first_seen_at": (
                self.first_seen_at.isoformat() if self.first_seen_at else None
            ),
            "shift_detected_at": self.shift_detected_at.isoformat(),
            "access_control": self.access_control,
            "related_reflection_id": self.related_reflection_id,
        }


@dataclass
class CognitiveTrajectory:
    """
    认知轨迹

    用户在某个维度上的完整认知变迁历史
    例如：Growth 维度 — "开发者" → "技术负责人" → "管理者"
    """

    dimension: str
    shifts: List[CognitiveShift] = field(default_factory=list)
    current_state: str = ""
    state_history: List[Dict] = field(default_factory=list)  # [{state, since, confidence}]

    def add_shift(self, shift: CognitiveShift):
        """添加一次认知变迁"""
        self.shifts.append(shift)
        self.current_state = shift.to_state
        self.state_history.append(
            {
                "state": shift.to_state,
                "since": shift.shift_detected_at.isoformat(),
                "confidence": shift.confidence,
            }
        )
