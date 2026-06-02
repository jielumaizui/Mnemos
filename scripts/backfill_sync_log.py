#!/usr/bin/env python3
"""
sync_log 回填脚本 — 为已有 Memos 记录补录追踪信息

策略：
1. 扫描所有 Memos 记录
2. 从标签中提取 agent/session/turn 信息
3. 为没有对应 sync_log 的记录创建最小化追踪条目
4. 记录状态为 'backfilled' 以区分实时同步记录
"""
import sys
import sqlite3
import hashlib
import json
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from integrations.styx import MemosClient

DB_PATH = Path.home() / ".mnemos" / "sync_log.db"


def extract_info_from_tags(tags: list) -> dict:
    """从 Memos 标签中提取结构化信息"""
    info = {
        "agent_name": "unknown",
        "session_id": "unknown",
        "turn_number": 0,
        "model": "",
        "content_type": "",
    }
    for tag in tags:
        if tag.startswith("source="):
            info["agent_name"] = tag.split("=", 1)[1]
        elif tag.startswith("agent="):
            info["agent_name"] = tag.split("=", 1)[1]
        elif tag.startswith("session="):
            info["session_id"] = tag.split("=", 1)[1]
        elif tag.startswith("turn="):
            try:
                info["turn_number"] = int(tag.split("=", 1)[1]) - 1
            except ValueError:
                pass
        elif tag.startswith("model="):
            info["model"] = tag.split("=", 1)[1]
        elif tag.startswith("content_type="):
            info["content_type"] = tag.split("=", 1)[1]
    return info


def compute_hash_from_content(content: str) -> str:
    """从内容计算 hash（最小化实现）"""
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:16]


def main():
    client = MemosClient()
    print("正在扫描 Memos 记录...")

    all_memos = client.list_all_memos(max_records=None)
    total = len(all_memos)
    print(f"Memos 总记录: {total}")

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    # 获取已有 sync_log 的 memos_uid 集合
    cursor.execute("SELECT memos_uids FROM sync_log WHERE memos_uids IS NOT NULL")
    existing_uids = set()
    for row in cursor.fetchall():
        try:
            uids = json.loads(row[0])
            if isinstance(uids, list):
                existing_uids.update(uids)
        except Exception:
            pass

    print(f"sync_log 已有记录对应的 UID 数: {len(existing_uids)}")

    inserted = 0
    skipped = 0
    errors = 0

    for memo in all_memos:
        try:
            uid = memo.get("uid") or memo.get("name", "").replace("memos/", "")
            if not uid:
                skipped += 1
                continue

            # 跳过已有 sync_log 的记录
            if uid in existing_uids:
                skipped += 1
                continue

            tags = memo.get("tags", [])
            info = extract_info_from_tags(tags)
            content = memo.get("content", "")
            content_hash = compute_hash_from_content(content)

            # 区分 session-record 和 doc/other
            status = "backfilled"
            if info["content_type"] == "session-record":
                distill_status = "pending"
            else:
                distill_status = "n/a"

            cursor.execute(
                """
                INSERT OR IGNORE INTO sync_log
                (agent_name, session_id, turn_number, content_hash, memos_uids,
                 status, synced_at, distill_status, error, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    info["agent_name"],
                    info["session_id"],
                    info["turn_number"],
                    content_hash,
                    json.dumps([uid]),
                    status,
                    datetime.now().isoformat(),
                    distill_status,
                    None,
                    json.dumps(tags),
                ),
            )
            if cursor.rowcount > 0:
                inserted += 1
            else:
                skipped += 1

        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  处理 memo 时出错: {e}")

    conn.commit()
    conn.close()

    print(f"\n回填完成:")
    print(f"  扫描总数: {total}")
    print(f"  新增 sync_log: {inserted}")
    print(f"  跳过（已有）: {skipped}")
    print(f"  错误: {errors}")


if __name__ == "__main__":
    main()
