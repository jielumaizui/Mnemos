# -*- coding: utf-8 -*-
"""
mnemos version — 版本时间旅行

- list <page_path>: 列出页面版本
- diff <page_path> [--from ID] [--to ID]: 对比版本
- restore <page_path> <snapshot_id> [--no-backup]: 回滚到指定版本
- create <page_path> [--summary TEXT]: 手动创建快照
- scan-all: 扫描全库为变更页面创建快照
"""

from __future__ import annotations

from pathlib import Path

from core.config import get_config
from core.kia.ananke import VersionTimeTravel, show_diff


def _resolve_page(args) -> Path:
    cfg = get_config()
    raw = getattr(args, "page_path", "")
    page = Path(raw)
    if not page.is_absolute():
        page = Path(cfg.wiki_dir) / raw
    return page


def cmd_version(args) -> int:
    """版本时间旅行 CLI 入口。"""
    cfg = get_config()
    vtt = VersionTimeTravel(wiki_base=str(cfg.wiki_dir))
    cmd = getattr(args, "version_cmd", None)

    if cmd == "list":
        page = _resolve_page(args)
        versions = vtt.list_versions(page)
        if not versions:
            print(f"页面暂无版本历史: {page}")
            return 0
        print(vtt.generate_timeline(page))
        return 0

    if cmd == "diff":
        page = _resolve_page(args)
        from_id = getattr(args, "from_id", None)
        to_id = getattr(args, "to_id", None)
        if from_id is None and to_id is None:
            rendered = show_diff(str(page), wiki_base=str(cfg.wiki_dir))
            if not rendered or rendered.startswith("暂无版本历史"):
                print("无法生成 diff：页面版本不足或指定版本不存在")
                return 1
            print(rendered)
            return 0

        diff = vtt.diff(page, from_snapshot=from_id, to_snapshot=to_id)
        if diff is None:
            print("无法生成 diff：页面版本不足或指定版本不存在")
            return 1
        print(vtt.diff_to_markdown(diff))
        return 0

    if cmd == "restore":
        page = _resolve_page(args)
        snapshot_id = getattr(args, "snapshot_id", None)
        if not snapshot_id:
            print("错误: 缺少 snapshot_id")
            return 1
        create_backup = not getattr(args, "no_backup", False)
        if vtt.restore(page, snapshot_id, create_backup=create_backup):
            print(f"已恢复 {page} 到版本 {snapshot_id[:8]}")
            return 0
        print(f"恢复失败: {page}")
        return 1

    if cmd == "create":
        page = _resolve_page(args)
        summary = getattr(args, "summary", "")
        snap = vtt.snapshot(page, change_summary=summary)
        if snap:
            print(f"已创建快照 {snap.snapshot_id[:8]} @ {snap.timestamp}")
            return 0
        print("快照未创建（内容未变化或页面不存在）")
        return 0

    if cmd == "scan-all":
        stats = vtt.scan_and_snapshot_all()
        print(
            f"扫描 {stats['scanned']} 页，新增快照 {stats['snapshotted']}，未变更 {stats['unchanged']}"
        )
        return 0

    print("未知子命令")
    return 1
