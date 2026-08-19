# -*- coding: utf-8 -*-
"""
mnemos stress — 知识压力测试

- run [--limit N] [--dry-run]: 对知识页面运行压力测试
- status: 查看最近测试结果统计
"""

from __future__ import annotations

import sqlite3

from core.config import get_config
from core.kia.stress_test import StressTestEngine, stress_test_page


def _count_recent(db_path) -> int:
    try:
        with sqlite3.connect(str(db_path), timeout=5) as conn:
            row = conn.execute("SELECT COUNT(*) FROM stress_test_results").fetchone()
            return row[0] if row else 0
    except sqlite3.Error:
        return 0


def cmd_stress(args) -> int:
    """压力测试 CLI 入口。"""
    cfg = get_config()
    dry_run = getattr(args, "dry_run", False)
    engine = StressTestEngine(wiki_base=str(cfg.wiki_dir), dry_run=dry_run)
    cmd = getattr(args, "stress_cmd", None)

    if cmd == "run":
        page = getattr(args, "page", None)
        if page:
            report = stress_test_page(page, wiki_base=str(cfg.wiki_dir), dry_run=dry_run)
            print(report)
            return 0
        limit = getattr(args, "limit", None)
        results = engine.batch_test(limit=limit, dry_run=dry_run)
        if not results:
            print("未找到可测试的知识页面")
            return 0
        avg = sum(r.resilience_score for r in results) / max(len(results), 1)
        total = sum(len(r.challenges) for r in results)
        print(f"压力测试完成: {len(results)} 个页面, " f"平均韧性 {avg:.1f}, {total} 个挑战")
        for r in results[:10]:
            print(f"  - {r.page_title}: {r.resilience_score:.1f} ({len(r.challenges)} 挑战)")
        return 0

    if cmd == "status":
        count = _count_recent(engine._db_path)
        print(f"已记录压力测试结果: {count} 条")
        return 0

    print("未知子命令")
    return 1
