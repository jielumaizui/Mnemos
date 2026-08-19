"""Persona-driven skill planning and its persisted value objects."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, cast

PERSONA_DRIVEN_SKILL_ENGINE_ENERGY_TO_FLYWHEEL_PARAMS_DAYS = 7
PARAMS_DAYS = 7

try:
    from core.cognitive.user_model_assets import UserCognitiveBlindspot
    from core.persona.pythia import CognitiveProfile, PreferenceProfile
    from core.persona.hamartia import BlindSpotProfile

    PERSONA_AVAILABLE = True
except ImportError:
    PERSONA_AVAILABLE = False


@dataclass
class AutomationSkillRecord:
    """Persist one approved automation derivative of a decision asset."""

    skill_name: str
    description: str = ""
    trigger_conditions: List[str] = field(default_factory=list)  # 触发条件
    input_template: str = ""  # 输入模板
    expected_output: str = ""  # 预期输出
    source_wiki_pages: List[str] = field(default_factory=list)  # 来源 Wiki
    usage_count: int = 0  # 总使用次数
    success_count: int = 0  # 成功次数
    failure_count: int = 0  # 失败次数
    status: str = "proposed"  # proposed / auto_generated / active / stale / deprecated
    created_at: str = ""
    updated_at: str = ""
    version: int = 1
    generation_source: str = ""  # behavior / static / manual
    last_used: str = ""
    created_by: str = ""
    parent_version: int = 0
    deviation_log: List[Dict] = field(default_factory=list)


@dataclass
class SkillUsageLog:
    """Skill 使用日志"""

    log_id: int = 0
    skill_name: str = ""
    timestamp: str = ""
    input_data: str = ""  # 输入数据摘要
    output_data: str = ""  # 输出数据摘要
    status: str = ""  # success / failure / partial
    exception_type: str = ""  # 异常类型
    exception_detail: str = ""  # 异常详情
    new_scenario: bool = False  # 是否遇到新场景
    user_marked: bool = False  # 用户是否标记为"应记录"
    generated_wiki: str = ""  # 是否生成了 Wiki 页面（路径）


@dataclass
class PersonaSkillGap:
    """基于能力雷达的技能缺口"""

    dimension: str  # 雷达维度名
    current_score: float  # 当前得分 0-1
    target_score: float  # 目标得分 0-1
    gap_severity: str  # critical/high/medium/low
    recommended_skill_category: str  # 建议补充的技能类别
    rationale: str  # 判断理由
    related_wiki_pages: List[str] = field(default_factory=list)


@dataclass
class SkillPath:
    """个性化学习路径"""

    path_id: str
    title: str
    description: str
    stages: List[Dict]  # 阶段列表
    cognitive_style: str  # 匹配的认知风格
    estimated_duration: str  # 预估时长
    priority: str  # high/medium/low


@dataclass
class SkillVerificationTask:
    """技能验证任务（盲区驱动）"""

    task_id: str
    task_type: str  # framing_challenge / option_gap_probe / temporal_check / preference_test
    description: str
    related_skill: str
    related_blindspot_type: str
    verification_method: str  # 验证方式描述
    expected_outcome: str
    status: str = "pending"  # pending / completed / dismissed


class PersonaDrivenSkillEngine:
    """
    画像驱动的技能引擎。

    将用户画像三层雷达转化为可执行的技能策略：
    - 能力短板 → 技能缺口推荐
    - 认知模式 → 学习路径风格
    - 价值优先级 → 任务生成策略
    - 能量模式 → 飞轮调速
    - 盲区画像 → 验证闭环
    """

    # 认知维度 → 技能类别映射
    COGNITIVE_TO_SKILL_CATEGORY = {
        "abstraction": {
            "low": ["模式识别", "案例归纳", "类比推理"],
            "high": ["原理推导", "形式化建模", "抽象设计"],
        },
        "system_view": {
            "low": ["全局分析", "系统思维", "关联映射"],
            "high": ["细节执行", "组件优化", "精准调试"],
        },
        "skepticism": {
            "low": ["质疑训练", "反例分析", "前提检验"],
            "high": ["框架构建", "共识建立", "信任协作"],
        },
        "creativity": {
            "low": ["创意激发", "头脑风暴", "跨界联想"],
            "high": ["优化落地", "流程固化", "效率提升"],
        },
        "deduction": {
            "low": ["逻辑推演", "公理化方法", "形式证明"],
            "high": ["经验总结", "模式提取", "案例学习"],
        },
    }

    # 能量模式 → 飞轮调速参数
    ENERGY_TO_FLYWHEEL_PARAMS = {
        "burst": {  # 爆发型
            "cycle_days": 3,
            "batch_size": 3,
            "intensity": "high",
            "max_parallel": 2,
        },
        "steady": {  # 匀速型
            "cycle_days": PERSONA_DRIVEN_SKILL_ENGINE_ENERGY_TO_FLYWHEEL_PARAMS_DAYS,
            "batch_size": 5,
            "intensity": "medium",
            "max_parallel": 3,
        },
        "mixed": {  # 混合型
            "cycle_days": 5,
            "batch_size": 4,
            "intensity": "adaptive",
            "max_parallel": 2,
        },
    }

    def __init__(
        self, persona: PreferenceProfile | None = None, blindspot: BlindSpotProfile | None = None
    ):
        self.persona = persona
        self.blindspot = blindspot

    def analyze_skill_gaps(self) -> List[PersonaSkillGap]:
        """
        分析能力雷达中的短板，生成技能缺口建议。

        策略：
        - 得分 < 0.3 的维度为 critical gap
        - 得分 0.3-0.4 的维度为 high gap
        - 同时参考认知维度的置信度（低置信度时降低gap severity）
        """
        if not self.persona or not PERSONA_AVAILABLE:
            return []

        gaps = []
        cognitive = self.persona.cognitive
        confidence_scale = cognitive.confidence  # 整体置信度作为缩放因子

        dimensions = [
            ("abstraction", cognitive.abstraction),
            ("system_view", cognitive.system_view),
            ("skepticism", cognitive.skepticism),
            ("creativity", cognitive.creativity),
            ("deduction", cognitive.deduction),
        ]

        for dim_name, score in dimensions:
            # 根据得分确定缺口严重程度
            if score < 0.3:
                severity = "critical"
                target = 0.5
            elif score < 0.4:
                severity = "high"
                target = 0.5
            elif score < 0.45:
                severity = "medium"
                target = 0.5
            else:
                continue  # 不是短板

            # 低置信度时降级（可能是数据不足导致的误判）
            if confidence_scale < 0.4:
                severity = {"critical": "high", "high": "medium", "medium": "low"}.get(
                    severity, severity
                )

            mapping = self.COGNITIVE_TO_SKILL_CATEGORY.get(dim_name, {})
            category = mapping.get("low" if score < 0.5 else "high", ["通用技能"])

            # 中文维度名映射
            dim_labels = {
                "abstraction": "抽象↔具象",
                "system_view": "系统↔单点",
                "skepticism": "质疑↔信任",
                "creativity": "创造↔优化",
                "deduction": "演绎↔归纳",
            }

            gaps.append(
                PersonaSkillGap(
                    dimension=dim_labels.get(dim_name, dim_name),
                    current_score=round(score, 2),
                    target_score=target,
                    gap_severity=severity,
                    recommended_skill_category=category[0],
                    rationale=f"{dim_labels.get(dim_name, dim_name)}得分{score:.2f}，"
                    f"属于{'低分端' if score < 0.5 else '极端'}，建议补充「{category[0]}」类技能",
                )
            )

        # 按严重程度排序
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        gaps.sort(key=lambda x: severity_order.get(x.gap_severity, 99))
        return gaps

    def generate_skill_paths(self, gaps: List[PersonaSkillGap] | None = None) -> List[SkillPath]:
        """
        根据认知模式生成个性化学习路径。

        路径风格：
        - 演绎型(deduction>0.6): 原理→应用→案例
        - 归纳型(deduction<0.4): 案例→模式→原理
        - 抽象型(abstraction>0.6): 概念框架→工具→实践
        - 具象型(abstraction<0.4): 具体工具→实践→原理
        - 系统视角(system_view>0.6): 全局结构→组件→关联
        """
        if not self.persona or not PERSONA_AVAILABLE:
            return []

        paths = []
        cognitive = self.persona.cognitive
        gaps = gaps or self.analyze_skill_gaps()

        for gap in gaps[:3]:  # 只为前3个缺口生成路径
            path = self._build_path_for_gap(gap, cognitive)
            if path:
                paths.append(path)

        return paths

    def _build_path_for_gap(
        self, gap: PersonaSkillGap, cognitive: CognitiveProfile
    ) -> Optional[SkillPath]:
        """为单个缺口构建学习路径"""

        # 根据认知模式决定路径风格
        if cognitive.deduction > 0.6:
            style = "deductive"
            stage_template = [
                {"name": "理解原理", "type": "theory", "description": "先理解底层原理和规则"},
                {
                    "name": "推导应用",
                    "type": "application",
                    "description": "用原理推导具体应用场景",
                },
                {"name": "案例验证", "type": "practice", "description": "通过案例验证理解"},
            ]
        elif cognitive.deduction < 0.4:
            style = "inductive"
            stage_template = [
                {"name": "接触案例", "type": "case", "description": "先看足够多的具体案例"},
                {"name": "提取模式", "type": "pattern", "description": "从案例中总结共性模式"},
                {"name": "升华原理", "type": "theory", "description": "将模式上升为可迁移的原理"},
            ]
        else:
            style = "balanced"
            stage_template = [
                {"name": "概览", "type": "overview", "description": "快速了解全貌"},
                {"name": "深入", "type": "deep", "description": "选择感兴趣的部分深入"},
                {"name": "整合", "type": "integrate", "description": "将碎片整合为系统"},
            ]

        # 根据抽象偏好调整
        if cognitive.abstraction > 0.6 and style != "deductive":
            # 抽象型：在每个阶段前加"概念框架"环节
            stage_template.insert(
                0, {"name": "概念框架", "type": "framework", "description": "先建立整体概念框架"}
            )
        elif cognitive.abstraction < 0.4 and style != "inductive":
            # 具象型：在每个阶段强调"动手"
            for stage in stage_template:
                stage["description"] = stage["description"].replace("理解", "通过动手理解")
                stage["description"] = stage["description"].replace("先看", "先动手做")

        return SkillPath(
            path_id=f"path_{gap.dimension.replace('↔', '_')}_{datetime.now().strftime('%Y%m%d')}",
            title=f"提升{gap.dimension}能力",
            description=gap.rationale,
            stages=stage_template,
            cognitive_style=style,
            estimated_duration="2-4周" if gap.gap_severity == "critical" else "1-2周",
            priority=gap.gap_severity,
        )

    def generate_tasks_by_values(self, skills: List[str]) -> List[Dict]:
        """
        根据价值优先级为Skill生成个性化任务。

        策略矩阵：
        - 正确性>效率：每个任务增加验证步骤
        - 深度>广度：每个skill深入一个具体场景
        - 完美>完成：任务要产出完整可交付物
        - 创新>稳妥：任务要尝试至少一个新方法
        - 自主>协作：任务要独立设计解决方案
        """
        if not self.persona or not PERSONA_AVAILABLE:
            return []

        value = self.persona.value
        tasks = []

        for skill in skills[:3]:
            task = {"skill": skill, "base_task": f"实践并掌握 {skill}"}

            # 正确性↔效率
            if value.correctness_vs_efficiency > 0.6:
                task["validation_step"] = "完成后需通过至少两种独立方式验证正确性"
                task["time_constraint"] = "无硬性时间限制，确保正确"
            elif value.correctness_vs_efficiency < 0.4:
                task["validation_step"] = "快速验证核心逻辑即可"
                task["time_constraint"] = "限时完成，优先交付"
            else:
                task["validation_step"] = "常规验证"
                task["time_constraint"] = "合理时间内完成"

            # 深度↔广度
            if value.depth_vs_breadth > 0.6:
                task["scope"] = "选择一个具体场景，深入挖掘所有细节和边界情况"
            elif value.depth_vs_breadth < 0.4:
                task["scope"] = "覆盖3-5个不同场景，建立全貌认知"
            else:
                task["scope"] = "先建立全貌，再选一个场景深入"

            # 完美↔完成
            if value.perfection_vs_completion > 0.6:
                task["deliverable"] = "完整的、可直接使用的交付物，含文档和测试"
            elif value.perfection_vs_completion < 0.4:
                task["deliverable"] = "最小可用版本，先跑通再完善"
            else:
                task["deliverable"] = "完整功能实现，文档可后续补充"

            # 创新↔稳妥
            if value.innovation_vs_safety > 0.6:
                task["method_constraint"] = "至少尝试一种你没用过的方法或工具"
            elif value.innovation_vs_safety < 0.4:
                task["method_constraint"] = "使用已验证的方法，避免引入不确定因素"
            else:
                task["method_constraint"] = "主路径用稳妥方法，可在一个子任务中尝试新方案"

            # 自主↔协作
            if value.autonomy_vs_collaboration > 0.6:
                task["collaboration"] = "独立设计并实施，完成后可寻求反馈"
            elif value.autonomy_vs_collaboration < 0.4:
                task["collaboration"] = "先与他人讨论方案，再执行"
            else:
                task["collaboration"] = "独立设计，执行中遇到问题及时讨论"

            tasks.append(task)

        return tasks

    def get_flywheel_params(self) -> Dict:
        """
        根据能量模式获取飞轮调速参数。

        Returns:
            {cycle_days, batch_size, intensity, max_parallel}
        """
        if not self.persona or not PERSONA_AVAILABLE:
            return self.ENERGY_TO_FLYWHEEL_PARAMS["mixed"]

        energy = self.persona.energy

        # 主要根据续航模式判断
        if energy.endurance_mode < 0.4:
            base = "burst"
        elif energy.endurance_mode > 0.6:
            base = "steady"
        else:
            base = "mixed"

        params = cast(Dict[str, Any], self.ENERGY_TO_FLYWHEEL_PARAMS[base].copy())

        # 启动难度修正
        if energy.startup_difficulty > 0.6:
            params["cycle_days"] = max(PARAMS_DAYS, params["cycle_days"] + 2)
            params["batch_size"] = min(8, params["batch_size"] + 2)

        # 切换弹性修正
        if energy.switching_flexibility < 0.4:
            params["max_parallel"] = 1

        return params

    def generate_verification_tasks(self, skills: List[str]) -> List[SkillVerificationTask]:
        """
        基于盲区画像生成技能验证任务。

        盲区类型 → 验证任务：
        - framing: 设计一个"反向假设"验证任务
        - option_gap: 设计一个"第三选项"探索任务
        - temporal: 设计一个"长期影响"评估任务
        - preference_rigidity: 设计一个"情境适配"测试任务
        """
        if not self.blindspot or not PERSONA_AVAILABLE:
            return []

        tasks = []
        confirmed = self.blindspot.confirmed
        suspected = self.blindspot.suspected

        # 优先为已确认盲区生成验证任务
        for bs in confirmed[:2]:
            for skill in skills[:2]:
                task = self._build_verification_task(bs, skill)
                if task:
                    tasks.append(task)

        # 如果已确认盲区不足，从 suspected 补充
        if len(tasks) < 2:
            for bs in suspected[:2]:
                if len(tasks) >= 2:
                    break
                for skill in skills[:1]:
                    task = self._build_verification_task(bs, skill)
                    if task:
                        tasks.append(task)

        return tasks

    def _build_verification_task(
        self, blindspot: UserCognitiveBlindspot, skill: str
    ) -> Optional[SkillVerificationTask]:
        """为单个盲区和技能构建验证任务"""

        task_templates = {
            "framing": {
                "description": f"在使用{skill}时，故意假设相反的前提，看是否也能得到合理的结果",
                "method": "写下你默认使用{skill}的前提假设，然后尝试否定它，看看否定前提下的方案",
                "outcome": "发现至少一个反向假设下的可行方案，或确认原前提的必要性",
            },
            "option_gap": {
                "description": f"用{skill}解决问题时，强制要求自己给出第3个选项",
                "method": '先列出你的前两个自然想到的方案，然后问自己"如果这两个都不行，还有吗？"',
                "outcome": "发现至少一个之前未考虑的替代方案",
            },
            "temporal": {
                "description": f"用{skill}完成当前任务后，评估6个月后的维护成本",
                "method": "写下当前方案的短期收益，再列出6个月后可能出现的问题和维护成本",
                "outcome": "对长期影响有清晰认知，或在方案中加入长期优化措施",
            },
            "preference_rigidity": {
                "description": f"这次使用{skill}时，故意选择与习惯相反的方式",
                "method": "识别你使用{skill}的默认习惯，本次任务中强制采用另一种方式",
                "outcome": "体验到不同方式的效果差异，或确认习惯的合理性",
            },
        }

        template = task_templates.get(blindspot.type)
        if not template:
            return None

        return SkillVerificationTask(
            task_id=f"verify_{blindspot.type}_{skill.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}",  # noqa: E501
            task_type=f"{blindspot.type}_challenge",
            description=template["description"],
            related_skill=skill,
            related_blindspot_type=blindspot.type,
            verification_method=template["method"],
            expected_outcome=template["outcome"],
        )

    def format_persona_insights(
        self,
        gaps: List[PersonaSkillGap],
        paths: List[SkillPath],
        tasks: List[Dict],
        verifications: List[SkillVerificationTask],
    ) -> str:
        """格式化画像驱动洞察为文本报告"""
        lines = ["## 画像驱动洞察", ""]

        # 技能缺口
        if gaps:
            lines.append(f"### 能力短板识别 ({len(gaps)} 个)")
            severity_marks = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
            for gap in gaps[:5]:
                mark = severity_marks.get(gap.gap_severity, "⚪")
                lines.append(
                    f"{mark} **{gap.dimension}**: {gap.current_score:.2f} → {gap.target_score:.2f}"
                )
                lines.append(f"   建议: {gap.recommended_skill_category}")
                lines.append(f"   理由: {gap.rationale}")
                lines.append("")
        else:
            lines.append("### 能力短板识别")
            lines.append("当前无显著能力短板，或画像数据不足。")
            lines.append("")

        # 学习路径
        if paths:
            lines.append(f"### 个性化学习路径 ({len(paths)} 条)")
            for path in paths:
                lines.append(
                    f"**{path.title}** (风格: {path.cognitive_style}, 预估: {path.estimated_duration})"
                )
                for i, stage in enumerate(path.stages, 1):
                    lines.append(f"  {i}. {stage['name']}: {stage['description']}")
                lines.append("")
        else:
            lines.append("### 个性化学习路径")
            lines.append("暂无推荐路径。")
            lines.append("")

        # 任务策略
        if tasks:
            lines.append(f"### 任务生成策略 ({len(tasks)} 个技能)")
            for task in tasks[:3]:
                lines.append(f"**{task['skill']}**")
                lines.append(f"  - 范围: {task['scope']}")
                lines.append(f"  - 交付: {task['deliverable']}")
                lines.append(f"  - 方法: {task['method_constraint']}")
                lines.append(f"  - 验证: {task['validation_step']}")
                lines.append("")
        else:
            lines.append("### 任务生成策略")
            lines.append("暂无任务策略。")
            lines.append("")

        # 验证任务
        if verifications:
            lines.append(f"### 盲区验证任务 ({len(verifications)} 个)")
            for v in verifications[:3]:
                lines.append(f"**{v.related_skill} - {v.related_blindspot_type}**")
                lines.append(f"  任务: {v.description}")
                lines.append(f"  方法: {v.verification_method}")
                lines.append(f"  预期: {v.expected_outcome}")
                lines.append("")
        else:
            lines.append("### 盲区验证任务")
            lines.append("暂无验证任务。")
            lines.append("")

        return "\n".join(lines)
