"""
Layer 5 外循环消费者 — Insight/Feedback/CognitiveShift 的下游消费管道

核心职责：
将 Layer 5 产生的数据（Insight 质量、反馈统计、认知变迁）
反哺到 Mnemos 的其他子系统中，形成完整闭环。

消费事件类型：
1. insight_generated — 新 Insight 生成时
2. feedback_collected — 用户反馈提交时
3. cognitive_shift — 认知变迁检测到时

设计原则：
- 消费者是可选的，ReflectionEngine 不依赖任何消费者存在
- 消费者异常不影响主流程
- 消费者只消费数据，不修改 Layer 5 内部状态
- 消费者实现可以是异步的（批量处理）
"""

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from core.config import get_config
from core.jsonl_rotation import rotate_jsonl
from core.reflection.models import CognitiveShift, ReflectionRecord

logger = logging.getLogger(__name__)


class Layer5Consumer(ABC):
    """Layer 5 外循环消费者基类"""

    @abstractmethod
    def on_insight_generated(self, record: ReflectionRecord):
        """新 Insight 生成时调用"""

    @abstractmethod
    def on_feedback_collected(self, record: ReflectionRecord):
        """用户反馈提交时调用"""

    @abstractmethod
    def on_cognitive_shift(self, shift: CognitiveShift):
        """认知变迁检测到时调用"""


class PersonaSignalConsumer(Layer5Consumer):
    """
    画像信号消费者 — 将 Insight 反馈数据作为用户画像信号

    消费内容：
    - 用户对哪些维度的 Insight 更认可 → 反映用户当前关注领域
    - Insight 准确性趋势 → 反映用户自我认知的清晰度
    - 认知变迁方向 → 反映用户价值观/优先级的变化

    实现：
    - 将信号写入 Persona 信号数据库
    - 触发画像更新（批量/定期）
    """

    def __init__(self, persona_store=None):
        self.persona_store = persona_store
        # 批量缓冲，避免每次事件都写入数据库
        self._signal_buffer: list = []
        self._buffer_size = 10

    def on_insight_generated(self, record: ReflectionRecord):
        """记录 Insight 涉及的维度作为用户关注信号"""
        if not record.mirror_dimensions:
            return

        # 将涉及的维度标记为用户当前关注领域
        for dimension in record.mirror_dimensions:
            self._buffer_signal(
                dimension="reflection_interest",
                value=dimension,
                # type: ignore[attr-defined]
                confidence=record.insight.confidence if record.insight else 0.5,  # type: ignore[attr-defined]  # noqa: E501
                source="layer5_insight",
            )

    def on_feedback_collected(self, record: ReflectionRecord):
        """Do not promote raw Reflection feedback into persona state."""

        del record

    def on_cognitive_shift(self, shift: CognitiveShift):
        """认知变迁作为用户成长信号"""
        self._buffer_signal(
            dimension="cognitive_shift",
            value=f"{shift.dimension}:{shift.shift_type}",
            confidence=shift.confidence,
            source="layer5_shift",
        )

    def flush(self):
        """立即刷新缓冲的信号"""
        self._flush_signals()

    def _buffer_signal(self, dimension: str, value: str, confidence: float, source: str):
        """缓冲信号，满批量后写入"""
        self._signal_buffer.append(
            {
                "dimension": dimension,
                "value": value,
                "confidence": confidence,
                "source": source,
            }
        )

        if len(self._signal_buffer) >= self._buffer_size:
            self._flush_signals()

    def _flush_signals(self):
        """将缓冲的信号写入画像存储；数据库不可用时降级到 JSONL。"""
        if not self._signal_buffer:
            return

        # 如果提供了 Persona 存储，优先写入 user_signals.db
        if self.persona_store is not None:
            try:
                # SignalStore 使用 add_signal 作为 Layer 5 反射信号的入口
                store_method = getattr(self.persona_store, "add_signal", None)
                if store_method:
                    for signal in self._signal_buffer:
                        store_method(**signal)
                    self._signal_buffer.clear()
                    return
                logger.warning(
                    "PersonaSignalConsumer: persona_store 没有 add_signal 方法，将降级到 JSONL"
                )
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
                logger.warning("PersonaSignalConsumer 写入画像存储失败: %s", e, exc_info=True)
        else:
            logger.warning(
                "PersonaSignalConsumer: persona_store 为空， reflection 信号降级到 JSONL"
            )

        # 降级：写入本地 JSONL 文件，避免信号丢失
        try:
            config = get_config()
            fallback_path = Path(config.database_dir) / "reflection_signals.jsonl"
            fallback_path.parent.mkdir(parents=True, exist_ok=True)
            with open(fallback_path, "a", encoding="utf-8") as f:
                for signal in self._signal_buffer:
                    record = {"timestamp": datetime.now().isoformat(), **signal}
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            rotate_jsonl(fallback_path)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as e:
            logger.warning("PersonaSignalConsumer 写入降级文件失败: %s", e, exc_info=True)

        self._signal_buffer.clear()


class KIAExperienceConsumer(Layer5Consumer):
    """
    KIA 经验消费者 — 将认知变迁和高质量 Insight 作为经验积累

    消费内容：
    - 认知变迁 → 积累到 KIA 的 retrospective 经验库
    - 高准确率 Insight → 作为知识注入的参考案例
    - 系统性偏差 → 作为 KIA 守护检查的触发条件

    实现：
    - 将认知变迁写入 KIA 的经验存储
    - 高质量 Reflection 作为 KIA preflight 的上下文素材
    """

    def __init__(self, kia_store=None):
        self.kia_store = kia_store
        self._experience_buffer: list = []
        self._buffer_size = 5

    def on_insight_generated(self, record: ReflectionRecord):
        """高质量 Insight 作为经验素材"""
        # 只收集置信度高的 Insight
        # type: ignore[attr-defined]
        if not record.insight or (record.insight.confidence or 0) < 0.7:  # type: ignore[attr-defined]  # noqa: E501
            return

        # 收集涉及的维度组合作为经验
        experience = {
            "type": "insight_pattern",
            "dimensions": record.mirror_dimensions,
            "trigger": record.trigger.value,
            "confidence": record.insight.confidence,  # type: ignore[attr-defined]
            "summary": record.insight.summary[:200],
        }
        self._buffer_experience(experience)

    def on_feedback_collected(self, record: ReflectionRecord):
        """Do not promote raw Reflection feedback into KIA experience."""

        del record

    def on_cognitive_shift(self, shift: CognitiveShift):
        """认知变迁作为 retrospective 经验"""
        experience = {
            "type": "cognitive_shift",
            "dimension": shift.dimension,
            "from_state": shift.from_state,
            "to_state": shift.to_state,
            "confidence": shift.confidence,
            "evidence": shift.evidence[:3] if shift.evidence else [],
        }
        self._buffer_experience(experience)

    def flush(self):
        """立即刷新缓冲的经验"""
        self._flush_experiences()

    def _buffer_experience(self, experience: Dict):
        self._experience_buffer.append(experience)
        if len(self._experience_buffer) >= self._buffer_size:
            self._flush_experiences()

    def _flush_experiences(self):
        if not self._experience_buffer:
            return

        # 如果提供了 KIA 经验存储，尝试写入
        if self.kia_store is not None:
            try:
                store_method = getattr(self.kia_store, "add_experience", None) or getattr(
                    self.kia_store, "save_experience", None
                )
                if store_method:
                    for exp in self._experience_buffer:
                        store_method(exp)
                    self._experience_buffer.clear()
                    return
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
                logger.warning("KIAExperienceConsumer 写入 KIA 经验库失败: %s", e, exc_info=True)

        # 降级：写入本地 JSONL 文件，避免经验丢失
        try:
            config = get_config()
            fallback_path = Path(config.database_dir) / "layer5_experiences.jsonl"
            fallback_path.parent.mkdir(parents=True, exist_ok=True)
            with open(fallback_path, "a", encoding="utf-8") as f:
                for exp in self._experience_buffer:
                    record = {"timestamp": datetime.now().isoformat(), **exp}
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            rotate_jsonl(fallback_path)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as e:
            logger.warning("KIAExperienceConsumer 写入降级文件失败: %s", e, exc_info=True)

        self._experience_buffer.clear()


class HephaestusCalibrationConsumer(Layer5Consumer):
    """
    Hephaestus 校准消费者 — 用 Insight 质量数据优化蒸馏策略

    消费内容：
    - 高准确率 Insight 的 Mirror 构建模式 → 优化知识蒸馏的维度选择
    - 低准确率 Insight 的常见问题 → 调整蒸馏提示词
    - 认知变迁方向 → 指导知识蒸馏的优先级

    实现：
    - 定期输出 Insight 质量报告到 Hephaestus 配置
    - 维度有效性数据用于指导蒸馏 Worker 的维度权重
    """

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path
        self._quality_stats: Dict[str, Dict] = {}

    def on_insight_generated(self, record: ReflectionRecord):
        """累积 Insight 生成统计"""
        for dim in record.mirror_dimensions:
            if dim not in self._quality_stats:
                self._quality_stats[dim] = {"total": 0, "with_feedback": 0}
            self._quality_stats[dim]["total"] += 1

    def on_feedback_collected(self, record: ReflectionRecord):
        """Do not use raw Reflection feedback to calibrate weights."""

        del record

    def on_cognitive_shift(self, shift: CognitiveShift):
        """认知变迁指导蒸馏优先级"""
        # 标记变迁维度为高优先级，当前仅记录统计
        if shift.dimension:
            if shift.dimension not in self._quality_stats:
                self._quality_stats[shift.dimension] = {"total": 0, "with_feedback": 0}
            self._quality_stats[shift.dimension]["shifts"] = (
                self._quality_stats[shift.dimension].get("shifts", 0) + 1
            )
            logger.debug("HephaestusCalibrationConsumer 记录认知变迁: %s", shift.dimension)

    def flush(self):
        """Keep reflection statistics as telemetry only.

        Reaction-derived Layer5 values are not objective outcomes and therefore
        cannot publish active scorer weights. A future governed optimizer must
        consume current train-split admissions through TrainingGovernanceStore.
        """

        return None

    def get_dimension_weights(self) -> Dict[str, float]:
        """
        获取基于反馈数据校准的维度权重

        高准确率维度 → 更高权重
        低准确率维度 → 降低权重
        """
        weights = {}
        for dim, stats in self._quality_stats.items():
            total_fb = stats.get("with_feedback", 0)
            if total_fb < 3:
                weights[dim] = 1.0  # 默认权重
                continue

            positive = stats.get("positive", 0)
            accuracy = positive / total_fb if total_fb > 0 else 0.5

            # 准确率映射到权重：0.5→0.5, 0.8→1.2, 1.0→1.5
            weight = 0.5 + accuracy * 1.0
            weights[dim] = round(min(1.5, max(0.3, weight)), 2)

        return weights


class ReflectionPolicyPatchConsumer(Layer5Consumer):
    """
    Policy patch consumer — 将高置信 Reflection 资产转成可注入策略补丁。

    不满足补丁条件时写入 policy_patch_feedback 的 no_patch 证据，避免
    policy_patches 长期为 0 时没有“本周期为何不生成”的审计线索。
    """

    def __init__(self, policy_store=None, min_confidence: float = 0.82):
        self.policy_store = policy_store
        self.min_confidence = min_confidence

    def on_insight_generated(self, record: ReflectionRecord):
        if not record.insight:
            self._record_no_patch("missing_insight", record_id=record.id)
            return
        confidence = self._record_confidence(record)
        if confidence < self.min_confidence:
            self._record_no_patch(
                "confidence_below_policy_threshold",
                record_id=record.id,
                confidence=confidence,
            )
            return
        trigger_keywords = self._record_trigger_keywords(record)
        if not trigger_keywords:
            self._record_no_patch("missing_trigger_keywords", record_id=record.id)
            return

        lesson = {
            "source_type": "reflection",
            "source_id": record.id,
            "task_type": "general",
            "subtype": "general",
            "scope": "global",
            "severity": "medium" if confidence < 0.9 else "high",
            "summary": self._insight_summary(record),
            "trigger_keywords": trigger_keywords,
            "confidence": confidence,
            "evidence_refs": self._record_evidence_refs(record),
            "metadata": {
                "reflection_trigger": record.trigger.value,
                "mirror_dimensions": list(record.mirror_dimensions),
                "key_points": list(record.insight.key_points or []),
            },
        }
        patch = self._propose_exact_patch(
            lesson,
            source_facts={"reflection": record.to_dict()},
            created_at=record.created_at.astimezone().isoformat(),
        )
        if patch is None:
            self._record_no_patch(
                "policy_store_rejected_insight",
                record_id=record.id,
                confidence=confidence,
            )

    def on_feedback_collected(self, record: ReflectionRecord):
        """Require a canonical policy_proposal command for feedback effects."""

        del record

    def on_cognitive_shift(self, shift: CognitiveShift):
        confidence = float(shift.confidence or 0.0)
        if confidence < self.min_confidence:
            self._record_no_patch(
                "shift_confidence_below_policy_threshold",
                record_id=self._shift_id(shift),
                confidence=confidence,
            )
            return
        trigger_keywords = [
            item
            for item in (shift.dimension, shift.shift_type, shift.to_state)
            if str(item or "").strip()
        ]
        if not trigger_keywords:
            self._record_no_patch("shift_missing_trigger_keywords", record_id=self._shift_id(shift))
            return
        lesson = {
            "source_type": "reflection_shift",
            "source_id": self._shift_id(shift),
            "task_type": "general",
            "subtype": "general",
            "scope": "global",
            "severity": "medium" if confidence < 0.9 else "high",
            "summary": (
                f"当 {shift.dimension} 从 {shift.from_state} 转向 {shift.to_state} 时，"
                "在相关任务开始前重新核对当前假设是否仍然成立。"
            ),
            "trigger_keywords": trigger_keywords,
            "confidence": confidence,
            "evidence_refs": list(shift.evidence or [])[:5],
            "metadata": {
                "shift_type": shift.shift_type,
                "from_state": shift.from_state,
                "to_state": shift.to_state,
            },
        }
        patch = self._propose_exact_patch(
            lesson,
            source_facts={"cognitive_shift": shift.to_dict()},
            created_at=shift.shift_detected_at.astimezone().isoformat(),
        )
        if patch is None:
            self._record_no_patch(
                "policy_store_rejected_shift",
                record_id=self._shift_id(shift),
                confidence=confidence,
            )

    def _store(self):
        if self.policy_store is None:
            from core.cognitive.policy_patch import PolicyPatchStore

            self.policy_store = PolicyPatchStore()
        return self.policy_store

    def _propose_exact_patch(
        self,
        lesson: Dict[str, Any],
        *,
        source_facts: Dict[str, Any],
        created_at: str,
    ):
        from core.cognitive.decision_trace import MaterialActionRequest
        from core.cognitive.policy_patch import (
            POLICY_PATCH_EXECUTOR,
            POLICY_PATCH_OWNER,
            POLICY_PATCH_PROPOSE_ACTION,
            authorize_exact_policy_patch_action,
            policy_patch_proposal_binding,
        )

        store = self._store()
        binding = policy_patch_proposal_binding(lesson, store.options)
        if binding is None:
            return None
        state_db_path = (store.options.database_dir / "producer_consumer_ledger.db").resolve(
            strict=False
        )
        request = MaterialActionRequest(
            owner=POLICY_PATCH_OWNER,
            executor_id=POLICY_PATCH_EXECUTOR,
            action_type=POLICY_PATCH_PROPOSE_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=str(state_db_path),
        )
        evidence_refs = tuple(
            dict.fromkeys(
                (
                    *(str(ref) for ref in lesson.get("evidence_refs") or ()),
                    f"policy-source:{lesson['source_type']}:{lesson['source_id']}",
                )
            )
        )
        material_action = authorize_exact_policy_patch_action(
            expected_request=request,
            state_db_path=state_db_path,
            source_namespace="reflection-policy-patch",
            source_facts={
                "schema_version": "mnemos.reflection_policy_patch_facts.v1",
                "lesson": lesson,
                **source_facts,
            },
            evidence_refs=evidence_refs,
            task=f"Create policy patch {binding['target_ref']}",
            goal="Persist only the exact bounded patch accepted by Layer 5.",
            constraints=(
                "Confidence and stable trigger terms must meet current policy.",
                "Reflection evidence, content, scope, and expiry cannot drift.",
            ),
            created_at=created_at,
            producer="reflection-policy-patch-consumer",
            evaluator_id="reflection-policy-patch-evaluator",
            approved_candidate_key="create_exact_reflection_patch",
            approved_candidate_summary=(
                "Create the exact bounded patch from the validated reflection."
            ),
            rejected_candidate_key="retain_policy_without_reflection_patch",
            rejected_candidate_summary=(
                "Retain current policy if confidence, triggers, or evidence drift."
            ),
            committed_metric="reflection_policy_patch_committed",
            rejected_metric="unbound_reflection_policy_patch_count",
        )
        return store.propose(lesson, material_action=material_action)

    def _record_no_patch(self, reason: str, **evidence: Any) -> None:
        try:
            patch_id = f"reflection-no-patch-{evidence.get('record_id', 'unknown')}"
            self._store().record_feedback(
                patch_id,
                outcome="no_patch",
                evidence={"reason": reason, **evidence},
            )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            logger.debug("ReflectionPolicyPatchConsumer no_patch 记录失败", exc_info=True)

    def _insight_summary(self, record: ReflectionRecord) -> str:
        summary = str(record.insight.summary or "").strip() if record.insight else ""
        if not summary:
            summary = "Reflection 发现了需要在后续任务中复核的稳定模式。"
        return summary[:240]

    def _record_trigger_keywords(self, record: ReflectionRecord) -> list[str]:
        values = list(record.mirror_dimensions or [])
        values.append(record.trigger.value)
        if record.insight:
            values.extend(record.insight.dimensions_involved or [])
        return [str(item).strip() for item in values if str(item or "").strip()]

    def _record_evidence_refs(self, record: ReflectionRecord) -> list[str]:
        refs = [f"reflection:{record.id}", f"trigger:{record.trigger.value}"]
        for snapshot in record.mirror_snapshots[:5]:
            if snapshot.observation_id:
                refs.append(f"observation:{snapshot.observation_id}")
        return refs

    def _record_confidence(self, record: ReflectionRecord) -> float:
        raw = getattr(record.insight, "confidence", None)
        if raw is None and isinstance(record.internal_validation, dict):
            raw = record.internal_validation.get("overall_score")
        if raw is None and record.insight and record.insight.key_points:
            raw = 0.85
        try:
            return float(raw or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _shift_id(self, shift: CognitiveShift) -> str:
        return (
            f"{shift.dimension}:{shift.shift_type}:"
            f"{shift.shift_detected_at.isoformat(timespec='seconds')}"
        )


class CompositeConsumer(Layer5Consumer):
    """组合消费者 — 将多个消费者组合在一起"""

    def __init__(self, consumers: list | None = None):
        self.consumers = consumers or []

    def add(self, consumer: Layer5Consumer):
        self.consumers.append(consumer)

    def flush(self):
        """刷新所有子消费者"""
        for consumer in self.consumers:
            try:
                flush = getattr(consumer, "flush", None)
                if flush:
                    flush()
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                ArithmeticError,
                RuntimeError,
            ):
                logger.debug("CompositeConsumer 子消费者 flush 失败", exc_info=True)

    def on_insight_generated(self, record: ReflectionRecord):
        for consumer in self.consumers:
            try:
                consumer.on_insight_generated(record)
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                ArithmeticError,
                RuntimeError,
            ):
                logger.debug("消费者 insight 处理失败", exc_info=True)

    def on_feedback_collected(self, record: ReflectionRecord):
        for consumer in self.consumers:
            try:
                consumer.on_feedback_collected(record)
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                ArithmeticError,
                RuntimeError,
            ):
                logger.debug("消费者 feedback 处理失败", exc_info=True)

    def on_cognitive_shift(self, shift: CognitiveShift):
        for consumer in self.consumers:
            try:
                consumer.on_cognitive_shift(shift)
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                ArithmeticError,
                RuntimeError,
            ):
                logger.debug("消费者 cognitive shift 处理失败", exc_info=True)
