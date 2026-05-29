"""
Report Generator - 自省报告生成器

职责：
- 每季度生成用户自省报告
- 对比当前 vs 上一周期画像变化
- 追踪盲区演变
- 预测成长趋势
- 输出结构化Markdown报告

输出位置：wiki/99-Reports/画像周报-YYYY-Q{N}.md
"""
# Rhapsode — 说唱诗人 — 报告生成，讲述用户画像故事
# 原模块: report_generator.py



import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

from .psyche import SignalStore, get_signal_store
import logging
logger = logging.getLogger(__name__)
from .pythia import (
    PreferenceProfile, PreferenceAnalyzer, EnergyProfile, CognitiveProfile, ValueProfile
)
from .hamartia import BlindSpotProfile, BlindSpot, BlindSpotProfileManager
from core.config import get_config



# ========== 配置 ==========

def _get_wiki_dir():
    """Lazy-load wiki directory to avoid side effects at import time."""
    return get_config().wiki_dir


class _LazyPath:
    """Lazy path that evaluates get_config() only when accessed."""
    __slots__ = ('_segments',)
    def __init__(self, *segments):
        self._segments = segments
    def __truediv__(self, other):
        return _LazyPath(*self._segments, other)
    def __rtruediv__(self, other):
        raise NotImplementedError
    def _resolve(self):
        result = _get_wiki_dir()
        for seg in self._segments:
            result = result / seg
        return result
    def __str__(self):
        return str(self._resolve())
    def __repr__(self):
        return f"LazyPath({'/'.join(self._segments)})"
    def __fspath__(self):
        return str(self._resolve())
    def __getattr__(self, name):
        return getattr(self._resolve(), name)
    def __hash__(self):
        return hash(self._resolve())
    def __eq__(self, other):
        return self._resolve() == other


WIKI_DIR = _LazyPath()
REPORTS_DIR = _LazyPath("99-Reports")


# ========== 数据模型 ==========

@dataclass
class DimensionChange:
    """单个维度的变化"""
    dimension: str
    layer: str                     # energy/cognitive/value
    previous: float
    current: float
    delta: float
    trend: str                     # stable / growing / declining / shifted
    significance: str              # major / minor / negligible


@dataclass
class BlindSpotChange:
    """盲区变化"""
    type: str
    previous_status: str           # none/suspected/confirmed/dismissed
    current_status: str
    change_type: str               # new_confirmed / new_suspected / dismissed / persisted / resolved
    confidence_delta: float


@dataclass
class GrowthPrediction:
    """成长预测"""
    area: str
    current_state: str
    predicted_state: str
    confidence: float
    timeframe: str
    rationale: str


@dataclass
class SelfReport:
    """自省报告"""
    period_label: str              # 2026-Q2
    generated_at: str
    persona_current: PreferenceProfile
    persona_previous: Optional[PreferenceProfile]
    dimension_changes: List[DimensionChange]
    blindspot_changes: List[BlindSpotChange]
    predictions: List[GrowthPrediction]
    recommendations: List[str]
    raw_markdown: str = ""


# ========== 报告生成器 ==========

class SelfReportGenerator:
    """自省报告生成器"""

    # 变化阈值
    MAJOR_DELTA = 0.15
    MINOR_DELTA = 0.08

    def __init__(self, store: SignalStore = None):
        self.store = store or get_signal_store()
        self.analyzer = PreferenceAnalyzer(self.store)
        self.blindspot_manager = BlindSpotProfileManager(self.store)

    def generate(self, days: int = 90,
                 previous_profile: PreferenceProfile = None) -> SelfReport:
        """
        生成季度自省报告。

        Args:
            days: 分析周期（默认90天=约一季度）
            previous_profile: 上一周期画像（None则尝试从数据库加载）

        Returns:
            SelfReport
        """
        # 1. 生成当前周期画像
        current = self.analyzer.analyze(days=days)

        # 2. 获取上一周期画像
        previous = previous_profile or self._load_previous_profile(current.version)

        # 3. 计算维度变化
        changes = self._calculate_dimension_changes(current, previous)

        # 4. 获取盲区变化
        bs_changes = self._calculate_blindspot_changes()

        # 5. 生成成长预测
        predictions = self._generate_predictions(current, changes)

        # 6. 生成建议
        recommendations = self._generate_recommendations(current, changes, bs_changes)

        # 7. 组装报告
        period_label = self._get_period_label(days)
        report = SelfReport(
            period_label=period_label,
            generated_at=datetime.now().isoformat(),
            persona_current=current,
            persona_previous=previous,
            dimension_changes=changes,
            blindspot_changes=bs_changes,
            predictions=predictions,
            recommendations=recommendations,
        )

        # 8. 生成Markdown
        report.raw_markdown = self._to_markdown(report)

        return report

    def save_report(self, report: SelfReport) -> Path:
        """保存报告到 wiki 报告目录"""
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORTS_DIR / f"画像周报-{report.period_label}.md"
        path.write_text(report.raw_markdown, encoding="utf-8")
        return path

    def save_to_wiki(self, report: SelfReport) -> Path:
        """兼容旧调用入口。"""
        return self.save_report(report)

    # ---- 内部方法 ----

    def _load_previous_profile(self, current_version: int) -> Optional[PreferenceProfile]:
        """从数据库加载上一周期画像"""
        if current_version <= 1:
            return None
        try:
            data = self.store.get_latest_persona_version()
            if data and data.get("version", 0) == current_version - 1:
                return self._dict_to_profile(data)
        except Exception as e:
            logger.warning(f"忽略异常: {e}")
        return None

    def _dict_to_profile(self, data: Dict) -> PreferenceProfile:
        """字典转画像（简化版）"""
        profile = PreferenceProfile(
            version=data.get("version", 0),
            generated_at=data.get("generated_at", ""),
            period_start=data.get("period_start", ""),
            period_end=data.get("period_end", ""),
            signal_count=data.get("signal_count", 0),
        )
        # 从字典中恢复各层雷达
        energy_data = data.get("energy", {})
        if energy_data:
            profile.energy.focus_depth = energy_data.get("focus_depth", {}).get("score", 0.5)
            profile.energy.startup_difficulty = energy_data.get("startup_difficulty", {}).get("score", 0.5)
            profile.energy.endurance_mode = energy_data.get("endurance_mode", {}).get("score", 0.5)
            profile.energy.switching_flexibility = energy_data.get("switching_flexibility", {}).get("score", 0.5)
            profile.energy.recovery_cycle = energy_data.get("recovery_cycle", {}).get("score", 0.5)
            profile.energy.confidence = energy_data.get("confidence", 0)

        cognitive_data = data.get("cognitive", {})
        if cognitive_data:
            profile.cognitive.abstraction = cognitive_data.get("abstraction", {}).get("score", 0.5)
            profile.cognitive.system_view = cognitive_data.get("system_view", {}).get("score", 0.5)
            profile.cognitive.skepticism = cognitive_data.get("skepticism", {}).get("score", 0.5)
            profile.cognitive.creativity = cognitive_data.get("creativity", {}).get("score", 0.5)
            profile.cognitive.deduction = cognitive_data.get("deduction", {}).get("score", 0.5)
            profile.cognitive.confidence = cognitive_data.get("confidence", 0)

        value_data = data.get("value", {})
        if value_data:
            profile.value.correctness_vs_efficiency = value_data.get("correctness_vs_efficiency", {}).get("score", 0.5)
            profile.value.depth_vs_breadth = value_data.get("depth_vs_breadth", {}).get("score", 0.5)
            profile.value.perfection_vs_completion = value_data.get("perfection_vs_completion", {}).get("score", 0.5)
            profile.value.innovation_vs_safety = value_data.get("innovation_vs_safety", {}).get("score", 0.5)
            profile.value.autonomy_vs_collaboration = value_data.get("autonomy_vs_collaboration", {}).get("score", 0.5)
            profile.value.confidence = value_data.get("confidence", 0)

        return profile

    def _calculate_dimension_changes(self, current: PreferenceProfile,
                                     previous: PreferenceProfile) -> List[DimensionChange]:
        """计算三层雷达各维度的变化"""
        changes = []

        if not previous:
            # 首次报告，所有变化标记为"baseline"
            for attr in ["focus_depth", "startup_difficulty", "endurance_mode",
                         "switching_flexibility", "recovery_cycle"]:
                changes.append(DimensionChange(
                    dimension=self._translate_dim(attr),
                    layer="energy",
                    previous=0.5,
                    current=getattr(current.energy, attr),
                    delta=getattr(current.energy, attr) - 0.5,
                    trend="baseline",
                    significance="major" if abs(getattr(current.energy, attr) - 0.5) > 0.15 else "minor",
                ))
            for attr in ["abstraction", "system_view", "skepticism", "creativity", "deduction"]:
                changes.append(DimensionChange(
                    dimension=self._translate_dim(attr),
                    layer="cognitive",
                    previous=0.5,
                    current=getattr(current.cognitive, attr),
                    delta=getattr(current.cognitive, attr) - 0.5,
                    trend="baseline",
                    significance="major" if abs(getattr(current.cognitive, attr) - 0.5) > 0.15 else "minor",
                ))
            for attr in ["correctness_vs_efficiency", "depth_vs_breadth",
                         "perfection_vs_completion", "innovation_vs_safety", "autonomy_vs_collaboration"]:
                changes.append(DimensionChange(
                    dimension=self._translate_dim(attr),
                    layer="value",
                    current=getattr(current.value, attr),
                    previous=0.5,
                    delta=getattr(current.value, attr) - 0.5,
                    trend="baseline",
                    significance="major" if abs(getattr(current.value, attr) - 0.5) > 0.15 else "minor",
                ))
            return changes

        # 非首次，计算真实变化
        energy_dims = [
            ("focus_depth", current.energy, previous.energy),
            ("startup_difficulty", current.energy, previous.energy),
            ("endurance_mode", current.energy, previous.energy),
            ("switching_flexibility", current.energy, previous.energy),
            ("recovery_cycle", current.energy, previous.energy),
        ]
        cognitive_dims = [
            ("abstraction", current.cognitive, previous.cognitive),
            ("system_view", current.cognitive, previous.cognitive),
            ("skepticism", current.cognitive, previous.cognitive),
            ("creativity", current.cognitive, previous.cognitive),
            ("deduction", current.cognitive, previous.cognitive),
        ]
        value_dims = [
            ("correctness_vs_efficiency", current.value, previous.value),
            ("depth_vs_breadth", current.value, previous.value),
            ("perfection_vs_completion", current.value, previous.value),
            ("innovation_vs_safety", current.value, previous.value),
            ("autonomy_vs_collaboration", current.value, previous.value),
        ]

        for attr, curr_obj, prev_obj in energy_dims + cognitive_dims + value_dims:
            curr_val = getattr(curr_obj, attr)
            prev_val = getattr(prev_obj, attr)
            delta = curr_val - prev_val

            if abs(delta) < 0.03:
                trend = "stable"
                significance = "negligible"
            elif abs(delta) < self.MINOR_DELTA:
                trend = "growing" if delta > 0 else "declining"
                significance = "minor"
            elif abs(delta) < self.MAJOR_DELTA:
                trend = "growing" if delta > 0 else "declining"
                significance = "minor"
            else:
                trend = "shifted"
                significance = "major"

            layer = "energy" if curr_obj == current.energy else \
                    "cognitive" if curr_obj == current.cognitive else "value"

            changes.append(DimensionChange(
                dimension=self._translate_dim(attr),
                layer=layer,
                previous=round(prev_val, 2),
                current=round(curr_val, 2),
                delta=round(delta, 2),
                trend=trend,
                significance=significance,
            ))

        # 按显著性排序
        sig_order = {"major": 0, "minor": 1, "negligible": 2}
        changes.sort(key=lambda x: (sig_order.get(x.significance, 99), abs(x.delta)), reverse=True)
        return changes

    def _calculate_blindspot_changes(self) -> List[BlindSpotChange]:
        """计算盲区变化"""
        changes = []
        try:
            profile = self.blindspot_manager._load_profile()
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at rhapsode.py", exc_info=True)
            return changes

        # 简化版：基于当前盲区画像生成快照对比
        # 实际实现需要存储历史盲区状态
        for bs in profile.confirmed:
            changes.append(BlindSpotChange(
                type=bs.type,
                previous_status="suspected",
                current_status="confirmed",
                change_type="new_confirmed",
                confidence_delta=bs.confidence,
            ))

        for bs in profile.suspected:
            changes.append(BlindSpotChange(
                type=bs.type,
                previous_status="none",
                current_status="suspected",
                change_type="new_suspected",
                confidence_delta=bs.confidence,
            ))

        for bs in profile.dismissed:
            changes.append(BlindSpotChange(
                type=bs.type,
                previous_status="suspected",
                current_status="dismissed",
                change_type="dismissed",
                confidence_delta=-bs.confidence,
            ))

        return changes

    def _generate_predictions(self, current: PreferenceProfile,
                              changes: List[DimensionChange]) -> List[GrowthPrediction]:
        """基于当前画像和变化趋势生成成长预测"""
        predictions = []

        # 基于认知雷达预测
        cognitive = current.cognitive
        value = current.value

        # 预测1：如果抽象能力在提升，预测未来可以更有效地处理复杂系统问题
        abstraction_change = next((c for c in changes if c.dimension == "抽象↔具象"), None)
        if abstraction_change and abstraction_change.delta > 0.05:
            predictions.append(GrowthPrediction(
                area="复杂问题解决",
                current_state="依赖具体案例",
                predicted_state="能独立进行抽象建模",
                confidence=min(0.7 + abstraction_change.delta, 0.95),
                timeframe="1-2个季度",
                rationale=f"抽象能力持续上升（{abstraction_change.previous:.2f}→{abstraction_change.current:.2f}），"
                          f"如果保持当前学习节奏，预计能形成稳定的抽象思维习惯",
            ))

        # 预测2：如果系统视角在提升，预测未来决策更全面
        system_change = next((c for c in changes if c.dimension == "系统↔单点"), None)
        if system_change and system_change.delta > 0.05:
            predictions.append(GrowthPrediction(
                area="决策质量",
                current_state="聚焦单点优化",
                predicted_state="能权衡系统级影响",
                confidence=min(0.7 + system_change.delta, 0.95),
                timeframe="1个季度",
                rationale=f"系统视角持续增强，表明你正在建立更全面的思维框架",
            ))

        # 预测3：如果深度优先价值在提升，预测需要警惕广度盲区
        depth_change = next((c for c in changes if c.dimension == "深度↔广度"), None)
        if depth_change and depth_change.current > 0.7:
            predictions.append(GrowthPrediction(
                area="知识视野",
                current_state="深度钻研型",
                predicted_state="可能出现广度盲区",
                confidence=0.6,
                timeframe="下个季度",
                rationale="深度优先模式持续强化，建议主动安排跨领域探索，防止视野收窄",
            ))

        return predictions[:3]  # MVP 阶段保留 3 条高置信预测

    def _generate_recommendations(self, current: PreferenceProfile,
                                  changes: List[DimensionChange],
                                  bs_changes: List[BlindSpotChange]) -> List[str]:
        """生成可操作建议"""
        recommendations = []

        # 基于重大变化生成建议
        major_changes = [c for c in changes if c.significance == "major"]
        for change in major_changes[:3]:
            if change.layer == "energy":
                recommendations.append(
                    f"【能量调整】{change.dimension}发生了{change.trend}变化（{change.previous:.2f}→{change.current:.2f}），"
                    f"建议关注这一变化是暂时的情境反应还是稳定的模式转变"
                )
            elif change.layer == "cognitive":
                recommendations.append(
                    f"【认知进化】{change.dimension}显著{change.trend}，"
                    f"建议在下个季度重点利用这一认知优势处理对应类型的任务"
                )
            elif change.layer == "value":
                recommendations.append(
                    f"【价值校准】{change.dimension}优先级发生了变化，"
                    f"建议审视这一变化是否符合你当前的人生/职业阶段"
                )

        # 基于盲区生成建议
        confirmed_bs = [b for b in bs_changes if b.change_type == "new_confirmed"]
        for bs in confirmed_bs[:2]:
            recommendations.append(
                f"【盲区应对】已确认存在「{bs.type}」盲区，"
                f"建议在日常决策中增加对应的验证步骤"
            )

        suspected_bs = [b for b in bs_changes if b.change_type == "new_suspected"]
        for bs in suspected_bs[:2]:
            recommendations.append(
                f"【盲区观察】发现「{bs.type}」盲区信号，"
                f"建议未来2周内有意识地检验这一假设"
            )

        # 基于能量模式生成建议
        energy = current.energy
        if energy.endurance_mode < 0.4 and energy.startup_difficulty > 0.6:
            recommendations.append(
                "【能量管理】你是'爆发型+启动难'组合，建议：1）减少任务切换频率；"
                "2）用'番茄工作法'降低启动门槛；3）为自己创造沉浸式环境"
            )

        # 基于价值冲突生成建议
        value = current.value
        if value.correctness_vs_efficiency > 0.6 and value.perfection_vs_completion > 0.6:
            recommendations.append(
                "【效率陷阱】正确性+完美主义双重高优先级可能导致交付延迟，"
                "建议为任务设定明确的'足够好'标准"
            )

        # 按建议类型去重：每类最多 2 条，总计最多 8 条
        type_counts = defaultdict(int)
        unique = []
        for rec in recommendations:
            tag = "其他"
            if rec.startswith("【") and "】" in rec:
                tag = rec[1:rec.index("】")]
            if type_counts[tag] >= 2:
                continue
            type_counts[tag] += 1
            unique.append(rec)
            if len(unique) >= 8:
                break

        return unique

    def _to_markdown(self, report: SelfReport) -> str:
        """生成Markdown报告"""
        lines = [
            "---",
            f"type: self-report",
            f"period: {report.period_label}",
            f"version: {report.persona_current.version}",
            f"generated: {report.generated_at[:10]}",
            f"signals: {report.persona_current.signal_count}",
            "---",
            "",
            f"# 自省报告：{report.period_label}",
            "",
            "> ⚠️ **免责声明**：此报告由AI基于有限行为信号自动推断生成，"
            "可能存在误读、过度归因或情境误配。报告内容不构成对你的人格判断，"
            "也不应替代你自己的自我认知。如有不符，请直接忽略——画像会随数据积累自我修正。",
            "",
            "> 🔄 **动态性提示**：画像会随时间演化，本期发现可能在下一期被推翻。"
            "重大人生变化（换工作、搬迁、健康变化）可能导致画像短期失真。",
            "",
        ]

        # 变化概览
        major = [c for c in report.dimension_changes if c.significance == "major"]
        minor = [c for c in report.dimension_changes if c.significance == "minor"]
        lines.extend([
            "## 变化概览",
            "",
            f"- **显著变化**: {len(major)} 个维度",
            f"- **轻微变化**: {len(minor)} 个维度",
            f"- **盲区状态**: {len(report.blindspot_changes)} 条记录",
            f"- **信号样本**: {report.persona_current.signal_count} 条",
            "",
        ])

        # AI不确定性标注
        lines.extend(["## AI不确定性标注", ""])
        avg_conf = (
            report.persona_current.energy.confidence +
            report.persona_current.cognitive.confidence +
            report.persona_current.value.confidence
        ) / 3
        if avg_conf >= 0.7:
            conf_label = "高"
            conf_emoji = "🟢"
        elif avg_conf >= 0.4:
            conf_label = "中"
            conf_emoji = "🟡"
        else:
            conf_label = "低"
            conf_emoji = "🔴"
        lines.append(
            f"{conf_emoji} **整体置信度**: {conf_label} ({avg_conf:.0%}) — "
            f"能量:{report.persona_current.energy.confidence:.0%} | "
            f"认知:{report.persona_current.cognitive.confidence:.0%} | "
            f"价值:{report.persona_current.value.confidence:.0%}"
        )
        lines.append("")
        if avg_conf < 0.5:
            lines.append(
                "> ⚠️ 当前信号样本不足，画像可能存在较大偏差。"
                "建议积累更多数据后再做重大决策参考。"
            )
            lines.append("")
        lines.append("**各模块可靠度**：")
        lines.append(f"- 雷达变化: {'可靠' if avg_conf > 0.6 else '参考'}")
        lines.append(f"- 盲区检测: {'可靠' if len(report.blindspot_changes) > 2 else '待观察'}")
        lines.append(f"- 成长预测: {'有依据' if report.persona_current.signal_count > 100 else '推测性'}")
        lines.append(f"- 建议: {'可尝试' if avg_conf > 0.5 else '谨慎参考'}")
        lines.append("")

        # 三层雷达变化详情
        lines.extend(["## Layer 1: 能量模式变化", ""])
        energy_changes = [c for c in report.dimension_changes if c.layer == "energy"]
        for change in energy_changes:
            arrow = "↑" if change.delta > 0.02 else "↓" if change.delta < -0.02 else "→"
            emoji = {"major": "🔴", "minor": "🟡", "negligible": "🟢"}.get(change.significance, "⚪")
            lines.append(
                f"{emoji} **{change.dimension}**: {change.previous:.2f} → {change.current:.2f} "
                f"({arrow}{abs(change.delta):.2f}) — {self._trend_label(change.trend)}"
            )
        lines.append("")

        lines.extend(["## Layer 2: 认知模式变化", ""])
        cognitive_changes = [c for c in report.dimension_changes if c.layer == "cognitive"]
        for change in cognitive_changes:
            arrow = "↑" if change.delta > 0.02 else "↓" if change.delta < -0.02 else "→"
            emoji = {"major": "🔴", "minor": "🟡", "negligible": "🟢"}.get(change.significance, "⚪")
            lines.append(
                f"{emoji} **{change.dimension}**: {change.previous:.2f} → {change.current:.2f} "
                f"({arrow}{abs(change.delta):.2f}) — {self._trend_label(change.trend)}"
            )
        lines.append("")

        lines.extend(["## Layer 3: 价值优先级变化", ""])
        value_changes = [c for c in report.dimension_changes if c.layer == "value"]
        for change in value_changes:
            arrow = "↑" if change.delta > 0.02 else "↓" if change.delta < -0.02 else "→"
            emoji = {"major": "🔴", "minor": "🟡", "negligible": "🟢"}.get(change.significance, "⚪")
            lines.append(
                f"{emoji} **{change.dimension}**: {change.previous:.2f} → {change.current:.2f} "
                f"({arrow}{abs(change.delta):.2f}) — {self._trend_label(change.trend)}"
            )
        lines.append("")

        # 盲区演变
        if report.blindspot_changes:
            lines.extend(["## 盲区演变", ""])
            for bs in report.blindspot_changes:
                icon = {
                    "new_confirmed": "⚠️",
                    "new_suspected": "🔍",
                    "dismissed": "✅",
                    "persisted": "⏳",
                    "resolved": "🎉",
                }.get(bs.change_type, "❓")
                lines.append(
                    f"{icon} **{bs.type}**: {bs.previous_status} → {bs.current_status} "
                    f"({bs.change_type})"
                )
            lines.append("")

        # 成长预测
        if report.predictions:
            lines.extend(["## 成长预测", ""])
            for i, pred in enumerate(report.predictions, 1):
                lines.append(f"{i}. **{pred.area}** (置信度: {pred.confidence:.0%})")
                lines.append(f"   - 当前: {pred.current_state}")
                lines.append(f"   - 预测: {pred.predicted_state}")
                lines.append(f"   - 时间: {pred.timeframe}")
                lines.append(f"   - 依据: {pred.rationale}")
                lines.append("")

        # 建议
        if report.recommendations:
            lines.extend(["## 可操作建议", ""])
            for i, rec in enumerate(report.recommendations, 1):
                lines.append(f"{i}. {rec}")
            lines.append("")

        cold_pages = self._get_cold_pages_for_report()
        if cold_pages:
            lines.extend(["## 冷却知识", ""])
            for page in cold_pages:
                title = page.title or Path(page.wiki_path).stem
                lines.append(f"- **{title}**（热力 {page.heat_score:.1f}，质量 {page.quality_score:.1f}）")
            lines.append("")

        # 底部
        lines.extend([
            "---",
            "",
            "*此报告由AI自动分析生成。如果你觉得某个观察不准确，忽略它即可——"
            "画像会随着更多数据的积累自我修正。*",
        ])

        return "\n".join(lines)

    # ---- 辅助方法 ----

    @staticmethod
    def _translate_dim(attr: str) -> str:
        """维度英文名转中文"""
        mapping = {
            "focus_depth": "专注深度",
            "startup_difficulty": "启动难度",
            "endurance_mode": "续航模式",
            "switching_flexibility": "切换弹性",
            "recovery_cycle": "恢复周期",
            "abstraction": "抽象↔具象",
            "system_view": "系统↔单点",
            "skepticism": "质疑↔信任",
            "creativity": "创造↔优化",
            "deduction": "演绎↔归纳",
            "correctness_vs_efficiency": "正确性↔效率",
            "depth_vs_breadth": "深度↔广度",
            "perfection_vs_completion": "完美↔完成",
            "innovation_vs_safety": "创新↔稳妥",
            "autonomy_vs_collaboration": "自主↔协作",
        }
        return mapping.get(attr, attr)

    @staticmethod
    def _trend_label(trend: str) -> str:
        """趋势标签翻译"""
        mapping = {
            "stable": "保持稳定",
            "growing": "上升趋势",
            "declining": "下降趋势",
            "shifted": "显著转变",
            "baseline": "首次基线",
        }
        return mapping.get(trend, trend)

    @staticmethod
    def _get_period_label(days: int) -> str:
        """生成周期标签"""
        now = datetime.now()
        quarter = (now.month - 1) // 3 + 1
        return f"{now.year}-Q{quarter}"

    @staticmethod
    def _get_cold_pages_for_report(limit: int = 5):
        try:
            from core.wiki_metrics import get_default_metrics
            return get_default_metrics().get_cold_pages(limit=limit)
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at rhapsode.py", exc_info=True)
            return []


# ========== 便捷函数 ==========

def generate_self_report(days: int = 90,
                         previous_profile: PreferenceProfile = None) -> SelfReport:
    """便捷函数：生成自省报告"""
    generator = SelfReportGenerator()
    return generator.generate(days=days, previous_profile=previous_profile)


def generate_and_save_report(days: int = 90) -> Path:
    """便捷函数：生成并保存报告到wiki"""
    generator = SelfReportGenerator()
    report = generator.generate(days=days)
    path = generator.save_report(report)
    logger.info(f"✅ 自省报告已保存: {path}")
    return path

# 兼容别名
generate_report = generate_self_report
