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

        # 6. 反复遇到的问题
        lines.extend(self._section_repeated_issues())

        # 7. 下周行动建议
        lines.extend(self._section_action_items())

        content = "\n".join(lines)

        # 写入 Wiki
        report_path = self.wiki_base / "99-Reports" / f"画像周报-{report_id}.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(content, encoding="utf-8")

        logger.info(f"周报已生成: {report_path}")
        return content

    def _section_knowledge_growth(self) -> List[str]:
        """本周知识增长统计——直接查询 wiki_state.db"""
        lines = [
            "## 知识增长",
            "",
        ]

        try:
            import sqlite3
            from core.config import get_config
            wiki_state_db = get_config().data_dir / "wiki_state.db"
            total_pages = 0
            new_this_week = 0
            if wiki_state_db.exists():
                with sqlite3.connect(str(wiki_state_db), timeout=10) as conn:
                    cursor = conn.execute("SELECT COUNT(*) FROM wiki_pages")
                    total_pages = cursor.fetchone()[0]
                    cursor = conn.execute(
                        "SELECT COUNT(*) FROM wiki_pages WHERE created_at >= datetime('now', '-7 days')"
                    )
                    new_this_week = cursor.fetchone()[0]

            lines.append(f"- 总 Wiki 页面：{total_pages}")
            lines.append(f"- 本周新增：{new_this_week}")
        except Exception as e:
            lines.append(f"- 统计数据获取失败: {e}")

        lines.append("")
        return lines

    def _section_domain_shifts(self) -> List[str]:
        """领域注意力变化——从 user_signals.db 读取本周高频 task_type"""
        lines = [
            "## 领域注意力变化",
            "",
        ]

        try:
            import sqlite3
            from core.config import get_config
            signals_db = get_config().data_dir / "user_signals.db"
            if signals_db.exists():
                with sqlite3.connect(str(signals_db), timeout=10) as conn:
                    cursor = conn.execute("""
                        SELECT task_type, COUNT(*) as cnt
                        FROM session_signals
                        WHERE timestamp >= datetime('now', '-7 days')
                          AND task_type IS NOT NULL
                        GROUP BY task_type
                        ORDER BY cnt DESC
                        LIMIT 8
                    """)
                    rows = cursor.fetchall()
                    if rows:
                        lines.append("| 领域 | Session 数 |")
                        lines.append("|------|------------|")
                        for task_type, count in rows:
                            lines.append(f"| {task_type or '未分类'} | {count} |")
                    else:
                        lines.append("- 本周无足够数据")
            else:
                lines.append("- 信号数据库未就绪")
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at weekly_report.py", exc_info=True)
            lines.append("- 数据采集未就绪")

        lines.append("")
        return lines

    def _section_blindspots(self) -> List[str]:
        """盲点发现——基于 wiki_metrics.db query_log 中搜索无结果的查询"""
        lines = [
            "## 盲点发现",
            "",
        ]

        try:
            import sqlite3
            from core.config import get_config
            metrics_db = get_config().data_dir / "wiki_metrics.db"
            no_result_queries = []
            if metrics_db.exists():
                with sqlite3.connect(str(metrics_db), timeout=10) as conn:
                    cursor = conn.execute("""
                        SELECT query_text, COUNT(*) as cnt
                        FROM query_log
                        WHERE created_at >= datetime('now', '-7 days')
                          AND matched_pages = '[]'
                          AND query_text IS NOT NULL
                        GROUP BY query_text
                        ORDER BY cnt DESC
                        LIMIT 5
                    """)
                    no_result_queries = cursor.fetchall()

            if no_result_queries:
                for query_text, count in no_result_queries:
                    lines.append(f"- **{query_text}**（搜索 {count} 次无结果）")
            else:
                # 回退到 blindspots.db
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
                    lines.append("- 盲点检测未就绪")
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
        """系统指标——从 sync_log.db 读取本周同步/跳过/失败统计"""
        lines = [
            "## 系统指标",
            "",
        ]

        try:
            import sqlite3
            from core.config import get_config
            data_dir = get_config().data_dir
            lines.append("| 指标 | 值 |")
            lines.append("|------|-----|")

            sync_db = data_dir / "sync_log.db"
            if sync_db.exists():
                with sqlite3.connect(str(sync_db), timeout=10) as conn:
                    # 本周同步
                    cursor = conn.execute(
                        "SELECT COUNT(*) FROM sync_log WHERE synced_at >= datetime('now', '-7 days')"
                    )
                    weekly_syncs = cursor.fetchone()[0]
                    lines.append(f"| 本周同步 | {weekly_syncs} |")
                    # 按状态分组
                    cursor = conn.execute("""
                        SELECT status, COUNT(*) FROM sync_log
                        WHERE synced_at >= datetime('now', '-7 days')
                        GROUP BY status
                    """)
                    for status, count in cursor.fetchall():
                        lines.append(f"| 状态: {status or 'unknown'} | {count} |")
                    # 失败数
                    cursor = conn.execute("""
                        SELECT COUNT(*) FROM sync_log
                        WHERE synced_at >= datetime('now', '-7 days')
                          AND (error IS NOT NULL OR distill_error IS NOT NULL)
                    """)
                    failed = cursor.fetchone()[0]
                    lines.append(f"| 失败/错误 | {failed} |")
            else:
                lines.append("- sync_log 数据库未找到")
        except Exception as e:
            lines.append(f"- 指标获取失败: {e}")

        lines.append("")
        return lines

    def _section_repeated_issues(self) -> List[str]:
        """反复遇到的问题——从 sync_log 读取本周高频跳过/错误原因"""
        lines = [
            "## 反复遇到的问题",
            "",
        ]

        try:
            import sqlite3
            from collections import Counter
            from core.config import get_config
            sync_db = get_config().data_dir / "sync_log.db"
            if sync_db.exists():
                with sqlite3.connect(str(sync_db), timeout=10) as conn:
                    cursor = conn.execute("""
                        SELECT error, distill_error, status
                        FROM sync_log
                        WHERE synced_at >= datetime('now', '-7 days')
                    """)
                    reasons = Counter()
                    for error, distill_error, status in cursor.fetchall():
                        if error:
                            reasons[f"同步错误: {error[:40]}"] += 1
                        if distill_error:
                            reasons[f"蒸馏错误: {distill_error[:40]}"] += 1
                        if status and status not in ("synced", "pending"):
                            reasons[f"状态: {status}"] += 1
                    if reasons:
                        lines.append("| 问题 | 次数 |")
                        lines.append("|------|------|")
                        for reason, count in reasons.most_common(5):
                            lines.append(f"| {reason} | {count} |")
                    else:
                        lines.append("- 本周未发现高频问题")
            else:
                lines.append("- sync_log 数据库未找到")
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at weekly_report.py", exc_info=True)
            lines.append("- 问题统计未就绪")

        lines.append("")
        return lines

    def _section_action_items(self) -> List[str]:
        """下周行动建议——基于上述数据生成避免重复工作的建议"""
        lines = [
            "## 下周行动建议",
            "",
        ]

        suggestions = []
        try:
            import sqlite3
            from core.config import get_config
            # 1. 如果有大量同步错误，建议检查配置
            sync_db = get_config().data_dir / "sync_log.db"
            if sync_db.exists():
                with sqlite3.connect(str(sync_db), timeout=10) as conn:
                    cursor = conn.execute("""
                        SELECT COUNT(*) FROM sync_log
                        WHERE synced_at >= datetime('now', '-7 days') AND error IS NOT NULL
                    """)
                    if cursor.fetchone()[0] >= 3:
                        suggestions.append("- sync_log 本周错误较多，建议检查 Memos API 连接和 Token 有效性")

            # 2. 如果有搜索无结果的查询，建议补充知识
            metrics_db = get_config().data_dir / "wiki_metrics.db"
            if metrics_db.exists():
                with sqlite3.connect(str(metrics_db), timeout=10) as conn:
                    cursor = conn.execute("""
                        SELECT COUNT(DISTINCT query_text) FROM query_log
                        WHERE created_at >= datetime('now', '-7 days') AND matched_pages = '[]'
                    """)
                    if cursor.fetchone()[0] >= 2:
                        suggestions.append("- 本周多次搜索无结果，建议将盲区主题补充进知识库")

            # 3. 如果新增页面少，建议增加蒸馏
            wiki_state_db = get_config().data_dir / "wiki_state.db"
            if wiki_state_db.exists():
                with sqlite3.connect(str(wiki_state_db), timeout=10) as conn:
                    cursor = conn.execute(
                        "SELECT COUNT(*) FROM wiki_pages WHERE created_at >= datetime('now', '-7 days')"
                    )
                    if cursor.fetchone()[0] < 2:
                        suggestions.append("- 本周 Wiki 新增页面较少，建议检查蒸馏流水线是否正常运转")
        except Exception:
            pass

        if suggestions:
            lines.extend(suggestions)
        else:
            lines.append("- 系统运行平稳，暂无特别建议")

        lines.append("")
        return lines
