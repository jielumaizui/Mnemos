"""
清理信号数据库中的重复记录。

策略:
- session_signals: 按 session_id 去重，保留最新
- git_signals: 按 commit_hash 去重，保留最新
- memos_signals: 按 memo_uid 去重(memo_uid 为空时按 timestamp+content_length)，保留最新
- wechat_signals: 按 content_hash 去重，保留最新
- file_system_signals: 按 file_path 去重(30天内)，保留最新

执行前自动备份数据库。
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from datetime import datetime

from core.config import get_config

DB_PATH = get_config().claude_data_dir / "user_signals.db"


def backup_db() -> Path:
    """备份数据库"""
    backup_path = DB_PATH.with_suffix(f".db.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(DB_PATH, backup_path)
    return backup_path


def get_counts(conn: sqlite3.Connection, table: str) -> dict:
    """获取表的记录数统计"""
    total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return {"total": total}


def deduplicate_sessions(conn: sqlite3.Connection) -> int:
    """去重 session_signals，保留每组最新的记录"""
    cursor = conn.execute("""
        DELETE FROM session_signals
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM session_signals
            GROUP BY session_id
        )
    """)
    return cursor.rowcount


def deduplicate_git(conn: sqlite3.Connection) -> int:
    """去重 git_signals，保留每组最新的记录"""
    cursor = conn.execute("""
        DELETE FROM git_signals
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM git_signals
            GROUP BY commit_hash
        )
    """)
    return cursor.rowcount


def deduplicate_memos(conn: sqlite3.Connection) -> int:
    """去重 memos_signals。
    有 memo_uid 的按 memo_uid 分组; 没有的按 timestamp+content_length 分组。
    同时清理 memo_uid 为空的重复记录(这些是因为之前的 bug 产生的)。
    """
    # 1. 先删除 memo_uid 为空且完全重复的记录(timestamp + content_length 相同)
    cursor1 = conn.execute("""
        DELETE FROM memos_signals
        WHERE memo_uid = ''
        AND id NOT IN (
            SELECT MAX(id)
            FROM memos_signals
            WHERE memo_uid = ''
            GROUP BY timestamp, content_length
        )
    """)
    removed_empty = cursor1.rowcount

    # 2. 有 memo_uid 的按 memo_uid 去重
    cursor2 = conn.execute("""
        DELETE FROM memos_signals
        WHERE memo_uid != ''
        AND id NOT IN (
            SELECT MAX(id)
            FROM memos_signals
            WHERE memo_uid != ''
            GROUP BY memo_uid
        )
    """)
    removed_uid = cursor2.rowcount

    return removed_empty + removed_uid


def deduplicate_wechat(conn: sqlite3.Connection) -> int:
    """去重 wechat_signals，保留每组最新的记录"""
    cursor = conn.execute("""
        DELETE FROM wechat_signals
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM wechat_signals
            GROUP BY content_hash
        )
    """)
    return cursor.rowcount


def deduplicate_file_system(conn: sqlite3.Connection) -> int:
    """去重 file_system_signals(7天窗口内按 file_path 去重)"""
    cursor = conn.execute("""
        DELETE FROM file_system_signals
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM file_system_signals
            GROUP BY file_path, date(timestamp)
        )
    """)
    return cursor.rowcount


def main():
    if not DB_PATH.exists():
        print(f"数据库不存在: {DB_PATH}")
        return

    # 备份
    backup_path = backup_db()
    print(f"[备份] 已备份到: {backup_path}")

    with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
        # 统计清理前
        tables = ["session_signals", "git_signals", "memos_signals",
                  "wechat_signals", "file_system_signals"]
        before = {t: get_counts(conn, t)["total"] for t in tables}

        print(f"\n[清理前统计]")
        for t, c in before.items():
            print(f"  {t}: {c}")

        # 执行去重
        results = {}
        results["session"] = deduplicate_sessions(conn)
        results["git"] = deduplicate_git(conn)
        results["memos"] = deduplicate_memos(conn)
        results["wechat"] = deduplicate_wechat(conn)
        results["file_system"] = deduplicate_file_system(conn)

        conn.commit()

        # 统计清理后
        after = {t: get_counts(conn, t)["total"] for t in tables}

        print(f"\n[清理结果]")
        for source, removed in results.items():
            table = f"{source}_signals"
            print(f"  {table}: 删除 {removed} 条重复 (剩余 {after[table]})")

        total_removed = sum(results.values())
        print(f"\n[总结] 共删除 {total_removed} 条重复记录")


if __name__ == "__main__":
    main()
