# -*- coding: utf-8 -*-
"""
mnenos freshness — 知识新鲜度管理

- list [--status stale|fresh|all]: 列出 wiki 中页面新鲜度状态
- refresh <page_path>: 手动刷新指定页面
- refresh-all [--limit N]: 批量刷新过期页面
"""

from __future__ import annotations

from pathlib import Path

from core import config as _config_mod
from core.app.freshness_refresh_worker import FreshnessRefreshWorker


def _wiki_base() -> Path:
    return Path(_config_mod.get_config().wiki_dir).expanduser()


def cmd_freshness(args) -> int:
    """新鲜度 CLI 入口。"""
    cfg = _config_mod.get_config()
    worker = FreshnessRefreshWorker(wiki_base=str(cfg.wiki_dir))
    cmd = getattr(args, "freshness_cmd", None)

    if cmd == "list":
        status = getattr(args, "status", "all") or "all"
        pages = worker.list_pages(status_filter=status)
        if not pages:
            print("暂无页面")
            return 0
        print(f"共 {len(pages)} 个页面 (status={status})")
        for p in pages:
            suffix = ""
            if p["status"] == "stale":
                suffix = f" [{p.get('severity', '')}] {p.get('message', '')}"
            print(f"  - {p['path']} ({p['status']}){suffix}")
        return 0

    if cmd == "refresh":
        page_path = getattr(args, "page_path", None)
        if not page_path:
            print("错误: 缺少页面路径")
            return 1
        result = worker.refresh_page(page_path)
        if result.status == "refreshed":
            print(
                f"已刷新: {result.path} (backup={result.backup_path}, updated_at={result.updated_at})"
            )
        elif result.status == "skipped":
            print(f"已跳过: {result.path} ({result.reason})")
        else:
            print(f"错误: {result.error}")
            return 1
        return 0

    if cmd == "refresh-all":
        limit = getattr(args, "limit", 10) or 10
        report = worker.refresh_all_stale(limit=limit)
        print(
            f"批量刷新完成: "
            f"scanned={report.get('scanned', 0)}, "
            f"refreshed={report.get('refreshed', 0)}, "
            f"skipped={report.get('skipped', 0)}, "
            f"errors={report.get('errors', 0)}"
        )
        for r in report.get("results", []):
            print(f"  - {r.status}: {r.path}")
        return 0

    print("未知子命令")
    return 1
