"""Events command for Mnemos CLI."""

import logging

from core.cli.helpers import _get_config
from core.cli.helpers import _get_sqlite_conn
from core.db_utils import sqlite_artifact_exists

logger = logging.getLogger(__name__)


def cmd_events(args):
    """事件总线管理"""
    import sqlite3

    config = _get_config()
    events_db = config.database_dir / "events.db"

    if args.events_cmd == "stats":
        if not sqlite_artifact_exists(events_db):
            print("events.db 不存在")
            return
        try:
            with _get_sqlite_conn()(
                str(events_db), timeout=5
            ) as conn:  # [P1-FIX] 使用 sqlite_conn 确保连接关闭
                total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                pending = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE status IN ('pending', 'processing')"
                ).fetchone()[0]
                dl = conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0]
                rows = conn.execute(
                    "SELECT event_type, status, COUNT(*) FROM events "
                    "GROUP BY event_type, status ORDER BY COUNT(*) DESC LIMIT 10"
                ).fetchall()
            print("events.db 统计:")
            print(f"  总数: {total}")
            print(f"  pending/processing: {pending}")
            print(f"  dead_letters: {dl}")
            print("  Top 10 事件类型:")
            for event_type, status, count in rows:
                print(f"    - {event_type}/{status}: {count}")
        except (OSError, sqlite3.Error) as e:
            print(f"统计失败: {e}")

    elif args.events_cmd == "archive-orphans":
        try:
            from core.mnemos_bus import _get_bus

            bus = _get_bus()
            archived = bus.archive_no_consumer_events()
            print(f"归档完成: {archived} 个无消费者历史事件已归档")
        except (OSError, ValueError, AttributeError) as e:
            print(f"归档失败: {e}")

    elif args.events_cmd == "replay":
        try:
            from core.mnemos_bus import _get_bus

            bus = _get_bus()
            trace_id = getattr(args, "trace_id", "") or ""
            if trace_id:
                success = bus.replay_dead_letter(trace_id)
                if success:
                    print(f"重放完成: trace_id={trace_id} 已回到事件队列")
                else:
                    print(f"重放失败: 未找到死信 trace_id={trace_id}")
                return

            event_types = getattr(args, "event_types", []) or None
            limit = int(getattr(args, "limit", 100) or 100)
            replayed = bus.replay_no_consumer_dead_letters(
                event_types=event_types,
                limit=limit,
            )
            print(f"重放完成: {replayed} 个 no_consumer 死信已回到事件队列")
            if replayed == 0:
                print(
                    "提示: 只会重放当前已有消费者的 no_consumer 死信；"
                    "仍无消费者的事件请先注册处理器，或运行 `mnemos events archive-orphans` 归档。"
                )
        except (OSError, ValueError, AttributeError) as e:
            print(f"重放失败: {e}")

    elif args.events_cmd == "cleanup":
        if not sqlite_artifact_exists(events_db):
            print("events.db 不存在")
            return
        try:
            with _get_sqlite_conn()(
                str(events_db), timeout=10
            ) as conn:  # [P1-FIX] 使用 sqlite_conn 确保连接关闭
                # 1. 统计待删除项
                done_old = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE status = 'done' "
                    "AND created_at < datetime('now', '-7 days')"
                ).fetchone()[0]
                dl_old = conn.execute(
                    "SELECT COUNT(*) FROM dead_letters WHERE timestamp < datetime('now', '-30 days')"  # noqa: E501
                ).fetchone()[0]
                orphaned = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE status = 'pending' "
                    "AND created_at < datetime('now', '-3 days')"
                ).fetchone()[0]

            print("[dry-run] 以下事件将被清理（使用 --confirm 执行）：")
            print(f"  已完成超过 7 天的事件: {done_old}")
            print(f"  死信超过 30 天的事件: {dl_old}")
            print(f"  orphaned pending 超过 3 天的事件: {orphaned}")

            if not getattr(args, "confirm", False):
                print(
                    "  未指定 --confirm，跳过删除。建议先运行 `mnemos events archive-orphans` 归档。"
                )
                return

            with _get_sqlite_conn()(
                str(events_db), timeout=10
            ) as conn:  # [P1-FIX] 使用 sqlite_conn 确保连接关闭
                cursor = conn.execute(
                    "DELETE FROM events WHERE status = 'done' "
                    "AND created_at < datetime('now', '-7 days')"
                )
                done_removed = cursor.rowcount

                cursor = conn.execute(
                    "DELETE FROM dead_letters WHERE timestamp < datetime('now', '-30 days')"
                )
                dl_removed = cursor.rowcount

                # [P0-FIX] 批量 DELETE 替代逐行删除，减少锁竞争和连接开销
                cursor = conn.execute(
                    "DELETE FROM events WHERE status = 'pending' "
                    "AND created_at < datetime('now', '-3 days')"
                )
                orphaned_removed = cursor.rowcount

                # [P0-FIX] VACUUM 在同一连接执行，避免重复打开文件
                conn.execute("VACUUM")

            print("清理完成:")
            print(f"  删除已完成旧事件: {done_removed}")
            print(f"  删除死信旧事件: {dl_removed}")
            print(f"  删除 orphaned pending 事件: {orphaned_removed}")
            print("  已执行 VACUUM 释放磁盘空间")
        except (OSError, sqlite3.Error) as e:
            print(f"清理失败: {e}")

    else:
        print("用法: mnemos events {stats|cleanup|archive-orphans|replay}")
