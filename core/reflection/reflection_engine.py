"""
Reflection Engine — 主控引擎

协调 Trigger → Mirror → Insight → Feedback 的完整流程。

使用方式：
    engine = ReflectionEngine()

    # 方式1：主动检测触发（传统模式，直接生成 Insight）
    result = engine.reflect_on_user_input("我要启动新项目X")

    # 方式2：偏差检测触发（用户设计模式）
    # 2a. 用户说"我想策划一场活动" → 启动监听
    engine.start_listening(session_id, "用户说要策划活动")
    # 2b. 用户继续输出想法 → 检测偏差
    result = engine.process_message_in_session(session_id, "预计2周完成")
    # 2c. 如果检测到偏差 → 自动生成 Mirror + Insight

    # 方式3：手动触发
    result = engine.reflect_manually("帮我分析最近的决策模式")

    # 方式4：获取认知轨迹
    trajectory = engine.get_cognitive_trajectory("growth")

设计原则：
- 运行时生成，不存储 Insight 全文
- 存储 ReflectionRecord（元数据 + 证据快照 + 洞察摘要）
- 自动执行 Feedback Loop（检测认知变迁 + 反哺）
- 支持两种触发模式：
  a) 直接触发：检测到关键词立即生成 Insight
  b) 偏差检测触发：检测到关键词后进入监听，只有发现偏差才生成 Insight
"""

import logging
from datetime import timedelta
from typing import Any, Dict, List, Mapping, Optional

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.observation_store import ObservationStore as ObsStore
from core.evidence.evidence_graph import EvidenceGraph
from core.reflection.feedback_collector import FeedbackCollector, FeedbackResult as UserFbResult
from core.reflection.deviation_detector import DeviationDetector, DeviationSignal, ListeningSession
from core.reflection.experience_matcher import ExperienceMatch, ExperienceMatcher
from core.reflection.feedback_loop import FeedbackLoop
from core.reflection.implicit_feedback import ImplicitFeedbackDetector, SessionContext
from core.reflection.insight_calibrator import InsightCalibrator
from core.reflection.insight_generator import InsightGenerator, InsightResult
from core.reflection.internal_validator import InternalValidator
from core.reflection.mirror_engine import MirrorEngine, MirrorResult
from core.reflection.models import (
    CognitiveTrajectory,
    FeedbackType,
    ReflectionRecord,
    ReflectionTrigger,
)
from core.reflection.reflection_store import ReflectionStore, derive_reflection_access
from core.reflection.reflection_router import ReflectionRouter
from core.reflection.reflection_exporter import ReflectionExporter
from core.reflection.time_awareness import TimeAwareness
from core.reflection.trigger_detector import TriggerContext, TriggerDetector, TriggerEvent
from core.reflection.consumers import (
    CompositeConsumer,
    HephaestusCalibrationConsumer,
    KIAExperienceConsumer,
    PersonaSignalConsumer,
    ReflectionPolicyPatchConsumer,
)

logger = logging.getLogger(__name__)


def _serialize_temporal_context(temporal) -> Optional[dict]:
    """将 TemporalContext 转为可 JSON 序列化的 dict（处理 datetime 等不可序列化类型）。"""
    if temporal is None:
        return None
    from dataclasses import asdict
    from datetime import datetime

    data = (
        asdict(temporal)
        if hasattr(temporal, "__dataclass_fields__")
        else getattr(temporal, "__dict__", {})
    )

    def _convert(value):
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: _convert(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_convert(v) for v in value]
        return value

    return _convert(data)  # type: ignore[no-any-return]


# Constants extracted from magic numbers
REFLECTION_ENGINE_GET_FEEDBACK_SUMMARY_DAYS = 30
REFLECTION_ENGINE_DURATION_BUCKET_MONTH_DAYS = 30
REFLECTION_ENGINE_GET_INSIGHT_QUALITY_REPORT_DAYS = 30


class ReflectionResult:
    """Reflection 完整结果"""

    def __init__(
        self,
        triggered: bool,
        trigger_event: Optional[TriggerEvent] = None,
        mirror: Optional[MirrorResult] = None,
        insight: Optional[InsightResult] = None,
        record: Optional[ReflectionRecord] = None,
        feedback_messages: Optional[List[str]] = None,
        listening: bool = False,
    ):
        self.triggered = triggered
        self.trigger_event = trigger_event
        self.mirror = mirror
        self.insight = insight
        self.record = record
        self.feedback_messages = feedback_messages or []
        self.listening = listening  # 是否处于偏差检测监听模式

    def to_dict(self) -> Dict:
        return {
            "triggered": self.triggered,
            "listening": self.listening,
            "trigger": self.trigger_event.to_dict() if self.trigger_event else None,
            "mirror_dimensions": self.mirror.dimensions_involved if self.mirror else [],
            "insight_summary": self.insight.summary if self.insight else "",
            "insight_confidence": self.insight.confidence if self.insight else 0.0,
            "feedback_messages": self.feedback_messages,
        }


class ReflectionEngine:
    """Reflection 主控引擎"""

    def __init__(
        self,
        observation_store: Optional[ObsStore] = None,
        reflection_store: Optional[ReflectionStore] = None,
        sensitivity: float = 1.0,
        evidence_graph: Optional[EvidenceGraph] = None,
        reflection_router: Optional[ReflectionRouter] = None,
        wiki_dir: Optional[str] = None,
        export_to_wiki: bool = True,
        register_default_consumers: bool = False,
        persona_store=None,
        kia_store=None,
        use_llm: bool = True,
        use_experience_matcher: bool = True,
    ):
        self.obs_store = observation_store or ObsStore()
        self.ref_store = reflection_store or ReflectionStore()
        self.evidence_graph = evidence_graph
        self.reflection_router = reflection_router or ReflectionRouter()
        self.time_awareness = TimeAwareness(self.obs_store, self.ref_store)
        self.trigger_detector = TriggerDetector(
            observation_store=self.obs_store, sensitivity=sensitivity
        )
        self.mirror_engine = MirrorEngine(self.obs_store, self.time_awareness)
        self.insight_generator = InsightGenerator(use_llm=use_llm)
        self.feedback_loop = FeedbackLoop(self.ref_store, self.obs_store)
        self.calibrator = InsightCalibrator()
        self.feedback_collector = FeedbackCollector(self.ref_store)
        self.internal_validator = InternalValidator()
        self.implicit_detector = ImplicitFeedbackDetector()
        self.deviation_detector = DeviationDetector()
        self.wiki_dir = wiki_dir
        self.experience_matcher = None
        if use_experience_matcher:
            try:
                self.experience_matcher = ExperienceMatcher(
                    reflection_store=self.ref_store,
                    wiki_dir=str(self.wiki_dir) if self.wiki_dir else None,
                )
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                logger.warning("ExperienceMatcher 初始化失败", exc_info=True)
        self._consumers: list = []  # Layer 5 外循环消费者列表
        self.export_to_wiki = export_to_wiki and wiki_dir is not None
        self._reflection_exporter = (
            ReflectionExporter(wiki_dir) if self.export_to_wiki else None  # type: ignore[arg-type]
        )  # type: ignore[arg-type]
        self.use_llm = use_llm

        if register_default_consumers:
            self._setup_default_consumers(persona_store=persona_store, kia_store=kia_store)

    def _setup_default_consumers(self, persona_store=None, kia_store=None):
        """注册默认的 Layer 5 外循环消费者（异常安全）"""
        try:
            composite = CompositeConsumer(
                [
                    PersonaSignalConsumer(persona_store=persona_store),
                    KIAExperienceConsumer(kia_store=kia_store),
                    ReflectionPolicyPatchConsumer(),
                    HephaestusCalibrationConsumer(),
                ]
            )
            self.register_consumer(composite)
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.warning("默认 Layer 5 消费者注册失败", exc_info=True)

    def _export_reflection_projection(
        self,
        record: ReflectionRecord,
        feedback_result: Optional[Any] = None,
    ):
        """将本次 Reflection 增量投影到 Obsidian Vault，并触发 L4→L2 知识更新写入。"""
        if not self._reflection_exporter:
            return
        canonical_record = self.ref_store.get_by_id(record.id)
        if canonical_record is None:
            raise RuntimeError(
                f"Reflection projection source is not committed: {record.id}"
            )
        self._reflection_exporter.export_record(canonical_record)
        shifts = self.ref_store.get_all_shifts_for_projection()
        records = self.ref_store.get_all_for_projection()
        self._reflection_exporter.export_shifts(shifts)
        week_start = self._reflection_exporter.week_start(record.created_at)
        week_end = week_start + timedelta(days=7)
        week_records = [
            candidate
            for candidate in records
            if week_start <= candidate.created_at < week_end
        ]
        self._reflection_exporter.export_weekly_report(
            week_records,
            shifts=self._reflection_exporter.shifts_for_week(shifts, week_start),
            week_start=week_start,
        )

        # P110 pages are already recorded and published by the typed lifecycle.
        if feedback_result and getattr(feedback_result, "knowledge_updates", None):
            self._reflection_exporter.export_knowledge_updates(
                canonical_record,
                feedback_result.knowledge_updates,
            )

    def reflect_on_user_input(
        self,
        text: str,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        source_access_control: Mapping[str, Any] | None = None,
    ) -> ReflectionResult:
        """
        基于用户输入自动检测触发并生成 Reflection

        Args:
            text: 用户输入文本

        Returns:
            ReflectionResult（如果未触发，triggered=False）
        """
        # 1. 获取时间上下文（用于节律触发检测）
        temporal = self.time_awareness.get_temporal_context(
            principal=principal,
            narrowing=narrowing,
        )

        # 2. 构建触发检测上下文
        trigger_context = TriggerContext(
            user_text=text,
            current_rhythm=temporal.rhythm,
        )

        # 3. 检测触发（Observation 突变 + 时间节律 + 关键词兜底）
        trigger_event = self.trigger_detector.detect(trigger_context)
        if not trigger_event:
            return ReflectionResult(triggered=False)

        # 4. 获取校准参数（Layer 5 闭环: 基于历史反馈调整生成策略）
        cal_params = self.calibrator.get_calibration_params()

        # 5. 构建 Mirror（融入校准维度权重 + 跳过低质量维度）
        scene = trigger_event.trigger.value
        mirror = self.mirror_engine.build_mirror(
            trigger_scene=scene,
            user_query=text,
            dimension_weights=cal_params.dimension_weights,
            skip_dimensions=cal_params.skip_dimensions,
            principal=principal,
            narrowing=narrowing,
        )

        # 6. 召回历史相似经验并生成 Insight
        experiences = self._find_similar_experiences(
            query=text,
            scene=trigger_event.trigger.value,
            dimensions=mirror.dimensions_involved,
            principal=principal,
            narrowing=narrowing,
        )
        insight = self.insight_generator.generate(
            mirror=mirror,
            temporal=temporal,
            user_query=text,
            calibration_hints=cal_params.generation_hints,
            min_confidence=cal_params.confidence_threshold,
            experiences=experiences,
        )

        # 7. 事后校准（涉及低质量维度的 Insight 降权）
        insight = self.calibrator.apply_to_insight_result(insight)

        # 7.5 内部一致性校验（零用户负担的系统自检）
        validation = self.internal_validator.validate(mirror, insight)

        # 8. 构建 ReflectionRecord
        record = ReflectionRecord(
            trigger=trigger_event.trigger,
            trigger_event=trigger_event.raw_text[:200],
            user_query=text[:500],
            mirror_snapshots=mirror.snapshots,
            mirror_dimensions=mirror.dimensions_involved,
            insight=insight.to_snapshot(),
            temporal_context=_serialize_temporal_context(temporal),
            internal_validation={
                "overall_score": validation.overall_score,
                "passed": validation.passed,
                "findings": [
                    {
                        "check": f.check_name,
                        "status": f.status,
                        "score": f.score,
                        "message": f.message,
                    }
                    for f in validation.findings
                ],
                "feedback_equivalent": validation.to_feedback_equivalent(),
            },
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

        # 9. 保存记录
        self.ref_store.save_record(record)

        # 10. 执行 Feedback Loop（反哺 Layer 3/Layer 2）
        feedback_result = self.feedback_loop.process_reflection(
            record,
            principal=principal,
            narrowing=narrowing,
        )

        # 10.25 通知 Layer 5 外循环消费者（反哺 Persona/KIA/Hephaestus）
        self._emit_consumer_events(record, feedback_result)

        # 10.5 增量投影到 L4-Reflections/
        self._export_reflection_projection(record, feedback_result)

        # 11. 添加内部校验消息
        if not validation.passed:
            feedback_result.messages.append(
                f"[系统自检] Insight 内部一致性得分 {validation.overall_score:.2f}，"
                f"未通过校验。建议谨慎参考。"
            )

        return ReflectionResult(
            triggered=True,
            trigger_event=trigger_event,
            mirror=mirror,
            insight=insight,
            record=record,
            feedback_messages=feedback_result.messages,
        )

    # ───────────────────────────────
    # 偏差检测触发模式（用户设计）
    # ───────────────────────────────

    def start_listening(
        self,
        session_id: str,
        trigger_text: str,
        trigger_scene: Optional[str] = None,
    ) -> Optional[ListeningSession]:
        """
        启动偏差检测监听会话

        用户说"我想策划一场活动"时，宿主 Agent 调用此方法。
        系统进入静默监听模式，预加载 Mirror，等待用户后续输入。

        Args:
            session_id: 宿主会话唯一标识
            trigger_text: 触发文本（如"我想策划一场活动"）
            trigger_scene: 触发场景标签（可选）

        Returns:
            ListeningSession if 成功启动，None if 已存在活跃会话
        """
        # 检查是否已有活跃会话
        existing = self.deviation_detector.get_session(session_id)
        if existing and not existing.is_expired:
            return existing

        # 检测触发（确认是有效触发）
        temporal = self.time_awareness.get_temporal_context()
        trigger_context = TriggerContext(
            user_text=trigger_text,
            current_rhythm=temporal.rhythm,
        )
        trigger_event = self.trigger_detector.detect(trigger_context)
        if not trigger_event:
            return None

        # 预加载 Mirror（用于后续偏差比对）
        cal_params = self.calibrator.get_calibration_params()
        scene = trigger_scene or trigger_event.trigger.value
        mirror = self.mirror_engine.build_mirror(
            trigger_scene=scene,
            user_query=trigger_text,
            dimension_weights=cal_params.dimension_weights,
            skip_dimensions=cal_params.skip_dimensions,
        )

        # 启动监听会话
        session = self.deviation_detector.start_listening(
            session_id=session_id,
            trigger_scene=scene,
            mirror=mirror,
        )
        return session

    def process_message_in_session(
        self,
        session_id: str,
        message: str,
    ) -> ReflectionResult:
        """
        处理监听会话中的用户消息，检测偏差

        用户在"我想策划一场活动"之后继续说：
        - "预计2周完成" → 系统检测是否有数值偏差
        - "我很少加班" → 系统检测是否有频率偏差
        - 多条消息后仍未考虑时间风险 → 系统检测是否有忽略偏差

        Args:
            session_id: 会话 ID
            message: 用户新输入的消息

        Returns:
            ReflectionResult
            - triggered=True + insight: 检测到偏差，已生成 Mirror + Insight
            - triggered=False + listening=True: 无偏差，继续监听
            - triggered=False: 监听会话不存在或已过期
        """
        # 检查会话是否存在
        session = self.deviation_detector.get_session(session_id)
        if not session:
            return ReflectionResult(triggered=False)

        # 添加消息并检测偏差
        signal = self.deviation_detector.add_user_message(session_id, message)

        if signal:
            # 🎯 偏差检测到！生成 Mirror + Insight
            return self._generate_insight_from_deviation(signal, session)

        # 无偏差，继续监听
        result = ReflectionResult(triggered=False)
        result.listening = True
        return result

    def _generate_insight_from_deviation(
        self,
        signal: DeviationSignal,
        session: ListeningSession,
    ) -> ReflectionResult:
        """
        基于检测到的偏差生成 Mirror + Insight

        这是用户设计的核心：只在有偏差时才展示 Insight。
        """
        # 1. 获取时间上下文
        temporal = self.time_awareness.get_temporal_context()

        # 2. 获取校准参数
        cal_params = self.calibrator.get_calibration_params()

        # 3. Mirror 已经预加载在 session 中，复用
        mirror = session.mirror

        # 4. 生成 Insight（注入偏差信号作为额外上下文）
        deviation_hint = (
            f"检测到偏差: {signal.suggestion}"
            f"（严重程度: {signal.severity}，类型: {signal.deviation_type}）"
        )
        # generation_hints 是字符串（来自 InsightCalibrator），需与偏差提示合并后传入
        base_hints = cal_params.generation_hints
        if isinstance(base_hints, list):
            base_hints = "\n".join(str(h) for h in base_hints)
        if base_hints:
            calibration_hints = f"{base_hints}\n\n额外偏差信号:\n{deviation_hint}"
        else:
            calibration_hints = f"偏差信号:\n{deviation_hint}"

        query_text = " ".join(session.user_messages)
        experiences = self._find_similar_experiences(
            query=query_text,
            scene=signal.deviation_type,
            dimensions=mirror.dimensions_involved,  # type: ignore[union-attr]
        )
        insight = self.insight_generator.generate(
            mirror=mirror,  # type: ignore[arg-type]
            temporal=temporal,
            user_query=query_text,
            calibration_hints=calibration_hints,
            min_confidence=cal_params.confidence_threshold,
            experiences=experiences,
        )

        # 5. 事后校准
        insight = self.calibrator.apply_to_insight_result(insight)

        # 6. 内部校验
        validation = self.internal_validator.validate(mirror, insight)  # type: ignore[arg-type]

        # 7. 构建 ReflectionRecord
        record = ReflectionRecord(
            trigger=ReflectionTrigger.MANUAL,  # 偏差检测触发标记为手动类型
            trigger_event=f"偏差检测: {signal.deviation_type}",
            user_query=" ".join(session.user_messages)[:500],
            mirror_snapshots=mirror.snapshots,  # type: ignore[union-attr]
            mirror_dimensions=mirror.dimensions_involved,  # type: ignore[union-attr]
            insight=insight.to_snapshot(),
            temporal_context=_serialize_temporal_context(temporal),
            internal_validation={
                "overall_score": validation.overall_score,
                "passed": validation.passed,
                "findings": [
                    {
                        "check": f.check_name,
                        "status": f.status,
                        "score": f.score,
                        "message": f.message,
                    }
                    for f in validation.findings
                ],
                "feedback_equivalent": validation.to_feedback_equivalent(),
            },
        )

        # 8. 保存记录
        self.ref_store.save_record(record)

        # 9. This historical listening path has no server principal or source ACL.
        # Keep its historical comparison fail-closed until callers provide the
        # same authenticated scope carried by the direct reflection entrypoint.
        feedback_result = self.feedback_loop.process_reflection(
            record,
            principal=None,
            narrowing=None,
        )

        # 10. 通知 Layer 5 外循环消费者
        self._emit_consumer_events(record, feedback_result)

        # 10.5 增量投影到 L4-Reflections/
        self._export_reflection_projection(record, feedback_result)

        # 11. 添加偏差检测消息
        feedback_result.messages.insert(0, f"💡 {signal.suggestion}")

        # 12. 添加内部校验消息
        if not validation.passed:
            feedback_result.messages.append(
                f"[系统自检] Insight 内部一致性得分 {validation.overall_score:.2f}，"
                f"未通过校验。建议谨慎参考。"
            )

        # 13. 关闭监听会话
        self.deviation_detector.close_session(session.id)  # type: ignore[attr-defined]

        return ReflectionResult(
            triggered=True,
            mirror=mirror,
            insight=insight,
            record=record,
            feedback_messages=feedback_result.messages,
        )

    # ───────────────────────────────
    # Layer 5 外循环消费者注册
    # ───────────────────────────────

    def register_consumer(self, consumer):
        """
        注册 Layer 5 外循环消费者

        消费者接口：
            consumer.on_insight_generated(record: ReflectionRecord)
            consumer.on_feedback_collected(record: ReflectionRecord)
            consumer.on_cognitive_shift(shift: CognitiveShift)

        Args:
            consumer: 实现了上述方法的对象
        """
        self._consumers.append(consumer)

    def _notify_consumers(self, event_type: str, data):
        """通知所有消费者"""
        for consumer in self._consumers:
            try:
                if event_type == "insight_generated" and hasattr(consumer, "on_insight_generated"):
                    consumer.on_insight_generated(data)
                elif event_type == "feedback_collected" and hasattr(
                    consumer, "on_feedback_collected"
                ):
                    consumer.on_feedback_collected(data)
                elif event_type == "cognitive_shift" and hasattr(consumer, "on_cognitive_shift"):
                    consumer.on_cognitive_shift(data)
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                # 消费者异常不应影响主流程，但必须记录以便排查
                logger.warning(
                    "[ReflectionEngine] Layer 5 消费者 %s.%s 处理失败",
                    getattr(consumer, "__class__", type(consumer)).__name__,
                    event_type,
                    exc_info=True,
                )

    def _emit_consumer_events(self, record: ReflectionRecord, feedback_result):
        """将 Reflection 结果通知 Layer 5 消费者并触发刷新"""
        if not self._consumers:
            return
        self._notify_consumers("insight_generated", record)
        for shift in getattr(feedback_result, "shifts_detected", []) or []:
            self._notify_consumers("cognitive_shift", shift)
        self._flush_consumers()

    def _flush_consumers(self):
        """刷新所有带 flush 方法的消费者，确保缓冲数据落盘"""
        for consumer in self._consumers:
            try:
                flush = getattr(consumer, "flush", None)
                if flush:
                    flush()
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                logger.debug("消费者 flush 失败", exc_info=True)

    def _find_similar_experiences(
        self,
        query: str,
        scene: Optional[str] = None,
        role: Optional[str] = None,
        dimensions: Optional[List[str]] = None,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> List[ExperienceMatch]:
        """召回与当前情境相似的历史经验（失败时返回空列表）"""
        if not self.experience_matcher:
            return []
        try:
            return self.experience_matcher.find_similar(
                query=query,
                scene=scene,
                role=role,
                dimensions=dimensions,
                top_k=3,
                principal=principal,
                narrowing=narrowing,
            )
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.debug("历史经验匹配失败", exc_info=True)
            return []

    def reflect_manually(
        self,
        query: str,
        trigger: ReflectionTrigger = ReflectionTrigger.MANUAL,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        source_access_control: Mapping[str, Any] | None = None,
    ) -> ReflectionResult:
        """
        手动触发 Reflection

        内部使用 ReflectionRouter + ReflectionCapability，保持与新版冻结架构兼容。
        Evidence Graph 非空时，会自动将本次 Reflection 链接为证据节点。

        Args:
            query: 用户的分析请求
            trigger: 触发类型（默认 MANUAL）

        Returns:
            ReflectionResult
        """
        from core.reflection.reflection_capability import ReflectionCapability

        # 用 Router 推断场景和角色；若未识别出场景，则退化为 trigger 值
        route = self.reflection_router.route(query)
        scene = route.scene if route.scene != "default" else trigger.value
        role = route.role.value

        capability = ReflectionCapability(
            observation_store=self.obs_store,
            reflection_store=self.ref_store,
            experience_matcher=self.experience_matcher,
            use_llm=self.use_llm,
        )
        result = capability.reflect(
            scene=scene,
            query=query,
            role=role,
            principal=principal,
            narrowing=narrowing,
            source_access_control=source_access_control,
        )

        # 优先使用 capability 生成的 insight，否则留空由宿主 Agent 填充
        insight_result = result.insight_result
        record = capability.store(
            result,
            insight_summary=insight_result.summary if insight_result else "",
            insight_key_points=insight_result.key_points if insight_result else [],
            evidence_graph=self.evidence_graph,
        )

        # 反哺 Layer 3/Layer 2
        feedback_result = self.feedback_loop.process_reflection(
            record,
            principal=principal,
            narrowing=narrowing,
        )

        # 通知 Layer 5 外循环消费者（反哺 Persona/KIA/Hephaestus）
        self._emit_consumer_events(record, feedback_result)

        # 增量投影到 L4-Reflections/
        self._export_reflection_projection(record, feedback_result)

        return ReflectionResult(
            triggered=True,
            mirror=result.mirror,
            insight=result.insight_result,
            record=record,
            feedback_messages=feedback_result.messages,
        )

    def submit_feedback(
        self,
        reflection_id: str,
        feedback_type: FeedbackType,
        comment: str = "",
    ) -> UserFbResult:
        """Reject the retired direct writer; use the application feedback owner."""

        del reflection_id, feedback_type, comment
        raise RuntimeError("legacy_reflection_feedback_write_retired")

    def get_pending_feedback(self, hours_since: float = 24.0, limit: int = 20):
        """获取用户还没反馈的 Insight 列表"""
        return self.feedback_collector.get_pending_feedback(hours_since, limit)

    def get_feedback_history(
        self, limit: int = 50, feedback_type: Optional[FeedbackType] = None
    ) -> List[Dict]:
        """获取用户反馈历史，支持按反馈类型过滤。"""
        return self.feedback_collector.get_feedback_history(limit, feedback_type)

    def get_feedback_summary(self, days: int = REFLECTION_ENGINE_GET_FEEDBACK_SUMMARY_DAYS) -> Dict:
        """获取反馈汇总统计"""
        return self.feedback_collector.get_feedback_summary(days)

    def get_calibration_params(
        self, days: int = REFLECTION_ENGINE_DURATION_BUCKET_MONTH_DAYS, force_refresh: bool = False
    ) -> Dict:
        """获取 Insight 生成校准参数"""
        params = self.calibrator.get_calibration_params(days, force_refresh)
        return params.to_dict()

    def get_insight_quality_report(
        self, days: int = REFLECTION_ENGINE_GET_INSIGHT_QUALITY_REPORT_DAYS
    ) -> Dict:
        """获取 Insight 质量综合报告"""
        from core.reflection.feedback_analytics import FeedbackAnalytics

        analytics = FeedbackAnalytics(self.ref_store)
        return analytics.get_insight_quality_report(days)

    def get_cognitive_trajectory(self, dimension: str) -> Optional[CognitiveTrajectory]:
        """
        获取某个维度的认知轨迹

        Args:
            dimension: 维度名称（如 "growth", "attention"）

        Returns:
            CognitiveTrajectory
        """
        shifts = self.ref_store.get_shifts(dimension=dimension, limit=50)
        if not shifts:
            return None

        trajectory = CognitiveTrajectory(
            dimension=dimension,
            current_state=shifts[-1].to_state if shifts else "",
        )

        for shift in shifts:
            trajectory.add_shift(shift)

        return trajectory

    def submit_session_context(self, context: SessionContext) -> Dict:
        """
        提交会话上下文，自动推断隐式反馈

        由宿主 Agent 在会话结束时调用。系统基于会话结构
        自动推断用户对 Insight 的态度，无需用户手动点击 👍/👎。

        Args:
            context: 会话上下文（包含 Insight 生成后的用户行为数据）

        Returns:
            Dict with inferred_feedback info
        """
        # 1. 推断隐式反馈
        implicit = self.implicit_detector.detect(context)

        if not implicit:
            return {
                "reflection_id": context.reflection_id,
                "inferred": False,
                "reason": "会话结构信号不足，无法推断",
            }

        # 2. 找到对应的 ReflectionRecord（使用主键索引查询）
        target = self.ref_store.get_by_id(context.reflection_id)

        if not target:
            return {
                "reflection_id": context.reflection_id,
                "inferred": False,
                "reason": "Reflection 记录不存在",
            }

        return {
            "reflection_id": context.reflection_id,
            "inferred": True,
            "recorded": False,
            "feedback_type": implicit.inferred_type.value,
            "confidence": implicit.confidence,
            "signals": implicit.signals,
            "reason": "implicit_feedback_is_tool_observation_not_canonical_user_feedback",
            "message": "隐式信号仅作本次只读观察，未写入认知或训练状态",
        }

    def get_stats(self) -> Dict:
        """获取 Reflection 统计"""
        ref_stats = self.ref_store.get_stats()
        temporal = self.time_awareness.get_temporal_context()

        return {
            **ref_stats,
            "temporal_context": {
                "rhythm": temporal.rhythm,
                "last_reflection_ago": temporal.last_reflection_ago,
            },
            "dimension_freshness": temporal.dimension_freshness,
        }
