# -*- coding: utf-8 -*-
"""
mnemos entropy — 知识熵减管理

- scan [--limit N] [--write-report]: 运行熵减扫描并打印摘要
- auto-fix [--apply-links]: 自动执行建议（默认不删除，可建立 KG 关系）
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import List

from core import config as _config_mod
from core.kia.eris import EntropyEngine, run_and_report, run_entropy_scan


def _wiki_base() -> Path:
    return Path(_config_mod.get_config().wiki_dir).expanduser()


def _write_report(report) -> Path:
    wiki_dir = _wiki_base()
    reports_dir = wiki_dir / "99-Reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = reports_dir / f"知识熵减报告-{timestamp}.md"
    report_text = run_and_report(wiki_base=str(wiki_dir), report=report)
    report_path.write_text(report_text, encoding="utf-8")
    return report_path


def cmd_entropy(args) -> int:
    """熵减 CLI 入口。"""
    cfg = _config_mod.get_config()
    cmd = getattr(args, "entropy_cmd", None)

    if cmd == "scan":
        limit = getattr(args, "limit", None)
        write_report = getattr(args, "write_report", False)
        report = run_entropy_scan(wiki_base=str(cfg.wiki_dir), sample_size=limit)
        print(
            f"熵减扫描完成: "
            f"pairs={report.total_pairs_scanned}, "
            f"candidates={len(report.candidates)}, "
            f"duplicates={report.duplicate_count}, "
            f"mergeable={report.mergeable_count}, "
            f"linkable={report.linkable_count}"
        )
        if report.candidates:
            print("前 5 个候选:")
            for c in report.candidates[:5]:
                print(f"  - {c.page_a} ↔ {c.page_b} ({c.merge_strategy}, {c.similarity:.3f})")
        if write_report:
            report_path = _write_report(report)
            print(f"报告已写入: {report_path}")
        return 0

    if cmd == "auto-fix":
        apply_links = getattr(args, "apply_links", False)
        engine = EntropyEngine(wiki_base=str(cfg.wiki_dir))
        report = engine.scan()
        if not report.candidates:
            print("无候选可处理")
            return 0
        actions: List[str] = engine.auto_fix(
            report, apply_duplicates=False, apply_links=apply_links
        )
        print(f"自动处理完成: {len(actions)} 个操作")
        for a in actions:
            print(f"  - {a}")
        return 0

    print("未知子命令")
    return 1
