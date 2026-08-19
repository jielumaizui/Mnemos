"""
Persona Store - 画像存储与wiki frontmatter反写

职责：
- 将画像数据持久化到wiki（方案A：frontmatter）
- 全量扫描wiki，计算知识-画像匹配度
- 反写匹配度字段到知识条目
- 版本控制（保留历史，标注迭代）

核心设计：
- 画像即知识：用户画像存储为wiki页面
- 每条知识自带匹配度字段
- 老字段保留，标注superseded
"""

# Delphi — 德尔斐神庙 — 画像存储，神谕/画像的持久化
# 原模块: persona_store.py


import re
import json
import copy
import yaml
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Any, Sequence, Tuple
from dataclasses import asdict
from datetime import datetime

from .psyche import (
    SignalStore,
    authorize_exact_persona_material_action,
    get_signal_store,
)
from .pythia import PreferenceProfile
from .hamartia import BlindSpotProfile
from .delphi_behavior_rules import (
    _COGNITIVE_RULES,
    _ENERGY_RULES,
    _VALUE_RULES,
    _append_dimension_lines,
)
from .projection_runtime import (
    PERSONA_HISTORY_RELATIVE,
    PERSONA_PAGE_RELATIVE,
    PersonaProjectionMixin,
)
from core.utils import LazyPath
from core.config import get_config
from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionCoordinator,
    MaterialActionRequest,
    authorize_exact_project_contract_action,
    resolve_material_action_authorization,
)
from core.cognitive.state_contract import sha256_json
from core.cognitive.state_store import CognitiveStateStore
from core.wiki_derived_projection import DerivedProjectionLifecycle
from core.wiki_page_roles import classify_wiki_page_role
import logging

# ========== 配置 ==========

logger = logging.getLogger(__name__)


WIKI_DIR = LazyPath("wiki_dir")
PERSONA_PAGE_PATH = LazyPath("wiki_dir", *PERSONA_PAGE_RELATIVE.parts)
PERSONA_HISTORY_DIR = LazyPath("wiki_dir", *PERSONA_HISTORY_RELATIVE.parts)
PERSONA_MARKDOWN_DECISION_CONTRACT_ID = (
    "project-contract:persona-markdown-material-actions"
)
PERSONA_MARKDOWN_DECISION_CONTRACT_REVISION = (
    "mnemos.persona_markdown_material_actions.v1"
)
PERSONA_MARKDOWN_DECISION_CONTRACT_TEXT = (
    "PersonaStore may write only the exact current or history Markdown bytes "
    "prepared for the bound Persona target and verified prior state."
)
PERSONA_MARKDOWN_DECISION_PRODUCER_HASH = sha256_json(
    {
        "module": "core.persona.delphi",
        "producer": "persona-store",
        "version": PERSONA_MARKDOWN_DECISION_CONTRACT_REVISION,
    }
)


# ========== 知识匹配度计算 ==========


class KnowledgeAligner:
    """
    计算知识与用户画像的匹配度。

    三维匹配：
    1. preference_match: 偏好匹配（用户喜欢这种呈现方式吗）
    2. capability_match: 能力匹配（这条知识在用户的学习区吗）
    3. context_match: 情境匹配（当前session需要这条知识吗）
    """

    # 知识类型-偏好兼容性矩阵
    TYPE_PREFERENCE_MATRIX = {
        "decision": {
            "feasibility_first": 1.0,
            "cost_first": 0.3,
            "risk_averse": 0.7,
            "risk_seeking": 0.8,
        },
        "snippet": {
            "code_first": 1.0,
            "explanation_first": 0.2,
        },
        "pattern": {
            "methodology_oriented": 1.0,
            "problem_oriented": 0.6,
        },
        "pitfall": {
            "risk_averse": 1.0,
            "risk_seeking": 0.5,
        },
        "reference": {
            "depth_first": 0.8,
            "breadth_first": 1.0,
        },
        "todo": {
            "action_oriented": 1.0,
            "plan_oriented": 0.7,
        },
    }

    def __init__(self, persona: PreferenceProfile):
        self.persona = persona

    def calculate_alignment(
        self, wiki_page: Dict, session_context: Dict | None = None
    ) -> Dict[str, float]:
        """
        计算单条知识与画像的三维匹配度。

        Args:
            wiki_page: 知识页面数据 {path, frontmatter, content_snippet}
            session_context: 当前session上下文

        Returns:
            {preference_match, capability_match, context_match, total}
        """
        alignment = {
            "preference_match": 0.5,
            "capability_match": 0.5,
            "context_match": 0.5,
            "total": 0.5,
        }

        frontmatter = wiki_page.get("frontmatter", {})
        page_type = frontmatter.get("type", "unknown")

        # 1. 偏好匹配
        alignment["preference_match"] = self._calc_preference_match(page_type, frontmatter)

        # 2. 能力匹配（i+1学习区理论）
        alignment["capability_match"] = self._calc_capability_match(frontmatter)

        # 3. 情境匹配
        if session_context:
            alignment["context_match"] = self._calc_context_match(wiki_page, session_context)

        # 综合（权重可调）
        weights = self._get_alignment_weights()
        alignment["total"] = (
            alignment["preference_match"] * weights["preference"]
            + alignment["capability_match"] * weights["capability"]
            + alignment["context_match"] * weights["context"]
        )

        return alignment

    def _derive_preference_tags(self) -> List[str]:
        """从画像价值雷达中推导出用户偏好标签集合。"""
        value = self.persona.value
        tags = []

        if value.correctness_vs_efficiency > 0.6:
            tags.append("feasibility_first")
        elif value.correctness_vs_efficiency < 0.4:
            tags.append("cost_first")

        if value.innovation_vs_safety > 0.6:
            tags.append("risk_seeking")
        elif value.innovation_vs_safety < 0.4:
            tags.append("risk_averse")

        if value.depth_vs_breadth > 0.6:
            tags.append("depth_first")
        elif value.depth_vs_breadth < 0.4:
            tags.append("breadth_first")

        if value.perfection_vs_completion > 0.6:
            tags.append("perfection_oriented")
        elif value.perfection_vs_completion < 0.4:
            tags.append("completion_oriented")

        if value.autonomy_vs_collaboration > 0.6:
            tags.append("autonomous")
        elif value.autonomy_vs_collaboration < 0.4:
            tags.append("collaborative")

        if value.action_vs_analysis > 0.6:
            tags.append("action_oriented")
        elif value.action_vs_analysis < 0.4:
            tags.append("analysis_oriented")

        return tags

    def _static_preference_score(self, page_type: str, tags: List[str]) -> float:
        """基于静态类型-偏好矩阵计算匹配度。

        只考虑与当前页面类型矩阵相关的偏好标签，避免无关标签稀释信号。
        """
        matrix = self.TYPE_PREFERENCE_MATRIX.get(page_type, {})
        if not matrix or not tags:
            return 0.5
        relevant = [tag for tag in tags if tag in matrix]
        if not relevant:
            return 0.5
        scores = [matrix[tag] for tag in relevant]
        return sum(scores) / len(scores)

    def _dynamic_preference_score(
        self, page_type: str, frontmatter: Dict, tags: List[str]
    ) -> float:
        """基于页面标签/偏好标签与用户画像标签的动态重叠计算匹配度。"""
        if not tags:
            return 0.5

        explicit_tags = set(frontmatter.get("tags", [])) | set(
            frontmatter.get("preference_tags", [])
        )
        page_tokens = explicit_tags | {page_type}
        page_tokens.discard("")

        matches = page_tokens & set(tags)
        if not matches:
            return 0.5
        return 0.3 + 0.7 * (len(matches) / len(tags))

    def _calc_preference_match(self, page_type: str, frontmatter: Dict) -> float:
        """计算偏好匹配度：静态矩阵 + 动态标签重叠。"""
        tags = self._derive_preference_tags()
        static_score = self._static_preference_score(page_type, tags)

        # 仅当页面存在显式偏好线索（tags / preference_tags）时才引入动态匹配，
        # 避免纯类型信息冲淡静态矩阵的已有信号。
        explicit_tags = set(frontmatter.get("tags", [])) | set(
            frontmatter.get("preference_tags", [])
        )
        explicit_tags.discard("")
        if explicit_tags:
            dynamic_score = self._dynamic_preference_score(page_type, frontmatter, tags)
            return 0.5 * static_score + 0.5 * dynamic_score

        return static_score

    def _calc_capability_match(self, frontmatter: Dict) -> float:
        """
        计算能力匹配度（i+1学习区理论）。

        - 用户已远超知识 → boredom（无聊区）→ 低分
        - 刚好在用户能力边缘 → sweet spot（学习区）→ 高分
        - 有点难但可触及 → stretch zone（拉伸区）→ 中高分
        - 太难 → panic zone（恐慌区）→ 低分
        """
        # 简化版：基于知识level和画像的复杂度推断
        level = frontmatter.get("level", "L2")
        try:
            level_num = int(re.search(r"L(\d+)", str(level)).group(1))  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            level_num = 2

        # 推断用户能力等级（从能量和认知雷达）
        user_level = self._estimate_user_level()

        gap = level_num - user_level

        if gap < -1:  # 知识太简单
            return 0.3
        elif gap == -1:  # 略简单，可复习
            return 0.6
        elif gap == 0:  # 学习区 sweet spot
            return 1.0
        elif gap == 1:  # 拉伸区
            return 0.7
        elif gap == 2:  # 有挑战
            return 0.4
        else:  # 太难了
            return 0.1

    def _calc_context_match(self, wiki_page: Dict, session_context: Dict) -> float:
        """计算情境匹配度"""
        score = 0.5

        frontmatter = wiki_page.get("frontmatter", {})
        page_tags = frontmatter.get("tags", [])

        # 任务类型匹配
        session_task = session_context.get("task_type", "")
        if session_task:
            task_parts = session_task.split("/")
            if any(tag in page_tags for tag in task_parts):
                score += 0.3

        # 工作目录匹配
        session_dir = session_context.get("working_dir", "")
        page_path = wiki_page.get("path", "")
        if session_dir and page_path:
            # 简单字符串匹配
            if any(part in page_path for part in session_dir.split("/")):
                score += 0.2

        # 最近查询历史匹配
        recent_queries = session_context.get("recent_queries", [])
        if recent_queries:
            if any(q in page_tags or q in page_path for q in recent_queries):
                score += 0.2

        return min(1.0, score)

    def _estimate_user_level(self) -> int:
        """估算用户能力等级（1-9）"""
        # 简化：基于能量雷达的专注深度和认知雷达的抽象能力
        energy_score = self.persona.energy.focus_depth
        cognitive_score = self.persona.cognitive.abstraction

        # 综合估算
        avg = (energy_score + cognitive_score) / 2
        return int(1 + avg * 8)  # 映射到1-9

    def _get_alignment_weights(self) -> Dict[str, float]:
        """获取匹配度权重"""
        # 可从画像中读取用户偏好的权重
        return {
            "preference": 0.3,
            "capability": 0.4,
            "context": 0.3,
        }


# ========== PersonaStore 类 ==========


class PersonaStore(PersonaProjectionMixin):
    """画像存储管理器"""

    def __init__(
        self,
        wiki_dir: Path | None = None,
        signal_store: SignalStore | None = None,
        *,
        material_action_resolver: Callable[
            [Mapping[str, str]], MaterialActionAuthorization
        ]
        | None = None,
        projection_lifecycle: DerivedProjectionLifecycle | None = None,
    ):
        self.wiki_dir = Path(wiki_dir) if wiki_dir is not None else Path(WIKI_DIR)
        self.signal_store = signal_store or get_signal_store()
        self._material_action_resolver = material_action_resolver
        if wiki_dir is None and isinstance(WIKI_DIR, LazyPath):
            self.persona_page = Path(PERSONA_PAGE_PATH)
            self.history_dir = Path(PERSONA_HISTORY_DIR)
        else:
            self.persona_page = self.wiki_dir / PERSONA_PAGE_RELATIVE
            self.history_dir = self.wiki_dir / PERSONA_HISTORY_RELATIVE
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.projection_lifecycle = projection_lifecycle or DerivedProjectionLifecycle(
            self.wiki_dir
        )
        self.last_trusted_push: Dict[str, Any] | None = None

    def _resolve_material_action(
        self,
        *,
        action_type: str,
        owner: str,
        executor: str,
        target_ref: str,
        input_hash: str,
        command_ids: Mapping[str, str] | None,
        expected_state_db: Path,
        source_facts: Mapping[str, Any],
        evidence_refs: tuple[str, ...],
        task: str,
        goal: str,
        created_at: str,
        approved_candidate_key: str,
        approved_candidate_summary: str,
        rejected_candidate_key: str,
        rejected_candidate_summary: str,
        committed_metric: str,
        rejected_metric: str,
    ) -> MaterialActionAuthorization:
        request = {
            "action_type": action_type,
            "owner": owner,
            "executor": executor,
            "target_ref": target_ref,
            "input_hash": input_hash,
            "expected_state_db": str(expected_state_db),
        }
        if self._material_action_resolver is not None:
            return self._material_action_resolver(request)
        if isinstance(command_ids, Mapping):
            command_id = str(command_ids.get(target_ref) or "").strip()
            if not command_id:
                raise PermissionError("persona mutation lacks its exact material command")
            return MaterialActionCoordinator(
                CognitiveStateStore(expected_state_db)
            ).bind(
                command_id,
                executor_id=executor,
            )
        try:
            authorization, _ = resolve_material_action_authorization(
                None,
                owner=owner,
                executor_id=executor,
                action_type=action_type,
                target_ref=target_ref,
                input_hash=input_hash,
                expected_state_db=expected_state_db,
            )
            return authorization
        except PermissionError as exc:
            if "canonical material-action authorization is required" not in str(exc):
                raise
        request_spec = MaterialActionRequest(
            owner=owner,
            executor_id=executor,
            action_type=action_type,
            target_ref=target_ref,
            input_hash=input_hash,
            expected_state_db=str(expected_state_db.resolve(strict=False)),
        )
        from core.trust.vault_mutation_service import (
            TRUSTED_MARKDOWN_ACTION_TYPE,
            TRUSTED_MARKDOWN_EXECUTOR,
            TRUSTED_MARKDOWN_OWNER,
        )

        if (owner, executor, action_type) == (
            TRUSTED_MARKDOWN_OWNER,
            TRUSTED_MARKDOWN_EXECUTOR,
            TRUSTED_MARKDOWN_ACTION_TYPE,
        ):
            return authorize_exact_project_contract_action(
                expected_request=request_spec,
                state_db_path=expected_state_db,
                contract_id=PERSONA_MARKDOWN_DECISION_CONTRACT_ID,
                contract_revision_id=PERSONA_MARKDOWN_DECISION_CONTRACT_REVISION,
                contract_text=PERSONA_MARKDOWN_DECISION_CONTRACT_TEXT,
                source_namespace="persona-markdown-material-action",
                source_facts=dict(source_facts),
                decision_checks={
                    "trusted_markdown_family_matches": True,
                    "persona_markdown_source_facts_present": bool(source_facts),
                    "persona_markdown_evidence_present": bool(evidence_refs),
                },
                evidence_refs=evidence_refs,
                task=task,
                goal=goal,
                constraints=(
                    "The exact target, bytes, and prior content hash must remain bound.",
                    "Only the prepared Persona current/history Markdown may commit.",
                ),
                created_at=created_at,
                producer="persona-store",
                producer_version=PERSONA_MARKDOWN_DECISION_CONTRACT_REVISION,
                producer_code_hash=PERSONA_MARKDOWN_DECISION_PRODUCER_HASH,
                evaluator_id="persona-markdown-material-action-evaluator",
                approved_candidate_key=approved_candidate_key,
                approved_candidate_summary=approved_candidate_summary,
                rejected_candidate_key=rejected_candidate_key,
                rejected_candidate_summary=rejected_candidate_summary,
                approved_reason_code="persona_markdown_binding_verified",
                rejected_reason_code="persona_markdown_binding_rejected",
                committed_metric=committed_metric,
                rejected_metric=rejected_metric,
            )
        return authorize_exact_persona_material_action(
            expected_request=request_spec,
            state_db_path=expected_state_db,
            source_namespace="persona-store-material-action",
            source_facts=dict(source_facts),
            evidence_refs=evidence_refs,
            task=task,
            goal=goal,
            constraints=(
                "The exact profile, target, and existing state must remain bound.",
                "Generated profile content and evidence cannot drift before commit.",
            ),
            created_at=created_at,
            producer="persona-store",
            evaluator_id="persona-store-material-action-evaluator",
            approved_candidate_key=approved_candidate_key,
            approved_candidate_summary=approved_candidate_summary,
            rejected_candidate_key=rejected_candidate_key,
            rejected_candidate_summary=rejected_candidate_summary,
            committed_metric=committed_metric,
            rejected_metric=rejected_metric,
        )

    # ---- 画像读写 ----

    def save_persona(
        self,
        profile: PreferenceProfile,
        blindspot: BlindSpotProfile | None = None,
        *,
        material_action_commands: Mapping[str, str] | None = None,
        consume_signal_ids: Mapping[str, Sequence[int]] | None = None,
    ):
        """Commit canonical Persona first, then publish its replayable Wiki projection."""

        if consume_signal_ids is None:
            consume_signal_ids = profile.source_signal_ids

        self._persist_persona_version(
            profile,
            blindspot,
            material_action_commands=material_action_commands,
            consume_signal_ids=consume_signal_ids,
        )
        versions = self.load_canonical_persona_versions_read_only(
            self.signal_store.db_path
        )
        if not versions or versions[0][0].version != profile.version:
            raise RuntimeError(
                f"Committed Persona version is not replayable: {profile.version}"
            )
        self.project_all_personas(versions)

        from core.mnemos_bus import publish_event

        trace_id = publish_event(
            "persona.updated",
            "persona_store",
            {
                "version": profile.version,
                "wiki_path": str(self.persona_page),
                "material_action_commands": dict(material_action_commands or {}),
            },
        )
        if not trace_id:
            raise RuntimeError("persona.updated publisher returned no trace id")

    def _persist_persona_version(
        self,
        profile: PreferenceProfile,
        blindspot: BlindSpotProfile | None,
        *,
        material_action_commands: Mapping[str, str] | None,
        consume_signal_ids: Mapping[str, Sequence[int]] | None,
    ) -> None:
        """Persist the canonical version before any derived Markdown changes."""

        from .psyche import (
            PERSONA_VERSION_ACTION,
            PERSONA_VERSION_EXECUTOR,
            PERSONA_VERSION_OWNER,
            persona_version_material_action_binding,
        )

        persona_binding = persona_version_material_action_binding(
            version=profile.version,
            generated_at=profile.generated_at,
            period_start=profile.period_start,
            period_end=profile.period_end,
            energy=asdict(profile.energy),
            cognitive=asdict(profile.cognitive),
            value=asdict(profile.value),
            blindspot=self._blindspot_to_dict(blindspot) if blindspot else {},
            signal_count=profile.signal_count,
            source_signal_ids=consume_signal_ids,
        )
        persona_action = self._resolve_material_action(
            action_type=PERSONA_VERSION_ACTION,
            owner=PERSONA_VERSION_OWNER,
            executor=PERSONA_VERSION_EXECUTOR,
            target_ref=persona_binding["target_ref"],
            input_hash=persona_binding["input_hash"],
            command_ids=material_action_commands,
            expected_state_db=(
                self.signal_store.db_path.parent / "producer_consumer_ledger.db"
            ),
            source_facts={
                "schema_version": "mnemos.persona_store_version_facts.v1",
                "profile": persona_binding["payload"],
                "wiki_target": str(self.persona_page.resolve(strict=False)),
            },
            evidence_refs=(
                f"persona-version:{profile.version}",
                f"persona-signal-count:{profile.signal_count}",
            ),
            task=f"Persist Persona version {profile.version}",
            goal="Persist the exact profile generated by the Persona workflow.",
            created_at=(
                datetime.fromisoformat(profile.generated_at)
                .astimezone()
                .isoformat()
            ),
            approved_candidate_key="persist_exact_persona_version",
            approved_candidate_summary=(
                "Persist the exact generated Persona version and its source count."
            ),
            rejected_candidate_key="retain_previous_persona_version",
            rejected_candidate_summary=(
                "Retain the previous Persona when generated profile bytes drift."
            ),
            committed_metric="persona_version_committed",
            rejected_metric="unbound_persona_version_count",
        )
        self.signal_store.save_persona_version(
            version=profile.version,
            period_start=profile.period_start,
            period_end=profile.period_end,
            energy=asdict(profile.energy),
            cognitive=asdict(profile.cognitive),
            value=asdict(profile.value),
            blindspot=self._blindspot_to_dict(blindspot) if blindspot else {},
            signal_count=profile.signal_count,
            generated_at=profile.generated_at,
            material_action=persona_action,
            source_signal_ids=consume_signal_ids,
        )

    def load_persona(self) -> Tuple[Optional[PreferenceProfile], Optional[BlindSpotProfile]]:
        """Load only the canonical Persona store; Wiki is never a reverse source."""

        profile, bs = self._load_persona_from_db()
        if profile is not None:
            return profile, bs

        logger.info("[画像] 无历史画像，返回默认冷启动模板")
        return self._create_default_persona(), None

    def _create_default_persona(self) -> PreferenceProfile:
        """创建默认冷启动画像模板

        所有维度中性（0.5），confidence=0.0，标记所有维度为数据不足。
        这是一个合法的画像对象，可以被 KIA 和其他模块安全使用。
        """
        from .pythia import PreferenceProfile, EnergyProfile, CognitiveProfile, ValueProfile
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        all_energy_dims = [
            "focus_depth",
            "startup_difficulty",
            "endurance_mode",
            "switching_flexibility",
            "recovery_cycle",
        ]
        all_cognitive_dims = ["abstraction", "system_view", "skepticism", "creativity", "deduction"]
        all_value_dims = [
            "correctness_vs_efficiency",
            "depth_vs_breadth",
            "perfection_vs_completion",
            "innovation_vs_safety",
            "autonomy_vs_collaboration",
            "action_vs_analysis",
        ]

        return PreferenceProfile(
            version=0,
            generated_at=now,
            period_start=now,
            period_end=now,
            energy=EnergyProfile(
                focus_depth=0.5,
                startup_difficulty=0.5,
                endurance_mode=0.5,
                switching_flexibility=0.5,
                recovery_cycle=0.5,
                confidence=0.0,
                insufficient_dimensions=all_energy_dims,
            ),
            cognitive=CognitiveProfile(
                abstraction=0.5,
                system_view=0.5,
                skepticism=0.5,
                creativity=0.5,
                deduction=0.5,
                confidence=0.0,
                insufficient_dimensions=all_cognitive_dims,
            ),
            value=ValueProfile(
                correctness_vs_efficiency=0.5,
                depth_vs_breadth=0.5,
                perfection_vs_completion=0.5,
                innovation_vs_safety=0.5,
                autonomy_vs_collaboration=0.5,
                confidence=0.0,
                insufficient_dimensions=all_value_dims,
            ),
            signal_count=0,
        )

    def _load_persona_from_db(self) -> Tuple[Optional[PreferenceProfile], Optional[Any]]:
        """从数据库重建最新画像"""
        latest = self.signal_store.get_latest_persona_version()
        if not latest:
            return None, None
        try:
            return self._profile_from_db_row(latest)
        except (TypeError, ValueError, KeyError, AttributeError):
            logger.warning("Caught unexpected error at delphi.py", exc_info=True)
            return None, None

    def load_recent_personas(self, limit: int = 2) -> List[PreferenceProfile]:
        """加载最近 N 个已保存的画像版本（不含冷启动模板）。"""
        rows = self.signal_store.get_recent_persona_versions(limit=limit)
        personas = []
        for row in rows:
            try:
                profile, _ = self._profile_from_db_row(row)
                if profile and profile.version > 0:
                    personas.append(profile)
            except (TypeError, ValueError, KeyError, AttributeError):
                logger.warning("忽略异常画像版本", exc_info=True)
        return personas

    def _generate_persona_page(
        self, profile: PreferenceProfile, blindspot: BlindSpotProfile | None = None
    ) -> str:
        """生成画像页面Markdown"""
        data = profile.to_dict()

        # 防御性处理：generated_at 可能是 datetime.date 对象
        generated_at = profile.generated_at
        if hasattr(generated_at, "isoformat"):
            generated_at = generated_at.isoformat()
        generated_at_str = str(generated_at)[:10]

        lines = [
            "---",
            "type: user-persona",
            f"version: {profile.version}",
            f"generated_at: {generated_at_str}",
            f"period: {profile.period_start} ~ {profile.period_end}",
            f"signal_count: {profile.signal_count}",
            f"source_count: {profile.signal_count}",
            "sources: "
            + json.dumps(
                [
                    f"signal_store:{self.signal_store.db_path}#period/"
                    f"{profile.period_start}/{profile.period_end}"
                ],
                ensure_ascii=False,
            ),
            f"evidence_level: {'multiple' if profile.signal_count > 1 else 'single'}",
            f"knowledge_stage: {'P2' if profile.signal_count else 'P3'}",
            f"status: {'active' if profile.signal_count else 'draft'}",
            f"user_confirmed: {str(profile.user_confirmed).lower()}",
            "confirmed_at: " + json.dumps(profile.confirmed_at, ensure_ascii=False),
            "calibration_score: "
            + (
                "null"
                if profile.calibration_score is None
                else str(round(float(profile.calibration_score), 3))
            ),
            f"confidence_energy: {profile.energy.confidence:.2f}",
            f"confidence_cognitive: {profile.cognitive.confidence:.2f}",
            f"confidence_value: {profile.value.confidence:.2f}",
            f"insufficient_energy: {json.dumps(profile.energy.insufficient_dimensions or [])}",
            f"insufficient_cognitive: {json.dumps(profile.cognitive.insufficient_dimensions or [])}",  # noqa: E501
            f"insufficient_value: {json.dumps(profile.value.insufficient_dimensions or [])}",
            "---",
            "",
            "# 用户画像",
            "",
            "> ⚠️ **AI生成声明**：此画像由AI基于你的行为信号自动推断，"
            "不等同于你的真实人格，也不具备临床或职业评估效力。"
            "画像中的每一项都应被视为假设而非事实。",
            "",
            "> 🔄 **动态性**：画像随时间演化，重大生活变化（换工作、搬迁、角色转变）"
            "可能导致短期失真。建议每季度审视一次。",
            "",
            f"> 📊 **数据基础**：基于{profile.signal_count}条信号，"
            f"整体置信度：能量{profile.energy.confidence:.0%}/"
            f"认知{profile.cognitive.confidence:.0%}/"
            f"价值{profile.value.confidence:.0%}。",
            "",
            "## 能量模式（Layer 1: How you work）",
            "",
        ]

        for key, val in data["energy"].items():
            if key == "confidence":
                continue
            score = val["score"]
            label = val["label"]
            if score == "—":
                lines.append(f"- **{key}**: {label} ❌")
            else:
                lines.append(f"- **{key}**: {label} ({score:.2f})")

        lines.extend(
            [
                "",
                "## 认知模式（Layer 2: How you think）",
                "",
            ]
        )

        for key, val in data["cognitive"].items():
            if key == "confidence":
                continue
            score = val["score"]
            label = val["label"]
            if score == "—":
                lines.append(f"- **{key}**: {label} ❌")
            else:
                lines.append(f"- **{key}**: {label} ({score:.2f})")

        lines.extend(
            [
                "",
                "## 价值优先级（Layer 3: What you care）",
                "",
            ]
        )

        for key, val in data["value"].items():
            if key == "confidence":
                continue
            score = val["score"]
            label = val["label"]
            if score == "—":
                lines.append(f"- **{key}**: {label} ❌")
            else:
                lines.append(f"- **{key}**: {label} ({score:.2f})")

        # 盲区画像
        if blindspot:
            lines.extend(
                [
                    "",
                    "## 盲区画像",
                    "",
                ]
            )

            if blindspot.confirmed:
                lines.append("### 已确认的盲区")
                for bs in blindspot.confirmed:
                    lines.append(f"- **{bs.type}**: {bs.description}")
                    lines.append(f"  - 置信度: {bs.confidence:.2f}, 挑战次数: {bs.challenge_count}")

            if blindspot.suspected:
                lines.append("### 待验证的盲区")
                for bs in blindspot.suspected:
                    lines.append(f"- **{bs.type}**: {bs.description}")
                    lines.append(f"  - 置信度: {bs.confidence:.2f}")

            lines.extend(
                [
                    "",
                    "### 挑战统计",
                    f"- 总挑战次数: {blindspot.total_challenges}",
                    f"- 接受: {blindspot.accepted_count} | 忽略: {blindspot.ignored_count} | 拒绝: {blindspot.rejected_count}",  # noqa: E501
                    f"- 接受率: {blindspot.acceptance_rate:.1%}",
                    f"- 当前信用: {blindspot.challenge_credit:.1f}/{blindspot.credit_max}",
                ]
            )

        lines.extend(
            [
                "",
                "---",
                "",
                "*此画像由 AI 自动分析生成，每季度更新一次。*",
            ]
        )

        return "\n".join(lines)

    def _parse_persona_page(
        self, content: str
    ) -> Tuple[Optional[PreferenceProfile], Optional[BlindSpotProfile]]:
        """解析画像页面。从markdown中提取分数重建PreferenceProfile。"""
        from .pythia import PreferenceProfile, EnergyProfile, CognitiveProfile, ValueProfile

        if not content.startswith("---"):
            return None, None

        parts = content.split("---", 2)
        if len(parts) < 3:
            return None, None

        try:
            fm = yaml.safe_load(parts[1]) or {}
        except (yaml.YAMLError, ValueError):
            logger.warning("Caught unexpected error at delphi.py", exc_info=True)
            return None, None

        # 从markdown列表项提取分数，格式：- **key**: label (0.85)
        body = parts[2]
        scores = {}
        for match in re.finditer(r"\*\*(\w+)\*\*:.+\(([\d.]+)\)", body):
            scores[match.group(1)] = float(match.group(2))

        # 构建画像（yaml.safe_load 会把日期解析为 datetime.date，需要转回字符串）
        generated_at = fm.get("generated_at", "")
        if hasattr(generated_at, "isoformat"):
            generated_at = generated_at.isoformat()

        profile = PreferenceProfile(
            version=fm.get("version", 0),
            generated_at=str(generated_at),
            period_start=(
                fm.get("period", "").split(" ~ ")[0] if "~" in fm.get("period", "") else ""
            ),
            period_end=fm.get("period", "").split(" ~ ")[1] if "~" in fm.get("period", "") else "",
            energy=EnergyProfile(
                focus_depth=scores.get("focus_depth", 0.5),
                startup_difficulty=scores.get("startup_difficulty", 0.5),
                endurance_mode=scores.get("endurance_mode", 0.5),
                switching_flexibility=scores.get("switching_flexibility", 0.5),
                recovery_cycle=scores.get("recovery_cycle", 0.5),
                confidence=fm.get("confidence_energy", 0.0),
                insufficient_dimensions=fm.get("insufficient_energy", []),
            ),
            cognitive=CognitiveProfile(
                abstraction=scores.get("abstraction", 0.5),
                system_view=scores.get("system_view", 0.5),
                skepticism=scores.get("skepticism", 0.5),
                creativity=scores.get("creativity", 0.5),
                deduction=scores.get("deduction", 0.5),
                confidence=fm.get("confidence_cognitive", 0.0),
                insufficient_dimensions=fm.get("insufficient_cognitive", []),
            ),
            value=ValueProfile(
                correctness_vs_efficiency=scores.get("correctness_vs_efficiency", 0.5),
                depth_vs_breadth=scores.get("depth_vs_breadth", 0.5),
                perfection_vs_completion=scores.get("perfection_vs_completion", 0.5),
                innovation_vs_safety=scores.get("innovation_vs_safety", 0.5),
                autonomy_vs_collaboration=scores.get("autonomy_vs_collaboration", 0.5),
                confidence=fm.get("confidence_value", 0.0),
                insufficient_dimensions=fm.get("insufficient_value", []),
            ),
            signal_count=fm.get("signal_count", 0),
        )

        return profile, None  # 盲区画像暂不从此解析

    def _backup_current_version(
        self,
        *,
        material_action_commands: Mapping[str, str] | None = None,
    ):
        """Project the latest canonical version into history without reading Wiki."""

        profile, blindspot = self._load_persona_from_db()
        if profile is None:
            return
        self._project_persona_history(
            profile,
            blindspot,
            material_action_commands=material_action_commands,
        )

    def _write_persona_markdown(
        self,
        path: Path,
        content: str,
        *,
        source: str,
        action: str,
        evidence_refs: List[str],
        metadata: Dict[str, Any] | None = None,
        material_action_commands: Mapping[str, str] | None = None,
    ):
        from core.trust.markdown_adapter import read_markdown_text
        from core.trust.models import sha256_text
        from core.trust.vault_mutation_service import (
            TRUSTED_MARKDOWN_ACTION_TYPE,
            TRUSTED_MARKDOWN_EXECUTOR,
            TRUSTED_MARKDOWN_OWNER,
            TrustedVaultMutationService,
            commit_trusted_markdown,
            trusted_markdown_material_action_binding,
        )

        service = TrustedVaultMutationService(wiki_base=self.wiki_dir)
        expected_existing_hash = (
            sha256_text(read_markdown_text(path))
            if path.is_file()
            else None
        )
        binding = trusted_markdown_material_action_binding(
            target_path=path,
            content=content,
            proposed_action=action,
            expected_existing_hash=expected_existing_hash,
        )
        material_action = self._resolve_material_action(
            action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
            owner=TRUSTED_MARKDOWN_OWNER,
            executor=TRUSTED_MARKDOWN_EXECUTOR,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            command_ids=material_action_commands,
            expected_state_db=(
                service.config.db_path.parent / "producer_consumer_ledger.db"
            ),
            source_facts={
                "schema_version": "mnemos.persona_markdown_facts.v1",
                "source": source,
                "action": action,
                "target_path": str(path.resolve(strict=False)),
                "content_hash": sha256_text(content),
                "expected_existing_hash": expected_existing_hash or "",
                "metadata": dict(metadata or {}),
            },
            evidence_refs=tuple(evidence_refs),
            task=f"Apply Persona Markdown action {action}",
            goal="Commit only the exact Persona Markdown mutation prepared here.",
            created_at=datetime.now().astimezone().isoformat(),
            approved_candidate_key="commit_exact_persona_markdown",
            approved_candidate_summary=(
                "Commit the exact Persona Markdown bytes to the bound target."
            ),
            rejected_candidate_key="retain_existing_persona_markdown",
            rejected_candidate_summary=(
                "Retain existing Markdown when target or content bytes drift."
            ),
            committed_metric="persona_markdown_committed",
            rejected_metric="unbound_persona_markdown_count",
        )
        result = service.submit_markdown(
            target_path=path,
            content=content,
            source=source,
            actor="system",
            evidence_refs=evidence_refs,
            proposed_action=action,
            expected_existing_hash=expected_existing_hash,
            metadata=dict(metadata or {}),
            material_action=material_action,
        )
        commit_trusted_markdown(
            result,
            target_path=path,
            content=content,
            material_action=material_action,
        )
        return result

    def _blindspot_to_dict(self, profile: BlindSpotProfile) -> Dict:
        """盲区画像转字典"""
        if not profile:
            return {}
        return {
            "confirmed": [asdict(b) for b in profile.confirmed],
            "suspected": [asdict(b) for b in profile.suspected],
            "dismissed": [asdict(b) for b in profile.dismissed],
            "total_challenges": profile.total_challenges,
            "accepted_count": profile.accepted_count,
            "ignored_count": profile.ignored_count,
            "rejected_count": profile.rejected_count,
            "acceptance_rate": profile.acceptance_rate,
            "challenge_credit": profile.challenge_credit,
        }

    # ---- 知识库反写 ----

    def align_all_wiki_pages(
        self,
        persona: PreferenceProfile,
        session_context: Dict | None = None,
        dry_run: bool = False,
        *,
        material_action_commands: Mapping[str, str] | None = None,
    ) -> Dict[str, int]:
        """
        全量扫描wiki，计算匹配度并反写frontmatter。

        Args:
            persona: 当前画像
            session_context: 可选的session上下文
            dry_run: 如果True，只计算不写入

        Returns:
            统计信息 {scanned, updated, skipped}
        """
        aligner = KnowledgeAligner(persona)
        stats = {"scanned": 0, "updated": 0, "skipped": 0}

        if not self.wiki_dir.exists():
            return stats

        for md_file in self.wiki_dir.rglob("*.md"):
            # 跳过画像页面本身
            if md_file.name == "user-persona.md":
                continue

            stats["scanned"] += 1

            try:
                content = md_file.read_text(encoding="utf-8")
                relative_path = md_file.relative_to(self.wiki_dir)
                page_role = classify_wiki_page_role(content, str(relative_path))
                root = relative_path.parts[0] if relative_path.parts else ""
                if page_role.startswith(("formal_derived:", "derived_report:")) or root in {
                    "L2.4-KG",
                    "L3-Observations",
                    "L4-Reflections",
                    "L5-Feedback",
                }:
                    stats["skipped"] += 1
                    continue
                frontmatter = self._extract_frontmatter(content)

                if frontmatter is None:
                    stats["skipped"] += 1
                    continue

                # 计算匹配度
                wiki_page = {
                    "path": str(relative_path),
                    "frontmatter": frontmatter,
                    "content_snippet": content[:500],
                }
                alignment = aligner.calculate_alignment(wiki_page, session_context)

                if dry_run:
                    continue

                # 更新frontmatter
                new_content = self._update_persona_frontmatter(
                    content, frontmatter, alignment, persona.version
                )

                if new_content != content:
                    self._write_persona_markdown(
                        md_file,
                        new_content,
                        source="persona_alignment",
                        action="align_wiki_page",
                        evidence_refs=[
                            f"persona_version:{persona.version}",
                            f"wiki_page:{relative_path}",
                        ],
                        metadata={
                            "alignment": alignment,
                            "page_path": str(relative_path),
                        },
                        material_action_commands=material_action_commands,
                    )
                    stats["updated"] += 1

            except (OSError, ValueError):
                logger.warning("Caught unexpected error at delphi.py", exc_info=True)
                stats["skipped"] += 1
                continue

        return stats

    def _extract_frontmatter(self, content: str) -> Optional[Dict]:
        """提取frontmatter，使用行首匹配避免内容中 --- 被误切分"""
        if not content.startswith("---"):
            return None

        # 匹配独立的 --- 行，避免 frontmatter 值内部的 --- 被误切
        match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)", content, re.DOTALL)
        if not match:
            return None

        try:
            return yaml.safe_load(match.group(1)) or {}
        except (yaml.YAMLError, ValueError):
            logger.warning("Caught unexpected error at delphi.py", exc_info=True)
            return None

    def _update_persona_frontmatter(
        self, content: str, frontmatter: Dict, alignment: Dict, persona_version: int
    ) -> str:
        """
        更新知识条目的画像frontmatter字段。

        策略：
        1. 如果已有persona_current，移到persona_history
        2. 写入新的persona_current
        """
        if not content.startswith("---"):
            return content

        parts = content.split("---", 2)
        if len(parts) < 3:
            return content

        # 深拷贝 frontmatter，避免副作用
        fm = copy.deepcopy(frontmatter)

        # 处理旧的persona_current
        if "persona_current" in fm:
            old_current = copy.deepcopy(fm["persona_current"])
            if "persona_history" not in fm:
                fm["persona_history"] = []

            # 标记为superseded
            old_current["status"] = "superseded"
            old_current["superseded_at"] = datetime.now().isoformat()[:10]
            old_current["superseded_by"] = persona_version

            fm["persona_history"].append(old_current)

            # 限制历史记录数量（保留最近5个版本）
            if len(fm["persona_history"]) > 5:
                fm["persona_history"] = fm["persona_history"][-5:]

        # 写入新的persona_current
        fm["persona_current"] = {
            "version": persona_version,
            "updated_at": datetime.now().isoformat()[:10],
            "preference_alignment": {
                "score": round(alignment["preference_match"], 2),
            },
            "capability_alignment": {
                "score": round(alignment["capability_match"], 2),
                "difficulty_for_user": (
                    "boredom"
                    if alignment["capability_match"] < 0.3
                    else (
                        "sweet_spot"
                        if alignment["capability_match"] > 0.8
                        else "stretch_zone" if alignment["capability_match"] > 0.5 else "panic_zone"
                    )
                ),
            },
            "context_alignment": {
                "score": round(alignment["context_match"], 2),
            },
            "total_alignment": round(alignment["total"], 2),
        }

        # 重新生成frontmatter
        new_frontmatter = yaml.dump(fm, allow_unicode=True, sort_keys=False)
        return f"---\n{new_frontmatter}---{parts[2]}"


# ========== 便捷函数 ==========


def save_persona_to_wiki(profile: PreferenceProfile, blindspot: BlindSpotProfile | None = None):
    """Reject the retired direct-write convenience entry point."""

    del profile, blindspot
    raise RuntimeError(
        "direct Persona persistence is retired; use PersonaApplicationService"
    )


def align_wiki_with_persona(persona: PreferenceProfile, dry_run: bool = False) -> Dict:
    """便捷函数：全量反写wiki匹配度"""
    store = PersonaStore()
    stats = store.align_all_wiki_pages(persona, dry_run=dry_run)
    logger.warning(
        "✅ Wiki扫描完成: %s 条, 更新 %s 条, 跳过 %s 条",
        stats["scanned"],
        stats["updated"],
        stats["skipped"],
    )
    return stats


# 便捷函数
_persona_store_instance = None


def get_persona_store() -> PersonaStore:
    """获取全局 PersonaStore 实例"""
    global _persona_store_instance
    if _persona_store_instance is None:
        _persona_store_instance = PersonaStore()
    return _persona_store_instance


# ========== Agent 适配画像策略 ==========

# A/B 测试状态：每个进程只确定一次
_ab_test_persona_driven: Optional[bool] = None


def _ensure_ab_test_group() -> bool:
    """确保 A/B 测试分组已确定（确定性哈希，同一设备始终同一组）"""
    global _ab_test_persona_driven
    if _ab_test_persona_driven is None:
        import hashlib
        import uuid

        machine_id = str(uuid.getnode())
        experiment_key = "mnemos_persona_driven_v1"
        hash_val = int(hashlib.md5((machine_id + experiment_key).encode(), usedforsecurity=False).hexdigest(), 16)
        _ab_test_persona_driven = (hash_val % 100) < 50
    return _ab_test_persona_driven


def _get_ab_test_group_label() -> Optional[str]:
    """
    返回当前 A/B 实验分组标签。

    Returns:
        "treatment" / "control" / None
    """
    if not get_config().get("persona.ab_test_enabled", False):
        return None
    return "treatment" if _ensure_ab_test_group() else "control"


def _load_base_behavior_prompt() -> str:
    """加载基础画像策略（所有 Agent 通用）"""
    try:
        if get_config().get("persona.ab_test_enabled", False) and not _ensure_ab_test_group():
            return "\n[Persona-Driven Behavior]\n- A/B 对照组：本次 session 不使用画像驱动策略"

        pstore = PersonaStore()
        profile, _ = pstore.load_persona()
        if not profile:
            return ""

        lines = ["\n[Persona-Driven Behavior]"]
        ins_energy = set(profile.energy.insufficient_dimensions or [])
        ins_cognitive = set(profile.cognitive.insufficient_dimensions or [])
        ins_value = set(profile.value.insufficient_dimensions or [])

        _append_dimension_lines(lines, profile.energy, ins_energy, _ENERGY_RULES)
        _append_dimension_lines(lines, profile.cognitive, ins_cognitive, _COGNITIVE_RULES)
        _append_dimension_lines(lines, profile.value, ins_value, _VALUE_RULES)

        if len(lines) > 1:
            return "\n".join(lines)
        return ""
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        logger.warning("Caught unexpected error at delphi.py", exc_info=True)
        return ""


def get_behavior_prompt(agent: str) -> str:
    """根据用户画像和 Agent 类型生成行为策略提示

    Args:
        agent: Agent 标识（claude/hermes/openclaw/opencode/codex）

    Returns:
        画像驱动的行为策略文本（含 Agent 特定策略）
    """
    base = _load_base_behavior_prompt()
    if not base:
        return ""

    lines = [base]

    # Agent 特定策略
    agent_notes = {
        "claude": "[Agent Note] 你当前使用 Claude Code，擅长深度技术讨论和代码分析。画像策略叠加：在技术讨论中优先提供深度分析，代码审查时关注架构设计和边界 case。",  # noqa: E501
        "hermes": "[Agent Note] 你当前使用 Hermes，擅长快速信息检索和多源搜索。画像策略叠加：在检索时优先返回高置信度来源，根据用户偏好调整信息密度（深度/广度）。",  # noqa: E501
        "openclaw": "[Agent Note] 你当前使用 OpenClaw，擅长分析和推理。画像策略叠加：在推理过程中主动展示思考链，根据用户质疑倾向调整论证详略。",
        "opencode": "[Agent Note] 你当前使用 OpenCode，擅长代码理解和生成。画像策略叠加：在代码生成时根据用户追求完美/完成的倾向调整详尽程度。",
        "codex": "[Agent Note] 你当前使用 Codex，专注代码生成任务。画像策略叠加：快速生成可运行代码，根据用户效率偏好提供简洁实现或完整方案。",
    }
    note = agent_notes.get(agent, "")
    if note:
        lines.append(note)
    result = "\n".join(lines)

    # 记录画像行为提示使用情况（失败不阻塞）
    try:
        from core.persona.behavior_tracker import BehaviorPromptTracker

        BehaviorPromptTracker().track(
            agent=agent,
            source="preflight",
            prompt_text=result,
            ab_test_group=_get_ab_test_group_label(),
        )
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        logger.debug("[get_behavior_prompt] 行为提示追踪失败", exc_info=True)

    return result


if __name__ == "__main__":
    # 测试
    store = PersonaStore()
    print("✅ PersonaStore initialized")
    print(f"   Wiki目录: {store.wiki_dir}")
    print(f"   画像页面: {store.persona_page}")
