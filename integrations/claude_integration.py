#!/usr/bin/env python3
"""
Claude Code Memos 集成脚本

意图判定规则：
1. 【上下文回忆类】→ 仅读取Memos
   - 历史对话、过往沟通细节、会话接续、任务复盘
   - 关键词：上次、之前、刚才、回忆、继续、复盘

2. 【知识查询类】→ 自动检索Wiki
   - 概念定义、架构规则、标准流程、专业知识点、既定规范
   - 关键词：是什么、如何、怎么、原理、架构、流程、规范

3. 【禁止】
   - 两类不混合滥用
   - 无意义重复检索
   - 随意交叉调用
"""

import os
import logging

logger = logging.getLogger(__name__)
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from enum import Enum


from integrations.memos_sdk import MemosClient
from integrations.ai_context_reader import AIContextReader
from integrations.wiki_reader import WikiReader

# Knowledge-in-Action 闭环系统
from core.kia.task_classifier import TaskClassifier
from core.kia.time_parser import TimeParser, should_load_knowledge
from core.kia.pre_flight_injector import PreFlightInjector
from core.kia.in_process_guard import InProcessGuard, GuardLevel
from core.kia.auto_retrospective import AutoRetrospective, should_retrospect
from core.kia.iteration_tracker import IterationTracker
from core.kia.knowledge_scheduler import KnowledgeScheduler

# 用户画像闭环系统
from core.persona.signal_store import SignalStore, get_signal_store, SessionSignal
from core.persona.signal_collector import SignalCollector
from core.persona.preference_analyzer import PreferenceAnalyzer
from core.persona.persona_store import PersonaStore
from core.persona.blindspot_analyzer import BlindSpotProfileManager
from core.config import get_config


class QueryIntent(Enum):
    """查询意图类型"""
    CONTEXT_RECALL = "context_recall"  # 上下文回忆类 → Memos
    KNOWLEDGE_QUERY = "knowledge_query"  # 知识查询类 → Wiki
    UNKNOWN = "unknown"


class IntentClassifier:
    """意图分类器"""

    # 【上下文回忆类】关键词
    CONTEXT_KEYWORDS = [
        # 时间指代
        "上次", "之前", "刚才", "刚才说的", "早些时候",
        "昨天", "今天早些时候", "刚才的",
        # 会话相关
        "继续", "接着", "回到", "刚才那个", "之前那个",
        # 回忆/复盘
        "回忆", "复盘", "回顾一下", "总结一下", "之前做过",
        "做到哪了", "进度", "状态",
        # 特定记录
        "聊天记录", "对话记录", "会话", "说过",
    ]

    # 【知识查询类】关键词
    KNOWLEDGE_KEYWORDS = [
        # 概念定义
        "是什么", "什么叫", "什么是", "定义", "概念",
        "解释一下", "介绍一下", "说明",
        # 架构/规则
        "架构", "结构", "设计", "原理", "机制",
        "规则", "规范", "约定", "标准",
        # 流程/方法
        "如何", "怎么", "怎样", "流程", "步骤",
        "方法", "方式", "做法", "实现",
        # 专业知识点
        "为什么", "原理是什么", "底层", "核心",
        "关键点", "注意事项", "最佳实践",
        # 系统/框架
        "系统", "框架", "模块", "组件", "接口",
    ]

    @classmethod
    def classify(cls, user_message: str) -> Tuple[QueryIntent, float, List[str]]:
        """
        判定用户意图

        Returns:
            (意图类型, 置信度, 匹配到的关键词)
        """
        if not user_message:
            return QueryIntent.UNKNOWN, 0.0, []

        msg_lower = user_message.lower()

        # 统计匹配
        context_matches = [kw for kw in cls.CONTEXT_KEYWORDS if kw in msg_lower]
        knowledge_matches = [kw for kw in cls.KNOWLEDGE_KEYWORDS if kw in msg_lower]

        context_score = len(context_matches)
        knowledge_score = len(knowledge_matches)

        # 判定逻辑
        if context_score > 0 and knowledge_score == 0:
            # 只有上下文关键词 → 明确是上下文回忆
            return QueryIntent.CONTEXT_RECALL, min(0.9, 0.5 + context_score * 0.1), context_matches

        elif knowledge_score > 0 and context_score == 0:
            # 只有知识关键词 → 明确是知识查询
            return QueryIntent.KNOWLEDGE_QUERY, min(0.9, 0.5 + knowledge_score * 0.1), knowledge_matches

        elif context_score > 0 and knowledge_score > 0:
            # 两者都有 → 需要看哪个更强
            if context_score > knowledge_score * 1.5:
                return QueryIntent.CONTEXT_RECALL, 0.7, context_matches
            elif knowledge_score > context_score * 1.5:
                return QueryIntent.KNOWLEDGE_QUERY, 0.7, knowledge_matches
            else:
                # 混合意图 → 默认按上下文处理（更安全）
                return QueryIntent.CONTEXT_RECALL, 0.6, context_matches + knowledge_matches

        else:
            # 无明确关键词 → 未知，默认不查Wiki
            return QueryIntent.UNKNOWN, 0.0, []


def get_wiki_knowledge(user_message: str) -> Optional[str]:
    """
    【知识查询类】专用 - 检索Wiki知识（热力值控制深度）

    流程：
    1. 搜索所有相关页面（不限数量）
    2. 按热力值分组
    3. 根据热力值读取对应深度：
       - L0: 元数据
       - L1-L3: 摘要100字
       - L4-L6: 段落500字
       - L7-L8: 全文+关联
       - L9: 全文+深度追踪
    """
    reader = WikiReader()

    # 获取Wiki知识（自动按热力值分层）
    result = reader.get_knowledge(user_message, include_related=True)

    # 记录查询轨迹（供暗知识挖掘使用）
    if result["found"]:
        try:
            from core.knowledge_trail import KnowledgeTrail
            trail = KnowledgeTrail()
            for page in result.get("pages", []):
                page_path = page.get("path", "")
                if page_path:
                    trail.log_query(page_path, context=user_message)
        except Exception:
            pass  # 轨迹记录失败不影响主流程

    if not result["found"]:
        return None

    # 组装上下文
    context_parts = [
        f"\n## Wiki知识参考（【知识查询类】自动检索）",
        f"查询: {result['query']}",
        f"找到 {result['total_pages']} 个相关页面，按热力值分层读取:\n"
    ]

    # 按热力值分组显示
    for level_group in ["L9", "L7-L8", "L4-L6", "L1-L3", "L0"]:
        if level_group in result["by_heat_level"]:
            group = result["by_heat_level"][level_group]
            if group["count"] > 0:
                context_parts.append(f"\n### [{level_group}] {group['count']}个页面 - {group['depth']}")
                for page in group["pages"][:3]:  # 每个等级最多3个
                    content = page["content"]
                    lines = [f"\n**{content.get('title', page['title'])}** [{page['heat_level']}]"]

                    if "content" in content:
                        lines.append(content["content"][:1500])
                    elif "summary" in content:
                        lines.append(content["summary"])

                    if content.get("related"):
                        lines.append(f"\n关联: {', '.join([r['page_id'] for r in content['related'][:3]])}")

                    context_parts.append("\n".join(lines))

    # 添加分隔线
    context_parts.append("\n---\n")

    # 【Context Fencing】标记 Wiki 引用，防止回流污染
    # 蒸馏层检测到此标记会跳过该消息，避免 AI 复述的 Wiki 内容又进入 Wiki
    full_context = "\n".join(context_parts)
    return f"""<wiki-context source="knowledge-query">
{full_context}
</wiki-context>"""


def get_memos_context(working_dir: str, authorize_cross: List[str] = None) -> str:
    """
    【上下文回忆类】专用 - 读取Memos历史记录

    有权限控制：同框架默认可读，跨框架需授权
    """
    reader = AIContextReader(agent="claude")

    if authorize_cross:
        reader.authorize_cross_agent(authorize_cross, duration_minutes=60)

    context_parts = []

    # 1. 读取自己的上下文
    my_memories = reader.read_my_context(limit=30, days=7)
    if my_memories:
        context_parts.append(f"\n## 最近会话上下文（{len(my_memories)}条）\n")
        for mem in my_memories[:10]:
            session_id = "unknown"
            for tag in mem.tags:
                if tag.startswith("session:"):
                    session_id = tag.split(":", 1)[1]
                    break
            content_preview = mem.content[:200].replace('\n', ' ')
            context_parts.append(f"- Session `{session_id}`: {content_preview}...")

    # 2. 读取跨框架上下文（如有授权）
    if authorize_cross:
        cross_context = reader.read_cross_agent(authorize_cross, limit=10)
        for agent, memories in cross_context.items():
            if memories:
                context_parts.append(f"\n## {agent} 框架共享记忆（{len(memories)}条）\n")
                for mem in memories[:5]:
                    content_preview = mem.content[:150].replace('\n', ' ')
                    context_parts.append(f"- {content_preview}...")

    # 3. 搜索与工作目录相关的上下文
    dir_name = Path(working_dir).name
    related = reader.search_context(dir_name, limit=10)
    if related:
        context_parts.append(f"\n## 相关记忆（{len(related)}条）\n")
        for r in related[:5]:
            mem = r['memory']
            source = r['source']
            content_preview = mem.content[:150].replace('\n', ' ')
            context_parts.append(f"- [{source}] {content_preview}...")

    if not context_parts:
        return "\n（暂无相关上下文）\n"

    return '\n'.join(context_parts)


# A/B 测试状态：当前 session 是否使用画像驱动
_ab_test_persona_driven = None

def _ensure_ab_test_group() -> bool:
    """确保 A/B 测试分组已确定（每个 session 只随机一次）"""
    global _ab_test_persona_driven
    if _ab_test_persona_driven is None:
        import random
        _ab_test_persona_driven = random.random() < 0.5
    return _ab_test_persona_driven

def _get_persona_behavior_prompt() -> str:
    """
    根据用户画像生成行为策略提示。
    将画像维度映射为具体的 AI 交互策略。
    支持 A/B 测试：50% 概率使用画像驱动，50% 概率不使用。
    """
    try:
        # A/B 分组：每个 session 只确定一次
        if not _ensure_ab_test_group():
            return "\n[Persona-Driven Behavior]\n- A/B 对照组：本次 session 不使用画像驱动策略"

        from core.persona.persona_store import PersonaStore
        pstore = PersonaStore()
        profile, _ = pstore.load_persona()
        if not profile:
            _ab_test_persona_driven = False
            return ""

        lines = ["\n[Persona-Driven Behavior]"]
        ins_energy = set(profile.energy.insufficient_dimensions or [])
        ins_cognitive = set(profile.cognitive.insufficient_dimensions or [])
        ins_value = set(profile.value.insufficient_dimensions or [])

        # 能量层映射
        if "focus_depth" not in ins_energy:
            fd = profile.energy.focus_depth
            if fd > 0.6:
                lines.append("- 用户专注深度高：提供结构化、层次化的深度回复，避免碎片化信息")
            elif fd < 0.4:
                lines.append("- 用户专注深度低：提供简短、可快速消化的信息，多用列表和要点")

        if "startup_difficulty" not in ins_energy:
            sd = profile.energy.startup_difficulty
            if sd > 0.6:
                lines.append("- 用户启动难度高：主动提供框架、模板或选项，降低决策成本")
            elif sd < 0.4:
                lines.append("- 用户启动容易：可以用开放性问题开场，给用户更多探索空间")

        if "switching_flexibility" not in ins_energy:
            sf = profile.energy.switching_flexibility
            if sf > 0.6:
                lines.append("- 用户切换弹性高：允许话题自然切换，不必强行锁定当前主题")
            elif sf < 0.4:
                lines.append("- 用户切换弹性低：坚持当前主线，切换话题时明确提示和确认")

        # 认知层映射
        if "abstraction" not in ins_cognitive:
            ab = profile.cognitive.abstraction
            if ab > 0.6:
                lines.append("- 用户偏抽象思维：先说原理/框架，再用案例佐证")
            elif ab < 0.4:
                lines.append("- 用户偏具象思维：先给具体案例，再归纳原理")

        if "system_view" not in ins_cognitive:
            sv = profile.cognitive.system_view
            if sv > 0.6:
                lines.append("- 用户偏好系统视角：先给全貌和关联，再深入细节")
            elif sv < 0.4:
                lines.append("- 用户偏好单点视角：聚焦当前问题，全局背景简要提及")

        if "skepticism" not in ins_cognitive:
            sk = profile.cognitive.skepticism
            if sk > 0.6:
                lines.append("- 用户质疑倾向强：主动展示推理过程、证据和局限性")
            elif sk < 0.4:
                lines.append("- 用户信任倾向强：直接给结论和建议，不必过度解释前提")

        # 价值层映射
        if "correctness_vs_efficiency" not in ins_value:
            ce = profile.value.correctness_vs_efficiency
            if ce > 0.6:
                lines.append("- 用户重视正确性：确保信息准确，不确定时明确说明")
            elif ce < 0.4:
                lines.append("- 用户重视效率：快速给出可行方案，不必追求完美")

        if "perfection_vs_completion" not in ins_value:
            pc = profile.value.perfection_vs_completion
            if pc > 0.6:
                lines.append("- 用户追求完美：提供详尽、完整的方案，考虑边界情况")
            elif pc < 0.4:
                lines.append("- 用户追求完成：先给 MVP 方案，细节后续补充")

        if "depth_vs_breadth" not in ins_value:
            db = profile.value.depth_vs_breadth
            if db > 0.6:
                lines.append("- 用户偏好深度：深入一个点，不必面面俱到")
            elif db < 0.4:
                lines.append("- 用户偏好广度：提供多种选择和视角，不必深入每个细节")

        if len(lines) > 1:
            return "\n".join(lines)
        return ""
    except Exception:
        _ab_test_persona_driven = False
        return ""


def get_ab_test_stats(days: int = 30) -> dict:
    """
    获取 A/B 测试统计。
    对比画像驱动组 vs 对照组的 session 表现。
    """
    try:
        import sqlite3
        from core.persona.signal_store import SIGNAL_DB_PATH

        db_path = str(SIGNAL_DB_PATH)
        with sqlite3.connect(db_path) as conn:
            # 获取画像驱动组的 session 指标
            driven = conn.execute("""
                SELECT
                    AVG(correction_count),
                    COUNT(CASE WHEN termination_type = 'satisfied' THEN 1 END),
                    COUNT(*)
                FROM session_signals s
                JOIN signal_metadata m ON m.signal_table = 'session' AND m.signal_id = s.id
                WHERE s.timestamp >= date('now', ?)
                  AND json_extract(m.session_context, '$.persona_driven') = 1
            """, (f'-{days} days',)).fetchone()

            # 获取对照组的 session 指标
            control = conn.execute("""
                SELECT
                    AVG(correction_count),
                    COUNT(CASE WHEN termination_type = 'satisfied' THEN 1 END),
                    COUNT(*)
                FROM session_signals s
                JOIN signal_metadata m ON m.signal_table = 'session' AND m.signal_id = s.id
                WHERE s.timestamp >= date('now', ?)
                  AND (
                    json_extract(m.session_context, '$.persona_driven') IS NULL
                    OR json_extract(m.session_context, '$.persona_driven') = 0
                  )
            """, (f'-{days} days',)).fetchone()

        result = {"days": days}
        if driven[2] and driven[2] > 0:
            result["driven"] = {
                "count": driven[2],
                "avg_correction": round(driven[0] or 0, 2),
                "satisfaction_rate": round((driven[1] or 0) / driven[2], 2),
            }
        if control[2] and control[2] > 0:
            result["control"] = {
                "count": control[2],
                "avg_correction": round(control[0] or 0, 2),
                "satisfaction_rate": round((control[1] or 0) / control[2], 2),
            }
        return result
    except Exception:
        return {}


def get_context_for_claude(
    working_dir: str = None,
    user_message: str = None,
    authorize_cross: List[str] = None
) -> str:
    """
    主入口：根据意图判定选择数据源

    【严格分离原则】
    - 上下文回忆类 → 仅Memos
    - 知识查询类 → 仅Wiki
    - 禁止混合滥用
    """
    if working_dir is None:
        working_dir = os.getcwd()

    # 1. 意图判定
    intent, confidence, keywords = IntentClassifier.classify(user_message or "")

    print(f"[Intent] 用户意图: {intent.value}, 置信度: {confidence:.2f}, 关键词: {keywords[:3]}")

    context_parts = []

    # 2. 根据意图选择数据源（严格分离）
    if intent == QueryIntent.CONTEXT_RECALL:
        # 【上下文回忆类】→ 仅Memos
        print(f"[Context] 判定为【上下文回忆类】，仅读取Memos...")
        memos_context = get_memos_context(working_dir, authorize_cross)
        context_parts.append(memos_context)

    elif intent == QueryIntent.KNOWLEDGE_QUERY:
        # 【知识查询类】→ 仅Wiki
        print(f"[Context] 判定为【知识查询类】，仅检索Wiki...")
        wiki_context = get_wiki_knowledge(user_message)
        if wiki_context:
            context_parts.append(wiki_context)
        else:
            context_parts.append("\n（Wiki中未找到相关知识）\n")

    elif intent == QueryIntent.UNKNOWN:
        # 未知意图 → 保守策略，仅读取Memos上下文
        print(f"[Context] 意图不明确，保守策略：仅读取Memos...")
        memos_context = get_memos_context(working_dir, authorize_cross)
        context_parts.append(memos_context)

    # 3. Knowledge-in-Action：装载历史经验
    # 仅在非上下文回忆类查询时加载（避免干扰回忆类查询）
    if intent != QueryIntent.CONTEXT_RECALL:
        kia_context = load_knowledge_in_action(user_message or "")
        if kia_context:
            context_parts.append(kia_context)

    # 4. Predictive Push：主动推送可能相关的知识
    if intent != QueryIntent.CONTEXT_RECALL and user_message:
        try:
            from core.predictive_push import PredictivePushEngine
            push_engine = PredictivePushEngine()
            push_decision = push_engine.decide_push(user_message)
            if push_decision and push_decision.should_push:
                context_parts.append(
                    f"\n[Predictive Push] 基于上下文主动推荐:\n"
                    f"  相关页面: {push_decision.page_title}\n"
                    f"  推荐理由: {push_decision.reason}\n"
                    f"  匹配度: {push_decision.match_score:.2f}\n"
                )
        except Exception as e:
            logger.warning(f"PredictivePush 失败: {e}")

    # 5. 画像驱动行为策略
    persona_behavior = _get_persona_behavior_prompt()
    if persona_behavior:
        context_parts.append(persona_behavior)

    # 6. 添加意图标记（用于调试和追踪）
    # 【第4层防护】标记上下文回忆，防止后续Ingest提取时循环污染
    if intent == QueryIntent.CONTEXT_RECALL:
        context_parts.append(f"\n<!-- Intent: {intent.value}, Confidence: {confidence:.2f}, ContainsContextRecall: true -->")
    else:
        context_parts.append(f"\n<!-- Intent: {intent.value}, Confidence: {confidence:.2f} -->")

    return '\n'.join(context_parts)


def load_knowledge_in_action(user_message: str) -> str:
    """
    Knowledge-in-Action 闭环系统 - 会话开始时装载历史经验

    流程：
    1. 识别任务类型（AI建议 + 关键词匹配）
    2. 解析时间窗口
    3. 即时/短期任务 → 装载历史校验清单
    4. 中期/长期任务 → 记入调度器，本次不装载
    """
    if not user_message:
        return ""

    try:
        classifier = TaskClassifier()
        injector = PreFlightInjector()
        scheduler = KnowledgeScheduler()

        # 1. 分类任务
        messages = [{"role": "user", "content": user_message}]
        result = classifier.classify(messages)

        if result.confidence < 0.7:
            return ""  # 不确定，不干扰

        task_label = classifier.get_task_type_label(result.task_type, result.subtype)
        print(f"[KIA] 识别任务: {task_label} (置信度: {result.confidence:.2f})")

        # 2. 解析时间窗口
        parser = TimeParser()
        time_window = parser.parse(user_message)

        # 3. 根据时间窗口决定策略
        if not parser.should_load_now(time_window):
            # 中期/长期任务，记入调度器
            if time_window.due_date:
                task_id = scheduler.schedule(
                    result.task_type, result.subtype,
                    time_window.due_date, context=user_message,
                    is_periodic=time_window.is_periodic,
                    period=time_window.period
                )
                print(f"[KIA] 任务已记入调度器: {task_id}，提前 {parser.get_reminder_days_before(time_window)} 天提醒")
            return ""

        # 4. 装载知识
        knowledge = injector.inject(
            result.task_type, result.subtype,
            time_window, context_text=user_message
        )

        if not knowledge:
            print(f"[KIA] 暂无历史经验 ({task_label})")
            return ""

        # 5. 格式化输出
        knowledge_text = injector.format_for_context(knowledge)

        # 6. 启动 InProcessGuard 并检查用户消息
        guard = InProcessGuard(knowledge)
        alert = guard.check(user_message, "")

        guard_lines = []
        if alert:
            emoji = {"interrupt": "🛑", "hint": "💡", "silent": "📝"}.get(alert.level.value, "⚠️")
            guard_lines.append(f"{emoji} [Guard Alert] {alert.checklist_item.item}")
            if alert.suggestion:
                guard_lines.append(f"   {alert.suggestion}")
            print(f"[KIA-Guard] {alert.level.value.upper()}: {alert.checklist_item.item}")

            # INTERRUPT 级别直接返回告警（阻止继续）
            if alert.level == GuardLevel.INTERRUPT:
                _guard_text = '\n'.join(guard_lines)
                return f"\n{knowledge_text}\n\n{_guard_text}\n"
        else:
            # 静默记录（轻微偏差不打扰用户，只记录供复盘使用）
            silent_records = guard.check_silent(user_message, "")
            if silent_records:
                print(f"[KIA-Guard] 静默记录 {len(silent_records)} 条")

        # 7. 保存 Guard 会话状态供复盘使用
        _save_guard_state(guard, result.task_type, result.subtype)

        # 8. 附加静态 Guard Rules（作为参考，不基于实时检测）
        static_guard_lines = ["[Guard Rules]"]
        for item in knowledge.checklist:
            if item.severity in ["critical", "high"]:
                static_guard_lines.append(f"⚠️ {item.item}")
        guard_text = "\n".join(static_guard_lines) if len(static_guard_lines) > 1 else ""

        print(f"[KIA] 已装载 {task_label} v{knowledge.version}，{len(knowledge.checklist)} 条经验")

        if guard_lines or guard_text:
            parts = [knowledge_text]
            if guard_lines:
                parts.append("\n".join(guard_lines))
            if guard_text:
                parts.append(guard_text)
            return "\n\n".join(parts) + "\n"
        return f"\n{knowledge_text}\n"

    except Exception as e:
        print(f"[KIA] 知识装载失败: {e}")
        return ""


# ========== Guard 状态持久化（跨轮次保持） ==========

def _guard_state_file() -> Path:
    """Guard 状态文件路径（统一在 ~/.mnemos/ 下）"""
    from core.config import get_config
    return get_config().data_dir / "guard_state.json"


def _save_guard_state(guard: InProcessGuard, task_type: str, subtype: str):
    """保存 Guard 会话状态到文件，供复盘时使用"""
    if not guard or not guard.session:
        return

    try:
        state = {
            "task_type": task_type,
            "subtype": subtype,
            "checklist": [
                {
                    "item": item.item,
                    "severity": item.severity,
                    "trigger_keywords": item.trigger_keywords,
                    "risk_patterns": item.risk_patterns,
                    "detail": item.detail,
                }
                for item in guard.session.checklist
            ],
            "triggered_alerts": [
                {
                    "level": alert.level.value,
                    "item": alert.checklist_item.item,
                    "triggered_by": alert.triggered_by,
                    "trigger_text": alert.trigger_text,
                }
                for alert in guard.session.triggered_alerts
            ],
            "silent_records": guard.session.silent_records,
            "timestamp": datetime.now().isoformat(),
        }
        _guard_state_file().parent.mkdir(parents=True, exist_ok=True)
        _guard_state_file().write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[KIA-Guard] 状态保存失败: {e}")


def _load_guard_state() -> Optional[Dict]:
    """加载 Guard 会话状态"""
    try:
        if _guard_state_file().exists():
            return json.loads(_guard_state_file().read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"加载 Guard 状态失败: {e}")
    return None


def _build_checklist_usage_from_guard(messages: List[Dict], task_type: str, subtype: str) -> List[Dict]:
    """
    基于消息历史和 Guard 状态构建 checklist 使用情况
    在 session_end 时调用，模拟 Guard 检查整个对话历史
    """
    usage = []

    # 1. 尝试加载历史 Guard 状态
    state = _load_guard_state()
    if state:
        # 任务类型匹配才使用
        if state.get("task_type") == task_type and state.get("subtype") == subtype:
            # 从状态恢复告警记录
            for alert in state.get("triggered_alerts", []):
                usage.append({
                    "item": alert["item"],
                    "loaded": True,
                    "used": True,
                    "triggered": alert["level"] in ("interrupt", "hint"),
                    "level": alert["level"],
                    "severity": "high",  # 被触发的通常级别较高
                    "reason_ignored": "",
                })
            for record in state.get("silent_records", []):
                usage.append({
                    "item": record["item"],
                    "loaded": True,
                    "used": True,
                    "triggered": False,
                    "level": "silent",
                    "severity": record.get("severity", "medium"),
                    "reason_ignored": "",
                })

    # 2. 如果没有历史状态，基于消息内容做简化推断
    if not usage:
        # 从消息中提取是否提到了 checklist 相关操作
        all_text = " ".join([m.get("content", "") for m in messages])
        # 简化：如果消息中提到了"已注意""已检查"等，认为 checklist 被使用了
        if "注意" in all_text or "检查了" in all_text or "确认" in all_text:
            usage.append({
                "item": "用户声明已注意风险",
                "loaded": True,
                "used": True,
                "triggered": False,
                "level": "none",
                "severity": "medium",
                "reason_ignored": "",
            })

    return usage


# ========== 用户画像闭环 ==========

PERSONA_MIN_SIGNALS = 10       # 最小信号数才分析
PERSONA_MIN_DAYS = 7           # 最少间隔天数


def _collect_session_signal(messages: List[Dict], working_dir: str,
                            task_type: str = "", task_subtype: str = "") -> int:
    """
    从本次会话提取行为信号并入库。
    在 session_end 时调用。
    """
    if not messages:
        return 0

    try:
        store = get_signal_store()

        user_msgs = [m for m in messages if m.get("role") == "user"]
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]

        if not user_msgs:
            return 0

        user_contents = [m.get("content", "") for m in user_msgs]
        avg_len = sum(len(c) for c in user_contents) / max(len(user_contents), 1)

        # 纠正检测
        correction_keywords = ["不对", "错了", "不是", "应该", "换个", "不对，"]
        correction_count = sum(
            1 for c in user_contents
            if any(kw in c for kw in correction_keywords)
        )

        # 追问深度
        follow_up_depth = 0
        for i, msg in enumerate(messages):
            if msg.get("role") == "user" and i > 0:
                prev = messages[:i]
                if any(m.get("role") == "assistant" for m in prev):
                    follow_up_depth += 1

        # 终止类型推断
        termination_type = "unknown"
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = m.get("content", "").lower()
                break

        if any(kw in last_user for kw in ["好的", "完美", "可以", "ok", "谢谢", "搞定了"]):
            termination_type = "satisfied"
        elif any(kw in last_user for kw in ["开始吧", "执行", "推进", "下一步", "继续"]):
            termination_type = "progress"
        elif any(kw in last_user for kw in ["你决定", "你来", "按你的"]):
            termination_type = "delegated"
        elif any(kw in last_user for kw in ["算了", "放弃", "不做了", "先这样吧"]):
            termination_type = "abandoned"

        # 产出类型推断
        all_text = " ".join(m.get("content", "") for m in messages)
        output_type = "discussion"
        if "```" in all_text or "def " in all_text or "class " in all_text:
            output_type = "code"
        elif "# " in all_text and len(all_text) > 500:
            output_type = "document"

        # 使用内容哈希生成稳定的 session_id，避免同一 session 多次保存产生重复
        import hashlib
        content_hash = hashlib.md5(all_text.encode()).hexdigest()[:16]
        dir_hash = hashlib.md5((working_dir or os.getcwd()).encode()).hexdigest()[:8]
        session_id = f"{dir_hash}:{content_hash}"

        signal = SessionSignal(
            session_id=session_id,
            timestamp=datetime.now().isoformat(),
            task_type=task_type,
            task_subtype=task_subtype,
            user_msg_count=len(user_msgs),
            avg_user_msg_length=avg_len,
            correction_count=correction_count,
            follow_up_depth=follow_up_depth,
            termination_type=termination_type,
            output_type=output_type,
            working_dir=working_dir or os.getcwd(),
            agent="claude",
        )

        # 记录 A/B 测试分组信息
        ab_context = {"persona_driven": bool(_ab_test_persona_driven)}

        # 盲区反馈闭环：分析用户对挑战的反应
        _analyze_blindspot_feedback(messages)

        store.insert_session_signal(signal, session_context=ab_context)
        return 1
    except Exception as e:
        print(f"[Persona] Session signal collection failed: {e}")
        return 0


def _analyze_blindspot_feedback(messages: List[Dict]):
    """
    分析 session 消息，检测用户对盲区挑战的反应。
    简化版：通过关键词匹配推断接受/忽略/拒绝。
    """
    try:
        from core.persona.blindspot_analyzer import ChallengeBalancer, BlindSpotProfileManager

        # 收集所有文本
        all_text = " ".join(m.get("content", "") for m in messages).lower()
        if not all_text:
            return

        # 接受挑战的信号
        accept_signals = ["你说得对", "确实", "有道理", "采纳", "按你说的", "好主意", "明白了", "懂了"]
        # 拒绝挑战的信号
        reject_signals = ["不对", "不是这样", "我不同意", "不用了", "算了", "没必要", "过虑了", "不需要"]
        # 忽略挑战的信号（直接回到原话题，没有对挑战的回应）
        # 简化处理：如果没有接受/拒绝信号，就认为是忽略

        reaction = "ignored"
        if any(s in all_text for s in accept_signals):
            reaction = "accepted"
        elif any(s in all_text for s in reject_signals):
            reaction = "rejected"

        # 只有检测到明确反应时才记录
        if reaction != "ignored":
            manager = BlindSpotProfileManager()
            balancer = manager.balancer
            balancer.record_reaction("auto_detected", reaction)
            # 保存更新后的盲区画像
            manager._save_profile()
    except Exception as e:
        logger.warning(f"记录盲区反应失败: {e}")


def _should_analyze_persona() -> bool:
    """检查是否应该触发画像分析（频率控制）。"""
    try:
        store = get_signal_store()
        stats = store.get_signal_stats(days=30)
        total = sum(v for v in stats.values() if v > 0)

        if total < PERSONA_MIN_SIGNALS:
            return False

        # 检查上次分析时间
        latest = store.get_latest_persona_version()
        if latest and latest.get("generated_at"):
            try:
                last = datetime.fromisoformat(latest["generated_at"].replace("Z", "+00:00"))
                days_since = (datetime.now() - last).days
                if days_since < PERSONA_MIN_DAYS:
                    return False
            except Exception as e:
                logger.warning(f"日期解析失败: {e}")

        return True
    except Exception as e:
        logger.warning(f"检查画像分析触发条件失败: {e}")
        return False


def _run_persona_cycle() -> str:
    """
    运行画像分析闭环：分析 → 保存 → 输出摘要。
    在 session_end 或 run_kia_cycles 中调用。
    """
    try:
        store = get_signal_store()

        # 1. 加载上一周期画像（用于对比变化）
        pstore = PersonaStore(signal_store=store)
        previous_profile, previous_blindspot = pstore.load_persona()

        # 2. 分析偏好画像（有历史画像时用增量模式，只处理新信号）
        analyzer = PreferenceAnalyzer(store)
        profile = analyzer.analyze(
            days=90,
            previous_profile=previous_profile,
            incremental=previous_profile is not None
        )

        # 3. 检测盲区
        bs_manager = BlindSpotProfileManager(store)
        blindspots = bs_manager.analyze_and_update(
            session_context={"task_type": "general"},
            user_options=[],
            persona=profile
        )

        # 4. 漂移检测
        alerts = analyzer.detect_drift(profile, previous_profile)

        # 5. 保存画像
        pstore.save_persona(profile, bs_manager.balancer.profile)

        # 6. 输出摘要
        lines = [
            f"[Persona] 画像分析完成 v{profile.version}",
            f"  信号数: {profile.signal_count}",
            f"  能量置信度: {profile.energy.confidence:.0%}",
            f"  认知置信度: {profile.cognitive.confidence:.0%}",
            f"  价值置信度: {profile.value.confidence:.0%}",
        ]
        if blindspots:
            lines.append(f"  盲区挑战: {len(blindspots)} 个")
        if alerts:
            lines.append(f"  漂移警报: {len(alerts)} 个")
            for a in alerts[:3]:
                lines.append(f"    - {a['dimension']}: {a['previous']} → {a['current']} ({a['type']})")

        return "\n".join(lines)
    except Exception as e:
        return f"[Persona] 画像分析失败: {e}"


def run_retrospective(messages_json: str) -> str:
    """
    Knowledge-in-Action 闭环系统 - 会话结束时自动复盘

    Args:
        messages_json: JSON格式的会话消息列表

    Returns:
        复盘结果文本
    """
    if not messages_json:
        return ""

    try:
        messages = json.loads(messages_json)
        if not messages or not should_retrospect(messages):
            return ""

        classifier = TaskClassifier()
        injector = PreFlightInjector()
        retrospective = AutoRetrospective()
        tracker = IterationTracker()

        # 识别任务类型
        result = classifier.classify(messages)
        if result.confidence < 0.7:
            return ""

        task_type = result.task_type
        subtype = result.subtype

        # 获取装载的 checklist 使用情况（基于 Guard 历史状态）
        checklist_usage = _build_checklist_usage_from_guard(messages, task_type, subtype)

        # 生成复盘
        retro_result = retrospective.generate(
            task_type, subtype, messages, checklist_usage
        )

        # 创建新版本
        new_path = tracker.create_next_version(retro_result)

        if new_path:
            return (
                f"\n[KIA] 复盘已生成: {new_path}\n"
                f"       新增 {len(retro_result.new_lessons)} 条教训\n"
                f"       版本: v{retro_result.version}\n"
            )

        return ""

    except Exception as e:
        print(f"[KIA] 复盘失败: {e}")
        return ""


def detect_private_keywords(user_message: str) -> bool:
    """
    检测用户是否要求私有记录
    【上下文回忆类】中的特殊标记
    """
    private_keywords = [
        "私有", "保密", "私密", "隐私",
        "private", "personal", "confidential",
        "不要共享", "不要分享", "仅你可见", "仅自己",
        "别让别人看到", "别共享", "别分享"
    ]

    message_lower = user_message.lower()
    return any(kw in message_lower or kw in user_message for kw in private_keywords)


def run_kia_cycles():
    """
    KIA Orchestrator 轻量周期任务（session_end 时触发）
    执行不依赖 Memos 的子系统周期：关联、维护、调度提醒
    """
    print("[KIA-Orchestrator] 启动轻量周期任务...")
    results = []

    # 1. 关联周期 (L2 → L3)
    try:
        from core.connect_worker import run_connect_cycle
        stats = run_connect_cycle(dry_run=False)
        results.append(f"关联: {stats.get('pages_processed', 0)} 页, "
                      f"{stats.get('links_created', 0)} 链接")
    except Exception as e:
        results.append(f"关联: 失败 ({e})")

    # 2a. 维护周期 (P/L 序列)
    try:
        tracker = IterationTracker()
        maint = tracker.run_maintenance()
        results.append(f"维护: promoted={maint.get('promoted', 0)}, "
                      f"demoted={maint.get('demoted', 0)}")
    except Exception as e:
        results.append(f"维护: 失败 ({e})")

    # 2b. 免疫系统健康扫描
    try:
        from core.knowledge_immune import KnowledgeImmuneSystem
        immune = KnowledgeImmuneSystem()
        report = immune.full_scan()
        health_score = report.health_score
        issue_count = len(report.issues)
        if issue_count > 0:
            results.append(f"免疫: {issue_count} 问题, 健康分 {health_score:.0f}")
            # 尝试自动修复
            if report.auto_fixable_count > 0:
                fixes = immune.auto_fix(report)
                results.append(f"免疫修复: {len(fixes)} 项")
        else:
            results.append(f"免疫: 健康分 {health_score:.0f}")
    except Exception as e:
        results.append(f"免疫: 失败 ({e})")

    # 3. DNA 指纹扫描（去重 + 相似度检测）
    try:
        from core.knowledge_dna import DNAEngine
        dna_engine = DNAEngine()
        dna_stats = dna_engine.scan_all_pages()
        total_dna = dna_engine.get_stats().get("total_fingerprints", 0)
        results.append(f"DNA: {dna_stats.get('computed', 0)} 新指纹, 库共 {total_dna}")

        # 检测相似页面
        if total_dna > 1:
            import sqlite3
            dup_count = 0
            with sqlite3.connect(str(dna_engine.db_path)) as conn:
                rows = conn.execute("SELECT page_path FROM knowledge_dna").fetchall()
                checked = set()
                for (page_path,) in rows:
                    dna = dna_engine.load_dna(page_path)
                    if dna and page_path not in checked:
                        dups = dna_engine.find_duplicates(dna)
                        for dup in dups:
                            checked.add(dup.page_path)
                        if dups:
                            dup_count += len(dups)
                        checked.add(page_path)
            if dup_count > 0:
                results.append(f"DNA去重: 发现 {dup_count} 对疑似重复")
    except Exception as e:
        results.append(f"DNA: 失败 ({e})")

    # 4. 暗知识挖掘（需要 trail 数据，无条件运行但可能为空）
    try:
        from core.dark_knowledge import DarkKnowledgeMiner
        miner = DarkKnowledgeMiner()
        associations = miner.mine_hidden_associations(min_confidence=0.5)
        gaps = miner.mine_knowledge_gaps(min_frequency=3)
        if associations or gaps:
            results.append(f"暗知识: {len(associations)} 关联, {len(gaps)} 缺口")
        else:
            results.append("暗知识: 无 trail 数据")
    except Exception as e:
        results.append(f"暗知识: 失败 ({e})")

    # 5. 知识图谱增强（间接关联发现）
    try:
        from core.quantum_entanglement import QuantumEntanglement
        qe = QuantumEntanglement()
        indirect = qe.discover_indirect_paths(max_depth=2, min_strength=0.3)
        cross = qe.discover_cross_domain()
        if indirect or cross:
            results.append(f"量子纠缠: {len(indirect)} 间接路径, {len(cross)} 跨域关联")
        else:
            results.append("量子纠缠: 无新发现")
    except Exception as e:
        results.append(f"量子纠缠: 失败 ({e})")

    # 6. Skill-Wiki 双飞轮扫描
    try:
        from core.skill_wiki_flywheel import SkillWikiFlywheel
        flywheel = SkillWikiFlywheel()
        insights = flywheel.scan_wiki_for_skills()
        if insights:
            results.append(f"Skill飞轮: {len(insights)} 个 skill 提案")
        else:
            results.append("Skill飞轮: 无新提案")
    except Exception as e:
        results.append(f"Skill飞轮: 失败 ({e})")

    # 7. 可证伪性标记扫描
    try:
        from core.falsifiability_marker import FalsifiabilityMarker
        marker = FalsifiabilityMarker()
        marks = marker.scan_all_marks(days_since_last_test=30)
        if marks:
            results.append(f"证伪: {len(marks)} 项待验证")
        else:
            results.append("证伪: 无待验证")
    except Exception as e:
        results.append(f"证伪: 失败 ({e})")

    # 8. 知识画像生成
    try:
        from core.knowledge_profile import ProfileGenerator
        gen = ProfileGenerator()
        profile = gen.generate()
        results.append(f"画像: {profile.total_knowledge} 页, 质量分 {profile.quality_score:.0f}")
    except Exception as e:
        results.append(f"画像: 失败 ({e})")

    # 9. 时间胶囊（自动扫描时效性知识）
    try:
        from core.time_capsule import TimeCapsule
        capsule = TimeCapsule()
        new_reminders = capsule.scan_for_auto_reminders()
        due = capsule.get_due_reminders(days_ahead=7)
        if new_reminders > 0 or due:
            results.append(f"时间胶囊: {new_reminders} 新提醒, {len(due)} 到期")
        else:
            results.append("时间胶囊: 无到期")
    except Exception as e:
        results.append(f"时间胶囊: 失败 ({e})")

    # 10. 熵引擎（知识混乱度扫描）
    try:
        from core.entropy_engine import EntropyEngine
        entropy = EntropyEngine()
        report = entropy.scan()
        if report.duplicate_count > 0 or report.mergeable_count > 0:
            results.append(f"熵: {report.duplicate_count} 重复, {report.mergeable_count} 可合并")
        else:
            results.append("熵: 正常")
    except Exception as e:
        results.append(f"熵: 失败 ({e})")

    # 11. 版本时间旅行（全量快照）
    try:
        from core.version_time_travel import VersionTimeTravel
        vtt = VersionTimeTravel()
        snap_stats = vtt.scan_and_snapshot_all()
        total_snaps = sum(snap_stats.values())
        if total_snaps > 0:
            results.append(f"快照: {total_snaps} 个新版本")
        else:
            results.append("快照: 无变更")
    except Exception as e:
        results.append(f"快照: 失败 ({e})")

    # 12. 影子页面（外部验证，需 Tavily API）
    try:
        from core.shadow_page import ShadowPageManager
        spm = ShadowPageManager()
        shadows = spm.list_shadows()
        if shadows:
            results.append(f"影子: {len(shadows)} 个跟踪中")
        else:
            results.append("影子: 未配置")
    except Exception as e:
        results.append(f"影子: 失败 ({e})")

    # 13. 调度提醒
    try:
        scheduler = KnowledgeScheduler()
        missed = scheduler.startup_compensation()
        pending = scheduler.get_pending_reminders()
        all_tasks = missed + pending
        for task in all_tasks:
            scheduler.mark_reminded(task.task_id)
        results.append(f"调度: {len(all_tasks)} 个提醒")
    except Exception as e:
        results.append(f"调度: 失败 ({e})")

    # 14. 用户画像分析闭环
    try:
        if _should_analyze_persona():
            persona_result = _run_persona_cycle()
            # 只输出摘要第一行，详情在 _run_persona_cycle 里已打印
            first_line = persona_result.split('\n')[0] if persona_result else "画像: 无输出"
            results.append(first_line.replace("[Persona] ", "画像: "))
        else:
            store = get_signal_store()
            stats = store.get_signal_stats(days=30)
            total = sum(v for v in stats.values() if v > 0)
            results.append(f"画像: 信号不足 ({total}/{PERSONA_MIN_SIGNALS}) 或间隔太短")
    except Exception as e:
        results.append(f"画像: 失败 ({e})")

    print(f"[KIA-Orchestrator] 周期完成: {' | '.join(results)}")


def show_stats():
    """显示 Mnemos v6.0 系统统计"""
    from core.iteration_tracker import IterationTracker
    from core.knowledge_scheduler import KnowledgeScheduler

    WIKI_DIR = get_config().wiki_dir

    print("=" * 50)
    print("Mnemos v6.0 系统统计")
    print("=" * 50)

    # 1. Wiki 文件统计
    if WIKI_DIR.exists():
        for subdir in ["00-Inbox", "01-People", "02-Projects", "03-Tech",
                       "04-Concepts", "05-MOCs", "retrospectives"]:
            path = WIKI_DIR / subdir
            if path.exists():
                count = len(list(path.rglob("*.md")))
                print(f"  {subdir}/: {count} 个文件")

    # 2. 知识状态统计
    try:
        tracker = IterationTracker()
        stats = tracker.get_stats()
        print(f"\n知识状态统计:")
        print(f"  总知识条目: {stats['total']}")
        print(f"  P序列分布: {stats['p_distribution']}")
        print(f"  L序列分布: {stats['l_distribution']}")
    except Exception as e:
        print(f"\n知识状态统计: 获取失败 ({e})")

    # 3. 调度任务统计
    try:
        scheduler = KnowledgeScheduler()
        tasks = scheduler.list_all()
        status_count = {}
        for t in tasks:
            status_count[t.status] = status_count.get(t.status, 0) + 1
        print(f"\n调度任务统计:")
        for status, count in sorted(status_count.items()):
            print(f"  {status}: {count}")
    except Exception as e:
        print(f"\n调度任务统计: 获取失败 ({e})")

    print()


def save_session(working_dir: str = None, summary: str = ""):
    """保存当前会话"""
    token = os.getenv("MEMOS_TOKEN")
    if not token:
        raise ValueError("MEMOS_TOKEN 环境变量未设置")
    client = MemosClient(token=token, agent="claude")

    if working_dir is None:
        working_dir = os.getcwd()

    client.save_session(working_dir, summary)
    print(f"Session saved: {working_dir}")


def main():
    parser = argparse.ArgumentParser(description="Claude Code Memos Integration")
    parser.add_argument("--session-start", action="store_true",
                        help="Session start - load context")
    parser.add_argument("--session-end", action="store_true",
                        help="Session end - save context")
    parser.add_argument("--working-dir", default=None,
                        help="Working directory")
    parser.add_argument("--user-message", default=None,
                        help="用户输入（用于意图判定）")
    parser.add_argument("--summary", default="",
                        help="Session summary")
    parser.add_argument("--authorize", nargs="+", default=None,
                        help="授权读取的跨agent列表，如 hermes openclaw")
    parser.add_argument("--session-messages", default=None,
                        help="会话消息历史(JSON格式)，用于自动复盘")
    parser.add_argument("--kia-check", action="store_true",
                        help="检查Knowledge-in-Action调度器中的到期提醒")
    parser.add_argument("--stats", action="store_true",
                        help="显示Mnemos系统统计")

    args = parser.parse_args()

    if args.stats:
        show_stats()
        return

    if args.session_start:
        context = get_context_for_claude(
            args.working_dir,
            user_message=args.user_message,
            authorize_cross=args.authorize
        )
        print(context)
    elif args.session_end:
        save_session(args.working_dir, args.summary)

        # Knowledge-in-Action 自动复盘
        retro_result = ""
        if args.session_messages:
            retro_result = run_retrospective(args.session_messages)
            if retro_result:
                print(retro_result)

        # 子 Agent 蒸馏：将 session 消息加入队列
        session_task_type = ""
        session_task_subtype = ""
        if args.session_messages:
            try:
                from core.distillation_queue import enqueue
                messages = json.loads(args.session_messages)
                # 生成 session_id（与 save_session 保持一致）
                wd = args.working_dir or os.getcwd()
                dir_hash = __import__('hashlib').md5(wd.encode()).hexdigest()[:8]
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                sid = f"claude:{dir_hash}:{ts}"
                enqueue(
                    session_id=sid,
                    messages=messages,
                    meta={
                        "source": "claude",
                        "working_dir": wd,
                        "summary": args.summary,
                        "has_retrospective": bool(retro_result),
                    }
                )
                print(f"[Distill] Session queued for agent distillation: {sid}")
                pending_count = len(__import__('core.distillation_queue', fromlist=['list_pending']).list_pending())
                print(f"[Distill] Pending tasks: {pending_count}")

                # 尝试识别任务类型（用于信号采集）
                try:
                    classifier = TaskClassifier()
                    result = classifier.classify(messages)
                    if result.confidence >= 0.7:
                        session_task_type = result.task_type
                        session_task_subtype = result.subtype
                except Exception as e:
                    logger.warning(f"任务分类失败: {e}")
            except Exception as e:
                print(f"[Distill] Queue failed: {e}")

        # ========== 用户画像闭环：信号采集 ==========
        if args.session_messages:
            try:
                messages = json.loads(args.session_messages)
                sig_count = _collect_session_signal(
                    messages,
                    working_dir=args.working_dir or os.getcwd(),
                    task_type=session_task_type,
                    task_subtype=session_task_subtype,
                )
                if sig_count > 0:
                    print(f"[Persona] Session signal collected: {sig_count}")
            except Exception as e:
                print(f"[Persona] Signal collection error: {e}")

        # KIA Orchestrator 周期任务（轻量模式，不依赖 Memos）
        run_kia_cycles()
    elif args.kia_check:
        # 检查 Knowledge-in-Action 调度器中的到期提醒
        scheduler = KnowledgeScheduler()
        reminders = scheduler.get_pending_reminders()
        missed = scheduler.startup_compensation()
        all_reminders = reminders + missed
        if all_reminders:
            print(f"[KIA] 发现 {len(all_reminders)} 个到期提醒:")
            for task in all_reminders:
                print(f"  - {task.task_type}/{task.subtype}: {task.due_date[:10]}")
                print(f"    {scheduler.format_reminder(task)}")
                scheduler.mark_reminded(task.task_id)
        else:
            print("[KIA] 暂无到期提醒")
    else:
        # 默认输出帮助
        parser.print_help()


if __name__ == "__main__":
    main()
