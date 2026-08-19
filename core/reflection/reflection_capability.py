"""
Reflection Capability — 无触发反射能力封装

只做：Mirror → Experience → Insight → Store → Evidence Graph
不决定何时触发。触发由 ReflectionRouter 决定。

使用方式（宿主 Agent）：
    cap = ReflectionCapability()
    result = cap.reflect(scene="new_project", query="我要重构 Mnemos", role="builder")
    # 宿主 Agent 使用 result.insight_result.prompt_used 调用 LLM
    # 拿到 LLM 输出后：
    record = cap.store(
        result,
        insight_summary="...",
        insight_key_points=["..."],
        evidence_graph=evidence_graph,
    )
"""

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, List, Mapping, Optional

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.observation_store import ObservationStore
from core.evidence.evidence_graph import EvidenceGraph
from core.reflection.experience_matcher import ExperienceMatch, ExperienceMatcher
from core.reflection.insight_generator import InsightGenerator, InsightResult
from core.reflection.mirror_engine import DECISION_DIMENSION_MAP, MirrorEngine, MirrorResult
from core.reflection.models import InsightSnapshot, ReflectionRecord, ReflectionTrigger
from core.reflection.reflection_store import ReflectionStore, derive_reflection_access
from core.reflection.time_awareness import TimeAwareness

# Constants extracted from magic numbers
RECORDS_LIMIT = 10000

logger = logging.getLogger(__name__)


@dataclass
class ReflectionCapabilityResult:
    """Reflection Capability 的返回结果"""

    record: ReflectionRecord
    mirror: MirrorResult
    experiences: List[ExperienceMatch] = field(default_factory=list)
    insight_result: Optional[InsightResult] = None
    scene: str = ""
    role: Optional[str] = None


class ReflectionCapability:
    """反射能力封装"""

    def __init__(
        self,
        observation_store: Optional[ObservationStore] = None,
        reflection_store: Optional[ReflectionStore] = None,
        experience_matcher: Optional[ExperienceMatcher] = None,
        insight_generator: Optional[InsightGenerator] = None,
        mirror_engine: Optional[MirrorEngine] = None,
        time_awareness: Optional[TimeAwareness] = None,
        wiki_dir: Optional[str] = None,
        use_experience_matcher: bool = True,
        use_llm: bool = True,
    ):
        self.observation_store = observation_store or ObservationStore()
        self.reflection_store = reflection_store or ReflectionStore()
        self.experience_matcher = experience_matcher
        if self.experience_matcher is None and use_experience_matcher:
            try:
                self.experience_matcher = ExperienceMatcher(
                    reflection_store=self.reflection_store,
                    wiki_dir=wiki_dir,
                )
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                logger.warning("ExperienceMatcher 初始化失败", exc_info=True)
        self.insight_generator = insight_generator or InsightGenerator(use_llm=use_llm)
        self.mirror_engine = mirror_engine or MirrorEngine(observation_store=self.observation_store)
        self.time_awareness = time_awareness or TimeAwareness()

    def reflect(
        self,
        scene: str,
        query: str,
        role: Optional[str] = None,
        experiences: Optional[List[ExperienceMatch]] = None,
        min_confidence: float = 0.0,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        source_access_control: Mapping[str, Any] | None = None,
    ) -> ReflectionCapabilityResult:
        """
        准备一次 Reflection

        Args:
            scene: 决策场景（如 new_project, major_decision, role_shift, repeated_stuck, default）
            query: 用户原始输入
            role: 当前角色标签
            experiences: 外部提供的历史经验（None 则自动召回）
            min_confidence: 洞察最低置信度阈值

        Returns:
            ReflectionCapabilityResult（包含 prompt_used，需宿主 Agent 调用 LLM 后填充 Insight）
        """
        # 1. 确定相关维度
        dimensions = DECISION_DIMENSION_MAP.get(scene, DECISION_DIMENSION_MAP["default"])
        dimension_names = [d.value for d in dimensions]

        # 2. 构建 Mirror
        mirror = self.mirror_engine.build_mirror(
            trigger_scene=scene,
            user_query=query,
            principal=principal,
            narrowing=narrowing,
        )

        # 3. 召回历史经验
        if experiences is None and self.experience_matcher:
            try:
                experiences = self.experience_matcher.find_similar(
                    query=query,
                    scene=scene,
                    role=role,
                    dimensions=dimension_names,
                    top_k=5,
                    principal=principal,
                    narrowing=narrowing,
                )
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
                logger.warning("Experience matching failed: %s", e)
                experiences = []
        experiences = experiences or []

        # 4. 生成 Insight prompt
        temporal = self.time_awareness.get_temporal_context(
            principal=principal,
            narrowing=narrowing,
        )
        insight_result = self.insight_generator.generate(
            mirror=mirror,
            temporal=temporal,
            user_query=query,
            min_confidence=min_confidence,
            experiences=experiences,
        )

        # 5. 构建 ReflectionRecord（Insight 留空，等待宿主 Agent 填充）
        record = ReflectionRecord(
            trigger=ReflectionTrigger.MANUAL,
            trigger_event=scene,
            user_query=query,
            mirror_snapshots=mirror.snapshots,
            mirror_dimensions=mirror.dimensions_involved,
            insight=None,
            temporal_context=self._serialize_temporal(temporal),
        )
        record_sources: List[Mapping[str, Any]] = []
        if isinstance(source_access_control, Mapping):
            record_sources.append(source_access_control)
        record_sources.extend(
            item
            for item in mirror.source_access_controls
            if isinstance(item, Mapping)
        )
        if principal is not None:
            record.access_control = derive_reflection_access(
                record_sources,
                reflection_id=record.id,
                owner_principal_id=principal.principal_id,
                owner_agent=principal.agent,
            )

        return ReflectionCapabilityResult(
            record=record,
            mirror=mirror,
            experiences=experiences,
            insight_result=insight_result,
            scene=scene,
            role=role,
        )

    @staticmethod
    def _serialize_temporal(temporal) -> dict:
        """把 TemporalContext 转成可 JSON 序列化的 dict"""
        data = asdict(temporal)

        def _convert(value):
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, dict):
                return {k: _convert(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_convert(v) for v in value]
            return value

        return _convert(data)  # type: ignore[no-any-return]

    def store(
        self,
        result: ReflectionCapabilityResult,
        insight_summary: str,
        insight_key_points: List[str],
        evidence_graph: Optional[EvidenceGraph] = None,
    ) -> ReflectionRecord:
        """
        保存 Reflection 结果并写入证据图谱

        Args:
            result: reflect() 返回的结果
            insight_summary: LLM 生成的一句话摘要
            insight_key_points: LLM 生成的关键结论
            evidence_graph: 可选的证据图谱实例

        Returns:
            保存后的 ReflectionRecord
        """
        record = result.record
        record.insight = InsightSnapshot(
            summary=insight_summary,
            key_points=insight_key_points,
            dimensions_involved=result.mirror.dimensions_involved,
        )

        # 持久化
        self.reflection_store.save_record(record)
        logger.info("Saved ReflectionRecord %s for scene=%s", record.id, result.scene)

        # 写入证据图谱
        if evidence_graph:
            try:
                evidence_graph.add_reflection_record(record)
                logger.info("Linked ReflectionRecord %s to Evidence Graph", record.id)
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
                logger.warning("Failed to link reflection %s to evidence graph: %s", record.id, e)

        return record

    def get_record(
        self,
        record_id: str,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Optional[ReflectionRecord]:
        """按 ID 获取 ReflectionRecord（使用主键索引查询）"""
        try:
            record, _summary = self.reflection_store.authorized_get_by_id(
                record_id,
                principal=principal,
                narrowing=narrowing,
                purpose="reflection_read",
            )
            return record
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
            logger.warning("Failed to get reflection record %s: %s", record_id, e)
        return None
