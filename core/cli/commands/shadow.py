# -*- coding: utf-8 -*-
"""
mnemos shadow — 影子页面管理

- sync [--page PATTERN]: 批量生成/更新影子页面
- premise [--page PATTERN]: 批量验证页面前提条件
- status: 查看当前影子页面统计
"""

from __future__ import annotations

from pathlib import Path

from core.config import get_config
from core.kia.hecate import PremiseValidator, ShadowPageManager


def cmd_shadow(args) -> int:
    """影子页面 CLI 入口。"""
    cfg = get_config()
    spm = ShadowPageManager(wiki_base=str(cfg.wiki_dir))
    cmd = getattr(args, "shadow_cmd", None)

    if cmd == "sync":
        pattern = getattr(args, "page", "*.md")
        stats = spm.batch_sync(page_pattern=pattern)
        status = stats.get("status", "ok")
        print(
            f"影子页面同步完成 (status={status}): "
            f"created={stats.get('created', 0)}, "
            f"updated={stats.get('updated', 0)}, "
            f"failed={stats.get('failed', 0)}, "
            f"total={stats.get('total', 0)}"
        )
        return 0 if status == "ok" else 1

    if cmd == "status":
        shadows = spm.list_shadows()
        print(f"影子页面目录: {spm.shadow_dir}")
        print(f"影子页面数量: {len(shadows)}")
        for s in shadows[:20]:
            print(f"  - {s.name}")
        return 0

    if cmd == "premise":
        pattern = getattr(args, "page", "*.md")
        validator = PremiseValidator(wiki_base=Path(cfg.wiki_dir).expanduser())
        results = validator.validate_batch(page_pattern=pattern)
        total_changes = sum(len(changes) for changes in results.values())
        print(
            "前提条件批量验证完成: "
            f"pages={len(results)}, changes={total_changes}"
        )
        for page_path, changes in sorted(results.items())[:20]:
            print(f"  - {page_path}: {len(changes)} 个变化")
        return 0

    print("未知子命令")
    return 1
