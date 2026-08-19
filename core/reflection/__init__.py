"""
Reflection Layer — 运行时洞察生成

核心设计：
- Reflection 是运行时生成的，但生成记录和认知变迁会被存储
- Mirror（证据链）从 ObservationStore 检索，不独立存储
- Insight（洞察）运行时生成，但摘要和反馈会被记录
- 认知变迁轨迹是长期资产，反哺 Layer 3 (Observations) 和 Layer 2 (Knowledge)

模块职责：
- mirror_engine: 从 Observation 检索 + 排序 + 拼接证据链
- insight_generator: 基于证据链调用 LLM 生成洞察
- trigger_detector: 检测用户是否在做重大决策
- time_awareness: 代码层时间感知（衰减权重、节律检测）
- feedback_loop: 认知变迁反哺机制
- reflection_store: Reflection 记录存储
"""

from core.reflection.mirror_engine import MirrorEngine, MirrorResult
from core.reflection.insight_generator import InsightGenerator, InsightResult
from core.reflection.trigger_detector import TriggerContext, TriggerDetector, TriggerEvent
from core.reflection.time_awareness import TimeAwareness, TemporalContext
from core.reflection.feedback_loop import FeedbackLoop, CognitiveShift
from core.reflection.reflection_store import ReflectionStore
from core.reflection.experience_matcher import ExperienceMatcher, ExperienceMatch
from core.reflection.reflection_capability import ReflectionCapability, ReflectionCapabilityResult
from core.reflection.reflection_router import ReflectionRouter, ReflectionRoute
from core.reflection.reflection_engine import ReflectionEngine, ReflectionResult
from core.reflection.feedback_collector import (
    FeedbackCollector,
    FeedbackResult,
    PendingFeedbackItem,
)
from core.reflection.feedback_analytics import (
    FeedbackAnalytics,
    DimensionEffectiveness,
    TriggerEffectiveness,
    FeedbackTrend,
)
from core.reflection.implicit_feedback import (
    ImplicitFeedbackDetector,
    SessionContext,
    ImplicitFeedback,
)
from core.reflection.insight_calibrator import InsightCalibrator, CalibrationParams
from core.reflection.internal_validator import (
    InternalValidator,
    ValidationResult,
    ValidationFinding,
)
from core.reflection.deviation_detector import (
    DeviationDetector,
    DeviationSignal,
    ListeningSession,
)
from core.reflection.consumers import (
    Layer5Consumer,
    PersonaSignalConsumer,
    KIAExperienceConsumer,
    HephaestusCalibrationConsumer,
    CompositeConsumer,
)
from core.reflection.models import (
    ReflectionRecord,
    ReflectionTrigger,
    UserFeedback,
    ImplicitFeedbackRecord,
    CognitiveTrajectory,
    FeedbackType,
)

__all__ = [
    "MirrorEngine",
    "MirrorResult",
    "InsightGenerator",
    "InsightResult",
    "TriggerContext",
    "TriggerDetector",
    "TriggerEvent",
    "TimeAwareness",
    "TemporalContext",
    "FeedbackLoop",
    "CognitiveShift",
    "ReflectionStore",
    "ExperienceMatcher",
    "ExperienceMatch",
    "ReflectionCapability",
    "ReflectionCapabilityResult",
    "ReflectionRouter",
    "ReflectionRoute",
    "ReflectionEngine",
    "ReflectionResult",
    "ReflectionRecord",
    "ReflectionTrigger",
    "UserFeedback",
    "ImplicitFeedbackRecord",
    "CognitiveTrajectory",
    "FeedbackType",
    # Layer 5: Feedback
    "FeedbackCollector",
    "FeedbackResult",
    "PendingFeedbackItem",
    "FeedbackAnalytics",
    "DimensionEffectiveness",
    "TriggerEffectiveness",
    "FeedbackTrend",
    "InsightCalibrator",
    "CalibrationParams",
    # 隐式反馈 + 内部校验
    "ImplicitFeedbackDetector",
    "SessionContext",
    "ImplicitFeedback",
    "InternalValidator",
    "ValidationResult",
    "ValidationFinding",
    # Layer 4: 偏差检测触发
    "DeviationDetector",
    "DeviationSignal",
    "ListeningSession",
    # Layer 5: 外循环消费者
    "Layer5Consumer",
    "PersonaSignalConsumer",
    "KIAExperienceConsumer",
    "HephaestusCalibrationConsumer",
    "CompositeConsumer",
]
