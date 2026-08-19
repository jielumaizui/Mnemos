"""
Cognitive Decision Flywheel - 认知决策资产与 Wiki/Skill 的双向演化飞轮

核心理念：
- Wiki 知识沉淀到一定成熟度 → 先沉淀为认知决策资产
- Skill 执行中遇到异常/新场景 → 反向沉淀为方法论/反模式/验证 recipe
- 只有资产稳定、边界清楚、验证通过后，才派生 automation skill

方向1：Wiki → Cognitive Decision Asset
- 触发信号：知识被反复使用、类型为方法论/经验法则、场景明确可复用
- 输出：认知决策资产（判断标准、适用边界、验证 recipe、失败模式）

方向2：Skill → Cognitive Decision Asset
- 触发信号：执行异常、新场景、结果偏差、用户标记
- 输出：新的知识片段（问题-解决、反模式、决策记录）

方向3：画像驱动（新增）
- 能力雷达短板 → 技能缺口识别与推荐
- 认知模式 → 学习路径风格匹配
- 价值优先级 → 任务生成策略
- 能量模式 → 飞轮调速控制
- 盲区画像 → 验证闭环设计

设计原则：
- 与现有系统解耦，通过接口接入
- 默认不直接生成 automation skill，先生成可审计的认知决策资产
- 追踪完整的资产-Wiki-Skill 关系图谱
"""

import json
import sqlite3
import re
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta, timezone
from core.config import get_config
from core.cognitive.state_contract import sha256_json
from core.pluggable import PluggableModule
from core.kia.cognitive_decision_assets import (
    BehaviorDrivenSkillGenerator,
    CognitiveDecisionAsset,
    CognitiveDecisionAssetMixin,
    FlywheelInsight,
)
from core.kia.ixion_schema import FlywheelSchemaMixin
from core.kia.ixion_api import (  # noqa: F401 - public convenience exports
    get_personalized_skill_paths,
    get_skill_gaps,
    get_verification_tasks,
    run_flywheel as _run_flywheel,
    run_persona_driven_flywheel as _run_persona_driven_flywheel,
)
from core.kia.persona_skill_engine import (  # noqa: F401
    PersonaDrivenSkillEngine,
    PersonaSkillGap,
    SkillPath,
    AutomationSkillRecord,
    SkillUsageLog,  # noqa: F401 - public value object export
    SkillVerificationTask,
)
from core.trust.vault_mutation_service import (
    TrustedVaultMutationService,
    commit_trusted_markdown,
)
from core.trust.formal_markdown import (
    TrustedMarkdownDecisionPolicy,
    authorize_exact_markdown_action,
    submit_or_write_markdown_with_decision,
)
from core.trust.models import sha256_text
import logging

# Constants extracted from magic numbers
BEHAVIOR_DRIVEN_SKILL_GENERATOR_BEHAVIOR_TRIGGERS_DAYS = 30
PERSONA_DRIVEN_SKILL_ENGINE_ENERGY_TO_FLYWHEEL_PARAMS_DAYS = 7
PARAMS_DAYS = 7
SKILL_WIKI_FLYWHEEL_DURATION_BUCKET_WEEK_DAYS = 7
SKILL_WIKI_FLYWHEEL_DURATION_BUCKET_MONTH_DAYS = 30
SKILL_WIKI_FLYWHEEL__FALLBACK_FROM_METIS_CYCLE_DAYS_DAYS = 7
RESULT_SECONDS = 30
TIMEOUT_SECONDS = 30


# 用户画像驱动（可选依赖）
logger = logging.getLogger(__name__)

FLYWHEEL_OPERATION_ERRORS = (
    ImportError,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    sqlite3.Error,
)

IXION_MARKDOWN_POLICY = TrustedMarkdownDecisionPolicy(
    contract_id="project-contract:ixion-wiki-lifecycle",
    contract_revision_id="mnemos.ixion_wiki_lifecycle.v1",
    contract_text=(
        "Ixion may mutate only the exact Wiki page or archive selected by an exact "
        "flywheel insight, skill record, or cognitive-decision asset lifecycle step."
    ),
    source_namespace="ixion-wiki-lifecycle",
    producer="ixion-flywheel",
    producer_code_hash=sha256_json(
        {
            "module": "core.kia.ixion",
            "producers": ["_archive_skill", "_submit_wiki_mutation"],
            "version": "mnemos.ixion_wiki_lifecycle.v1",
        }
    ),
    evaluator_id="ixion-wiki-lifecycle-evaluator",
    constraints=(
        "Target, preimage, lifecycle action, source evidence, metadata, and output remain exact.",
        "A proposal interception may not be represented as a committed direct page mutation.",
    ),
    approved_candidate_key="apply_exact_ixion_wiki_lifecycle_step",
    approved_candidate_summary="Apply the exact reviewed Ixion Wiki lifecycle step.",
    rejected_candidate_key="retain_ixion_wiki_state",
    rejected_candidate_summary="Retain Wiki state when flywheel or skill facts drift.",
    approved_reason_code="ixion_wiki_binding_verified",
    rejected_reason_code="ixion_wiki_binding_rejected",
    committed_metric="ixion_wiki_lifecycle_committed",
    rejected_metric="unbound_ixion_wiki_lifecycle_count",
)


def _utc_now_iso() -> str:
    value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


try:
    from core.persona.pythia import PreferenceProfile
    from core.persona.hamartia import (
        BlindSpotProfile,
    )

    PERSONA_AVAILABLE = True
except ImportError:
    PERSONA_AVAILABLE = False


# ========== 飞轮管理器 ==========


class CognitiveDecisionFlywheel(
    FlywheelSchemaMixin,
    CognitiveDecisionAssetMixin,
    PluggableModule,
):
    """Generate, validate, and consume cognitive decision assets."""

    # Wiki → Cognitive Decision Asset 的触发阈值
    WIKI_TO_COGNITIVE_DECISION_SIGNALS = {
        "min_usage_count": 5,  # 最少使用次数
        "min_age_days": SKILL_WIKI_FLYWHEEL_DURATION_BUCKET_WEEK_DAYS,  # 知识沉淀至少7天
        "preferred_forms": ["方法论", "经验法则", "决策记录"],
        "trigger_keywords": ["步骤", "流程", "指南", "模板", "检查清单", "脚本"],
        "min_confidence": 0.6,
    }

    # Skill → Cognitive Decision Asset 的触发阈值
    SKILL_TO_COGNITIVE_DECISION_SIGNALS = {
        "failure_rate_threshold": 0.3,  # 失败率超过30%触发
        "new_scenario_threshold": 3,  # 同一新场景出现3次
        "exception_threshold": 2,  # 同类异常出现2次
    }

    def __init__(
        self,
        wiki_base: str | None = None,
        db_path: str | None = None,
        persona: PreferenceProfile | None = None,
        blindspot: BlindSpotProfile | None = None,
    ):
        cfg = get_config()
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else (cfg.wiki_dir)
        self.db_path = Path(db_path) if db_path else (self.wiki_base / ".kg" / "flywheel.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.behavior_generator = BehaviorDrivenSkillGenerator(self.db_path)
        flywheel_cfg = cfg.get("skill.cognitive_decision_flywheel")
        if isinstance(flywheel_cfg, dict):
            self.configure(flywheel_cfg)
        self._init_db()
        self._enabled = True

        # 画像驱动引擎（可选）
        self.persona_engine = None
        if PERSONA_AVAILABLE:
            self.persona_engine = PersonaDrivenSkillEngine(persona, blindspot)
        else:
            self.persona_engine = None

    # ---- PluggableModule 接口 ----

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def configure(self, cfg: Dict[str, Any]) -> None:
        if "cognitive_decision_flywheel" in cfg and isinstance(
            cfg["cognitive_decision_flywheel"], dict
        ):
            cfg = cfg["cognitive_decision_flywheel"]
        self.behavior_generator.configure(cfg)
        wiki_mapping = {
            "min_usage_count": "min_usage_count",
            "min_age_days": "min_age_days",
            "min_confidence": "min_confidence",
        }
        for source_key, target_key in wiki_mapping.items():
            if source_key in cfg:
                self.WIKI_TO_COGNITIVE_DECISION_SIGNALS[target_key] = cfg[source_key]
        skill_mapping = {
            "failure_rate_threshold": "failure_rate_threshold",
            "new_scenario_threshold": "new_scenario_threshold",
            "exception_threshold": "exception_threshold",
        }
        for source_key, target_key in skill_mapping.items():
            if source_key in cfg:
                self.SKILL_TO_COGNITIVE_DECISION_SIGNALS[target_key] = cfg[source_key]
        if "wiki_to_cognitive_decision_signals" in cfg:
            self.WIKI_TO_COGNITIVE_DECISION_SIGNALS.update(cfg["wiki_to_cognitive_decision_signals"])
        if "skill_to_cognitive_decision_signals" in cfg:
            self.SKILL_TO_COGNITIVE_DECISION_SIGNALS.update(cfg["skill_to_cognitive_decision_signals"])

    def handle_event(self, event_type: str, data: Dict[str, Any]) -> None:
        if not self._enabled:
            return
        if event_type == "task_completed":
            self.behavior_generator.analyze()
        elif event_type == "skill_executed":
            self._log_skill_execution(data)
        elif event_type == "skill_deviated":
            self._record_deviation(data)
        elif event_type == "page_accessed":
            page_path = data.get("page_path")
            if isinstance(page_path, str):
                self.log_wiki_usage(page_path, data.get("access_type", "read"))
        elif event_type == "periodic_cleanup":
            self.cleanup_stale_skills()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row  # noqa
        return conn

    def _scan_all_wiki_pages(self) -> List[Path]:
        excluded = {".git", ".obsidian", ".kg", "99-Archive", "99-Reports", "__pycache__"}
        pages = []
        for page in self.wiki_base.rglob("*.md"):
            rel_parts = page.relative_to(self.wiki_base).parts
            if any(part in excluded or part.startswith(".") for part in rel_parts):
                continue
            pages.append(page)
        return pages

    # ========== 方向2：Skill → Cognitive Decision Asset ==========

    def log_skill_usage(
        self,
        skill_name: str,
        input_data: str = "",
        output_data: str = "",
        status: str = "success",
        exception_type: str = "",
        exception_detail: str = "",
        new_scenario: bool = False,
        user_marked: bool = False,
        generated_wiki: str = "",
    ) -> int:
        """
        记录 Skill 使用日志

        Returns:
            log_id
        """
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO skill_usage_logs
                   (skill_name, timestamp, input_data, output_data, status,
                    exception_type, exception_detail, new_scenario, user_marked,
                    generated_wiki)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    skill_name,
                    datetime.now().isoformat()[:19],
                    input_data[:500],
                    output_data[:500],
                    status,
                    exception_type,
                    exception_detail[:1000],
                    new_scenario,
                    user_marked,
                    generated_wiki,
                ),
            )
            conn.commit()
            log_id = cursor.lastrowid

        # 更新 Skill 统计
        self._update_skill_stats(skill_name)

        return log_id if log_id is not None else 0

    def _update_skill_stats(self, skill_name: str):
        """更新 Skill 使用统计"""
        with self._conn() as conn:
            stats = conn.execute(
                """SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
                    SUM(CASE WHEN status='failure' THEN 1 ELSE 0 END) as failure
                   FROM skill_usage_logs WHERE skill_name=?""",
                (skill_name,),
            ).fetchone()

            conn.execute(
                """UPDATE skills SET usage_count=?, success_count=?, failure_count=?,
                   updated_at=? WHERE skill_name=?""",
                (
                    stats["total"],
                    stats["success"],
                    stats["failure"],
                    datetime.now().isoformat()[:19],
                    skill_name,
                ),
            )
            conn.commit()

    def analyze_skill_for_wiki(self, skill_name: str) -> List[FlywheelInsight]:
        """
        分析 Skill 使用日志，判断是否需要反向沉淀为认知决策资产。

        返回需要生成/复审的认知决策资产建议列表。
        """
        insights: List[FlywheelInsight] = []

        with self._conn() as conn:
            # 检查失败率
            skill = conn.execute(
                "SELECT * FROM skills WHERE skill_name=?", (skill_name,)
            ).fetchone()

            if not skill:
                return insights

            total = skill["usage_count"] or 0
            failures = skill["failure_count"] or 0
            failure_rate = failures / total if total > 0 else 0

            if failure_rate >= self.SKILL_TO_COGNITIVE_DECISION_SIGNALS["failure_rate_threshold"]:
                insights.append(
                    FlywheelInsight(
                        direction="skill_to_cognitive_decision",
                        source=skill_name,
                        target=f"{skill_name} 失败处理认知决策资产",
                        confidence=round(min(failure_rate * 2, 1.0), 2),
                        reason=f"失败率 {failure_rate:.0%}，超过阈值 {self.SKILL_TO_COGNITIVE_DECISION_SIGNALS['failure_rate_threshold']:.0%}",  # noqa: E501
                        suggested_action="提取常见失败模式、适用边界和验证 recipe，生成反模式资产",
                        asset_type="pitfall_pattern",
                        evidence_refs=[f"skill_usage:{skill_name}:failure_rate"],
                        failure_modes=[f"failure_rate={failure_rate:.0%}"],
                        verification_recipe=["抽样失败日志，确认共同前置条件和回滚策略"],
                    )
                )

            # 检查常见异常
            exceptions = conn.execute(
                """SELECT exception_type, COUNT(*) as cnt
                   FROM skill_usage_logs
                   WHERE skill_name=? AND status='failure' AND exception_type != ''
                   GROUP BY exception_type
                   HAVING cnt >= ?""",
                (skill_name, self.SKILL_TO_COGNITIVE_DECISION_SIGNALS["exception_threshold"]),
            ).fetchall()

            for exc in exceptions:
                insights.append(
                    FlywheelInsight(
                        direction="skill_to_cognitive_decision",
                        source=skill_name,
                        target=f"{skill_name} 异常：{exc['exception_type']}",
                        confidence=min(exc["cnt"] * 0.2, 0.9),
                        reason=f"异常 '{exc['exception_type']}' 出现 {exc['cnt']} 次",
                        suggested_action="记录异常根因、判断信号和验证 recipe",
                        asset_type="pitfall_pattern",
                        evidence_refs=[f"skill_usage:{skill_name}:exception:{exc['exception_type']}"],
                        failure_modes=[f"exception:{exc['exception_type']}"],
                        verification_recipe=["复现异常输入，验证修复前置检查"],
                    )
                )

            # 检查新场景
            new_scenarios = conn.execute(
                """SELECT input_data, COUNT(*) as cnt
                   FROM skill_usage_logs
                   WHERE skill_name=? AND new_scenario=1
                   GROUP BY input_data
                   HAVING cnt >= ?""",
                (skill_name, self.SKILL_TO_COGNITIVE_DECISION_SIGNALS["new_scenario_threshold"]),
            ).fetchall()

            for ns in new_scenarios:
                insights.append(
                    FlywheelInsight(
                        direction="skill_to_cognitive_decision",
                        source=skill_name,
                        target=f"{skill_name} 新场景处理",
                        confidence=min(ns["cnt"] * 0.15, 0.8),
                        reason=f"新场景出现 {ns['cnt']} 次",
                        suggested_action="分析新场景特征，扩展资产适用/不适用边界",
                        asset_type="decision_heuristic",
                        evidence_refs=[f"skill_usage:{skill_name}:new_scenario"],
                        applicability=[str(ns["input_data"])],
                        verification_recipe=["用新场景样本验证资产边界"],
                    )
                )

            # 用户主动标记
            user_marked = conn.execute(
                """SELECT input_data, output_data, exception_detail
                   FROM skill_usage_logs
                   WHERE skill_name=? AND user_marked=1
                   ORDER BY timestamp DESC LIMIT 5""",
                (skill_name,),
            ).fetchall()

            if user_marked:
                insights.append(
                    FlywheelInsight(
                        direction="skill_to_cognitive_decision",
                        source=skill_name,
                        target=f"{skill_name} 用户标记案例",
                        confidence=0.9,
                        reason=f"用户主动标记 {len(user_marked)} 条记录应沉淀为知识",
                        suggested_action="整理用户标记案例，生成经验法则或决策记录资产",
                        asset_type="methodology",
                        evidence_refs=[f"skill_usage:{skill_name}:user_marked"],
                        verification_recipe=["回看用户标记原因，补齐适用边界和失败样本"],
                    )
                )

        return insights

    def _log_skill_execution(self, data: Dict[str, Any]) -> None:
        """记录 Skill 执行事件（由 event bus 触发）"""
        try:
            skill_name = data.get("skill_name", "unknown")
            self.log_skill_usage(
                skill_name=skill_name,
                input_data=data.get("input_data", ""),
                output_data=data.get("output_data", ""),
                status=data.get("status", "success"),
                exception_type=data.get("exception_type", ""),
                exception_detail=data.get("exception_detail", ""),
                new_scenario=data.get("new_scenario", False),
                user_marked=data.get("user_marked", False),
            )
        except FLYWHEEL_OPERATION_ERRORS as e:
            logger.warning("记录 skill 执行日志失败: %s", e, exc_info=True)

    def _record_deviation(self, data: Dict[str, Any]) -> None:
        """记录 Skill 偏离事件（由 event bus 触发）"""
        try:
            skill_name = data.get("skill_name", "unknown")
            self.log_skill_usage(
                skill_name=skill_name,
                input_data=data.get("input_data", ""),
                output_data=data.get("output_data", ""),
                status="deviated",
                exception_type=data.get("exception_type", ""),
                exception_detail=data.get("exception_detail", ""),
                new_scenario=True,
                user_marked=False,
            )
        except FLYWHEEL_OPERATION_ERRORS as e:
            logger.warning("记录 skill 偏离日志失败: %s", e, exc_info=True)

    # ========== Wiki 使用追踪 ==========

    def log_wiki_usage(self, page_path: str, access_type: str = "read", context: str = ""):
        """记录 Wiki 页面使用"""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO wiki_usage_logs (page_path, timestamp, access_type, context)
                   VALUES (?, ?, ?, ?)""",
                (page_path, datetime.now().isoformat()[:19], access_type, context[:500]),
            )
            conn.commit()

    def _get_wiki_usage(
        self, page_path: str, days: int = SKILL_WIKI_FLYWHEEL_DURATION_BUCKET_MONTH_DAYS
    ) -> int:
        """获取 Wiki 页面在 N 天内的使用次数"""
        since = (datetime.now() - timedelta(days=days)).isoformat()[:19]
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM wiki_usage_logs WHERE page_path=? AND timestamp >=?",
                (page_path, since),
            ).fetchone()
        return row[0] if row else 0

    # ========== Skill CRUD ==========

    def create_skill(self, skill: AutomationSkillRecord) -> bool:
        """创建 Skill 记录"""
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO skills
                       (skill_name, description, trigger_conditions, input_template,
                        expected_output, source_wiki_pages, status, created_at, updated_at,
                        version, generation_source, last_used, created_by,
                        parent_version, deviation_log)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        skill.skill_name,
                        skill.description,
                        json.dumps(skill.trigger_conditions, ensure_ascii=False),
                        skill.input_template,
                        skill.expected_output,
                        json.dumps(skill.source_wiki_pages, ensure_ascii=False),
                        skill.status or "proposed",
                        skill.created_at or datetime.now().isoformat()[:19],
                        datetime.now().isoformat()[:19],
                        skill.version,
                        skill.generation_source,
                        skill.last_used,
                        skill.created_by,
                        skill.parent_version,
                        json.dumps(skill.deviation_log, ensure_ascii=False),
                    ),
                )
                conn.execute(
                    """INSERT OR REPLACE INTO skill_versions
                       (skill_name, version, trigger_conditions, input_template,
                        expected_output, change_summary, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        skill.skill_name,
                        skill.version,
                        json.dumps(skill.trigger_conditions, ensure_ascii=False),
                        skill.input_template,
                        skill.expected_output,
                        (
                            "initial create"
                            if skill.parent_version == 0
                            else f"from v{skill.parent_version}"
                        ),
                        skill.created_at or datetime.now().isoformat()[:19],
                    ),
                )
                conn.commit()
                return True
        except sqlite3.Error:
            return False

    def get_skill(self, skill_name: str) -> Optional[AutomationSkillRecord]:
        """获取 Skill 记录"""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM skills WHERE skill_name=?", (skill_name,)).fetchone()

        if not row:
            return None

        return AutomationSkillRecord(
            skill_name=row["skill_name"],
            description=row["description"],
            trigger_conditions=json.loads(row["trigger_conditions"] or "[]"),
            input_template=row["input_template"],
            expected_output=row["expected_output"],
            source_wiki_pages=json.loads(row["source_wiki_pages"] or "[]"),
            usage_count=row["usage_count"],
            success_count=row["success_count"],
            failure_count=row["failure_count"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            version=row["version"] or 1,
            generation_source=row["generation_source"] or "",
            last_used=row["last_used"] or "",
            created_by=row["created_by"] or "",
            parent_version=row["parent_version"] or 0,
            deviation_log=json.loads(row["deviation_log"] or "[]"),
        )

    def list_skills(self, status: str | None = None) -> List[AutomationSkillRecord]:
        """列出所有 Skill"""
        with self._conn() as conn:
            if status:
                rows = conn.execute("SELECT * FROM skills WHERE status=?", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM skills").fetchall()

        return [
            AutomationSkillRecord(
                skill_name=row["skill_name"],
                description=row["description"],
                trigger_conditions=json.loads(row["trigger_conditions"] or "[]"),
                input_template=row["input_template"],
                expected_output=row["expected_output"],
                source_wiki_pages=json.loads(row["source_wiki_pages"] or "[]"),
                usage_count=row["usage_count"],
                success_count=row["success_count"],
                failure_count=row["failure_count"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                version=row["version"] or 1,
                generation_source=row["generation_source"] or "",
                last_used=row["last_used"] or "",
                created_by=row["created_by"] or "",
                parent_version=row["parent_version"] or 0,
                deviation_log=json.loads(row["deviation_log"] or "[]"),
            )
            for row in rows
        ]

    # ========== 飞轮运行 ==========

    def run_cycle(self) -> Dict[str, Any]:
        """
        运行一个飞轮周期

        Returns:
            {"wiki_to_cognitive_decision": [...], "skill_to_cognitive_decision": [...], ...}
        """
        wiki_assets = self.scan_wiki_for_cognitive_decision_assets()
        behavior_assets = self.behavior_generator.analyze()
        skill_assets: List[FlywheelInsight] = []
        results: Dict[str, Any] = {
            "wiki_to_cognitive_decision": wiki_assets,
            "behavior_to_cognitive_decision": behavior_assets,
            "skill_to_cognitive_decision": skill_assets,
            "persona_driven": {},
            "cleanup": [],
        }

        # 扫描所有 Skill 的使用日志
        for skill in self.list_skills():
            insights = self.analyze_skill_for_wiki(skill.skill_name)
            skill_assets.extend(insights)

        # 按置信度排序
        results["cleanup"] = self.cleanup_stale_skills()

        for direction in [
            "wiki_to_cognitive_decision",
            "behavior_to_cognitive_decision",
            "skill_to_cognitive_decision",
        ]:
            results[direction].sort(key=lambda x: x.confidence, reverse=True)

        # 画像驱动分析
        if self.persona_engine:
            results["persona_driven"] = self._run_persona_driven_cycle()
            self._log_persona_cycle(results["persona_driven"])
        else:
            results["persona_driven"] = self._fallback_from_metis()

        # [P2-13] 自动执行可操作项
        executed = self.execute_insights(results)
        results["executed"] = executed

        # 生成并保存报告
        report_path = self.write_report(results)
        if report_path:
            results["report_path"] = str(report_path)

        # Git 自动留痕
        actions = executed.get("actions", [])
        if actions or report_path:
            self._git_commit_changes(report_path, actions)

        return results

    def _fallback_from_metis(self) -> Dict:
        """画像依赖缺失时，用 Metis 知识画像生成简化飞轮参数。"""
        try:
            from core.kia.metis import ProfileGenerator

            profile = ProfileGenerator(wiki_base=str(self.wiki_base)).generate()
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logging.getLogger(__name__).warning(
                "Caught unexpected error at ixion.py", exc_info=True
            )
            return {
                "fallback": "metis_unavailable",
                "flywheel_params": PersonaDrivenSkillEngine.ENERGY_TO_FLYWHEEL_PARAMS["mixed"],
            }

        entropy = getattr(profile, "domain_entropy", 0.0)
        total = getattr(profile, "total_knowledge", 0)
        intensity = "high" if total >= 50 and entropy > 0.5 else "medium"
        return {
            "fallback": "metis",
            "domain_entropy": entropy,
            "total_knowledge": total,
            "flywheel_params": {
                "cycle_days": SKILL_WIKI_FLYWHEEL__FALLBACK_FROM_METIS_CYCLE_DAYS_DAYS,
                "batch_size": 3 if intensity == "medium" else 5,
                "intensity": intensity,
                "max_parallel": 1,
            },
        }

    def record_task_completed(
        self,
        task_type: str,
        subtype: str,
        wiki_pages: List[str] | None = None,
        input_summary: str = "",
        output_summary: str = "",
    ):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO task_history
                   (task_type, subtype, wiki_pages, input_summary, output_summary, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    task_type,
                    subtype,
                    json.dumps(wiki_pages or [], ensure_ascii=False),
                    input_summary,
                    output_summary,
                    datetime.now().isoformat(),
                ),
            )

    def cleanup_stale_skills(
        self,
        cleanup_days: int = 60,
        grace_period_days: int = SKILL_WIKI_FLYWHEEL_DURATION_BUCKET_WEEK_DAYS,
    ) -> List[str]:
        now = datetime.now()
        cleanup_cutoff = now - timedelta(days=cleanup_days)
        grace_cutoff = now - timedelta(days=grace_period_days)
        archived = []

        def _parse_time(value: str) -> datetime:
            if not value:
                return datetime.fromisoformat("1970-01-01T00:00:00")
            try:
                return datetime.fromisoformat(value[:19])
            except ValueError:
                return datetime.fromisoformat("1970-01-01T00:00:00")

        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM skills
                   WHERE status IN ('active', 'auto_generated', 'stale')"""
            ).fetchall()
            for row in rows:
                skill = self.get_skill(row["skill_name"])
                if not skill:
                    continue
                last_activity = _parse_time(skill.last_used or skill.updated_at or skill.created_at)
                if skill.status == "stale":
                    stale_since = _parse_time(skill.updated_at or skill.created_at)
                    if stale_since > grace_cutoff:
                        continue
                    self._archive_skill(skill)
                    conn.execute(
                        "UPDATE skills SET status='deprecated', updated_at=? WHERE skill_name=?",
                        (now.isoformat()[:19], skill.skill_name),
                    )
                    archived.append(skill.skill_name)
                    continue
                if last_activity >= cleanup_cutoff:
                    continue
                if skill.generation_source == "behavior":
                    self._archive_skill(skill)
                    conn.execute(
                        "UPDATE skills SET status='deprecated', updated_at=? WHERE skill_name=?",
                        (now.isoformat()[:19], skill.skill_name),
                    )
                    archived.append(skill.skill_name)
                else:
                    conn.execute(
                        "UPDATE skills SET status='stale', updated_at=? WHERE skill_name=?",
                        (now.isoformat()[:19], skill.skill_name),
                    )
            conn.commit()
        return archived

    def _archive_skill(self, skill: AutomationSkillRecord):
        archive_path = self.wiki_base / "03-Archive" / "Skills" / f"{skill.skill_name}-归档.md"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "---",
            "type: skill_archive",
            f"skill_name: {skill.skill_name}",
            f"archived_at: {datetime.now().isoformat()[:19]}",
            "---",
            "",
            f"# {skill.skill_name} 归档",
            "",
            skill.description,
            "",
            "## 来源页面",
        ]
        lines.extend(f"- {page}" for page in skill.source_wiki_pages)
        rendered_content = "\n".join(lines)
        evidence_refs = [f"skill:{skill.skill_name}"]
        submit_or_write_markdown_with_decision(
            decision_policy=IXION_MARKDOWN_POLICY,
            decision_facts={
                "schema_version": "mnemos.ixion_skill_archive_facts.v1",
                "skill_name": skill.skill_name,
                "description": skill.description,
                "source_wiki_pages": list(skill.source_wiki_pages),
            },
            decision_task=f"Archive stale skill {skill.skill_name}",
            decision_goal="Preserve the exact stale skill record in its archive page.",
            decision_created_at=_utc_now_iso(),
            wiki_base=self.wiki_base,
            target_path=archive_path,
            content=rendered_content,
            source="ixion_skill_archive",
            actor="ixion",
            evidence_refs=evidence_refs,
            proposed_action="archive_skill_page",
        )

    def _run_persona_driven_cycle(self) -> Dict[str, Any]:
        """运行画像驱动的飞轮子周期"""
        engine = self.persona_engine
        if engine is None:
            return {}

        # 1. 技能缺口分析
        gaps = engine.analyze_skill_gaps()

        # 2. 学习路径生成
        paths = engine.generate_skill_paths(gaps)

        # 3. 任务生成
        all_skills = [s.skill_name for s in self.list_skills()]
        if not all_skills:
            # 如果没有已有skill，用缺口推荐的类别
            all_skills = [g.recommended_skill_category for g in gaps]
        tasks = engine.generate_tasks_by_values(all_skills)

        # 4. 飞轮调速参数
        flywheel_params = engine.get_flywheel_params()

        # 5. 验证任务生成
        verifications = engine.generate_verification_tasks(all_skills)

        # 保存路径和验证任务到数据库
        self._save_skill_paths(paths)
        self._save_verification_tasks(verifications)

        result = {
            "gaps": gaps,
            "paths": paths,
            "tasks": tasks,
            "flywheel_params": flywheel_params,
            "verifications": verifications,
        }
        self._record_profile_usage_for_flywheel(result)
        return result

    @staticmethod
    def _record_profile_usage_for_flywheel(persona_results: Dict[str, Any]) -> None:
        """Skip profile usage logging when this background path has no ACL scope."""

        logger.debug(
            "cognitive flywheel profile usage skipped: principal and scope are required"
        )

    def _save_skill_paths(self, paths: List[SkillPath]):
        """保存学习路径到数据库"""
        with self._conn() as conn:
            for path in paths:
                conn.execute(
                    """INSERT OR REPLACE INTO skill_paths
                       (path_id, title, description, stages, cognitive_style,
                        estimated_duration, priority, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        path.path_id,
                        path.title,
                        path.description,
                        json.dumps(path.stages, ensure_ascii=False),
                        path.cognitive_style,
                        path.estimated_duration,
                        path.priority,
                        "active",
                        datetime.now().isoformat()[:19],
                    ),
                )
            conn.commit()

    def _save_verification_tasks(self, tasks: List[SkillVerificationTask]):
        """保存验证任务到数据库"""
        with self._conn() as conn:
            for task in tasks:
                # 先检查是否已存在相同skill+blindspot的pending任务
                existing = conn.execute(
                    """SELECT task_id FROM skill_verification_tasks
                       WHERE related_skill=? AND related_blindspot_type=?
                       AND status='pending'""",
                    (task.related_skill, task.related_blindspot_type),
                ).fetchone()
                if existing:
                    continue  # 避免重复创建

                conn.execute(
                    """INSERT OR REPLACE INTO skill_verification_tasks
                       (task_id, task_type, description, related_skill,
                        related_blindspot_type, verification_method,
                        expected_outcome, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        task.task_id,
                        task.task_type,
                        task.description,
                        task.related_skill,
                        task.related_blindspot_type,
                        task.verification_method,
                        task.expected_outcome,
                        task.status,
                        datetime.now().isoformat()[:19],
                    ),
                )
            conn.commit()

    def _log_persona_cycle(self, persona_results: Dict):
        """记录画像驱动周期日志"""
        params = persona_results.get("flywheel_params", {})
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO persona_flywheel_logs
                   (cycle_date, persona_version, gaps_detected, paths_created,
                    verifications_created, flywheel_params)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now().isoformat()[:10],
                    persona_results.get("persona_version", 1),
                    len(persona_results.get("gaps", [])),
                    len(persona_results.get("paths", [])),
                    len(persona_results.get("verifications", [])),
                    json.dumps(params, ensure_ascii=False),
                ),
            )
            conn.commit()

    def generate_cycle_report(self, results: Dict) -> str:
        """生成飞轮周期报告"""
        lines = [
            "# 认知决策飞轮周期报告",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
        ]

        # 画像驱动调速参数（如果有）
        persona_driven = results.get("persona_driven", {})
        flywheel_params = persona_driven.get("flywheel_params", {})
        if flywheel_params:
            lines.extend(
                [
                    "## 飞轮调速（画像驱动）",
                    f"周期间隔: {flywheel_params.get('cycle_days', 5)} 天",
                    f"每批处理: {flywheel_params.get('batch_size', 4)} 个",
                    f"强度模式: {flywheel_params.get('intensity', 'adaptive')}",
                    f"最大并行: {flywheel_params.get('max_parallel', 2)} 个",
                    "",
                ]
            )

        # Wiki → Cognitive Decision Asset
        wiki_to_asset = results.get("wiki_to_cognitive_decision", [])
        lines.extend(
            [
                f"## Wiki → 认知决策资产 ({len(wiki_to_asset)} 个建议)",
                "",
            ]
        )
        if wiki_to_asset:
            for i, insight in enumerate(wiki_to_asset[:10], 1):
                page_name = Path(insight.source).name
                lines.append(f"{i}. **{insight.target}** (置信度: {insight.confidence})")
                lines.append(f"   - 资产类型: `{insight.asset_type}`")
                lines.append(f"   - 来源: `{page_name}`")
                lines.append(f"   - 理由: {insight.reason}")
                lines.append("")
        else:
            lines.append("暂无 Wiki → 认知决策资产的沉淀建议。\n")

        # Skill → Cognitive Decision Asset
        skill_to_asset = results.get("skill_to_cognitive_decision", [])
        lines.extend(
            [
                f"## Skill → 认知决策资产 ({len(skill_to_asset)} 个建议)",
                "",
            ]
        )
        if skill_to_asset:
            for i, insight in enumerate(skill_to_asset[:10], 1):
                lines.append(f"{i}. **{insight.target}** (置信度: {insight.confidence})")
                lines.append(f"   - 资产类型: `{insight.asset_type}`")
                lines.append(f"   - 来源 Skill: `{insight.source}`")
                lines.append(f"   - 理由: {insight.reason}")
                lines.append("")
        else:
            lines.append("暂无 Skill → 认知决策资产的沉淀建议。\n")

        # 画像驱动洞察
        if persona_driven and self.persona_engine:
            lines.append(
                self.persona_engine.format_persona_insights(
                    persona_driven.get("gaps", []),
                    persona_driven.get("paths", []),
                    persona_driven.get("tasks", []),
                    persona_driven.get("verifications", []),
                )
            )

        return "\n".join(lines)

    # ========== 自动执行（P2-13） ==========

    def execute_insights(self, results: Dict) -> Dict:
        """
        自动执行所有 auto_applicable=True 的 insight。

        Returns:
            {"actions": ["描述1", "描述2", ...], "errors": ["错误1", ...]}
        """
        actions: List[str] = []
        errors: List[str] = []

        # 1. Wiki → Cognitive Decision Asset
        wiki_insights = results.get("wiki_to_cognitive_decision", [])
        for insight in wiki_insights:
            if getattr(insight, "auto_applicable", False):
                try:
                    result = self._execute_wiki_to_cognitive_decision(insight)
                    if result.get("action"):
                        actions.append(result["action"])
                except FLYWHEEL_OPERATION_ERRORS as e:
                    errors.append(f"wiki_to_cognitive_decision {insight.source}: {e}")

        # 2. Behavior → Cognitive Decision Asset
        behavior_insights = results.get("behavior_to_cognitive_decision", [])
        for insight in behavior_insights:
            if getattr(insight, "auto_applicable", False):
                try:
                    result = self._execute_behavior_to_cognitive_decision(insight)
                    if result.get("action"):
                        actions.append(result["action"])
                except FLYWHEEL_OPERATION_ERRORS as e:
                    errors.append(f"behavior_to_cognitive_decision {insight.source}: {e}")

        # 3. Skill → Cognitive Decision Asset / Wiki review marker
        skill_insights = results.get("skill_to_cognitive_decision", [])
        for insight in skill_insights:
            try:
                result = self._execute_skill_to_cognitive_decision(insight)
                if result.get("action"):
                    actions.append(result["action"])
            except FLYWHEEL_OPERATION_ERRORS as e:
                errors.append(f"skill_to_cognitive_decision {insight.source}: {e}")

        return {"actions": actions, "errors": errors, "count": len(actions)}

    def _execute_wiki_to_cognitive_decision(self, insight: FlywheelInsight) -> Dict:
        """执行 wiki → cognitive decision asset：标记 frontmatter + 创建资产。"""
        page_path = Path(insight.source)
        if not page_path.exists():
            return {}

        # 读取并更新 frontmatter
        content = page_path.read_text(encoding="utf-8")
        try:
            from core.frontmatter import parse_frontmatter, write_frontmatter

            fm, body = parse_frontmatter(content)
            if fm is None:
                fm = {}
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            fm, body = {}, content

        asset_id = self._asset_id_from(insight.target, insight.source)
        fm["cognitive_decision_asset_candidate"] = True
        fm["cognitive_decision_asset_id"] = asset_id
        fm["cognitive_decision_asset_type"] = insight.asset_type
        fm["automation_skill_derivative_allowed"] = bool(insight.automation_derivative_allowed)
        fm["flywheel_marked_at"] = datetime.now().isoformat()[:19]

        try:
            new_content = write_frontmatter(fm, body)
        except (ImportError, NameError):
            # 回退：直接 YAML dump
            import yaml

            fm_text = yaml.safe_dump(
                fm, allow_unicode=True, sort_keys=False, default_flow_style=False
            ).strip()
            new_content = f"---\n{fm_text}\n---\n\n{body}"
        trusted = self._submit_wiki_mutation(
            page_path,
            new_content,
            source="ixion_flywheel",
            proposed_action="mark_cognitive_decision_asset",
            evidence_refs=insight.evidence_refs or [str(page_path)],
            metadata={"asset_id": asset_id, "asset_type": insight.asset_type},
            expected_existing_hash=sha256_text(content),
        )
        if trusted.intercepted:
            return {
                "action": (
                    "[mnemos-auto] cognitive_decision_flywheel: "
                    f"已提交页面 `{page_path.name}` 标记 proposal `{trusted.proposal_id}`"
                )
            }
        commit_trusted_markdown(
            trusted,
            target_path=page_path,
            content=new_content,
        )

        asset = CognitiveDecisionAsset(
            asset_id=asset_id,
            title=insight.target,
            asset_type=insight.asset_type,
            decision_context=insight.reason,
            source_refs=[str(page_path)],
            evidence_refs=insight.evidence_refs or [str(page_path)],
            applicability=insight.applicability,
            failure_modes=insight.failure_modes,
            verification_recipe=insight.verification_recipe,
            automation_derivative_allowed=insight.automation_derivative_allowed,
            status="produced",
            confidence=insight.confidence,
            created_at=datetime.now().isoformat()[:19],
        )
        self.create_cognitive_decision_asset(asset)
        if insight.automation_derivative_allowed:
            self._derive_automation_skill_from_asset(asset)

        return {
            "action": f"[mnemos-auto] cognitive_decision_flywheel: 将 `{page_path.name}` 标记为认知决策资产 `{asset_id}`"  # noqa: E501
        }

    def _execute_behavior_to_cognitive_decision(self, insight: FlywheelInsight) -> Dict:
        """执行 behavior → cognitive decision asset：创建资产记录。"""
        asset_id = self._asset_id_from(insight.target, insight.source)
        asset = CognitiveDecisionAsset(
            asset_id=asset_id,
            title=insight.target,
            asset_type=insight.asset_type,
            decision_context=insight.reason,
            source_refs=[insight.source],
            evidence_refs=insight.evidence_refs or [f"task_history:{insight.source}"],
            applicability=insight.applicability or [insight.source],
            failure_modes=insight.failure_modes,
            verification_recipe=insight.verification_recipe,
            automation_derivative_allowed=insight.automation_derivative_allowed,
            status="produced",
            confidence=insight.confidence,
            created_at=datetime.now().isoformat()[:19],
        )
        self.create_cognitive_decision_asset(asset)
        if insight.automation_derivative_allowed:
            self._derive_automation_skill_from_asset(asset)
        return {
            "action": f"[mnemos-auto] cognitive_decision_flywheel: 从行为模式创建认知决策资产 `{asset_id}`"
        }

    def _derive_automation_skill_from_asset(self, asset: CognitiveDecisionAsset) -> None:
        """Derive an automation skill only after the asset explicitly allows it."""
        skill = AutomationSkillRecord(
            skill_name=asset.title.replace("认知决策资产", "自动化助手"),
            description=asset.decision_context,
            trigger_conditions=asset.applicability,
            source_wiki_pages=asset.source_refs,
            status="auto_generated",
            created_at=datetime.now().isoformat()[:19],
            generation_source="cognitive_decision_asset",
        )
        self.create_skill(skill)

    def _execute_skill_to_cognitive_decision(self, insight: FlywheelInsight) -> Dict:
        """执行 skill → wiki 标记：在来源 wiki 页面 frontmatter 中标记 needs_review。"""
        # 查找与该 skill 相关的 wiki 页面
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT source_wiki_pages FROM skills WHERE skill_name=?", (insight.source,)
            ).fetchall()

        marked = False
        for row in rows:
            pages = json.loads(row["source_wiki_pages"] or "[]")
            for page_str in pages:
                page_path = Path(page_str)
                if not page_path.exists():
                    continue
                content = page_path.read_text(encoding="utf-8")
                try:
                    from core.frontmatter import parse_frontmatter, write_frontmatter

                    fm, body = parse_frontmatter(content)
                    if fm is None:
                        fm = {}
                except (
                    OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError,
                    sqlite3.Error
                ):
                    fm, body = {}, content

                fm["needs_review"] = True
                fm["review_reason"] = insight.reason[:200]
                fm["flywheel_review_at"] = datetime.now().isoformat()[:19]
                try:
                    new_content = write_frontmatter(fm, body)
                except (ImportError, NameError):
                    import yaml

                    fm_text = yaml.safe_dump(
                        fm, allow_unicode=True, sort_keys=False, default_flow_style=False
                    ).strip()
                    new_content = f"---\n{fm_text}\n---\n\n{body}"
                trusted = self._submit_wiki_mutation(
                    page_path,
                    new_content,
                    source="ixion_flywheel",
                    proposed_action="mark_skill_source_review",
                    evidence_refs=insight.evidence_refs or [str(page_path)],
                    metadata={"skill": insight.source, "reason": insight.reason[:200]},
                    expected_existing_hash=sha256_text(content),
                )
                if not trusted.intercepted:
                    commit_trusted_markdown(
                        trusted,
                        target_path=page_path,
                        content=new_content,
                    )
                marked = True

        if marked:
            return {
                "action": f"[mnemos-auto] flywheel: 标记 skill `{insight.source}` 来源页面需复审"
            }
        return {}

    def _submit_wiki_mutation(
        self,
        page_path: Path,
        content: str,
        *,
        source: str,
        proposed_action: str,
        evidence_refs: List[str],
        metadata: Dict[str, Any],
        expected_existing_hash: str | None = None,
    ):
        material_action = authorize_exact_markdown_action(
            policy=IXION_MARKDOWN_POLICY,
            wiki_base=self.wiki_base,
            target_path=page_path,
            content=content,
            proposed_action=proposed_action,
            expected_existing_hash=expected_existing_hash,
            source_facts={
                "schema_version": "mnemos.ixion_wiki_mutation_facts.v1",
                "source": source,
                "proposed_action": proposed_action,
                "metadata": dict(metadata),
            },
            evidence_refs=evidence_refs,
            task=f"Apply Ixion Wiki action {proposed_action} to {page_path.name}",
            goal="Apply only the exact Wiki lifecycle mutation selected by Ixion.",
            created_at=_utc_now_iso(),
        )
        return TrustedVaultMutationService(wiki_base=self.wiki_base).submit_markdown(
            target_path=page_path,
            content=content,
            source=source,
            actor="system",
            evidence_refs=evidence_refs,
            proposed_action=proposed_action,
            expected_existing_hash=expected_existing_hash,
            metadata=metadata,
            material_action=material_action,
        )

    def write_report(self, results: Dict) -> Optional[Path]:
        """生成飞轮报告并写入 wiki/06-Retrospectives/flywheel/。"""
        from core.kia.flywheel_report import write_flywheel_report

        write_result = write_flywheel_report(
            wiki_base=self.wiki_base,
            db_path=self.db_path,
            results=results,
            render_cycle=self.generate_cycle_report,
        )
        self._last_report_write_result = write_result
        return write_result.report_path

    def _git_commit_changes(self, report_path: Optional[Path], actions: List[str]) -> bool:
        """Git 自动留痕：对 wiki 目录的变更执行 git commit。"""
        wiki_git = self.wiki_base / ".git"
        if not wiki_git.exists():
            return False

        import subprocess

        try:
            # 检查是否有变更
            result = subprocess.run(
                ["git", "-C", str(self.wiki_base), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=RESULT_SECONDS,
            )
            if not result.stdout.strip():
                return False

            # 添加所有变更
            subprocess.run(
                ["git", "-C", str(self.wiki_base), "add", "-A"],
                capture_output=True,
                timeout=TIMEOUT_SECONDS,
                check=False,
            )

            # 构建 commit message
            if actions:
                msg_body = "\n".join(f"- {a}" for a in actions[:10])
                if len(actions) > 10:
                    msg_body += f"\n- ... 等共 {len(actions)} 项操作"
                commit_msg = f"[mnemos-auto] flywheel: 执行 {len(actions)} 项自动操作\n\n{msg_body}"
            else:
                commit_msg = "[mnemos-auto] flywheel: 生成周期报告"

            subprocess.run(
                ["git", "-C", str(self.wiki_base), "commit", "-m", commit_msg],
                capture_output=True,
                timeout=TIMEOUT_SECONDS,
                check=False,
            )
            return True
        # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            return False

    # ========== 辅助方法 ==========

    @staticmethod
    def _extract_frontmatter(content: str) -> Dict:
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    import yaml

                    return yaml.safe_load(parts[1]) or {}
                except ImportError as e:
                    logger.warning("忽略异常: %s", e, exc_info=True)
        return {}

    @staticmethod
    def _extract_body(content: str) -> str:
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                return parts[2]
        return content

    @staticmethod
    def _extract_title(content: str) -> str:
        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        return match.group(1).strip() if match else ""

    # ========== 画像驱动便捷查询 ==========

    def update_persona(self, persona: PreferenceProfile, blindspot: BlindSpotProfile | None = None):
        """更新画像（允许运行时动态更新）"""
        if PERSONA_AVAILABLE:
            self.persona_engine = PersonaDrivenSkillEngine(persona, blindspot)


def run_persona_driven_flywheel(
    persona: PreferenceProfile | None = None,
    blindspot: BlindSpotProfile | None = None,
    wiki_base: str | None = None,
) -> str:
    """Run a persona-driven cycle and return its report."""
    return _run_persona_driven_flywheel(
        persona=persona,
        blindspot=blindspot,
        wiki_base=wiki_base,
        flywheel_factory=CognitiveDecisionFlywheel,
    )


def run_flywheel(
    wiki_base: str | None = None,
    persona: PreferenceProfile | None = None,
    blindspot: BlindSpotProfile | None = None,
    report: bool = False,
) -> Dict | str:
    """Run one flywheel cycle through the current patchable public facade."""
    return _run_flywheel(
        wiki_base=wiki_base,
        persona=persona,
        blindspot=blindspot,
        report=report,
        flywheel_factory=CognitiveDecisionFlywheel,
        persona_report_runner=run_persona_driven_flywheel,
    )
