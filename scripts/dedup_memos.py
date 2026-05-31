#!/usr/bin/env python3
"""
Memos 重复数据清理脚本

策略：按 (session_id, turn_number) 分组，保留 createTime 最新的一条，删除其余。

安全机制：
- 默认 dry-run，只统计不删除
- 执行前自动备份 UID 列表到 ~/.mnemos/backups/
- 删除失败不中断，记录错误日志
"""

import sys
import json
import os
from pathlib import Path
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))

from integrations.styx import MemosClient
from core.config import get_config


def dedup_memos(dry_run: bool = True):
    config = get_config()
    token = config.memos_token or os.getenv("MEMOS_TOKEN")
    if not token:
        raise ValueError("MEMOS_TOKEN 环境变量未设置")

    client = MemosClient(token=token, agent="dedup")

    print("[Dedup] 获取所有记录...")
    memos = client.list_all()
    print(f"[Dedup] 总计 {len(memos)} 条记录")

    # 按 (session, turn) 分组
    groups = defaultdict(list)
    for m in memos:
        sid = ""
        turn = ""
        for t in m.tags:
            if t.startswith("session="):
                sid = t.split("=", 1)[1]
            if t.startswith("turn="):
                turn = t.split("=", 1)[1]
        if sid and turn:
            groups[(sid, turn)].append({
                "uid": m.uid,
                "createTime": m.created_at or "",
                "len": len(m.content),
            })

    # 找出重复组
    dupe_groups = {k: v for k, v in groups.items() if len(v) > 1}
    to_delete = []
    keep_count = 0
    delete_count = 0

    for key, items in dupe_groups.items():
        # 按 createTime 降序排序，保留最新的一条
        sorted_items = sorted(items, key=lambda x: x["createTime"], reverse=True)
        keep = sorted_items[0]
        delete = sorted_items[1:]
        keep_count += 1
        delete_count += len(delete)
        for d in delete:
            to_delete.append({
                "uid": d["uid"],
                "session": key[0],
                "turn": key[1],
                "createTime": d["createTime"],
                "len": d["len"],
            })

    print(f"[Dedup] 分组数: {len(groups)}")
    print(f"[Dedup] 重复组: {len(dupe_groups)}")
    print(f"[Dedup] 保留: {keep_count} 条")
    print(f"[Dedup] 待删除: {delete_count} 条")

    if dry_run:
        print(f"\n[Dedup] DRY RUN — 未执行删除")
        print(f"  如需执行删除，请运行: python3 scripts/dedup_memos.py --execute")
        return

    if delete_count == 0:
        print("[Dedup] 无重复数据，无需清理")
        return

    # 备份
    backup_dir = Path.home() / ".mnemos" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"dedup_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    backup_path.write_text(
        json.dumps(to_delete, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[Dedup] 备份已保存: {backup_path}")

    # 执行删除
    print(f"[Dedup] 开始删除 {delete_count} 条记录...")
    deleted = 0
    failed = 0
    for i, item in enumerate(to_delete, 1):
        if i % 50 == 0 or i == 1:
            print(f"  [Dedup] 删除中 {i}/{delete_count}...")
        try:
            ok = client.delete(item["uid"])
            if ok:
                deleted += 1
            else:
                failed += 1
                print(f"  [Dedup] 删除失败: {item['uid'][:12]}")
        except Exception as e:
            failed += 1
            print(f"  [Dedup] 删除异常 {item['uid'][:12]}: {e}")

    print(f"\n[Dedup] 完成: 成功删除 {deleted} 条, 失败 {failed} 条")


def main():
    dry_run = "--execute" not in sys.argv
    dedup_memos(dry_run=dry_run)


if __name__ == "__main__":
    main()
