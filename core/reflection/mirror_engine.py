"""
Mirror Engine — 证据链检索与拼接

核心职责：
1. 从 ObservationStore 检索与当前决策相关的 Observation
2. 按时间衰减权重排序
3. 拼接成证据链（Mirror）

设计原则：
- 不扫描全库，只查询相关维度
- 代码层实现权重计算（不依赖 Agent 推理）
- 证据链包含时间上下文（让 Insight 生成器知道数据的时效性）
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.models import Dimension, Observation
from core.cognitive.observation_store import ObservationStore as ObsStore
from core.reflection.models import MirrorSnapshot
from core.reflection.time_awareness import TimeAwareness

# 决策场景 → 相关维度映射
# 不同决策场景需要关注不同的 Observation 维度
DECISION_DIMENSION_MAP = {
    "new_project": [
        Dimension.ATTENTION,
        Dimension.DECISIONS,
        Dimension.TIME,
        Dimension.ACTIONS,
        Dimension.GROWTH,
    ],
    "abandon_project": [Dimension.ATTENTION, Dimension.DECISIONS, Dimension.STRESS, Dimension.TIME],
    "long_term_plan": [
        Dimension.GROWTH,
        Dimension.ATTENTION,
        Dimension.TIME,
        Dimension.RELATIONSHIPS,
    ],
    "major_decision": [Dimension.DECISIONS, Dimension.STRESS, Dimension.TIME, Dimension.GROWTH],
    "role_shift": [Dimension.GROWTH, Dimension.RELATIONSHIPS, Dimension.DECISIONS],
    "relationship_change": [Dimension.RELATIONSHIPS, Dimension.STRESS, Dimension.DECISIONS],
    "default": [Dimension.ATTENTION, Dimension.DECISIONS, Dimension.TIME, Dimension.STRESS],
}

# 触发类型 → 决策场景
TRIGGER_SCENE_MAP = {
    "new_project": "new_project",
    "abandon_project": "abandon_project",
    "long_term_plan": "long_term_plan",
    "major_decision": "major_decision",
    "role_shift": "role_shift",
    "relationship_change": "relationship_change",
}


@dataclass
class MirrorResult:
    """Mirror 证据链结果"""

    snapshots: List[MirrorSnapshot] = field(default_factory=list)
    dimensions_involved: List[str] = field(default_factory=list)
    total_observations_scanned: int = 0
    total_weighted_score: float = 0.0
    temporal_note: str = ""  # 时间上下文说明
    # Kept in-memory only.  The record writer derives a strict ACL from these
    # headers; ``to_prompt_context`` deliberately never serializes them.
    source_access_controls: List[Dict[str, Any]] = field(default_factory=list)

    def to_prompt_context(self) -> str:
        """转换为供 LLM 使用的提示上下文"""
        lines = [
            "## Mirror（证据链）",
            "",
            f"**时间上下文**: {self.temporal_note}",
            f"**涉及维度**: {', '.join(self.dimensions_involved)}",
            "",
        ]

        for i, snap in enumerate(self.snapshots, 1):
            lines.append(f"### 证据 {i}: [{snap.dimension}]")
            lines.append(f"- **数据**: {snap.value_summary}")
            if snap.evidence_summary:
                lines.append(f"- **情境**: {snap.evidence_summary}")
            lines.append(
                f"- **置信度**: {snap.confidence:.2f} | **时效权重**: {snap.recency_weight:.2f}"
            )
            lines.append("")

        return "\n".join(lines)


class MirrorEngine:
    """证据链引擎"""

    def __init__(
        self,
        observation_store: Optional[ObsStore] = None,
        time_awareness: Optional[TimeAwareness] = None,
    ):
        self.obs_store = observation_store
        self.time_awareness = time_awareness or TimeAwareness()

    def build_mirror(
        self,
        trigger_scene: str,
        user_query: str = "",
        limit_per_dim: int = 3,
        min_weight: float = 0.2,
        dimension_weights: Optional[Dict[str, float]] = None,
        skip_dimensions: Optional[List[str]] = None,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        purpose: str = "reflection_prompt",
    ) -> MirrorResult:
        """
        构建 Mirror 证据链

        Args:
            trigger_scene: 决策场景（如 "new_project", "major_decision"）
            user_query: 用户的原始输入（用于关键词匹配）
            limit_per_dim: 每个维度最多取几条 Observation
            min_weight: 最小时间衰减权重（低于此值的 Observation 被过滤）

        Returns:
            MirrorResult
        """
        if not self.obs_store:
            return MirrorResult(temporal_note="ObservationStore 未配置")
        if principal is None:
            return MirrorResult(temporal_note="principal_required")
        if not str(purpose or "").strip():
            return MirrorResult(temporal_note="purpose_required")

        # 1. 确定相关维度（应用跳过列表）
        dimensions = DECISION_DIMENSION_MAP.get(trigger_scene, DECISION_DIMENSION_MAP["default"])
        if skip_dimensions:
            dimensions = [d for d in dimensions if d.value not in skip_dimensions]

        # 2. 获取时间上下文
        temporal = self.time_awareness.get_temporal_context(
            principal=principal,
            narrowing=narrowing,
        )

        # 3. 按维度检索 Observation
        all_candidates = []
        for dim in dimensions:
            try:
                observations, _access_summary = self.obs_store.authorized_query(
                    principal=principal,
                    narrowing=narrowing,
                    purpose=purpose,
                    dimension=dim,
                    limit=limit_per_dim * 2,
                )
                for obs in observations:
                    weight = self.time_awareness.recency_weight(
                        obs.period_end, dim.value, temporal.now
                    )
                    if weight >= min_weight:
                        all_candidates.append((obs, weight, dim.value))
            # DEBT(S8): 容错跳过，避免单条记录中断批量处理
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                # 单个维度查询失败不影响整体
                continue

        # 4. 按权重排序（融入校准维度权重）
        def _sort_score(x):
            recency_weight, obs, dim_name = x[1], x[0], x[2]
            dim_w = dimension_weights.get(dim_name, 1.0) if dimension_weights else 1.0
            # 维度权重低于阈值直接排到末尾
            if dim_w < 0.3:
                return -1.0
            return recency_weight * dim_w * obs.confidence

        all_candidates.sort(key=_sort_score, reverse=True)

        # 5. 构建 MirrorSnapshot（每个维度最多 limit_per_dim 条）
        dim_counts = {}  # type: ignore[var-annotated]
        snapshots = []
        source_access_controls: List[Dict[str, Any]] = []
        for obs, weight, dim_name in all_candidates:
            if dim_counts.get(dim_name, 0) >= limit_per_dim:
                continue

            # 提取 value 的摘要
            value_summary = self._summarize_value(obs)
            evidence_summary = obs.evidence[0] if obs.evidence else ""

            snapshots.append(
                MirrorSnapshot(
                    observation_id=obs.id,
                    dimension=dim_name,
                    value_summary=value_summary,
                    evidence_summary=evidence_summary,
                    confidence=obs.confidence,
                    recency_weight=weight,
                    period_end=obs.period_end,
                )
            )
            if isinstance(obs.access_control, dict):
                source_access_controls.append(dict(obs.access_control))
            dim_counts[dim_name] = dim_counts.get(dim_name, 0) + 1

        # 6. 生成时间上下文说明
        temporal_note = self._build_temporal_note(temporal, dim_counts)

        # 7. 计算总分
        total_score = sum(s.confidence * s.recency_weight for s in snapshots)

        return MirrorResult(
            snapshots=snapshots,
            dimensions_involved=list(dim_counts.keys()),
            total_observations_scanned=len(all_candidates),
            total_weighted_score=round(total_score, 2),
            temporal_note=temporal_note,
            source_access_controls=source_access_controls,
        )

    def _summarize_value(self, obs: Observation) -> str:
        """提取 Observation value 的简短摘要"""
        value = obs.value

        # 频率型
        if isinstance(value, dict):
            # 取最多的前2项
            top_items = sorted(
                value.items(),
                key=lambda x: x[1] if isinstance(x[1], (int, float)) else 0,
                reverse=True,
            )[:2]
            parts = [f"{k}: {v}" for k, v in top_items]
            return " | ".join(parts)

        # 数值型
        if isinstance(value, (int, float)):
            return f"{value}{obs.unit}"

        # 其他
        return str(value)[:60]

    def _build_temporal_note(self, temporal, dim_counts: Dict[str, int]) -> str:
        """构建时间上下文说明"""
        parts = [temporal.rhythm_description]

        # 添加数据新鲜度信息
        fresh_dims = []
        stale_dims = []
        for dim, count in dim_counts.items():
            if dim in temporal.dimension_freshness:
                status = temporal.dimension_freshness[dim]["status"]
                if status == "fresh":
                    fresh_dims.append(dim)
                elif status == "stale":
                    stale_dims.append(dim)

        if fresh_dims:
            parts.append(f"数据较新的维度: {', '.join(fresh_dims)}")
        if stale_dims:
            parts.append(f"数据较旧的维度: {', '.join(stale_dims)}")

        # 上次 Reflection 间隔
        if temporal.last_reflection_ago is not None:
            humanized = self.time_awareness.humanize_duration(temporal.last_reflection_ago)
            parts.append(f"距离上次分析: {humanized}")

        return " | ".join(parts)
