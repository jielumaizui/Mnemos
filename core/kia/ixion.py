"""
Skill-Wiki Flywheel - Skill 与 Wiki 双向演化飞轮

核心理念：
- Wiki 知识沉淀到一定成熟度 → 建议自动化为 Skill
- Skill 执行中遇到异常/新场景 → 反向沉淀为 Wiki 知识
- 形成正反馈：知识越多 → Skill 越多 → 反馈越多 → 新知识越多

方向1：Wiki → Skill
- 触发信号：知识被反复使用、类型为方法论/经验法则、场景明确可复用
- 输出：Skill 建议报告（含触发条件、输入模板、预期输出）

方向2：Skill → Wiki
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
- 不自动执行转化，只生成建议供用户决策
- 追踪完整的 Skill-Wiki 关系图谱
"""
# Ixion — 伊克西翁 — 技能-Wiki 飞轮，永远在旋转之火轮上
# 原模块: skill_wiki_flywheel.py



import json
import sqlite3
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from core.config import get_config
import logging

logger = logging.getLogger(__name__)

# 用户画像驱动（可选依赖）
try:
    from core.persona.pythia import (
        PreferenceProfile, EnergyProfile, CognitiveProfile, ValueProfile
    )
    from core.persona.hamartia import (
        BlindSpotProfile, BlindSpot, BlindSpotProfileManager, ChallengeBalancer
    )
    PERSONA_AVAILABLE = True
except ImportError:
    PERSONA_AVAILABLE = False


# ========== 数据模型 ==========

@dataclass
class SkillRecord:
    """Skill 记录"""
    skill_name: str
    description: str = ""
    trigger_conditions: List[str] = field(default_factory=list)  # 触发条件
    input_template: str = ""        # 输入模板
    expected_output: str = ""       # 预期输出
    source_wiki_pages: List[str] = field(default_factory=list)  # 来源 Wiki
    usage_count: int = 0            # 总使用次数
    success_count: int = 0          # 成功次数
    failure_count: int = 0          # 失败次数
    status: str = "proposed"        # proposed / active / deprecated
    created_at: str = ""
    updated_at: str = ""


@dataclass
class SkillUsageLog:
    """Skill 使用日志"""
    log_id: int = 0
    skill_name: str = ""
    timestamp: str = ""
    input_data: str = ""            # 输入数据摘要
    output_data: str = ""           # 输出数据摘要
    status: str = ""                # success / failure / partial
    exception_type: str = ""        # 异常类型
    exception_detail: str = ""      # 异常详情
    new_scenario: bool = False      # 是否遇到新场景
    user_marked: bool = False       # 用户是否标记为"应记录"
    generated_wiki: str = ""        # 是否生成了 Wiki 页面（路径）


@dataclass
class FlywheelInsight:
    """飞轮洞察"""
    direction: str                  # wiki_to_skill / skill_to_wiki
    source: str                     # 来源（Wiki 路径 或 Skill 名称）
    target: str                     # 目标建议
    confidence: float               # 置信度
    reason: str                     # 判断理由
    suggested_action: str = ""      # 建议动作
    auto_applicable: bool = False   # 是否可自动应用


# ========== 画像驱动数据模型 ==========

@dataclass
class PersonaSkillGap:
    """基于能力雷达的技能缺口"""
    dimension: str                  # 雷达维度名
    current_score: float            # 当前得分 0-1
    target_score: float             # 目标得分 0-1
    gap_severity: str               # critical/high/medium/low
    recommended_skill_category: str  # 建议补充的技能类别
    rationale: str                  # 判断理由
    related_wiki_pages: List[str] = field(default_factory=list)


@dataclass
class SkillPath:
    """个性化学习路径"""
    path_id: str
    title: str
    description: str
    stages: List[Dict]              # 阶段列表
    cognitive_style: str            # 匹配的认知风格
    estimated_duration: str         # 预估时长
    priority: str                   # high/medium/low


@dataclass
class SkillVerificationTask:
    """技能验证任务（盲区驱动）"""
    task_id: str
    task_type: str                  # framing_challenge / option_gap_probe / temporal_check / preference_test
    description: str
    related_skill: str
    related_blindspot_type: str
    verification_method: str        # 验证方式描述
    expected_outcome: str
    status: str = "pending"         # pending / completed / dismissed


# ========== 画像驱动引擎 ==========

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
        "burst": {          # 爆发型
            "cycle_days": 3,
            "batch_size": 3,
            "intensity": "high",
            "max_parallel": 2,
        },
        "steady": {         # 匀速型
            "cycle_days": 7,
            "batch_size": 5,
            "intensity": "medium",
            "max_parallel": 3,
        },
        "mixed": {          # 混合型
            "cycle_days": 5,
            "batch_size": 4,
            "intensity": "adaptive",
            "max_parallel": 2,
        },
    }

    def __init__(self, persona: PreferenceProfile = None,
                 blindspot: BlindSpotProfile = None):
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
                severity = {"critical": "high", "high": "medium", "medium": "low"}.get(severity, severity)

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

            gaps.append(PersonaSkillGap(
                dimension=dim_labels.get(dim_name, dim_name),
                current_score=round(score, 2),
                target_score=target,
                gap_severity=severity,
                recommended_skill_category=category[0],
                rationale=f"{dim_labels.get(dim_name, dim_name)}得分{score:.2f}，"
                          f"属于{'低分端' if score < 0.5 else '极端'}，建议补充「{category[0]}」类技能",
            ))

        # 按严重程度排序
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        gaps.sort(key=lambda x: severity_order.get(x.gap_severity, 99))
        return gaps

    def generate_skill_paths(self, gaps: List[PersonaSkillGap] = None) -> List[SkillPath]:
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

    def _build_path_for_gap(self, gap: PersonaSkillGap,
                            cognitive: CognitiveProfile) -> Optional[SkillPath]:
        """为单个缺口构建学习路径"""

        # 根据认知模式决定路径风格
        if cognitive.deduction > 0.6:
            style = "deductive"
            stage_template = [
                {"name": "理解原理", "type": "theory", "description": "先理解底层原理和规则"},
                {"name": "推导应用", "type": "application", "description": "用原理推导具体应用场景"},
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
            stage_template.insert(0, {"name": "概念框架", "type": "framework",
                                      "description": "先建立整体概念框架"})
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

        params = self.ENERGY_TO_FLYWHEEL_PARAMS[base].copy()

        # 启动难度修正
        if energy.startup_difficulty > 0.6:
            params["cycle_days"] = max(7, params["cycle_days"] + 2)
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

    def _build_verification_task(self, blindspot: BlindSpot,
                                  skill: str) -> Optional[SkillVerificationTask]:
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
            task_id=f"verify_{blindspot.type}_{skill.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}",
            task_type=f"{blindspot.type}_challenge",
            description=template["description"],
            related_skill=skill,
            related_blindspot_type=blindspot.type,
            verification_method=template["method"],
            expected_outcome=template["outcome"],
        )

    def format_persona_insights(self, gaps: List[PersonaSkillGap],
                                 paths: List[SkillPath],
                                 tasks: List[Dict],
                                 verifications: List[SkillVerificationTask]) -> str:
        """格式化画像驱动洞察为文本报告"""
        lines = ["## 画像驱动洞察", ""]

        # 技能缺口
        if gaps:
            lines.append(f"### 能力短板识别 ({len(gaps)} 个)")
            severity_marks = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
            for gap in gaps[:5]:
                mark = severity_marks.get(gap.gap_severity, "⚪")
                lines.append(f"{mark} **{gap.dimension}**: {gap.current_score:.2f} → {gap.target_score:.2f}")
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
                lines.append(f"**{path.title}** (风格: {path.cognitive_style}, 预估: {path.estimated_duration})")
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


# ========== 飞轮管理器 ==========

class SkillWikiFlywheel:
    """Skill-Wiki 双向飞轮"""

    # Wiki → Skill 的触发阈值
    WIKI_TO_SKILL_SIGNALS = {
        "min_usage_count": 5,           # 最少使用次数
        "min_age_days": 7,              # 知识沉淀至少7天
        "preferred_forms": ["方法论", "经验法则", "决策记录"],
        "trigger_keywords": ["步骤", "流程", "指南", "模板", "检查清单", "脚本"],
        "min_confidence": 0.6,
    }

    # Skill → Wiki 的触发阈值
    SKILL_TO_WIKI_SIGNALS = {
        "failure_rate_threshold": 0.3,   # 失败率超过30%触发
        "new_scenario_threshold": 3,     # 同一新场景出现3次
        "exception_threshold": 2,        # 同类异常出现2次
    }

    def __init__(self, wiki_base: str = None, db_path: str = None,
                 persona: PreferenceProfile = None,
                 blindspot: BlindSpotProfile = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else (
            get_config().wiki_dir
        )
        self.db_path = Path(db_path) if db_path else (
            self.wiki_base / ".kg" / "flywheel.db"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

        # 画像驱动引擎（可选）
        self.persona_engine = None
        if PERSONA_AVAILABLE:
            self.persona_engine = PersonaDrivenSkillEngine(persona, blindspot)
        elif persona or blindspot:
            # 用户传了画像但依赖不可用
            pass

    def _init_db(self):
        """初始化数据库"""
        schema = """
        CREATE TABLE IF NOT EXISTS skills (
            skill_name TEXT PRIMARY KEY,
            description TEXT,
            trigger_conditions TEXT,      -- JSON
            input_template TEXT,
            expected_output TEXT,
            source_wiki_pages TEXT,       -- JSON
            usage_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'proposed',
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS skill_usage_logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT,
            timestamp TEXT,
            input_data TEXT,
            output_data TEXT,
            status TEXT,
            exception_type TEXT,
            exception_detail TEXT,
            new_scenario BOOLEAN DEFAULT 0,
            user_marked BOOLEAN DEFAULT 0,
            generated_wiki TEXT,
            FOREIGN KEY (skill_name) REFERENCES skills(skill_name)
        );

        CREATE TABLE IF NOT EXISTS wiki_usage_logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_path TEXT,
            timestamp TEXT,
            access_type TEXT,             -- read / quote / modify / share
            context TEXT                  -- 使用上下文
        );

        CREATE INDEX IF NOT EXISTS idx_skill_usage ON skill_usage_logs(skill_name, timestamp);
        CREATE INDEX IF NOT EXISTS idx_wiki_usage ON wiki_usage_logs(page_path, timestamp);

        -- 画像驱动相关表
        CREATE TABLE IF NOT EXISTS skill_paths (
            path_id TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            stages TEXT,              -- JSON
            cognitive_style TEXT,
            estimated_duration TEXT,
            priority TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS skill_verification_tasks (
            task_id TEXT PRIMARY KEY,
            task_type TEXT,
            description TEXT,
            related_skill TEXT,
            related_blindspot_type TEXT,
            verification_method TEXT,
            expected_outcome TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS persona_flywheel_logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_date TEXT,
            persona_version INTEGER,
            gaps_detected INTEGER,
            paths_created INTEGER,
            verifications_created INTEGER,
            flywheel_params TEXT       -- JSON
        );
        """
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.executescript(schema)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ========== 方向1：Wiki → Skill ==========

    def analyze_wiki_for_skill(self, page_path: Path) -> Optional[FlywheelInsight]:
        """
        分析单个 Wiki 页面，判断是否适合沉淀为 Skill

        返回 Skill 建议或 None
        """
        if not page_path.exists():
            return None

        try:
            content = page_path.read_text(encoding="utf-8")
            fm = self._extract_frontmatter(content)
            body = self._extract_body(content)
        except Exception:
            return None

        signals = []
        confidence = 0.0

        # 信号1：知识类型
        form = fm.get("类型", "")
        if form in self.WIKI_TO_SKILL_SIGNALS["preferred_forms"]:
            signals.append(f"类型为'{form}'，适合自动化")
            confidence += 0.25

        # 信号2：标题关键词
        title = self._extract_title(content) or page_path.stem
        for kw in self.WIKI_TO_SKILL_SIGNALS["trigger_keywords"]:
            if kw in title:
                signals.append(f"标题包含'{kw}'，暗示可流程化")
                confidence += 0.15
                break

        # 信号3：内容结构（是否有步骤/流程）
        if re.search(r"步骤[一二三四五]|第[一二三四五]步|\d+\.", body):
            signals.append("内容包含步骤化描述")
            confidence += 0.15

        # 信号4：使用频率
        usage = self._get_wiki_usage(str(page_path), days=30)
        if usage >= self.WIKI_TO_SKILL_SIGNALS["min_usage_count"]:
            signals.append(f"近30天被使用{usage}次，需求稳定")
            confidence += 0.20
        elif usage >= 2:
            signals.append(f"近30天被使用{usage}次，有使用迹象")
            confidence += 0.05

        # 信号5：置信度
        page_confidence = float(fm.get("置信度", 0))
        if page_confidence >= self.WIKI_TO_SKILL_SIGNALS["min_confidence"]:
            signals.append(f"置信度{page_confidence}，知识较可靠")
            confidence += 0.15

        # 信号6：适用场景明确
        scenes = fm.get("触发场景", [])
        if scenes and len(scenes) >= 1:
            signals.append(f"触发场景明确: {', '.join(scenes[:2])}")
            confidence += 0.10

        if confidence >= 0.5:
            return FlywheelInsight(
                direction="wiki_to_skill",
                source=str(page_path),
                target=self._suggest_skill_name(title),
                confidence=round(min(confidence, 1.0), 2),
                reason="; ".join(signals),
                suggested_action=self._generate_skill_proposal(page_path, fm, title, body),
            )

        return None

    def scan_wiki_for_skills(self) -> List[FlywheelInsight]:
        """扫描所有 Wiki 页面，找出适合 Skill 化的知识"""
        insights = []
        inbox = self.wiki_base / "00-Inbox"

        if not inbox.exists():
            return insights

        for page in inbox.glob("*.md"):
            insight = self.analyze_wiki_for_skill(page)
            if insight:
                insights.append(insight)

        # 按置信度排序
        insights.sort(key=lambda x: x.confidence, reverse=True)
        return insights

    def _generate_skill_proposal(self, page_path: Path, frontmatter: Dict,
                                  title: str, body: str) -> str:
        """生成 Skill 建议书"""
        scenes = frontmatter.get("触发场景", ["未指定场景"])
        tools = frontmatter.get("关键词", {}).get("工具实体", [])

        proposal = f"""建议将知识自动化为 Skill：

**Skill 名称**: {self._suggest_skill_name(title)}
**来源**: {page_path.name}

**触发条件**:
- {'; '.join(scenes[:3])}

**输入模板**:
- 根据具体场景填充参数

**预期输出**:
- 参照原文"核心内容"章节的结果

**建议实现方式**:
- 如果涉及{', '.join(tools[:2]) if tools else '特定工具'}操作，可以写脚本自动化
- 如果是判断/决策流程，可以用规则引擎
- 如果是内容生成，可以用 Prompt 模板

**注意事项**:
- 原文中的适用边界需要硬编码为前置检查
- 反模式需要作为异常处理逻辑
"""
        return proposal

    def _suggest_skill_name(self, title: str) -> str:
        """基于标题建议 Skill 名称"""
        # 去掉疑问词
        name = re.sub(r"^(为什么|怎么|如何|什么是)", "", title).strip()
        # 加 Skill 后缀
        if not name.endswith("助手"):
            name = name + "助手"
        return name

    # ========== 方向2：Skill → Wiki ==========

    def log_skill_usage(self, skill_name: str, input_data: str = "",
                        output_data: str = "", status: str = "success",
                        exception_type: str = "", exception_detail: str = "",
                        new_scenario: bool = False, user_marked: bool = False) -> int:
        """
        记录 Skill 使用日志

        Returns:
            log_id
        """
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO skill_usage_logs
                   (skill_name, timestamp, input_data, output_data, status,
                    exception_type, exception_detail, new_scenario, user_marked)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (skill_name, datetime.now().isoformat()[:19], input_data[:500],
                 output_data[:500], status, exception_type, exception_detail[:1000],
                 new_scenario, user_marked)
            )
            conn.commit()
            log_id = cursor.lastrowid

        # 更新 Skill 统计
        self._update_skill_stats(skill_name)

        return log_id

    def _update_skill_stats(self, skill_name: str):
        """更新 Skill 使用统计"""
        with self._conn() as conn:
            stats = conn.execute(
                """SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
                    SUM(CASE WHEN status='failure' THEN 1 ELSE 0 END) as failure
                   FROM skill_usage_logs WHERE skill_name=?""",
                (skill_name,)
            ).fetchone()

            conn.execute(
                """UPDATE skills SET usage_count=?, success_count=?, failure_count=?,
                   updated_at=? WHERE skill_name=?""",
                (stats["total"], stats["success"], stats["failure"],
                 datetime.now().isoformat()[:19], skill_name)
            )
            conn.commit()

    def analyze_skill_for_wiki(self, skill_name: str) -> List[FlywheelInsight]:
        """
        分析 Skill 使用日志，判断是否需要反向沉淀为 Wiki 知识

        返回需要生成的 Wiki 知识建议列表
        """
        insights = []

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

            if failure_rate >= self.SKILL_TO_WIKI_SIGNALS["failure_rate_threshold"]:
                insights.append(FlywheelInsight(
                    direction="skill_to_wiki",
                    source=skill_name,
                    target=f"{skill_name} 失败处理指南",
                    confidence=round(min(failure_rate * 2, 1.0), 2),
                    reason=f"失败率 {failure_rate:.0%}，超过阈值 {self.SKILL_TO_WIKI_SIGNALS['failure_rate_threshold']:.0%}",
                    suggested_action="提取常见失败模式，生成反模式 Wiki",
                ))

            # 检查常见异常
            exceptions = conn.execute(
                """SELECT exception_type, COUNT(*) as cnt
                   FROM skill_usage_logs
                   WHERE skill_name=? AND status='failure' AND exception_type != ''
                   GROUP BY exception_type
                   HAVING cnt >= ?""",
                (skill_name, self.SKILL_TO_WIKI_SIGNALS["exception_threshold"])
            ).fetchall()

            for exc in exceptions:
                insights.append(FlywheelInsight(
                    direction="skill_to_wiki",
                    source=skill_name,
                    target=f"{skill_name} 异常：{exc['exception_type']}",
                    confidence=min(exc["cnt"] * 0.2, 0.9),
                    reason=f"异常 '{exc['exception_type']}' 出现 {exc['cnt']} 次",
                    suggested_action=f"记录异常根因和解决方案，生成问题-解决对 Wiki",
                ))

            # 检查新场景
            new_scenarios = conn.execute(
                """SELECT input_data, COUNT(*) as cnt
                   FROM skill_usage_logs
                   WHERE skill_name=? AND new_scenario=1
                   GROUP BY input_data
                   HAVING cnt >= ?""",
                (skill_name, self.SKILL_TO_WIKI_SIGNALS["new_scenario_threshold"])
            ).fetchall()

            for ns in new_scenarios:
                insights.append(FlywheelInsight(
                    direction="skill_to_wiki",
                    source=skill_name,
                    target=f"{skill_name} 新场景处理",
                    confidence=min(ns["cnt"] * 0.15, 0.8),
                    reason=f"新场景出现 {ns['cnt']} 次",
                    suggested_action="分析新场景的特征，扩展 Skill 的适用范围或生成新的决策记录",
                ))

            # 用户主动标记
            user_marked = conn.execute(
                """SELECT input_data, output_data, exception_detail
                   FROM skill_usage_logs
                   WHERE skill_name=? AND user_marked=1
                   ORDER BY timestamp DESC LIMIT 5""",
                (skill_name,)
            ).fetchall()

            if user_marked:
                insights.append(FlywheelInsight(
                    direction="skill_to_wiki",
                    source=skill_name,
                    target=f"{skill_name} 用户标记案例",
                    confidence=0.9,
                    reason=f"用户主动标记 {len(user_marked)} 条记录应沉淀为知识",
                    suggested_action="整理用户标记的案例，生成经验法则或决策记录",
                ))

        return insights

    # ========== Wiki 使用追踪 ==========

    def log_wiki_usage(self, page_path: str, access_type: str = "read",
                       context: str = ""):
        """记录 Wiki 页面使用"""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO wiki_usage_logs (page_path, timestamp, access_type, context)
                   VALUES (?, ?, ?, ?)""",
                (page_path, datetime.now().isoformat()[:19], access_type, context[:500])
            )
            conn.commit()

    def _get_wiki_usage(self, page_path: str, days: int = 30) -> int:
        """获取 Wiki 页面在 N 天内的使用次数"""
        since = (datetime.now() - timedelta(days=days)).isoformat()[:19]
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM wiki_usage_logs WHERE page_path=? AND timestamp >=?",
                (page_path, since)
            ).fetchone()
        return row[0] if row else 0

    # ========== Skill CRUD ==========

    def create_skill(self, skill: SkillRecord) -> bool:
        """创建 Skill 记录"""
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO skills
                       (skill_name, description, trigger_conditions, input_template,
                        expected_output, source_wiki_pages, status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        skill.skill_name, skill.description,
                        json.dumps(skill.trigger_conditions, ensure_ascii=False),
                        skill.input_template, skill.expected_output,
                        json.dumps(skill.source_wiki_pages, ensure_ascii=False),
                        skill.status or "proposed",
                        skill.created_at or datetime.now().isoformat()[:19],
                        datetime.now().isoformat()[:19],
                    )
                )
                conn.commit()
                return True
        except sqlite3.Error:
            return False

    def get_skill(self, skill_name: str) -> Optional[SkillRecord]:
        """获取 Skill 记录"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM skills WHERE skill_name=?", (skill_name,)
            ).fetchone()

        if not row:
            return None

        return SkillRecord(
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
        )

    def list_skills(self, status: str = None) -> List[SkillRecord]:
        """列出所有 Skill"""
        with self._conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM skills WHERE status=?", (status,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM skills").fetchall()

        return [
            SkillRecord(
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
            )
            for row in rows
        ]

    # ========== 飞轮运行 ==========

    def run_cycle(self) -> Dict[str, List[FlywheelInsight]]:
        """
        运行一个飞轮周期

        Returns:
            {"wiki_to_skill": [...], "skill_to_wiki": [...], "persona_driven": {...}}
        """
        results = {
            "wiki_to_skill": self.scan_wiki_for_skills(),
            "skill_to_wiki": [],
            "persona_driven": {},
        }

        # 扫描所有 Skill 的使用日志
        for skill in self.list_skills():
            insights = self.analyze_skill_for_wiki(skill.skill_name)
            results["skill_to_wiki"].extend(insights)

        # 按置信度排序
        for direction in ["wiki_to_skill", "skill_to_wiki"]:
            results[direction].sort(key=lambda x: x.confidence, reverse=True)

        # 画像驱动分析
        if self.persona_engine:
            results["persona_driven"] = self._run_persona_driven_cycle()
            self._log_persona_cycle(results["persona_driven"])

        return results

    def _run_persona_driven_cycle(self) -> Dict:
        """运行画像驱动的飞轮子周期"""
        engine = self.persona_engine

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

        return {
            "gaps": gaps,
            "paths": paths,
            "tasks": tasks,
            "flywheel_params": flywheel_params,
            "verifications": verifications,
        }

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
                        path.path_id, path.title, path.description,
                        json.dumps(path.stages, ensure_ascii=False),
                        path.cognitive_style, path.estimated_duration,
                        path.priority, "active",
                        datetime.now().isoformat()[:19],
                    )
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
                    (task.related_skill, task.related_blindspot_type)
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
                        task.task_id, task.task_type, task.description,
                        task.related_skill, task.related_blindspot_type,
                        task.verification_method, task.expected_outcome,
                        task.status, datetime.now().isoformat()[:19],
                    )
                )
            conn.commit()

    def _log_persona_cycle(self, persona_results: Dict):
        """记录画像驱动周期日志"""
        params = persona_results.get("flywheel_params", {})
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO persona_flywheel_logs
                   (cycle_date, gaps_detected, paths_created,
                    verifications_created, flywheel_params)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    datetime.now().isoformat()[:10],
                    len(persona_results.get("gaps", [])),
                    len(persona_results.get("paths", [])),
                    len(persona_results.get("verifications", [])),
                    json.dumps(params, ensure_ascii=False),
                )
            )
            conn.commit()

    def generate_cycle_report(self, results: Dict) -> str:
        """生成飞轮周期报告"""
        lines = [
            "# Skill-Wiki 飞轮周期报告",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
        ]

        # 画像驱动调速参数（如果有）
        persona_driven = results.get("persona_driven", {})
        flywheel_params = persona_driven.get("flywheel_params", {})
        if flywheel_params:
            lines.extend([
                "## 飞轮调速（画像驱动）",
                f"周期间隔: {flywheel_params.get('cycle_days', 5)} 天",
                f"每批处理: {flywheel_params.get('batch_size', 4)} 个",
                f"强度模式: {flywheel_params.get('intensity', 'adaptive')}",
                f"最大并行: {flywheel_params.get('max_parallel', 2)} 个",
                "",
            ])

        # Wiki → Skill
        wiki_to_skill = results.get("wiki_to_skill", [])
        lines.extend([
            f"## Wiki → Skill ({len(wiki_to_skill)} 个建议)",
            "",
        ])
        if wiki_to_skill:
            for i, insight in enumerate(wiki_to_skill[:10], 1):
                page_name = Path(insight.source).name
                lines.append(f"{i}. **{insight.target}** (置信度: {insight.confidence})")
                lines.append(f"   - 来源: `{page_name}`")
                lines.append(f"   - 理由: {insight.reason}")
                lines.append("")
        else:
            lines.append("暂无 Wiki → Skill 的转化建议。\n")

        # Skill → Wiki
        skill_to_wiki = results.get("skill_to_wiki", [])
        lines.extend([
            f"## Skill → Wiki ({len(skill_to_wiki)} 个建议)",
            "",
        ])
        if skill_to_wiki:
            for i, insight in enumerate(skill_to_wiki[:10], 1):
                lines.append(f"{i}. **{insight.target}** (置信度: {insight.confidence})")
                lines.append(f"   - 来源 Skill: `{insight.source}`")
                lines.append(f"   - 理由: {insight.reason}")
                lines.append("")
        else:
            lines.append("暂无 Skill → Wiki 的沉淀建议。\n")

        # 画像驱动洞察
        if persona_driven and self.persona_engine:
            lines.append(self.persona_engine.format_persona_insights(
                persona_driven.get("gaps", []),
                persona_driven.get("paths", []),
                persona_driven.get("tasks", []),
                persona_driven.get("verifications", []),
            ))

        return "\n".join(lines)

    # ========== 辅助方法 ==========

    @staticmethod
    def _extract_frontmatter(content: str) -> Dict:
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    import yaml
                    return yaml.safe_load(parts[1]) or {}
                except Exception as e:
                    logger.warning(f"忽略异常: {e}")
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

    def get_pending_verification_tasks(self) -> List[Dict]:
        """获取待完成的验证任务"""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM skill_verification_tasks
                   WHERE status='pending'
                   ORDER BY created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def complete_verification_task(self, task_id: str, outcome: str = "") -> bool:
        """标记验证任务为已完成"""
        try:
            with self._conn() as conn:
                conn.execute(
                    """UPDATE skill_verification_tasks
                       SET status='completed', completed_at=?
                       WHERE task_id=?""",
                    (datetime.now().isoformat()[:19], task_id)
                )
                conn.commit()
            return True
        except sqlite3.Error:
            return False

    def dismiss_verification_task(self, task_id: str, reason: str = "") -> bool:
        """驳回验证任务"""
        try:
            with self._conn() as conn:
                conn.execute(
                    """UPDATE skill_verification_tasks
                       SET status='dismissed', completed_at=?
                       WHERE task_id=?""",
                    (datetime.now().isoformat()[:19], task_id)
                )
                conn.commit()
            return True
        except sqlite3.Error:
            return False

    def get_active_skill_paths(self) -> List[Dict]:
        """获取活跃的学习路径"""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM skill_paths
                   WHERE status='active'
                   ORDER BY priority, created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def complete_skill_path(self, path_id: str) -> bool:
        """标记学习路径为已完成"""
        try:
            with self._conn() as conn:
                conn.execute(
                    """UPDATE skill_paths
                       SET status='completed', completed_at=?
                       WHERE path_id=?""",
                    (datetime.now().isoformat()[:19], path_id)
                )
                conn.commit()
            return True
        except sqlite3.Error:
            return False

    def update_persona(self, persona: PreferenceProfile,
                       blindspot: BlindSpotProfile = None):
        """更新画像（允许运行时动态更新）"""
        if PERSONA_AVAILABLE:
            self.persona_engine = PersonaDrivenSkillEngine(persona, blindspot)


# ========== 便捷函数 ==========

def run_flywheel(wiki_base: str = None,
                 persona: PreferenceProfile = None,
                 blindspot: BlindSpotProfile = None) -> Dict:
    """便捷函数：运行飞轮周期（支持画像驱动）"""
    flywheel = SkillWikiFlywheel(
        wiki_base=wiki_base,
        persona=persona,
        blindspot=blindspot,
    )
    return flywheel.run_cycle()


def run_persona_driven_flywheel(persona: PreferenceProfile,
                                blindspot: BlindSpotProfile = None,
                                wiki_base: str = None) -> str:
    """
    便捷函数：运行画像驱动的飞轮周期并返回报告。

    这是画像驱动飞轮的主要入口。
    """
    flywheel = SkillWikiFlywheel(
        wiki_base=wiki_base,
        persona=persona,
        blindspot=blindspot,
    )
    results = flywheel.run_cycle()
    return flywheel.generate_cycle_report(results)


def get_skill_gaps(persona: PreferenceProfile) -> List[PersonaSkillGap]:
    """便捷函数：获取技能缺口分析"""
    engine = PersonaDrivenSkillEngine(persona)
    return engine.analyze_skill_gaps()


def get_personalized_skill_paths(persona: PreferenceProfile) -> List[SkillPath]:
    """便捷函数：获取个性化学习路径"""
    engine = PersonaDrivenSkillEngine(persona)
    gaps = engine.analyze_skill_gaps()
    return engine.generate_skill_paths(gaps)


def get_verification_tasks(persona: PreferenceProfile,
                           blindspot: BlindSpotProfile,
                           skills: List[str]) -> List[SkillVerificationTask]:
    """便捷函数：获取盲区验证任务"""
    engine = PersonaDrivenSkillEngine(persona, blindspot)
    return engine.generate_verification_tasks(skills)
