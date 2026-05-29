# -*- coding: utf-8 -*-
"""
WeeklyReportGenerator — 每周画像报告

每周日自动生成，写入 wiki/99-Reports/画像周报-YYYY-WXX.md
1 页 A4 简洁格式。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional



logger = logging.getLogger(__name__)
class WeeklyReportGenerator:
    """每周画像报告生成器"""

    def __init__(self, wiki_base: Optional[str] = None):
        if wiki_base:
            self.wiki_base = Path(wiki_base).expanduser()
        else:
            from core.config import get_config
            self.wiki_base = get_config().wiki_dir

    def generate_weekly_report(self) -> str:
        """
        生成本周画像报告。

        Returns:
            报告 Markdown 内容
        """
        now = datetime.now()
        week_num = now.isocalendar()[1]
        year = now.year
        report_id = f"{year}-W{week_num:02d}"

        lines = [
            f"# 画像周报 {report_id}",
            f"",
            f"> 生成时间：{now.strftime('%Y-%m-%d %H:%M')}",
            f"",
        ]

        # 1. 知识增长统计
        lines.extend(self._section_knowledge_growth())

        # 2. 领域注意力变化
        lines.extend(self._section_domain_shifts())

        # 3. 盲点发现
        lines.extend(self._section_blindspots())

        # 4. 演化信号
        lines.extend(self._section_evolution_signals())

        # 5. 系统指标
        lines.extend(self._section_system_metrics())

        content = "\n".join(lines)

        # 写入 Wiki
        report_path = self.wiki_base / "99-Reports" / f"画像周报-{report_id}.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(content, encoding="utf-8")

        logger.info(f"周报已生成: {report_path}")
        return content

    def _section_knowledge_growth(self) -> List[str]:
        """本周知识增长统计"""
        lines = [
            "## 知识增长",
            "",
        ]

        try:
            from core.kia.knowledge_graph import KnowledgeGraph
            kg = KnowledgeGraph(wiki_base=str(self.wiki_base))
            stats = kg.get_stats()

            lines.append(f"- 总知识条目：{stats.get('total_entities', 0)}")
            lines.append(f"- 本周新增：{stats.get('new_this_week', 0)}")

            # 按形态分类
            by_form = stats.get("by_form", {})
            if by_form:
                lines.append("")
                lines.append("| 形态 | 数量 |")
                lines.append("|------|------|")
                for form, count in sorted(by_form.items(), key=lambda x: -x[1]):
                    lines.append(f"| {form} | {count} |")
        except Exception as e:
            lines.append(f"- 统计数据获取失败: {e}")

        lines.append("")
        return lines

    def _section_domain_shifts(self) -> List[str]:
        """领域注意力变化"""
        lines = [
            "## 领域注意力变化",
            "",
        ]

        try:
            from core.persona.daimon import SignalCollector
            from core.persona.psyche import get_signal_store
            store = get_signal_store()
            stats = store.get_signal_stats(days=7)

            if stats:
                lines.append("| 源 | 信号数 |")
                lines.append("|----|--------|")
                for source, count in sorted(stats.items(), key=lambda x: -x[1]):
                    lines.append(f"| {source} | {count} |")
            else:
                lines.append("- 本周无足够数据")
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at weekly_report.py", exc_info=True)
            lines.append("- 数据采集未就绪")

        lines.append("")
        return lines

    def _section_blindspots(self) -> List[str]:
        """盲点发现"""
        lines = [
            "## 盲点发现",
            "",
        ]

        try:
            from core.app.blindspot_discovery import BlindspotDiscovery
            bd = BlindspotDiscovery(wiki_base=str(self.wiki_base))
            blindspots = bd.get_weekly_summary()

            if blindspots:
                for bs in blindspots[:5]:
                    lines.append(f"- **{bs.get('topic', '')}**：{bs.get('description', '')}")
            else:
                lines.append("- 本周未发现新盲点")
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at weekly_report.py", exc_info=True)
            lines.append("- 盲点检测未就绪")

        lines.append("")
        return lines

    def _section_evolution_signals(self) -> List[str]:
        """演化信号"""
        lines = [
            "## 演化信号",
            "",
        ]

        try:
            from core.persona.evolution_timeline import PersonaEvolutionTimeline
            timeline = PersonaEvolutionTimeline()
            report = timeline.generate()
            # 只取关键事件部分
            event_section = report.split("## 维度变化")[0] if "## 维度变化" in report else report
            lines.append(event_section.strip())
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at weekly_report.py", exc_info=True)
            lines.append("- 演化数据不足（需至少 2 个快照）")

        lines.append("")
        return lines

    def _section_system_metrics(self) -> List[str]:
        """系统指标"""
        lines = [
            "## 系统指标",
            "",
        ]

        try:
            from core.config import get_config
            data_dir = get_config().data_dir

            # 蒸馏统计
            from core.hephaestus.distillation_engine import DistillationEngine
            # 简单统计
            lines.append("| 指标 | 值 |")
            lines.append("|------|-----|")

            # sync_log 统计
            import sqlite3
            sync_db = data_dir / "sync_log.db"
            if sync_db.exists():
                with sqlite3.connect(str(sync_db), timeout=10) as conn:
                    cursor = conn.execute("SELECT COUNT(*) FROM sync_log WHERE date(synced_at) >= date('now', '-7 days')")
                    weekly_syncs = cursor.fetchone()[0]
                    lines.append(f"| 本周同步 | {weekly_syncs} |")

        except Exception as e:
            lines.append(f"- 指标获取失败: {e}")

        lines.append("")
        return lines
