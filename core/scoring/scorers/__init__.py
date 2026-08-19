"""子系统评分器

V1 五域 scorer（DistillScorer/KGScorer/OpsScorer/ProfileScorer/SyncScorer）
已下线，其有价值的规则函数已迁移到 core.scoring.rule_helpers。
当前仅保留 V2 桥接评分器。
"""

from .distill_scorer_v2 import DistillScorerV2
from .domain_scorers import (
    DOMAIN_SCORERS,
    SCORER_DIMENSIONS,
    BaseDomainScorer,
    KGDomainScorer,
    OpsDomainScorer,
    ProfileDomainScorer,
    RawMemoryScorer,
    SyncDomainScorer,
    dimension_catalog,
    score_domain,
)

__all__ = [
    "DistillScorerV2",
    "BaseDomainScorer",
    "SyncDomainScorer",
    "RawMemoryScorer",
    "KGDomainScorer",
    "ProfileDomainScorer",
    "OpsDomainScorer",
    "DOMAIN_SCORERS",
    "SCORER_DIMENSIONS",
    "dimension_catalog",
    "score_domain",
]
