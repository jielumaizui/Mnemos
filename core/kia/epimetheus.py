"""
Auto-Retrospective - 自动复盘引擎

触发条件：
1. 用户明确说复盘关键词
2. 任务自然结束检测
3. 用户超过30分钟未回复（可选）

功能：
1. 提取预期目标 vs 实际结果
2. 对比分析差异
3. 记录 checklist 使用情况
4. 提取新增教训
5. 生成结构化复盘报告
"""

# Epimetheus — 后知之神 — 自动复盘引擎，事后反思与教训提取
# 原模块: auto_retrospective.py


import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional


import logging

# Constants extracted from magic numbers
AUTO_RETROSPECTIVE__ANALYZE_GAPS_DIFF_PCT = 30
RECAP_AREA_LABELS = {
    "budget": "预算/资源消耗",
    "participants": "参与人数",
    "conversion_rate": "转化率",
}
logger = logging.getLogger(__name__)
try:
    pass

    PERSONA_AVAILABLE = True
except ImportError:
    PERSONA_AVAILABLE = False


@dataclass
class GoalComparison:
    """目标对比"""

    area: str
    expected: str
    actual: str
    gap: str
    severity: str = "medium"  # critical/high/medium/low


@dataclass
class ChecklistUsage:
    """Checklist 使用情况"""

    item: str
    loaded: bool
    used: bool
    triggered: bool
    level: str  # none/silent/hint/interrupt
    severity: str
    reason_ignored: str = ""


@dataclass
class BlindSpotFocus:
    """盲区复盘焦点"""

    blindspot_type: str
    was_triggered: bool
    evidence: str
    recommendation: str
    severity: str = "medium"


@dataclass
class RetrospectiveResult:
    """复盘结果"""

    task_type: str
    subtype: str
    version: int
    expected_goals: Dict = field(default_factory=dict)
    actual_results: Dict = field(default_factory=dict)
    gaps: List[GoalComparison] = field(default_factory=list)
    checklist_usage: List[ChecklistUsage] = field(default_factory=list)
    new_lessons: List[str] = field(default_factory=list)
    blindspot_focus: List[BlindSpotFocus] = field(default_factory=list)
    summary: str = ""
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


class AutoRetrospective:
    """自动复盘引擎"""

    # 复盘触发关键词
    TRIGGER_PATTERNS = [
        r"复盘",
        r"总结一下",
        r"结束了",
        r"完成了",
        r"效果怎么样",
        r"结果如何",
        r"效果如何",
        r"review",
        r"retrospective",
        r"wrap up",
        r"done",
        r"finish",
        r"总结",
        r"收尾",
    ]

    # 自然结束检测模式
    ENDING_PATTERNS = [
        r"好的[,，]",
        r"谢谢",
        r"没问题",
        r"ok",
        r"收到",
    ]

    # 预期目标提取模式
    GOAL_PATTERNS = {
        "participants": [
            r"(\d+)[\s个]*人",
            r"参与[人数]*[:：]?\s*(\d+)",
            r"目标[人数]*[:：]?\s*(\d+)",
        ],
        "conversion_rate": [
            r"转化率[:：]?\s*(\d+(?:\.\d+)?)\s*%",
            r"转化[:：]?\s*(\d+(?:\.\d+)?)\s*%",
        ],
        "budget": [r"预算[:：]?\s*(\d+(?:\.\d+)?)\s*[万kK]?", r"费用[:：]?\s*(\d+(?:\.\d+)?)"],
        "timeline": [r"(\d+)[\s个]*天", r"周期[:：]?\s*(\d+)", r"时间[:：]?\s*(\d+)"],
    }

    def __init__(
        self, wiki_base: str | Path | None = None, recap_db_path: str | Path | None = None
    ):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else None
        self.recap_db_path = Path(recap_db_path).expanduser() if recap_db_path else None

    def should_trigger(self, messages: List[Dict]) -> bool:
        """
        检测是否应该触发复盘

        Args:
            messages: 完整会话记录

        Returns:
            是否触发
        """
        if not messages:
            return False

        # 1. 用户明确说复盘关键词
        last_message = messages[-1]
        if last_message.get("role") == "user":
            content = last_message.get("content", "")
            for pattern in self.TRIGGER_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    return True

        # CLI 场景下不把“谢谢/好的/ok”视为自然结束，避免误触发复盘。
        # checklist 完成、/done 等显式信号由调用方转成触发关键词或直接调用 generate()。
        return False

    def generate(
        self,
        task_type: str,
        subtype: str,
        messages: List[Dict],
        checklist_usage: List[Dict],
        expected_goals: Optional[Dict] = None,
        blindspot_profile: Optional[object] = None,
    ) -> RetrospectiveResult:
        """
        生成复盘报告

        Args:
            task_type: 任务类型
            subtype: 子类型
            messages: 完整会话记录
            checklist_usage: checklist 使用情况
            expected_goals: 预期目标（外部传入）
            blindspot_profile: 盲区画像（可选）

        Returns:
            RetrospectiveResult
        """
        # 1. 提取预期目标
        if not expected_goals:
            expected_goals = self._extract_goals(messages, is_expected=True)

        # 2. 提取实际结果
        actual_results = self._extract_goals(messages, is_expected=False)

        # 3. 对比分析
        gaps = self._analyze_gaps(expected_goals, actual_results)

        # 4. 格式化 checklist usage
        formatted_usage = self._format_checklist_usage(checklist_usage)

        # 5. 提取新增教训
        new_lessons = self._extract_new_lessons(messages, gaps, formatted_usage)

        # 6. 盲区校准（新增）
        blindspot_focus = self._analyze_blindspot_focus(
            messages, gaps, formatted_usage, blindspot_profile
        )

        # 7. 生成摘要
        summary = self._generate_summary(gaps, new_lessons, blindspot_focus)

        return RetrospectiveResult(
            task_type=task_type,
            subtype=subtype,
            version=0,  # 由 iteration_tracker 填充
            expected_goals=expected_goals,
            actual_results=actual_results,
            gaps=gaps,
            checklist_usage=formatted_usage,
            new_lessons=new_lessons,
            blindspot_focus=blindspot_focus,
            summary=summary,
        )

    def _extract_goals(self, messages: List[Dict], is_expected: bool = True) -> Dict:
        """从会话中提取目标信息"""
        results = {}

        all_text = self._select_goal_text(messages, is_expected=is_expected)

        # 提取各类目标
        for goal_type, patterns in self.GOAL_PATTERNS.items():
            for pattern in patterns:
                matches = re.findall(pattern, all_text)
                if matches:
                    # 取最后一个匹配（通常是确认后的最终值）
                    last_match = matches[-1]
                    if isinstance(last_match, tuple):
                        last_match = last_match[0]
                    results[goal_type] = last_match
                    break

        # 提取文本描述型目标
        if is_expected:
            # 找"预期"、"目标"、"希望"后面的内容
            expectation_patterns = [
                r"预期[:：]\s*(.+?)(?:\n|$)",
                r"目标[:：]\s*(.+?)(?:\n|$)",
                r"希望[:：]\s*(.+?)(?:\n|$)",
                r"想要[:：]\s*(.+?)(?:\n|$)",
            ]
            for pattern in expectation_patterns:
                matches = re.findall(pattern, all_text)
                match = matches[-1] if matches else None
                if match:
                    results["description"] = str(match).strip()
                    break
        else:
            # 找"实际"、"结果"后面的内容
            result_patterns = [
                r"实际[:：]\s*(.+?)(?:\n|$)",
                r"结果[:：]\s*(.+?)(?:\n|$)",
                r"最终[:：]\s*(.+?)(?:\n|$)",
            ]
            for pattern in result_patterns:
                matches = re.findall(pattern, all_text)
                match = matches[-1] if matches else None
                if match:
                    results["description"] = str(match).strip()
                    break

        return results

    @staticmethod
    def _select_goal_text(messages: List[Dict], is_expected: bool = True) -> str:
        expected_markers = ["预期", "目标", "希望", "想要", "计划"]
        actual_markers = ["实际", "结果", "最终", "达成", "完成"]
        markers = expected_markers if is_expected else actual_markers
        selected = [
            m.get("content", "")
            for m in messages
            if any(marker in m.get("content", "") for marker in markers)
        ]
        if selected:
            return "\n".join(selected)
        return "\n".join(m.get("content", "") for m in messages)

    def _analyze_gaps(self, expected: Dict, actual: Dict) -> List[GoalComparison]:
        """对比预期与实际，找出差异"""
        gaps = []

        # 对比数值型目标
        numeric_keys = ["participants", "conversion_rate", "budget"]
        for key in numeric_keys:
            if key in expected:
                exp_val = self._parse_numeric(expected[key])
                act_val = self._parse_numeric(actual.get(key, "0"))

                if exp_val is not None and act_val is not None:
                    diff_pct = ((act_val - exp_val) / exp_val * 100) if exp_val != 0 else 0

                    if abs(diff_pct) <= 10:
                        severity = "low"
                        gap_desc = f"基本达标（差异 {diff_pct:+.1f}%）"
                    elif abs(diff_pct) <= AUTO_RETROSPECTIVE__ANALYZE_GAPS_DIFF_PCT:
                        severity = "medium"
                        gap_desc = f"未完全达标（差异 {diff_pct:+.1f}%）"
                    else:
                        severity = "high"
                        gap_desc = f"显著偏差（差异 {diff_pct:+.1f}%）"

                    gaps.append(
                        GoalComparison(
                            area=key,
                            expected=str(expected[key]),
                            actual=str(actual.get(key, "未提及")),
                            gap=gap_desc,
                            severity=severity,
                        )
                    )
                elif act_val is None:
                    gaps.append(
                        GoalComparison(
                            area=key,
                            expected=str(expected[key]),
                            actual="未记录",
                            gap="缺少实际结果数据",
                            severity="medium",
                        )
                    )

        # 对比描述型目标
        if "description" in expected:
            if "description" not in actual:
                gaps.append(
                    GoalComparison(
                        area="目标达成",
                        expected=expected["description"],
                        actual="未记录结果",
                        gap="缺少结果反馈",
                        severity="medium",
                    )
                )

        return gaps

    def _parse_numeric(self, value) -> Optional[float]:
        """解析数值"""
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            # 去掉单位，提取数字
            cleaned = re.sub(r"[^\d.]", "", value)
            try:
                return float(cleaned) if cleaned else None
            except ValueError:
                return None
        return None

    def _format_checklist_usage(self, raw_usage: List[Dict]) -> List[ChecklistUsage]:
        """格式化 checklist 使用记录"""
        formatted = []
        for item in raw_usage:
            formatted.append(
                ChecklistUsage(
                    item=item.get("item", ""),
                    loaded=item.get("loaded", False),
                    used=item.get("used", False),
                    triggered=item.get("triggered", False),
                    level=item.get("level", "none"),
                    severity=item.get("severity", "medium"),
                    reason_ignored=item.get("reason_ignored", ""),
                )
            )
        return formatted

    def _extract_new_lessons(
        self,
        messages: List[Dict],
        gaps: List[GoalComparison],
        checklist_usage: List[ChecklistUsage],
    ) -> List[str]:
        """提取新增教训"""
        lessons = []
        all_text = " ".join([m.get("content", "") for m in messages])

        # 1. 从差异中提取教训
        for gap in gaps:
            if gap.severity in ["high", "critical"]:
                lesson = f"{gap.area}：{gap.gap}"
                lessons.append(lesson)

        # 2. 从 checklist 未执行项中提取教训
        for usage in checklist_usage:
            if usage.loaded and not usage.used:
                if usage.reason_ignored:
                    lessons.append(f"忽略「{usage.item}」的原因：{usage.reason_ignored}")
                else:
                    lessons.append(f"未执行「{usage.item}」，建议下次强制校验")

        # 3. 从会话中的"教训"、"发现"、"问题"等关键词提取
        lesson_patterns = [
            r"教训[:：]\s*(.+?)(?:\n|$)",
            r"发现[:：]\s*(.+?)(?:\n|$)",
            r"问题[:：]\s*(.+?)(?:\n|$)",
            r"不足[:：]\s*(.+?)(?:\n|$)",
            r"下次[:：]?\s*(.+?)(?:\n|$)",
        ]
        for pattern in lesson_patterns:
            matches = re.findall(pattern, all_text, re.IGNORECASE)
            for match in matches:
                if match.strip() and match.strip() not in lessons:
                    lessons.append(match.strip())

        # 去重并限制数量
        seen = set()
        unique_lessons = []
        for lesson in lessons:
            key = lesson[:50]  # 前50字作为去重键
            if key not in seen:
                seen.add(key)
                unique_lessons.append(lesson)

        return unique_lessons[:10]  # 最多10条

    def _analyze_blindspot_focus(
        self,
        messages: List[Dict],
        gaps: List[GoalComparison],
        checklist_usage: List[ChecklistUsage],
        blindspot_profile: object | None = None,
    ) -> List[BlindSpotFocus]:
        """
        分析本次任务是否触发了已知的盲区。

        策略：
        1. 如果有已确认的盲区，检查本次任务是否表现出相同模式
        2. 如果有suspected盲区，记录可能的证据
        3. 如果checklist中有盲区相关项被忽略，标记为盲区触发
        """
        focus_list = []

        if not blindspot_profile or not PERSONA_AVAILABLE:
            focus_list.append(
                BlindSpotFocus(
                    blindspot_type="blindspot_profile_unavailable",
                    was_triggered=False,
                    evidence="盲区画像未接入，本次复盘仅输出目标差异和 checklist 教训",
                    recommendation="可在画像系统可用后重新生成复盘焦点",
                    severity="low",
                )
            )
            return focus_list

        all_text = " ".join([m.get("content", "") for m in messages]).lower()

        # 检查已确认的盲区
        for bs in blindspot_profile.confirmed:  # type: ignore[attr-defined]
            triggered, evidence = self._check_blindspot_triggered(
                bs, all_text, gaps, checklist_usage
            )
            if triggered:
                focus_list.append(
                    BlindSpotFocus(
                        blindspot_type=bs.type,
                        was_triggered=True,
                        evidence=evidence,
                        recommendation=self._get_blindspot_recommendation(bs.type),
                        severity="high" if bs.confidence > 0.7 else "medium",
                    )
                )

        # 检查suspected盲区
        for bs in blindspot_profile.suspected:  # type: ignore[attr-defined]
            triggered, evidence = self._check_blindspot_triggered(
                bs, all_text, gaps, checklist_usage
            )
            if triggered:
                focus_list.append(
                    BlindSpotFocus(
                        blindspot_type=bs.type,
                        was_triggered=True,
                        evidence=evidence,
                        recommendation=f"本次任务表现出「{bs.type}」盲区特征，建议验证此假设",
                        severity="medium",
                    )
                )

        return focus_list

    def _check_blindspot_triggered(
        self,
        blindspot,
        all_text: str,
        gaps: List[GoalComparison],
        checklist_usage: List[ChecklistUsage],
    ) -> tuple:
        """检查单个盲区是否在本次任务中被触发"""
        if blindspot.type == "framing":
            return self._check_framing_blindspot(all_text)
        if blindspot.type == "option_gap":
            return self._check_option_gap_blindspot(all_text)
        if blindspot.type == "temporal":
            return self._check_temporal_blindspot(all_text, gaps)
        if blindspot.type == "preference_rigidity":
            return self._check_preference_rigidity_blindspot(checklist_usage)
        return False, ""

    def _check_framing_blindspot(self, all_text: str) -> tuple:
        """框架盲区：检查会话文本中是否有框架锁定词。"""
        framing_signals = ["只能", "只能", "必须", "不得不", "没有别的办法"]
        found = [s for s in framing_signals if s in all_text]
        if found:
            return True, f"检测到框架锁定信号：{'、'.join(found[:3])}"
        return False, ""

    def _check_option_gap_blindspot(self, all_text: str) -> tuple:
        """选项盲区：检查是否只考虑了 2 个选项。"""
        option_signals = ["二选一", "两个选择", "a还是b", "要么...要么"]
        found = [s for s in option_signals if s in all_text]
        if found:
            return True, f"检测到二元选择模式：{'、'.join(found[:3])}"
        return False, ""

    def _check_temporal_blindspot(
        self, all_text: str, gaps: List[GoalComparison]
    ) -> tuple:
        """时间盲区：检查短期导向信号或显著偏差。"""
        short_term_signals = ["先解决眼前", "不管以后", "以后再说", "暂时不管"]
        found = [s for s in short_term_signals if s in all_text]
        if found:
            return True, f"检测到短期导向信号：{'、'.join(found[:3])}"

        high_gaps = [g for g in gaps if g.severity == "high"]
        if high_gaps:
            return True, f"存在显著偏差：{high_gaps[0].gap}，可能与时间盲区相关"
        return False, ""

    def _check_preference_rigidity_blindspot(
        self, checklist_usage: List[ChecklistUsage]
    ) -> tuple:
        """偏好僵化：检查 checklist 中是否有用户习惯性忽略的项。"""
        ignored_habits = [u for u in checklist_usage if not u.used and u.level == "none"]
        if ignored_habits:
            return (
                True,
                f"习惯性忽略：{'、'.join([u.item for u in ignored_habits[:2]])}",
            )
        return False, ""

    @staticmethod
    def _get_blindspot_recommendation(blindspot_type: str) -> str:
        """获取盲区类型对应的建议"""
        recommendations = {
            "framing": "下次遇到类似问题时，先问自己：'这个问题本身可以被质疑吗？'",
            "option_gap": "强制要求自己列出第3个选项，即使它看起来不完美",
            "temporal": "在方案评估时增加'6个月后'的时间维度",
            "preference_rigidity": "本次任务中你习惯性地做了某些选择，下次尝试反直觉的选项",
        }
        return recommendations.get(blindspot_type, "注意此类盲区，下次有意识地检验")

    def _generate_summary(
        self,
        gaps: List[GoalComparison],
        new_lessons: List[str],
        blindspot_focus: List[BlindSpotFocus] | None = None,
    ) -> str:
        """生成复盘摘要"""
        lines = []

        if gaps:
            high_gaps = [g for g in gaps if g.severity in ["high", "critical"]]
            if high_gaps:
                lines.append(f"存在 {len(high_gaps)} 个显著偏差")
            else:
                lines.append("整体基本达标")

        if new_lessons:
            lines.append(f"提取 {len(new_lessons)} 条新教训")

        if blindspot_focus:
            triggered = [b for b in blindspot_focus if b.was_triggered]
            if triggered:
                lines.append(f"触发 {len(triggered)} 个已知盲区")

        return "；".join(lines) if lines else "暂无显著发现"

    def to_markdown(self, result: RetrospectiveResult) -> str:
        """生成 Markdown 复盘报告"""
        lines = [
            "---",
            "hermes_type: retrospective",
            f"task_type: {result.task_type}/{result.subtype}",
            f"version: {result.version}",
            f"created: {result.created_at[:10]}",
            "status: active",
            "---",
            "",
            f"# {result.task_type}/{result.subtype} v{result.version} 复盘",
            "",
            "## 预期目标",
        ]

        for key, val in result.expected_goals.items():
            lines.append(f"- **{key}**: {val}")
        if not result.expected_goals:
            lines.append("- 未明确记录预期目标")

        lines.extend(["", "## 实际结果"])
        for key, val in result.actual_results.items():
            lines.append(f"- **{key}**: {val}")
        if not result.actual_results:
            lines.append("- 未记录实际结果")

        if result.gaps:
            lines.extend(["", "## 差异分析"])
            for gap in result.gaps:
                emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(
                    gap.severity, "⚪"
                )
                lines.append(f"{emoji} **{gap.area}**")
                lines.append(f"  - 预期: {gap.expected}")
                lines.append(f"  - 实际: {gap.actual}")
                lines.append(f"  - 差异: {gap.gap}")

        if result.checklist_usage:
            lines.extend(["", "## Checklist 使用情况"])
            for usage in result.checklist_usage:
                status = "✅" if usage.used else "❌"
                lines.append(f"{status} **{usage.item}** ({usage.severity})")
                if not usage.used and usage.reason_ignored:
                    lines.append(f"   未执行原因: {usage.reason_ignored}")

        if result.new_lessons:
            lines.extend(["", "## 新增教训（进入下一版本校验清单）"])
            for i, lesson in enumerate(result.new_lessons, 1):
                lines.append(f"{i}. {lesson}")

        # 盲区校准与复盘焦点（新增）
        if result.blindspot_focus:
            lines.extend(["", "## 盲区校准与复盘焦点"])
            lines.append("")
            lines.append(
                "> 此部分基于你的盲区画像自动生成。如果分析不准确，"
                "你的反馈会帮助AI校准盲区检测。"
            )
            lines.append("")
            for focus in result.blindspot_focus:
                emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(focus.severity, "⚪")
                lines.append(f"{emoji} **{focus.blindspot_type}**")
                lines.append(f"  - 证据: {focus.evidence}")
                lines.append(f"  - 建议: {focus.recommendation}")
                lines.append("")

        if result.summary:
            lines.extend(["", "## 总结", result.summary])

        return "\n".join(lines)

    def create_recap_todo(
        self,
        result: RetrospectiveResult,
        report_path: str = "00-Dashboard.md",
    ) -> Optional[str]:
        """将复盘结果登记为系统复盘待办，交给强制复盘策略追达。"""
        try:
            from core.app.forced_retrospective import ForcedRetrospective

            forced = ForcedRetrospective(
                db_path=str(self.recap_db_path) if self.recap_db_path else None
            )
            severity = self._result_severity(result)
            topic = self._build_recap_topic(result)
            context = self._build_recap_context(result)
            suggested_points = self._build_recap_suggestions(result)
            return forced.create_system_recap(
                topic=topic,
                severity=severity,
                context=context,
                suggested_points=suggested_points,
            )
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logging.getLogger(__name__).warning(
                "Caught unexpected error at epimetheus.py", exc_info=True
            )
            return None

    def _build_recap_topic(self, result: RetrospectiveResult) -> str:
        """生成对人可读的复盘主题，避免只显示“存在 N 个显著偏差”。"""
        high_gaps = [g for g in result.gaps if g.severity in ("high", "critical")]
        if high_gaps:
            gap = high_gaps[0]
            area = self._readable_gap_area(gap.area)
            return (
                f"{result.task_type}/{result.subtype}：{area}偏差"
                f"（预期 {gap.expected}，实际 {gap.actual}）"
            )
        return result.summary or f"{result.task_type}/{result.subtype} 复盘"

    def _build_recap_context(self, result: RetrospectiveResult) -> str:
        """根据复盘结果生成提醒页面的'本次复盘背景'内容。"""
        lines = [
            f"**任务类型**：{result.task_type}/{result.subtype}",
            f"**复盘摘要**：{result.summary or '暂无摘要'}",
        ]

        if result.expected_goals:
            lines.append("\n**预期目标**：")
            for key, val in result.expected_goals.items():
                lines.append(f"- {key}: {val}")

        if result.actual_results:
            lines.append("\n**实际结果**：")
            for key, val in result.actual_results.items():
                lines.append(f"- {key}: {val}")

        if result.gaps:
            high_gaps = [g for g in result.gaps if g.severity in ("high", "critical")]
            if high_gaps:
                gap = high_gaps[0]
                area = self._readable_gap_area(gap.area)
                lines.append("\n**这次复盘要回答**：")
                lines.append(
                    f"- 为什么{area}从预期 {gap.expected} 变成实际 {gap.actual}？"
                )

            lines.append("\n**关键差异**：")
            for gap in result.gaps:
                area = self._readable_gap_area(gap.area)
                area_text = f"{area}（{gap.area}）" if area != gap.area else area
                lines.extend(
                    [
                        f"- 偏差项：{area_text}",
                        f"  - 严重程度：{gap.severity}",
                        f"  - 预期值：{gap.expected}",
                        f"  - 实际值：{gap.actual}",
                        f"  - 偏差含义：{gap.gap}",
                    ]
                )

        if result.new_lessons:
            lines.append("\n**已提取教训**：")
            for lesson in result.new_lessons[:5]:
                lines.append(f"- {lesson}")

        if result.blindspot_focus:
            triggered = [b for b in result.blindspot_focus if b.was_triggered]
            if triggered:
                lines.append("\n**盲区触发**：")
                for focus in triggered[:3]:
                    lines.append(f"- {focus.blindspot_type}: {focus.evidence}")

        return "\n".join(lines)

    def _build_recap_suggestions(self, result: RetrospectiveResult) -> str:
        """根据复盘结果生成'建议复盘重点'。"""
        suggestions = []

        if result.gaps:
            high_gaps = [g for g in result.gaps if g.severity in ("high", "critical")]
            if high_gaps:
                gap = high_gaps[0]
                area = self._readable_gap_area(gap.area)
                suggestions.extend(
                    [
                        f"- 为什么{area}从预期 {gap.expected} 变成实际 {gap.actual}？",
                        f"- 下次如何在执行中提前发现{area}偏差？",
                    ]
                )
            else:
                suggestions.append("- 复核差异项，确认是否已转化为可执行的改进项")

        if result.new_lessons:
            suggestions.append("- 将本次提取的教训补充到对应知识页面的校验清单")

        ignored_checklist = [
            u
            for u in result.checklist_usage
            if u.loaded and not u.used and u.severity in ("high", "critical")
        ]
        if ignored_checklist:
            suggestions.append(f"- 检查被忽略的高风险 checklist 项：{ignored_checklist[0].item}")

        if result.blindspot_focus:
            triggered = [b for b in result.blindspot_focus if b.was_triggered]
            if triggered:
                suggestions.append(
                    f"- 验证盲区假设：{triggered[0].blindspot_type} — {triggered[0].recommendation}"
                )

        if not suggestions:
            suggestions.append("- 回顾本次任务流程，确认是否有遗漏的决策或改进点")

        return "\n".join(suggestions)

    @staticmethod
    def _readable_gap_area(area: str) -> str:
        """把内部差异字段转成提醒页里用户能理解的名称。"""
        return RECAP_AREA_LABELS.get(area, area)

    def write_dashboard(
        self, recaps: List[Dict], dashboard_path: str | Path | None = None
    ) -> Optional[Path]:
        """写入 Wiki 看板兜底，让待复盘事项即使未被 Agent 提醒也可见。"""
        if dashboard_path:
            path = Path(dashboard_path)
        elif self.wiki_base:
            path = self.wiki_base / "00-Dashboard.md"
        else:
            return None

        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "---",
            "hermes_type: dashboard",
            "auto_updated: true",
            f"updated: {datetime.now().isoformat()[:19]}",
            "---",
            "",
            "# 待复盘看板",
            "",
        ]

        if not recaps:
            lines.append("暂无待复盘事项。")
        else:
            lines.extend(
                ["## 本周新增", "", "| 时间 | 来源 | 复盘摘要 | 状态 |", "|---|---|---|---|"]
            )
            for recap in recaps:
                lines.append(
                    f"| {recap.get('time', datetime.now().strftime('%Y-%m-%d'))} "
                    f"| {recap.get('source', 'system')} "
                    f"| {recap.get('summary', '')} "
                    f"| {recap.get('status', 'pending')} |"
                )

        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    @staticmethod
    def _result_severity(result: RetrospectiveResult) -> str:
        severities = [gap.severity for gap in result.gaps]
        if "critical" in severities:
            return "critical"
        if "high" in severities:
            return "high"
        if result.new_lessons:
            return "medium"
        return "low"


# ========== 便捷函数 ==========


def should_retrospect(messages: List[Dict]) -> bool:
    """便捷函数：判断是否应该触发复盘"""
    engine = AutoRetrospective()
    return engine.should_trigger(messages)


def generate_retrospective(
    task_type: str,
    subtype: str,
    messages: List[Dict],
    checklist_usage: List[Dict],
    expected_goals: Optional[Dict] = None,
    blindspot_profile: object | None = None,
) -> RetrospectiveResult:
    """便捷函数：生成复盘（支持盲区画像）"""
    engine = AutoRetrospective()
    return engine.generate(
        task_type, subtype, messages, checklist_usage, expected_goals, blindspot_profile
    )
