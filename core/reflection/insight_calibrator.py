"""
Insight Calibrator — 洞察生成校准器

核心职责：
1. 基于用户反馈数据，动态调整 Insight 生成策略
2. 为 InsightGenerator 提供校准参数
3. 识别并降低低质量维度的权重，提升高质量维度的权重
4. 生成生成提示词的校准指令

使用方式：
    calibrator = InsightCalibrator(feedback_analytics)

    # 获取校准参数（供 InsightGenerator 使用）
    params = calibrator.get_calibration_params()
    # {
    #   "dimension_weights": {"attention": 1.2, "stress": 0.6, ...},
    #   "confidence_threshold": 0.55,
    #   "skip_dimensions": ["stress"],
    #   "generation_hints": "用户近期对 stress 维度的反馈较差...",
    # }

    # 获取生成提示词校准
    hints = calibrator.get_generation_hints()

设计原则：
- 校准参数在代码层计算，不依赖 Agent 推理
- 调整是渐进式的（有平滑因子），避免剧烈波动
- 保留原始默认值，校准作为增量调整
- 校准结果可被序列化/持久化，跨会话生效
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.reflection.feedback_analytics import FeedbackAnalytics, DimensionEffectiveness

# Constants extracted from magic numbers
INSIGHT_CALIBRATOR_GET_CALIBRATION_PARAMS_DAYS = 30
INSIGHT_CALIBRATOR_GET_GENERATION_HINTS_DAYS = 30
INSIGHT_CALIBRATOR_GET_WEIGHTED_MIRROR_PARAMS_DAYS = 30
INSIGHT_CALIBRATOR_APPLY_TO_INSIGHT_RESULT_INSIGHT_RESULT_DAYS = 30


# 默认维度权重（未经校准的基准值）
DEFAULT_DIMENSION_WEIGHTS = {
    "attention": 1.0,
    "decisions": 1.0,
    "actions": 1.0,
    "time": 1.0,
    "stress": 1.0,
    "relationships": 1.0,
    "growth": 1.0,
}

# 校准配置
CALIBRATION_CONFIG = {
    "min_samples_for_adjustment": 5,  # 最少样本数才调整
    "high_quality_threshold": 0.75,  # 准确率 > 此值为高质量
    "low_quality_threshold": 0.4,  # 准确率 < 此值为低质量
    "max_weight_multiplier": 1.5,  # 最大权重倍数
    "min_weight_multiplier": 0.3,  # 最小权重倍数
    "smoothing_factor": 0.3,  # 平滑因子（新值 * 0.3 + 旧值 * 0.7）
    "confidence_boost_for_high_quality": 0.1,  # 高质量维度的置信度加成
    "confidence_penalty_for_low_quality": 0.15,  # 低质量维度的置信度惩罚
}


@dataclass
class CalibrationParams:
    """校准参数 — 供 InsightGenerator 消费"""

    dimension_weights: Dict[str, float] = field(default_factory=dict)
    confidence_threshold: float = 0.5  # 生成 Insight 的最低置信度阈值
    skip_dimensions: List[str] = field(default_factory=list)
    boost_dimensions: List[str] = field(default_factory=list)
    generation_hints: str = ""  # 给提示词的校准指令
    calibrated_at: str = ""  # 校准时间

    def to_dict(self) -> Dict:
        return {
            "dimension_weights": self.dimension_weights,
            "confidence_threshold": self.confidence_threshold,
            "skip_dimensions": self.skip_dimensions,
            "boost_dimensions": self.boost_dimensions,
            "generation_hints": self.generation_hints,
            "calibrated_at": self.calibrated_at,
        }


class InsightCalibrator:
    """洞察生成校准器"""

    def __init__(
        self,
        feedback_analytics: Optional[FeedbackAnalytics] = None,
        config: Optional[Dict] = None,
    ):
        self.analytics = feedback_analytics or FeedbackAnalytics()
        self.config = {**CALIBRATION_CONFIG, **(config or {})}
        self._cached_params: Optional[CalibrationParams] = None
        self._cache_timestamp: Optional[str] = None

    def get_calibration_params(
        self,
        days: int = INSIGHT_CALIBRATOR_GET_CALIBRATION_PARAMS_DAYS,
        force_refresh: bool = False,
    ) -> CalibrationParams:
        """
        获取校准参数

        Args:
            days: 反馈数据时间窗口
            force_refresh: 强制重新计算（忽略缓存）

        Returns:
            CalibrationParams
        """
        from datetime import datetime

        cache_key = f"{days}_{datetime.now().strftime('%Y-%m-%d-%H')}"
        if not force_refresh and self._cached_params and self._cache_timestamp == cache_key:
            return self._cached_params

        # 1. 获取各维度有效性
        dim_eff = self.analytics.effectiveness_by_dimension(days)

        # 2. 计算维度权重调整
        weights = self._calculate_dimension_weights(dim_eff)

        # 3. 确定跳过/提升的维度
        skip_dims = self._identify_skip_dimensions(dim_eff)
        boost_dims = self._identify_boost_dimensions(dim_eff)

        # 4. 计算置信度阈值
        conf_threshold = self._calculate_confidence_threshold(dim_eff)

        # 5. 生成提示词校准指令
        hints = self._build_generation_hints(dim_eff, skip_dims, boost_dims)

        params = CalibrationParams(
            dimension_weights=weights,
            confidence_threshold=conf_threshold,
            skip_dimensions=skip_dims,
            boost_dimensions=boost_dims,
            generation_hints=hints,
            calibrated_at=datetime.now().isoformat(),
        )

        self._cached_params = params
        self._cache_timestamp = cache_key

        return params

    def get_generation_hints(self, days: int = INSIGHT_CALIBRATOR_GET_GENERATION_HINTS_DAYS) -> str:
        """获取生成提示词的校准指令"""
        params = self.get_calibration_params(days)
        return params.generation_hints

    def get_weighted_mirror_params(
        self, days: int = INSIGHT_CALIBRATOR_GET_WEIGHTED_MIRROR_PARAMS_DAYS
    ) -> Dict[str, float]:
        """
        获取 Mirror 构建时的维度权重

        供 MirrorEngine 在构建证据链时使用，
        优先选取高权重维度的 Observation。
        """
        params = self.get_calibration_params(days)
        return params.dimension_weights

    def apply_to_insight_result(
        self,
        insight_result,
        days: int = INSIGHT_CALIBRATOR_APPLY_TO_INSIGHT_RESULT_INSIGHT_RESULT_DAYS,
    ):
        """
        将校准应用到 Insight 结果

        例如：如果某维度是低质量的，降低该 Insight 的置信度
        """
        params = self.get_calibration_params(days)

        # 如果 Insight 涉及跳过的维度，标记为需要人工复核
        involved = set(insight_result.dimensions_involved)
        skipped = set(params.skip_dimensions)

        if involved & skipped:
            insight_result.confidence *= 0.7  # 降低置信度
            insight_result.calibration_note = (
                f"注意：本洞察涉及近期反馈较差的维度 ({', '.join(involved & skipped)})，"
                "建议谨慎参考"
            )

        return insight_result

    # ───────────────────────────────
    # 内部计算方法
    # ───────────────────────────────

    def _calculate_dimension_weights(
        self, dim_eff: Dict[str, DimensionEffectiveness]
    ) -> Dict[str, float]:
        """
        计算维度权重

        策略：
        - 高质量维度（准确率 > 阈值）：提升权重
        - 低质量维度（准确率 < 阈值）：降低权重
        - 数据不足：保持默认权重
        - 应用平滑因子避免剧烈波动
        """
        weights = dict(DEFAULT_DIMENSION_WEIGHTS)
        min_samples = self.config["min_samples_for_adjustment"]
        high_thresh = self.config["high_quality_threshold"]
        low_thresh = self.config["low_quality_threshold"]
        max_mult = self.config["max_weight_multiplier"]
        min_mult = self.config["min_weight_multiplier"]
        smoothing = self.config["smoothing_factor"]

        for dim, eff in dim_eff.items():
            if dim not in weights:
                continue

            # 数据不足，不调整
            if eff.total < min_samples:
                continue

            base_weight = weights[dim]

            # 根据准确率计算目标权重倍数
            if eff.accuracy_rate >= high_thresh:
                # 高质量：提升权重
                target_mult = 1.0 + (eff.accuracy_rate - high_thresh) * 2.0
                target_mult = min(target_mult, max_mult)
            elif eff.accuracy_rate <= low_thresh:
                # 低质量：降低权重
                target_mult = 1.0 - (low_thresh - eff.accuracy_rate) * 1.5
                target_mult = max(target_mult, min_mult)
            else:
                # 中等质量：微调
                target_mult = 1.0 + (eff.accuracy_rate - 0.6) * 0.5

            # 应用平滑
            new_weight = base_weight * (1 - smoothing) + base_weight * target_mult * smoothing
            weights[dim] = round(new_weight, 2)

        return weights

    def _identify_skip_dimensions(self, dim_eff: Dict[str, DimensionEffectiveness]) -> List[str]:
        """识别应该跳过的维度（低质量且样本足够）"""
        min_samples = self.config["min_samples_for_adjustment"]
        low_thresh = self.config["low_quality_threshold"]

        return [
            dim
            for dim, eff in dim_eff.items()
            if eff.total >= min_samples and eff.accuracy_rate < low_thresh
        ]

    def _identify_boost_dimensions(self, dim_eff: Dict[str, DimensionEffectiveness]) -> List[str]:
        """识别应该提升的维度（高质量且样本足够）"""
        min_samples = self.config["min_samples_for_adjustment"]
        high_thresh = self.config["high_quality_threshold"]

        return [
            dim
            for dim, eff in dim_eff.items()
            if eff.total >= min_samples and eff.accuracy_rate >= high_thresh
        ]

    def _calculate_confidence_threshold(self, dim_eff: Dict[str, DimensionEffectiveness]) -> float:
        """
        计算生成 Insight 的最低置信度阈值

        策略：如果整体准确率较低，提高阈值（更严格）
              如果整体准确率较高，降低阈值（更宽松）
        """
        # 收集有足够样本的维度
        valid_dims = [
            eff
            for eff in dim_eff.values()
            if eff.total >= self.config["min_samples_for_adjustment"]
        ]

        if not valid_dims:
            return 0.5  # 默认值

        # 平均准确率
        avg_accuracy = sum(d.accuracy_rate for d in valid_dims) / len(valid_dims)

        # 阈值与准确率负相关
        # 准确率 0.3 -> 阈值 0.7
        # 准确率 0.7 -> 阈值 0.4
        threshold = 0.85 - avg_accuracy * 0.5
        return round(max(0.3, min(0.8, threshold)), 2)

    def _build_generation_hints(
        self,
        dim_eff: Dict[str, DimensionEffectiveness],
        skip_dims: List[str],
        boost_dims: List[str],
    ) -> str:
        """构建生成提示词的校准指令"""
        hints = []

        # 高质量维度提示
        if boost_dims:
            hints.append(f"以下维度的洞察用户反馈较好，可以多关注: {', '.join(boost_dims)}")

        # 低质量维度提示
        if skip_dims:
            hints.append(
                f"以下维度的洞察用户反馈较差，生成时需要更谨慎或降低权重: {', '.join(skip_dims)}"
            )

        # 趋势提示
        improving = [d.dimension for d in dim_eff.values() if d.trend == "improving"]
        declining = [d.dimension for d in dim_eff.values() if d.trend == "declining"]

        if improving:
            hints.append(f"这些维度的洞察质量在提升: {', '.join(improving)}")
        if declining:
            hints.append(f"这些维度的洞察质量在下降，需要检查原因: {', '.join(declining)}")

        # 整体校准摘要
        valid_dims = [
            d for d in dim_eff.values() if d.total >= self.config["min_samples_for_adjustment"]
        ]
        if valid_dims:
            avg_rate = sum(d.accuracy_rate for d in valid_dims) / len(valid_dims)
            hints.append(f"整体洞察准确率: {avg_rate:.0%}")

        return "\n".join(hints) if hints else "暂无校准数据"
