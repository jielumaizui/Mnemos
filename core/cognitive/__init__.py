"""
Cognitive Layer — Mnemos vNext Observation Layer

从 L1 (Memory) + L2 (Knowledge) 提取客观观察，
为 L4 (Reflection) 提供事实基础。

核心模块：
- models: Observation 数据模型
- sources: 数据源读取器 (raw + wiki)
- dimension_extractors: 7 个维度的提取器
- observation_store: SQLite 持久化
- observation_engine: 协调引擎

使用示例：
    from core.cognitive import ObservationEngine
    engine = ObservationEngine()
    batch = engine.run()
"""

from core.cognitive.auto_calibration import CalibrationEngine, CalibrationReport
from core.cognitive.models import (
    Dimension,
    Observation,
    ObservationBatch,
    ObservationType,
    SourceType,
)
from core.cognitive.observation_engine import ObservationEngine
from core.cognitive.observation_store import ObservationIndex, ObservationStore
from core.cognitive.sources import SourceItem, SourceReader
from core.cognitive.consolidator import CognitiveConsolidator, CognitiveConsolidationOptions
from core.cognitive.delivery_router import (
    DeliveryBudgetPolicy,
    DeliveryDecision,
    KnowledgeDeliveryRouter,
)
from core.cognitive.policy_patch import (
    PolicyPatch,
    PolicyPatchOptions,
    PolicyPatchStore,
)
from core.cognitive.trust_scorer import (
    KnowledgeTrustOptions,
    KnowledgeTrustScorer,
    TrustDecision,
)

__all__ = [
    "Dimension",
    "Observation",
    "ObservationBatch",
    "ObservationType",
    "SourceType",
    "ObservationEngine",
    "ObservationIndex",
    "ObservationStore",
    "SourceItem",
    "SourceReader",
    "CalibrationEngine",
    "CalibrationReport",
    "CognitiveConsolidator",
    "CognitiveConsolidationOptions",
    "DeliveryBudgetPolicy",
    "DeliveryDecision",
    "KnowledgeDeliveryRouter",
    "PolicyPatch",
    "PolicyPatchOptions",
    "PolicyPatchStore",
    "KnowledgeTrustOptions",
    "KnowledgeTrustScorer",
    "TrustDecision",
]
