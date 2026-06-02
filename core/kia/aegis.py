"""
In-process Guard - 执行中守护

执行过程中检测风险点，三级策略：
- 轻微偏差：静默记录，任务完成后汇总报告
- 中等偏差：AI 回复中自然融入提醒
- 严重偏差：打断用户，明确要求确认

避免打断用户思路，非侵入式保护。
"""
# Aegis — 宙斯神盾 — 执行中守护，KIA 闭环的实时防护
# 原模块: in_process_guard.py



import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Dict, Optional, Tuple, Set

from .prophasis import ChecklistItem, LoadedKnowledge
from core.persona.hamartia import BlindSpotProfileManager, BlindSpot, ChallengeBalancer


import logging
logger = logging.getLogger(__name__)
class GuardLevel(Enum):
    """守护级别"""
    SILENT = "silent"       # 轻微：静默记录
    HINT = "hint"           # 中等：自然融入
    INTERRUPT = "interrupt" # 严重：打断确认


# ==================== SmartMatcher 三层匹配引擎 ====================

class SmartMatcher:
    """三层级联匹配引擎

    【E14 三层匹配引擎补全】
    层级1 — 精确匹配：文本完全相等（最高置信度）
    层级2 — 关键词匹配：子串包含（当前已有）
    层级3 — 语义匹配：基于词袋 Jaccard 相似度（零 API 成本）
    """

    def __init__(self, semantic_threshold: float = 0.65):
        self.semantic_threshold = semantic_threshold
        self.negation_words = {"不要", "别", "勿", "无需", "不用", "禁止", "避免", "不能", "不要直接"}
        self.question_markers = {"?", "？", "怎么", "如何", "为什么", "会怎样", "是什么意思", "是否"}
        self.command_verbs = {"删除", "清空", "执行", "运行", "部署", "发布", "修改", "覆盖", "drop", "truncate", "rm"}

    def match_exact(self, text: str, candidates: List[str]) -> Optional[Tuple[str, float]]:
        """精确匹配：文本与候选完全相等"""
        text_lower = text.strip().lower()
        for cand in candidates:
            if cand.strip().lower() == text_lower:
                return cand, 1.0
        return None

    def match_keyword(self, text: str, keywords: List[str]) -> Optional[Tuple[str, float]]:
        """关键词匹配：子串包含"""
        text_lower = text.lower()
        for kw in keywords:
            kw_lower = kw.lower()
            idx = text_lower.find(kw_lower)
            if idx >= 0:
                score = self._contextual_score(text, kw, idx, 0.85)
                if score >= 0.5:
                    return kw, score
        return None

    def match_semantic(self, text: str, references: List[str]) -> Optional[Tuple[str, float]]:
        """语义匹配：词袋 Jaccard 相似度（零 API 成本）"""
        text_words = set(re.findall(r'[\w\u4e00-\u9fa5]+', text.lower()))
        if not text_words:
            return None

        best_ref = None
        best_score = 0.0

        for ref in references:
            ref_words = set(re.findall(r'[\w\u4e00-\u9fa5]+', ref.lower()))
            if not ref_words:
                continue
            intersection = text_words & ref_words
            union = text_words | ref_words
            score = len(intersection) / len(union) if union else 0.0
            if score > best_score and score >= self.semantic_threshold:
                best_score = score
                best_ref = ref

        if best_ref:
            return best_ref, best_score
        return None

    def match_three_tier(self, text: str,
                         exact_candidates: List[str] = None,
                         keywords: List[str] = None,
                         semantic_refs: List[str] = None) -> Optional[Dict]:
        """三层级联匹配：依次尝试精确 → 关键词 → 语义"""
        # Layer 1: Exact
        if exact_candidates:
            result = self.match_exact(text, exact_candidates)
            if result:
                return {"layer": 1, "type": "exact", "match": result[0], "score": result[1]}

        # Layer 2: Keyword
        if keywords:
            result = self.match_keyword(text, keywords)
            if result:
                return {"layer": 2, "type": "keyword", "match": result[0], "score": result[1]}

        # Layer 3: Semantic
        if semantic_refs:
            result = self.match_semantic(text, semantic_refs)
            if result:
                return {"layer": 3, "type": "semantic", "match": result[0], "score": result[1]}

        return None

    def _contextual_score(self, text: str, keyword: str, pos: int, base_score: float) -> float:
        window_start = max(0, pos - 10)
        window_end = min(len(text), pos + len(keyword) + 10)
        window = text[window_start:window_end]
        prefix = text[max(0, pos - 8):pos]
        score = base_score

        if any(word in prefix for word in self.negation_words):
            score *= 0.2
        if any(marker in text for marker in self.question_markers):
            score *= 0.45
        if self._is_in_code_block(text, pos) or self._is_quoted(text, pos):
            score *= 0.5
        keyword_lower = keyword.lower()
        if any(verb in window.lower().replace(keyword_lower, "") for verb in self.command_verbs):
            score = min(1.0, score + 0.1)
        return score

    @staticmethod
    def _is_in_code_block(text: str, pos: int) -> bool:
        return text[:pos].count("```") % 2 == 1

    @staticmethod
    def _is_quoted(text: str, pos: int) -> bool:
        before = text[:pos]
        return before.count("`") % 2 == 1 or before.count('"') % 2 == 1 or before.count("“") > before.count("”")


class DuplicateWorkDetector:
    """重复工作检测器

    检测用户是否在做之前已经做过/讨论过的工作。
    基于消息指纹 + 关键词重叠 + 语义相似度。
    """

    def __init__(self, history_messages: List[str] = None):
        self.history = history_messages or []
        self.matcher = SmartMatcher(semantic_threshold=0.55)

    def _fingerprint(self, text: str) -> str:
        """生成文本指纹"""
        cleaned = re.sub(r'[^\w\u4e00-\u9fa5]', '', text.lower())
        return cleaned[:100]

    def is_duplicate(self, message: str, threshold: float = 0.70) -> Tuple[bool, float, str]:
        """
        检测消息是否与历史记录重复

        Returns:
            (是否重复, 相似度, 原因)
        """
        if not self.history:
            return False, 0.0, "No history"

        msg_fp = self._fingerprint(message)

        for hist in self.history:
            hist_fp = self._fingerprint(hist)

            # 1. 指纹精确匹配
            if msg_fp == hist_fp and len(msg_fp) > 10:
                return True, 1.0, f"Exact fingerprint match with history"

            # 2. 语义相似度
            result = self.matcher.match_semantic(message, [hist])
            if result:
                score = result[1]
                if score >= threshold:
                    return True, score, f"Semantic similarity {score:.2f} with history"

        # 3. 关键词重叠率（快速过滤）
        msg_words = set(re.findall(r'[\w\u4e00-\u9fa5]+', message.lower()))
        if len(msg_words) < 3:
            return False, 0.0, "Too few words"

        best_overlap = 0.0
        best_hist = ""
        for hist in self.history:
            hist_words = set(re.findall(r'[\w\u4e00-\u9fa5]+', hist.lower()))
            if not hist_words:
                continue
            overlap = len(msg_words & hist_words) / len(msg_words | hist_words)
            if overlap > best_overlap:
                best_overlap = overlap
                best_hist = hist[:50]

        if best_overlap >= threshold:
            return True, best_overlap, f"Keyword overlap {best_overlap:.2f} with: {best_hist}..."

        return False, best_overlap, "No significant overlap"

    def add_message(self, message: str):
        """添加消息到历史"""
        self.history.append(message)
        # 限制历史长度，防止内存膨胀
        if len(self.history) > 1000:
            self.history = self.history[-500:]


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
        self.smart_matcher = SmartMatcher()
        self.duplicate_detector = DuplicateWorkDetector()
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
        # 重置重复检测器历史
        self.duplicate_detector = DuplicateWorkDetector()

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

    # 通用高风险关键词（不依赖 checklist，作为兜底规则）
    _DEFAULT_CRITICAL_KEYWORDS = [
        "删除生产", "删除数据库", "drop database", "drop table",
        "rm -rf", "rm -rf /", "覆盖生产", "truncate", "delete from",
        "git push --force", "terraform apply", "kubectl delete",
        "密钥", "password", "token", "api key", "secret",
        "不可逆", "无法回滚", "未测试", "直接上线",
    ]

    def check(self, user_message: str, ai_response: str = "",
              context: Optional[Dict] = None) -> Optional[GuardAlert]:
        """
        检测当前对话是否触及风险点

        Args:
            user_message: 用户消息
            ai_response: AI 回复（如果有）
            context: 用户操作上下文（current_file, current_command, git_status）

        Returns:
            GuardAlert 或 None
        """
        # 0. 默认高风险规则检查（不依赖 checklist/session）
        combined = (user_message + " " + ai_response).lower()
        for kw in self._DEFAULT_CRITICAL_KEYWORDS:
            if kw.lower() in combined:
                return GuardAlert(
                    level=GuardLevel.INTERRUPT,
                    checklist_item=ChecklistItem(
                        item="高风险操作检测",
                        source="system",
                        severity="critical"
                    ),
                    triggered_by="system",
                    trigger_text=kw,
                    suggestion=f"⚠️ 检测到高风险操作关键词「{kw}」，请确认是否继续？"
                )

        # 1.5 上下文语义风险检查（不依赖 checklist）
        ctx_alert = self._check_context_risk(context, user_message)
        if ctx_alert:
            return ctx_alert

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

        # 2. 重复工作检测（SmartMatcher Layer 3 语义匹配）
        is_dup, dup_score, dup_reason = self.duplicate_detector.is_duplicate(user_message)
        self.duplicate_detector.add_message(user_message)
        if is_dup and dup_score >= 0.80:
            return GuardAlert(
                level=GuardLevel.HINT,
                checklist_item=ChecklistItem(
                    item="重复工作提醒",
                    source="system",
                    severity="medium"
                ),
                triggered_by="user",
                trigger_text=user_message[:100],
                suggestion=f"💡 检测到可能与之前工作重复（相似度 {dup_score:.0%}）：{dup_reason}"
            )

        # 3. 检查 checklist 中的风险点（三层匹配引擎）
        for item in self.session.checklist:
            # 跳过已触发的严重项
            if item.item in [a.checklist_item.item for a in self.session.triggered_alerts
                            if a.level == GuardLevel.INTERRUPT]:
                continue

            # 检查用户消息中的触发关键词（三层匹配）
            if item.trigger_keywords:
                match_result = self._match_three_tier(user_message, item.trigger_keywords)
                if match_result:
                    level = self._determine_level(item, "user")
                    # 根据情境调整级别
                    level = self._adjust_level_by_context(level, self.contextual_mode)
                    # 语义匹配降低一级（减少误报）
                    if match_result.get("layer") == 3 and level == GuardLevel.INTERRUPT:
                        level = GuardLevel.HINT

                    alert = GuardAlert(
                        level=level,
                        checklist_item=item,
                        triggered_by="user",
                        trigger_text=match_result["match"],
                        suggestion=self._generate_suggestion(item)
                    )
                    self._record_alert(alert)
                    return alert

            # 检查 AI 回复中的风险模式
            if ai_response and item.risk_patterns:
                match_result = self._match_three_tier(ai_response, item.risk_patterns)
                if match_result:
                    level = self._determine_level(item, "ai")
                    # 根据情境调整级别
                    level = self._adjust_level_by_context(level, self.contextual_mode)
                    # 语义匹配降低一级
                    if match_result.get("layer") == 3 and level == GuardLevel.INTERRUPT:
                        level = GuardLevel.HINT

                    alert = GuardAlert(
                        level=level,
                        checklist_item=item,
                        triggered_by="ai",
                        trigger_text=match_result["match"],
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
            match_layer = 0
            if item.trigger_keywords:
                result = self._match_three_tier(user_message, item.trigger_keywords)
                if result:
                    matched = result["match"]
                    match_layer = result.get("layer", 2)
            if not matched and ai_response and item.risk_patterns:
                result = self._match_three_tier(ai_response, item.risk_patterns)
                if result:
                    matched = result["match"]
                    match_layer = result.get("layer", 2)

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
        combined_raw = user_message + " " + ai_response
        combined = combined_raw.lower()

        critical_keywords = self.CRITICAL_KEYWORDS.get(self.session.task_type, [])
        for kw in critical_keywords:
            pos = combined.find(kw.lower())
            if pos >= 0:
                score = self.smart_matcher._contextual_score(combined_raw, kw, pos, 1.0)
                if score < 0.5:
                    continue
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

    def _check_context_risk(self, context: Optional[Dict], user_message: str = "") -> Optional[GuardAlert]:
        """基于用户操作上下文进行语义风险匹配"""
        if not context:
            return None
        score = 0
        hints = []
        current_file = context.get("current_file", "") or ""
        current_command = context.get("current_command", "") or ""
        git_status = context.get("git_status", "") or ""

        file_lower = current_file.lower()
        op_text = ((current_command or "") + " " + user_message).lower()

        # 高风险：生产环境文件 + 危险操作
        if any(p in file_lower for p in ("prod/", "production/")):
            if any(k in op_text for k in ("rm", "delete", "覆盖", "truncate", "drop")):
                score += 4
                hints.append("生产环境文件危险操作")

        # 高风险：高危命令
        cmd_lower = (current_command or "").lower()
        if any(k in cmd_lower for k in ("git push --force", "terraform apply", "kubectl delete")):
            score += 4
            hints.append("高危命令执行")

        # 中风险：未提交修改 + git checkout
        git_status_text = git_status or ""
        if "未提交" in git_status_text or "modified" in git_status_text.lower() or "changes" in git_status_text.lower():
            if "git checkout" in cmd_lower or "git checkout" in op_text:
                score += 2
                hints.append("未提交修改下切换分支")

        if score >= 4:
            msg = "⚠️ " + "，".join(hints) + "，请确认。"
            msg = msg[:80]
            return GuardAlert(
                level=GuardLevel.INTERRUPT,
                checklist_item=ChecklistItem(
                    item="上下文高风险",
                    source="context_guard",
                    severity="critical"
                ),
                triggered_by="system",
                trigger_text="; ".join(hints),
                suggestion=msg
            )
        elif score >= 2:
            msg = "⚠️ " + "，".join(hints) + "，建议检查。"
            msg = msg[:80]
            return GuardAlert(
                level=GuardLevel.HINT,
                checklist_item=ChecklistItem(
                    item="上下文中风险",
                    source="context_guard",
                    severity="high"
                ),
                triggered_by="system",
                trigger_text="; ".join(hints),
                suggestion=msg
            )
        return None

    def smart_check(self, user_message: str, ai_response: str = "") -> List[GuardAlert]:
        """返回所有匹配风险点，按级别排序；用于批量守护和测试。"""
        alerts = []
        first = self.check(user_message, ai_response)
        if first:
            alerts.append(first)
        for record in self.check_silent(user_message, ai_response):
            alerts.append(GuardAlert(
                level=GuardLevel.SILENT,
                checklist_item=ChecklistItem(
                    item=record["item"],
                    source="system",
                    severity=record["severity"],
                ),
                triggered_by="system",
                trigger_text=record["trigger"],
                suggestion=f"静默记录：{record['item']}",
                timestamp=record["timestamp"],
            ))
        order = {GuardLevel.INTERRUPT: 0, GuardLevel.HINT: 1, GuardLevel.SILENT: 2}
        return sorted(alerts, key=lambda alert: order[alert.level])

    def _match_three_tier(self, text: str, keywords: List[str]) -> Optional[Dict]:
        """三层级联匹配引擎（精确 → 关键词 → 语义）

        Args:
            text: 待检测文本
            keywords: 关键词/模式列表

        Returns:
            {"layer": int, "type": str, "match": str, "score": float} 或 None
        """
        return self.smart_matcher.match_three_tier(
            text,
            exact_candidates=keywords,
            keywords=keywords,
            semantic_refs=keywords
        )

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
        # 发射 guard_alert 事件
        try:
            from core.mnemos_bus import publish_event
            publish_event("guard_alert", "aegis", {
                "level": alert.level.value,
                "checklist_item": alert.checklist_item.item,
                "triggered_by": alert.triggered_by,
                "trigger_text": alert.trigger_text[:200],
                "session_id": getattr(self.session, 'task_type', 'unknown') if self.session else 'unknown',
            })
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
            pass
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
