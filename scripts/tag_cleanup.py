#!/usr/bin/env python3
"""
Memos 标签清理脚本
1. 删除无值标签 (from, scope)
2. 将 claude-private 替换为 scope=private
3. 给有 source+session 但缺 level 的记录补打 level=L1
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# 相对项目根目录的路径
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from integrations.styx import MemosClient
from core.config import get_config


def cleanup_tags(batch_size: int = None, dry_run: bool = False):
    config = get_config()
    token = config.memos_token or os.getenv("MEMOS_TOKEN")
    if not token:
        raise ValueError("MEMOS_TOKEN 环境变量未设置")

    client = MemosClient(token=token, agent="tag-cleanup")

    print("[TagCleanup] 获取所有记录...")
    memos = client.list_all_memos()
    print(f"[TagCleanup] 总计 {len(memos)} 条记录")

    stats = {
        "empty_tag_removed": 0,
        "private_fixed": 0,
        "level_added": 0,
        "skipped": 0,
        "errors": 0,
        "updated": 0,
    }

    to_process = memos[:batch_size] if batch_size else memos
    total = len(to_process)

    for i, memo in enumerate(to_process):
        if (i + 1) % 100 == 0 or i == 0:
            print(f"  [TagCleanup] 处理中 {i+1}/{total}...")

        name = memo.get("name", "")
        uid = name.replace("memos/", "") if name.startswith("memos/") else name
        tags = memo.get("tags", [])
        content = memo.get("content", "")

        if not uid:
            continue

        # Build cleaned tag list
        new_tags = []
        changed = False

        for tag in tags:
            # 1. Delete empty-value tags
            if tag in ("from", "scope"):
                stats["empty_tag_removed"] += 1
                changed = True
                continue

            # 2. Replace claude-private with scope=private
            if tag == "claude-private":
                new_tags.append("scope=private")
                stats["private_fixed"] += 1
                changed = True
                continue

            # Keep other tags, deduplicate
            if tag not in new_tags:
                new_tags.append(tag)

        # 3. Add level=L1 for records with source+session but no level
        has_session = any(t.startswith("session=") for t in new_tags)
        has_level = any(t.startswith("level=") for t in new_tags)
        has_source = any(t.startswith("source=") for t in new_tags)

        if has_source and has_session and not has_level:
            new_tags.append("level=L1")
            stats["level_added"] += 1
            changed = True

        if not changed:
            stats["skipped"] += 1
            continue

        if dry_run:
            stats["updated"] += 1
            continue

        # Update record
        try:
            # Remove trailing tag lines from content
            lines = content.split("\n")
            while lines and lines[-1].strip().startswith("#"):
                lines.pop()
            while lines and not lines[-1].strip():
                lines.pop()

            new_content = "\n".join(lines)

            # Add cleaned tags at the end
            tag_line = " ".join([f"#{t}" for t in new_tags])
            new_content = f"{new_content}\n\n{tag_line}"

            result = client.update_memo(uid, content=new_content)
            if result:
                stats["updated"] += 1
            else:
                stats["errors"] += 1
                print(f"    [TagCleanup] 更新失败 {uid[:16]}...")

            # Small delay to avoid rate limiting
            time.sleep(0.05)

        except Exception as e:
            stats["errors"] += 1
            print(f"    [TagCleanup] 异常 {uid[:16]}...: {e}")

    print(f"\n=== 标签清理完成 ===")
    print(f"  需更新: {stats['updated']}")
    print(f"  删除空值标签: {stats['empty_tag_removed']}")
    print(f"  修复 private: {stats['private_fixed']}")
    print(f"  补打 level=L1: {stats['level_added']}")
    print(f"  跳过: {stats['skipped']}")
    print(f"  错误: {stats['errors']}")

    return stats


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Memos Tag Cleanup")
    parser.add_argument("--dry-run", action="store_true", help="统计不执行")
    parser.add_argument("--batch", type=int, help="仅处理前N条")
    args = parser.parse_args()

    cleanup_tags(batch_size=args.batch, dry_run=args.dry_run)
