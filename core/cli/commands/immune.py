# -*- coding: utf-8 -*-
"""
mnemos immune — 知识免疫扫描管理

- scan [--write-report]: 运行知识免疫扫描并打印摘要，可写入 Markdown 报告
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from core.config import get_config
from core.kia.hygieia import KnowledgeImmuneSystem


def _write_report(immune: KnowledgeImmuneSystem, report) -> Path:
    wiki_dir = Path(get_config().wiki_dir).expanduser()
    reports_dir = wiki_dir / "99-Reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = reports_dir / f"知识免疫报告-{timestamp}.md"
    report_path.write_text(immune.generate_report_markdown(report), encoding="utf-8")
    return report_path


def cmd_immune(args) -> int:
    """知识免疫 CLI 入口。"""
    cfg = get_config()
    cmd = getattr(args, "immune_cmd", None)

    if cmd == "scan":
        immune = KnowledgeImmuneSystem(wiki_base=str(cfg.wiki_dir))
        report = immune.full_scan()
        print(
            f"免疫扫描完成: "
            f"pages={report.scanned_pages}, "
            f"issues={len(report.issues)}, "
            f"critical={report.critical_count}, "
            f"auto_fixable={report.auto_fixable_count}, "
            f"score={report.health_score:.0f}/100"
        )
        if report.summary:
            print("问题分布:")
            for name, count in report.summary.items():
                print(f"  - {name}: {count}")
        if getattr(args, "write_report", False):
            report_path = _write_report(immune, report)
            print(f"报告已写入: {report_path}")
        return 0

    print("未知子命令")
    return 1
