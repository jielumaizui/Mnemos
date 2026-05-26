# -*- coding: utf-8 -*-
"""Hephaestus — 蒸馏子系统"""

from .distillation_engine import (
    DistillationEngine,
    DistillationResult,
    KnowledgeFragment,
    PipelineLayerResult,
    HostAgentCaller,
    NoiseFilter,
    ValuePrejudgment,
    LLMValueJudge,
    KnowledgeExtractor,
    DistillSelfCheck,
    CrossAgentLinker,
    DistillFeedbackLoop,
)
from .prompt_builder import (
    PromptBuilder,
    DistillTask,
    TokenBudget,
)
from .incremental_distiller import IncrementalDistiller
from .deferred_distill import (
    DeferredDistillationQueue,
    WikiIncrementalDistiller,
    FragmentationDetector,
    CrossPageDistiller,
    AutoSwitchWeightAdapter,
)
from .evolution_tracker import (
    TemporalEvolutionTracker,
    DarkKnowledgeIntegration,
    RecirculationGuard,
)
