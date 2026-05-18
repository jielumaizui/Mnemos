"""
In-process Guard - 执行中守护

执行过程中检测风险点，三级策略：
- 轻微偏差：静默记录，任务完成后汇总报告
- 中等偏差：AI 回复中自然融入提醒
- 严重偏差：打断用户，明确要求确认

避免打断用户思路，非侵入式保护。
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Dict, Optional

from .pre_flight_injector import ChecklistItem, LoadedKnowledge
from core.persona.blindspot_analyzer import BlindSpotProfileManager, BlindSpot, ChallengeBalancer


class GuardLevel(Enum):
    """守护级别"""
    SILENT = "silent"       # 轻微：静默记录
    HINT = "hint"           # 中等：自然融入
    INTERRUPT = "interrupt" # 严重：打断确认


@dataclass
class GuardAlert:
    """守护告警"""
    level: GuardLevel
    checklist_item: ChecklistItem
    triggered_by: str       # 触发来源：user/ai
    trigger_text: str       # 触发文本
    suggestion: str         # 建议内容
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


@dataclass
class GuardSession:
    """守护会话状态"""
    task_type: str
    subtype: str
    checklist: List[ChecklistItem]
    triggered_alerts: List[GuardAlert] = field(default_factory=list)
    silent_records: List[Dict] = field(default_factory=list)
    hint_used: set = field(default_factory=set)


class InProcessGuard:
    """执行中守护"""

    # 严重偏差关键词（触发 INTERRUPT）
    CRITICAL_KEYWORDS = {
        "coding": ["rm -rf", "drop table", "delete from", "truncate", "os.system"],
        "marketing": ["全部预算", "all budget", "无门槛", "无限制"],
        "analysis": ["删除数据", "修改原始数据", "造假"],
        "strategy": ["all in", "全部押注", "孤注一掷"],
    }

    def __init__(self, knowledge: Optional[LoadedKnowledge] = None):
        self.session = None
        self.blindspot_manager = BlindSpotProfileManager()
        self.challenge_balancer = ChallengeBalancer()
        self.contextual_mode = "normal"  # normal/exploration/execution/fatigue/urgency
        self.session_messages = []  # 记录session消息用于情境推断
        if knowledge:
            self.start_session(knowledge)

    def start_session(self, knowledge: LoadedKnowledge):
        """开始守护会话"""
        self.session = GuardSession(
            task_type=knowledge.task_type,
            subtype=knowledge.subtype,
            checklist=knowledge.checklist
        )
        self.session_messages = []
        self.contextual_mode = "normal"

    def _infer_contextual_mode(self, user_message: str) -> str:
        """
        推断当前情境模式。

        Returns:
            normal / exploration / execution / fatigue / urgency
        """
        content = user_message.lower()
        all_text = " ".join(m.lower() for m in self.session_messages[-5:]) if self.session_messages else content

        # 疲劳检测
        fatigue_signals = ["累了", "困了", "先这样", "明天再说", "懒得", "没精力"]
        if any(s in all_text for s in fatigue_signals):
            return "fatigue"

        # 紧急检测
        urgency_signals = ["快点", "着急", "马上", "立刻", "deadline", "今晚必须", "急"]
        if any(s in content for s in urgency_signals):
            return "urgency"

        # 探索模式检测
        exploration_signals = ["如果", "能不能", "试试", "还有其他", "发散", "假设", "也许"]
        if any(s in content for s in exploration_signals):
            return "exploration"

        # 执行模式检测
        execution_signals = ["开始吧", "执行", "推进", "下一步", "继续", "就这个", "确定"]
        if any(s in content for s in execution_signals):
            return "execution"

        return "normal"

    def _adjust_level_by_context(self, level: GuardLevel, mode: str) -> GuardLevel:
        """根据情境调整守护级别"""
        if mode == "fatigue":
            # 疲劳时降低打扰
            if level == GuardLevel.INTERRUPT:
                return GuardLevel.HINT
            elif level == GuardLevel.HINT:
                return GuardLevel.SILENT
        elif mode == "urgency":
            # 紧急时只保留严重告警
            if level == GuardLevel.HINT:
                return GuardLevel.SILENT
        elif mode == "exploration":
            # 探索模式允许更多试错
            if level == GuardLevel.INTERRUPT:
                return GuardLevel.HINT
        elif mode == "execution":
            # 执行模式减少干扰
            if level == GuardLevel.HINT:
                return GuardLevel.SILENT

        return level

    def check(self, user_message: str, ai_response: str = "") -> Optional[GuardAlert]:
        """
        检测当前对话是否触及风险点

        Args:
            user_message: 用户消息
            ai_response: AI 回复（如果有）

        Returns:
            GuardAlert 或 None
        """
        if not self.session or not self.session.checklist:
            return None

        # 记录消息用于情境推断
        self.session_messages.append(user_message)

        # 推断当前情境模式
        self.contextual_mode = self._infer_contextual_mode(user_message)

        # 1. 先检查严重偏差
        critical = self._check_critical(user_message, ai_response)
        if critical:
            # 严重偏差不受情境模式影响，始终告警
            return critical

        # 2. 检查 checklist 中的风险点
        for item in self.session.checklist:
            # 跳过已触发的严重项
            if item.item in [a.checklist_item.item for a in self.session.triggered_alerts
                            if a.level == GuardLevel.INTERRUPT]:
                continue

            # 检查用户消息中的触发关键词
            if item.trigger_keywords:
                matched_kw = self._match_keywords(user_message, item.trigger_keywords)
                if matched_kw:
                    level = self._determine_level(item, "user")
                    # 根据情境调整级别
                    level = self._adjust_level_by_context(level, self.contextual_mode)

                    alert = GuardAlert(
                        level=level,
                        checklist_item=item,
                        triggered_by="user",
                        trigger_text=matched_kw,
                        suggestion=self._generate_suggestion(item)
                    )
                    self._record_alert(alert)
                    return alert

            # 检查 AI 回复中的风险模式
            if ai_response and item.risk_patterns:
                matched_pattern = self._match_keywords(ai_response, item.risk_patterns)
                if matched_pattern:
                    level = self._determine_level(item, "ai")
                    # 根据情境调整级别
                    level = self._adjust_level_by_context(level, self.contextual_mode)

                    alert = GuardAlert(
                        level=level,
                        checklist_item=item,
                        triggered_by="ai",
                        trigger_text=matched_pattern,
                        suggestion=self._generate_suggestion(item)
                    )
                    self._record_alert(alert)
                    return alert

        return None

    def check_silent(self, user_message: str, ai_response: str = "") -> List[Dict]:
        """
        静默检测（不返回告警，只记录到内部日志）
        用于轻微偏差的批量检测

        Returns:
            记录列表
        """
        records = []
        if not self.session or not self.session.checklist:
            return records

        for item in self.session.checklist:
            # 只处理轻微级别的项
            if item.severity not in ["low", "medium"]:
                continue

            matched = None
            if item.trigger_keywords:
                matched = self._match_keywords(user_message, item.trigger_keywords)
            if not matched and ai_response and item.risk_patterns:
                matched = self._match_keywords(ai_response, item.risk_patterns)

            if matched:
                record = {
                    "item": item.item,
                    "severity": item.severity,
                    "trigger": matched,
                    "timestamp": datetime.now().isoformat(),
                }
                self.session.silent_records.append(record)
                records.append(record)

        return records

    def _check_critical(self, user_message: str, ai_response: str) -> Optional[GuardAlert]:
        """检查严重偏差"""
        combined = (user_message + " " + ai_response).lower()

        critical_keywords = self.CRITICAL_KEYWORDS.get(self.session.task_type, [])
        for kw in critical_keywords:
            if kw.lower() in combined:
                return GuardAlert(
                    level=GuardLevel.INTERRUPT,
                    checklist_item=ChecklistItem(
                        item="严重风险检测",
                        source="system",
                        severity="critical"
                    ),
                    triggered_by="system",
                    trigger_text=kw,
                    suggestion=f"⚠️ 检测到高风险操作关键词「{kw}」，请确认是否继续？"
                )

        return None

    def _match_keywords(self, text: str, keywords: List[str]) -> Optional[str]:
        """匹配关键词，返回第一个匹配的"""
        text_lower = text.lower()
        for kw in keywords:
            if kw.lower() in text_lower:
                return kw
        return None

    def _determine_level(self, item: ChecklistItem, triggered_by: str) -> GuardLevel:
        """确定守护级别"""
        # 严重性映射
        if item.severity == "critical":
            return GuardLevel.INTERRUPT
        elif item.severity == "high":
            return GuardLevel.HINT if triggered_by == "user" else GuardLevel.INTERRUPT
        elif item.severity == "medium":
            # 用户触发 -> HINT，AI 自身风险 -> SILENT（让 AI 自己调整）
            return GuardLevel.HINT if triggered_by == "user" else GuardLevel.SILENT
        else:
            return GuardLevel.SILENT

    def _generate_suggestion(self, item: ChecklistItem) -> str:
        """生成建议文本"""
        if item.detail:
            return f"💡 {item.item}：{item.detail}"
        return f"💡 注意：{item.item}"

    def _record_alert(self, alert: GuardAlert):
        """记录告警"""
        if self.session:
            self.session.triggered_alerts.append(alert)

    def get_silent_summary(self) -> str:
        """获取静默记录汇总（任务完成后报告）"""
        if not self.session or not self.session.silent_records:
            return ""

        lines = ["📋 本次任务偏差记录："]
        for i, record in enumerate(self.session.silent_records, 1):
            lines.append(f"  {i}. {record['item']}（触发词：{record['trigger']}）")

        return "\n".join(lines)

    def get_checklist_usage(self) -> List[Dict]:
        """
        获取 checklist 使用情况（用于复盘）

        Returns:
            [{"item": str, "loaded": bool, "used": bool, "triggered": bool, "level": str}, ...]
        """
        if not self.session:
            return []

        usage = []
        triggered_items = {a.checklist_item.item: a.level.value for a in self.session.triggered_alerts}
        silent_items = {r["item"]: r["severity"] for r in self.session.silent_records}

        for item in self.session.checklist:
            item_name = item.item
            usage.append({
                "item": item_name,
                "loaded": True,
                "used": item_name in triggered_items or item_name in silent_items,
                "triggered": item_name in triggered_items,
                "level": triggered_items.get(item_name, silent_items.get(item_name, "none")),
                "severity": item.severity,
            })

        return usage

    def format_hint_for_ai(self, alert: GuardAlert) -> str:
        """
        格式化中等偏差提示，供 AI 自然融入回复

        返回的文本应该能被 AI 在回复中自然引用
        """
        if alert.level != GuardLevel.HINT:
            return ""

        return (
            f"[Guard Hint] {alert.checklist_item.item}"
            f"{f' - {alert.checklist_item.detail}' if alert.checklist_item.detail else ''}"
        )

    def format_interrupt_message(self, alert: GuardAlert) -> str:
        """格式化严重偏差打断消息"""
        if alert.level != GuardLevel.INTERRUPT:
            return ""

        return (
            f"⚠️ **风险提醒**\n\n"
            f"检测到可能的问题：{alert.checklist_item.item}\n\n"
            f"{alert.suggestion}\n\n"
            f"请确认是否继续当前操作？"
        )


# ========== 便捷函数 ==========

def create_guard(knowledge: LoadedKnowledge) -> InProcessGuard:
    """便捷函数：创建守护"""
    return InProcessGuard(knowledge)
