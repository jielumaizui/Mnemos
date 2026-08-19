"""
Reflection Markdown Exporter — L4 Reflection Index 的只读投影

把 ReflectionRecord 与 CognitiveShift 导出为 Obsidian Markdown 文件，实现：
1. 可见 — 用户可直接查看反思记录与认知变迁
2. 可追溯 — 每条记录链接回原始证据与 Observation
3. 只读 — 本目录是 Reflection Store 的只读投影，用户可写备注，但不影响系统数据

输出结构（在认知 Vault 下）：
  L4-Reflections/
    Reflections/{date}/{id}.md    # 单次反思记录
    Shifts/{dimension}.md          # 按维度聚合的认知变迁
    Reports/weekly-{date}.md       # 周报汇总
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from core.config import get_config
from core.reflection.feedback_loop import knowledge_update_from_shift
from core.reflection.models import CognitiveShift, ReflectionRecord
from core.reflection.reflection_store import ReflectionStore
from core.wiki_derived_projection import (
    DerivedProjectionLifecycle,
    ProjectionPageSpec,
    canonical_projection_revision,
)

logger = logging.getLogger(__name__)


def _safe(value: Optional[str]) -> str:
    """将字符串安全化为 YAML 标量（简单转义双引号）"""
    if value is None:
        return ""
    return value.replace('"', '\\"').replace("\n", " ")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _format_frontmatter_field(key: str, value) -> List[str]:
    """把单个 frontmatter 字段格式化为 YAML 行列表"""
    if value is None:
        return [f"{key}:"]
    if isinstance(value, bool):
        return [f"{key}: {str(value).lower()}"]
    if isinstance(value, (list, tuple)):
        if not value:
            return [f"{key}: []"]
        lines = [f"{key}:"]
        for item in value:
            lines.append(f'  - "{_safe(str(item))}"')
        return lines
    if isinstance(value, str):
        return [f'{key}: "{_safe(value)}"']
    return [f"{key}: {value}"]


class ReflectionExporter:
    """Reflection 层 Markdown 导出器 — Reflection Store 的只读投影"""

    def __init__(
        self,
        vault_dir: str,
        *,
        lifecycle: DerivedProjectionLifecycle | None = None,
    ):
        self.vault_dir = Path(vault_dir).expanduser().resolve(strict=False)
        self.base_dir = self.vault_dir / "L4-Reflections"
        self.reflections_dir = self.base_dir / "Reflections"
        self.shifts_dir = self.base_dir / "Shifts"
        self.knowledge_updates_dir = self.base_dir / "KnowledgeUpdates"
        self.reports_dir = self.base_dir / "Reports"
        for d in (
            self.reflections_dir,
            self.shifts_dir,
            self.knowledge_updates_dir,
            self.reports_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        self.lifecycle = lifecycle or DerivedProjectionLifecycle(self.vault_dir)
        self._pending_pages: List[ProjectionPageSpec] | None = None

    @staticmethod
    def _config_int(key: str, default: int) -> int:
        try:
            value = int(get_config().get(key, default))
        except (TypeError, ValueError):
            return default
        return max(value, 0)

    # ───────────────────────────────
    # 单次记录导出
    # ───────────────────────────────

    @staticmethod
    def _is_empty_record(record: ReflectionRecord) -> bool:
        """判断是否为无意义的空/测试 reflection"""
        te = (record.trigger_event or "").strip()
        uq = (record.user_query or "").strip()
        insight_summary = (record.insight.summary if record.insight else "").strip()
        return (
            len(te) <= 2
            and len(uq) <= 2
            and len(insight_summary) <= 2
            and not record.mirror_snapshots
        )

    def export_record(self, record: ReflectionRecord) -> Optional[Path]:
        """导出单条 ReflectionRecord；空/测试记录不导出并删除旧文件"""
        date_dir = self.reflections_dir / record.created_at.strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        file_path = date_dir / f"{record.id}.md"

        if self._is_empty_record(record):
            self._delete_page(file_path)
            return None

        max_per_day = self._config_int("reflection_export.max_records_per_day", 20)
        if max_per_day and self._pending_pages is None and not file_path.exists():
            existing_count = len(list(date_dir.glob("*.md")))
            if existing_count >= max_per_day:
                logger.info(
                    "[ReflectionExporter] 当日 reflection 投影已达上限 %s，跳过: %s",
                    max_per_day,
                    file_path,
                )
                return None

        lines = self._render_record(record)
        self._publish_page(
            file_path,
            "\n".join(lines),
            page_role="formal_derived:reflection",
            canonical_source=record,
            source_refs=(f"reflection-store:reflections/{record.id}",),
        )
        return file_path

    def _render_record(self, record: ReflectionRecord) -> List[str]:
        lines = []
        lines.extend(self._render_header(record))
        lines.extend(self._render_body(record))
        lines.extend(self._render_footer(record))
        return lines

    def _render_header(self, record: ReflectionRecord) -> List[str]:
        """渲染 frontmatter 与标题"""
        sources = [f"reflection-store:reflections/{record.id}"]
        sources.extend(
            f"observation:{snapshot.observation_id}"
            for snapshot in (record.mirror_snapshots or [])
            if snapshot.observation_id
        )
        sources = list(dict.fromkeys(sources))
        frontmatter = {
            "id": record.id,
            "created_at": _iso(record.created_at),
            "trigger": record.trigger.value,
            "trigger_event": record.trigger_event,
            "user_query": record.user_query,
            "dimensions": record.mirror_dimensions,
            "fed_back_to_observations": record.fed_back_to_observations,
            "fed_back_to_knowledge": record.fed_back_to_knowledge,
            "source_count": len(sources),
            "sources": sources,
            "evidence_level": "multiple" if len(sources) > 1 else "single",
            "knowledge_stage": "P2",
            "status": "active",
        }

        lines = ["---"]
        for k, v in frontmatter.items():
            lines.extend(_format_frontmatter_field(k, v))
        lines.append("---")
        lines.append("")

        lines.append(f"# Reflection {record.id}")
        lines.append("")
        lines.append(f"> 触发: `{record.trigger.value}` — {record.trigger_event or '无事件描述'}")
        lines.append(f"> 生成时间: {record.created_at.isoformat() if record.created_at else '—'}")
        lines.append("")
        return lines

    def _render_body(self, record: ReflectionRecord) -> List[str]:
        """渲染主体：Insight、Mirror、时间上下文、内部校验、用户反馈"""
        lines = []
        lines.extend(self._render_insight(record.insight))
        lines.extend(self._render_mirror_snapshots(record.mirror_snapshots))
        lines.extend(self._render_temporal_context(record.temporal_context))
        lines.extend(self._render_internal_validation(record.internal_validation))
        lines.extend(self._render_user_feedback(record.user_feedback))
        return lines

    @staticmethod
    def _render_insight(insight) -> List[str]:
        """渲染 Insight 摘要"""
        if not insight:
            return []
        lines = [
            "## Insight 摘要",
            "",
            insight.summary or "_无摘要_",
            "",
        ]
        if insight.key_points:
            lines.append("### 关键结论")
            lines.append("")
            for point in insight.key_points:
                lines.append(f"- {point}")
            lines.append("")
        if insight.dimensions_involved:
            lines.append(f"**涉及维度**: {', '.join(insight.dimensions_involved)}")
            lines.append("")
        return lines

    def _render_mirror_snapshots(self, mirror_snapshots) -> List[str]:
        """渲染 Mirror 证据链"""
        if not mirror_snapshots:
            return []
        lines = []
        lines.append("## Mirror 证据链")
        lines.append("")
        lines.append("| 维度 | Observation | 值摘要 | 置信度 | 时间权重 |")
        lines.append("|------|-------------|--------|--------|----------|")
        for snap in mirror_snapshots:
            lines.append(
                f"| {snap.dimension} | `{snap.observation_id}` | {snap.value_summary} | "
                f"{snap.confidence:.2f} | {snap.recency_weight:.2f} |"
            )
        lines.append("")

        lines.append("### 证据片段")
        lines.append("")
        for i, snap in enumerate(mirror_snapshots, 1):
            lines.append(f"{i}. **{snap.dimension}** `{snap.observation_id}`")
            if snap.evidence_summary:
                lines.append(f"   - {snap.evidence_summary}")
            lines.append(
                f"   - 置信度: {snap.confidence:.2f}, 时间权重: {snap.recency_weight:.2f}"
            )
        lines.append("")
        return lines

    @staticmethod
    def _render_temporal_context(temporal_context) -> List[str]:
        """渲染时间上下文 JSON"""
        if not temporal_context:
            return []
        return [
            "## 时间上下文",
            "",
            "```json",
            json.dumps(temporal_context, ensure_ascii=False, indent=2),
            "```",
            "",
        ]

    @staticmethod
    def _render_internal_validation(internal_validation) -> List[str]:
        """渲染内部校验结果"""
        if not internal_validation:
            return []
        lines = ["## 内部校验", ""]
        score = internal_validation.get("overall_score")
        passed = internal_validation.get("passed")
        lines.append(f"- 总体得分: {score if score is not None else '—'}")
        lines.append(
            f"- 是否通过: {'✅ 是' if passed else '❌ 否' if passed is False else '—'}"
        )
        findings = internal_validation.get("findings", [])
        if findings:
            lines.append("")
            lines.append("| 检查项 | 状态 | 得分 | 信息 |")
            lines.append("|--------|------|------|------|")
            for f in findings:
                lines.append(
                    f"| {f.get('check', '—')} | {f.get('status', '—')} | "
                    f"{f.get('score', '—')} | {f.get('message', '—')} |"
                )
        lines.append("")
        return lines

    @staticmethod
    def _render_user_feedback(user_feedback) -> List[str]:
        """渲染用户反馈"""
        if not user_feedback:
            return []
        return [
            "## 用户反馈",
            "",
            f"- 类型: `{user_feedback.feedback_type.value}`",
            f"- 评论: {user_feedback.comment or '—'}",
            f"- 时间: {_iso(user_feedback.given_at) or '—'}",
            "",
        ]

    @staticmethod
    def _render_footer(record: ReflectionRecord) -> List[str]:
        """渲染反哺状态与用户备注区"""
        return [
            "## 反哺状态",
            "",
            f"- 已反哺 Observation (L3): {'✅' if record.fed_back_to_observations else '⏳'}",
            f"- 已反哺 Knowledge (L2): {'✅' if record.fed_back_to_knowledge else '⏳'}",
            "",
            "## 用户备注",
            "",
            "```",
            "# 你可以在这里写补充说明，例如：",
            '# - "这条 Insight 帮我意识到自己对时间估算过于乐观"',
            '# - "建议后续重点关注 decisions 维度"',
            "```",
            "",
            "_（在此写下你的判断...）_",
            "",
            "<!-- 注意：直接编辑此处不会同步回系统。如需系统级纠错，请通过 reflection 反馈 API。 -->",
            "",
        ]

    # ───────────────────────────────
    # 认知变迁聚合导出
    # ───────────────────────────────

    def export_shifts(self, shifts: Iterable[CognitiveShift]) -> Dict[str, Path]:
        """按维度聚合导出 CognitiveShift"""
        by_dimension: Dict[str, List[CognitiveShift]] = defaultdict(list)
        for shift in shifts:
            by_dimension[shift.dimension].append(shift)

        written: Dict[str, Path] = {}
        for dimension, dim_shifts in by_dimension.items():
            file_path = self._write_dimension_shifts(dimension, dim_shifts)
            written[dimension] = file_path
        return written

    def _write_dimension_shifts(self, dimension: str, shifts: List[CognitiveShift]) -> Path:
        """为单个维度写认知变迁 Markdown"""
        file_path = self.shifts_dir / f"{dimension}.md"
        # 按检测时间倒序
        shifts = sorted(shifts, key=lambda s: s.shift_detected_at or datetime.min, reverse=True)
        sources = [
            "reflection-store:cognitive_shifts/"
            f"{dimension}/{_iso(shift.shift_detected_at) or 'unknown'}/{shift.shift_type}"
            for shift in shifts
        ]

        frontmatter = {
            "dimension": dimension,
            "generated_at": self._shift_projection_timestamp(shifts),
            "shift_count": len(shifts),
            "source_count": len(sources),
            "sources": sources,
            "evidence_level": "multiple" if len(sources) > 1 else "single",
            "knowledge_stage": "P2",
            "status": "active",
        }

        lines = []
        lines.append("---")
        for k, v in frontmatter.items():
            lines.extend(_format_frontmatter_field(k, v))
        lines.append("---")
        lines.append("")

        lines.append(f"# {dimension} — 认知变迁")
        lines.append("")
        lines.append("> 本文件由 Reflection Engine 自动生成，是系统 Reflection Store 的只读投影。")
        lines.append("")

        if not shifts:
            lines.append("_暂无认知变迁记录_")
            lines.append("")
        else:
            lines.append("## 变迁时间线")
            lines.append("")
            lines.append("| 时间 | 类型 | 从 | 到 | 置信度 |")
            lines.append("|------|------|----|----|--------|")
            for shift in shifts:
                timestamp = shift.shift_detected_at or shift.first_seen_at
                ts = timestamp.strftime("%Y-%m-%d") if timestamp else "unknown"
                lines.append(
                    f"| {ts} | {shift.shift_type} | {shift.from_state} | "
                    f"{shift.to_state} | {shift.confidence:.2f} |"
                )
            lines.append("")

            lines.append("## 详细变迁")
            lines.append("")
            for i, shift in enumerate(shifts, 1):
                lines.append(f"### #{i} {shift.shift_type}")
                lines.append("")
                lines.append(f"- **维度**: {shift.dimension}")
                lines.append(f"- **变化**: `{shift.from_state}` → `{shift.to_state}`")
                lines.append(f"- **置信度**: {shift.confidence:.2f}")
                first_seen = _iso(shift.first_seen_at)
                detected = _iso(shift.shift_detected_at)
                lines.append(f"- **首次出现**: {first_seen or '—'}")
                lines.append(f"- **检测时间**: {detected or '—'}")
                if shift.evidence:
                    lines.append("- **证据**:")
                    for ev in shift.evidence[:10]:
                        lines.append(f"  - {ev}")
                lines.append("")

        self._publish_page(
            file_path,
            "\n".join(lines),
            page_role="formal_derived:reflection_shift",
            canonical_source=shifts,
            source_refs=tuple(sources) or (f"reflection-store:cognitive_shifts/{dimension}",),
        )
        return file_path

    @staticmethod
    def _shift_projection_timestamp(shifts: List[CognitiveShift]) -> str:
        timestamps = [
            timestamp
            for shift in shifts
            for timestamp in (shift.shift_detected_at, shift.first_seen_at)
            if timestamp is not None
        ]
        return max(timestamps).isoformat() if timestamps else "canonical-empty"

    # ───────────────────────────────
    # 知识更新建议导出（P110：L4 → L2 Wiki）
    # ───────────────────────────────

    def export_knowledge_updates(
        self,
        record: ReflectionRecord,
        knowledge_updates: Optional[Iterable[Dict]],
    ) -> List[Path]:
        """把 Reflection 生成的知识更新建议写成 L4-Reflections/KnowledgeUpdates/*.md。"""
        if not knowledge_updates:
            return []
        knowledge_updates = sorted(
            list(knowledge_updates),
            key=lambda update: (
                str(update.get("dimension") or ""),
                str(update.get("shift_type") or ""),
                str(update.get("detected_at") or ""),
                str(update.get("from_state") or ""),
                str(update.get("to_state") or ""),
            ),
        )
        if not knowledge_updates:
            return []

        date_dir = self.knowledge_updates_dir / record.created_at.strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)

        written: List[Path] = []
        for i, upd in enumerate(knowledge_updates, 1):
            dimension = upd.get("dimension", "unknown")
            shift_type = upd.get("shift_type", "unknown")
            safe_shift = str(shift_type).replace("/", "_").replace(" ", "_")[:40]
            file_path = date_dir / f"{record.id}_{i:02d}_{dimension}_{safe_shift}.md"

            frontmatter = {
                "id": record.id,
                "dimension": dimension,
                "shift_type": shift_type,
                "confidence": float(upd.get("confidence", 0.0) or 0.0),
                "from_state": upd.get("from_state", ""),
                "to_state": upd.get("to_state", ""),
                "detected_at": upd.get("detected_at", ""),
                "tags": ["reflection", "knowledge-update"],
            }

            lines = []
            lines.append("---")
            for k, v in frontmatter.items():
                if v is None:
                    lines.append(f"{k}:")
                elif isinstance(v, bool):
                    lines.append(f"{k}: {str(v).lower()}")
                elif isinstance(v, (list, tuple)):
                    if not v:
                        lines.append(f"{k}: []")
                    else:
                        lines.append(f"{k}:")
                        for item in v:
                            lines.append(f'  - "{_safe(str(item))}"')
                elif isinstance(v, str):
                    lines.append(f'{k}: "{_safe(v)}"')
                else:
                    lines.append(f"{k}: {v}")
            lines.append("---")
            lines.append("")

            lines.append(f"# 知识更新建议：{dimension}")
            lines.append("")
            lines.append(f"> **{shift_type}**（置信度 {frontmatter['confidence']:.2f}）")
            lines.append("")
            lines.append(f"- 从：`{frontmatter['from_state']}`")
            lines.append(f"- 到：`{frontmatter['to_state']}`")
            lines.append("")
            lines.append("## 建议")
            lines.append("")
            lines.append(str(upd.get("suggestion", "") or "_无具体建议_"))
            lines.append("")
            lines.append("## 来源")
            lines.append("")
            date_label = record.created_at.strftime("%Y-%m-%d")
            lines.append(
                f"[[L4-Reflections/Reflections/{date_label}/{record.id}|Reflection {record.id}]]"
            )
            lines.append("")
            lines.append(
                "<!-- 注意：本页由 Reflection Engine 自动生成，是 L4→L2 知识更新建议的只读投影。 -->"
            )

            self._publish_page(
                file_path,
                "\n".join(lines),
                page_role="formal_derived:reflection_knowledge_update",
                canonical_source={
                    "reflection": record,
                    "ordinal": i,
                    "update": upd,
                },
                source_refs=(f"reflection-store:reflections/{record.id}",),
            )
            written.append(file_path)
        return written

    # ───────────────────────────────
    # 周报导出
    # ───────────────────────────────

    @staticmethod
    def _resolve_week_range(
        records: Iterable[ReflectionRecord], week_start: Optional[datetime]
    ) -> Tuple[List[ReflectionRecord], datetime]:
        """确定周报所覆盖的周起始日。"""
        records = list(records)
        if week_start is None:
            latest = max(
                (r.created_at for r in records if r.created_at), default=datetime.now()
            )
            week_start = latest - timedelta(days=latest.weekday())
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        records.sort(key=lambda record: (record.created_at, record.id), reverse=True)
        return records, week_start

    @staticmethod
    def week_start(value: datetime) -> datetime:
        """Return the canonical Monday boundary for one Reflection timestamp."""

        return (value - timedelta(days=value.weekday())).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

    @classmethod
    def shifts_for_week(
        cls,
        shifts: Iterable[CognitiveShift],
        week_start: datetime,
    ) -> List[CognitiveShift]:
        """Return only shifts whose committed timestamp falls in the week."""

        week_end = week_start + timedelta(days=7)
        return sorted(
            (
                shift
                for shift in shifts
                if shift.shift_detected_at is not None
                and week_start <= shift.shift_detected_at < week_end
            ),
            key=lambda shift: (
                shift.shift_detected_at or datetime.min,
                shift.dimension,
                shift.shift_type,
            ),
            reverse=True,
        )

    @staticmethod
    def _render_weekly_frontmatter(
        week_start: datetime,
        week_end: datetime,
        records: List[ReflectionRecord],
    ) -> List[str]:
        """渲染周报 YAML frontmatter。"""
        sources = [f"reflection-store:reflections/{record.id}" for record in records]
        if not sources:
            sources = [
                f"reflection-store:weekly/{week_start.date().isoformat()}"
                f"/{week_end.date().isoformat()}"
            ]
        frontmatter = {
            "report_type": "weekly",
            "week_start": _iso(week_start),
            "week_end": _iso(week_end),
            "generated_at": max(
                (record.created_at for record in records if record.created_at),
                default=week_end,
            ).isoformat(),
            "reflection_count": len(records),
            "source_count": len(sources),
            "sources": sources,
            "evidence_level": "multiple" if len(sources) > 1 else "single",
            "knowledge_stage": "P2",
            "status": "active",
        }

        lines = ["---"]
        for k, v in frontmatter.items():
            lines.extend(_format_frontmatter_field(k, v))
        lines.append("---")
        lines.append("")
        return lines

    @staticmethod
    def _render_trigger_distribution(records: List[ReflectionRecord]) -> List[str]:
        """渲染触发类型分布。"""
        trigger_counts: Dict[str, int] = defaultdict(int)
        for r in records:
            trigger_counts[r.trigger.value] += 1

        lines = ["## 触发分布", ""]
        if trigger_counts:
            for trigger, count in sorted(trigger_counts.items(), key=lambda x: -x[1]):
                lines.append(f"- `{trigger}`: {count} 次")
        else:
            lines.append("_本周无反思记录_")
        lines.append("")
        return lines

    @staticmethod
    def _render_dimension_distribution(records: List[ReflectionRecord]) -> List[str]:
        """渲染涉及维度分布。"""
        dim_counts: Dict[str, int] = defaultdict(int)
        for r in records:
            for dim in r.mirror_dimensions or []:
                dim_counts[dim] += 1

        if not dim_counts:
            return []

        lines = ["## 涉及维度", ""]
        for dim, count in sorted(dim_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {dim}: {count} 次")
        lines.append("")
        return lines

    @staticmethod
    def _render_weekly_insights(records: List[ReflectionRecord]) -> List[str]:
        """渲染 Insight 摘要列表。"""
        if not records:
            return []

        lines = ["## Insight 摘要", ""]
        for r in sorted(records, key=lambda x: x.created_at or datetime.min, reverse=True):
            summary = r.insight.summary if r.insight else "_无摘要_"
            ts = (r.created_at or datetime.now()).strftime("%Y-%m-%d")
            lines.append(f"- **{ts}** `{r.id}` ({r.trigger.value}): {summary}")
        lines.append("")
        return lines

    @staticmethod
    def _render_shift_summary(shifts: Optional[Iterable[CognitiveShift]]) -> List[str]:
        """渲染认知变迁汇总。"""
        if shifts is None:
            return []

        shift_list = list(shifts)
        dim_shift_counts: Dict[str, int] = defaultdict(int)
        for s in shift_list:
            dim_shift_counts[s.dimension] += 1

        if not dim_shift_counts:
            return []

        lines = ["## 认知变迁", ""]
        for dim, count in sorted(dim_shift_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {dim}: {count} 次")
        lines.append("")
        return lines

    @staticmethod
    def _render_weekly_footer() -> List[str]:
        """渲染周报底部用户备注区。"""
        return [
            "## 用户备注",
            "",
            "```",
            "# 你可以在这里写周报复盘，例如：",
            '# - "本周 decisions 维度触发最多，说明决策压力较大"',
            '# - "growth 维度的变迁值得写入项目总结"',
            "```",
            "",
            "_（在此写下你的判断...）_",
            "",
        ]

    def export_weekly_report(
        self,
        records: Iterable[ReflectionRecord],
        shifts: Optional[Iterable[CognitiveShift]] = None,
        week_start: Optional[datetime] = None,
    ) -> Path:
        """导出周度反思汇总报告"""
        records, week_start = self._resolve_week_range(records, week_start)
        shift_list = list(shifts or ())
        week_end = week_start + timedelta(days=6)
        date_label = week_start.strftime("%Y-%m-%d")

        file_path = self.reports_dir / f"weekly-{date_label}.md"

        lines: List[str] = []
        lines.extend(self._render_weekly_frontmatter(week_start, week_end, records))

        lines.append(f"# 反思周报 {date_label}")
        lines.append("")
        lines.append(f"周期: {week_start.date()} ~ {week_end.date()}")
        lines.append("")

        lines.extend(self._render_trigger_distribution(records))
        lines.extend(self._render_dimension_distribution(records))
        lines.extend(self._render_weekly_insights(records))
        lines.extend(self._render_shift_summary(shift_list))
        lines.extend(self._render_weekly_footer())

        self._publish_page(
            file_path,
            "\n".join(lines),
            page_role="derived_report:reflection_weekly",
            canonical_source={
                "week_start": week_start,
                "week_end": week_end,
                "records": records,
                "shifts": shift_list,
            },
            source_refs=tuple(
                f"reflection-store:reflections/{record.id}" for record in records
            )
            or (f"reflection-store:weekly/{date_label}",),
        )
        return file_path

    # ───────────────────────────────
    # 全量导出
    # ───────────────────────────────

    def export_all(self, store: ReflectionStore):
        """从 Store 全量导出：记录、维度变迁、周报（过滤空记录）"""
        records = store.get_all_for_projection()
        shifts = store.get_all_shifts_for_projection()

        if self._pending_pages is not None:
            raise RuntimeError("Reflection projection generation is already active")
        self._pending_pages = []
        try:
            exported = 0
            skipped = 0
            for record in records:
                if self._is_empty_record(record):
                    skipped += 1
                    continue
                self.export_record(record)
                exported += 1

            self.export_shifts(shifts)

            records_by_id = {record.id: record for record in records}
            shifts_by_reflection: Dict[str, List[CognitiveShift]] = defaultdict(list)
            for shift in shifts:
                if shift.related_reflection_id in records_by_id:
                    shifts_by_reflection[shift.related_reflection_id].append(shift)
            for reflection_id, related_shifts in sorted(shifts_by_reflection.items()):
                self.export_knowledge_updates(
                    records_by_id[reflection_id],
                    [knowledge_update_from_shift(shift) for shift in related_shifts],
                )

            records_by_week: Dict[datetime, List[ReflectionRecord]] = defaultdict(list)
            for record in records:
                records_by_week[self.week_start(record.created_at)].append(record)
            for week_start, week_records in sorted(records_by_week.items()):
                self.export_weekly_report(
                    week_records,
                    shifts=self.shifts_for_week(shifts, week_start),
                    week_start=week_start,
                )
            pages = tuple(self._pending_pages)
        finally:
            self._pending_pages = None

        for scope in (
            self.reflections_dir,
            self.shifts_dir,
            self.knowledge_updates_dir,
            self.reports_dir,
        ):
            scoped_pages = [page for page in pages if page.path.is_relative_to(scope)]
            generation = self.lifecycle.publish_generation(
                projection_kind="reflection",
                scope_root=scope,
                pages=scoped_pages,
                full=True,
                owned_paths=tuple(page.path for page in scoped_pages),
            )
            if generation.status != "committed":
                raise RuntimeError(f"Reflection projection generation did not commit: {scope}")

        if skipped:
            logger.info(
                "[ReflectionExporter] 跳过 %s 条空 reflection，导出 %s 条", skipped, exported
            )

        return {
            "records": exported,
            "shifts": len(shifts),
        }

    def _scope_for_path(self, path: Path) -> Path:
        for scope in (
            self.reflections_dir,
            self.shifts_dir,
            self.knowledge_updates_dir,
            self.reports_dir,
        ):
            if path.is_relative_to(scope):
                return scope
        raise ValueError(f"Reflection projection path is outside its owned scopes: {path}")

    def _publish_page(
        self,
        path: Path,
        content: str,
        *,
        page_role: str,
        canonical_source: object,
        source_refs: tuple[str, ...],
    ) -> None:
        page = ProjectionPageSpec(
            path=path,
            content=content,
            page_role=page_role,
            canonical_revision=canonical_projection_revision(canonical_source),
            source_refs=source_refs,
        )
        if self._pending_pages is not None:
            self._pending_pages.append(page)
            return
        generation = self.lifecycle.publish_generation(
            projection_kind="reflection",
            scope_root=self._scope_for_path(path),
            pages=[page],
            full=False,
        )
        if generation.status != "committed":
            raise RuntimeError(f"Reflection projection page was not published: {path}")

    def _delete_page(self, path: Path) -> None:
        if self._pending_pages is not None:
            return
        generation = self.lifecycle.publish_generation(
            projection_kind="reflection",
            scope_root=self._scope_for_path(path),
            pages=[],
            full=False,
            stale_paths=[path],
        )
        if generation.status != "committed":
            raise RuntimeError(f"Reflection projection deletion did not commit: {path}")
