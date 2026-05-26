"""
scorer — 评分层兼容包

文档引用 core.scorer/，实际实现位于 core.scoring/
此包提供兼容导入。
"""
from core.scoring.adaptive_scorer import *
from core.scoring.online_stats import *

# V2 骨架（数据积累阶段，接口已定义，算法待实现）
from core.scoring.adaptive_scorer_v2 import (
    AdaptiveScorerV2,
    ScoreCardV2,
    FeedbackV2,
    GroundTruth,
)
