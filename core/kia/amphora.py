"""
Distillation Queue - 子 Agent 蒸馏任务队列管理

Amphora — 双耳瓶 — 蒸馏队列，存放待提炼的原始材料
原模块: distillation_queue.py

职责：
- 接收待蒸馏的 session 数据（SQLite 存储）
- 管理队列状态（pending / processing / done / failed / archived）
- 支持优先级、重试、进度追踪
- 原子操作：BEGIN IMMEDIATE + 行级锁
- 提供 CLI 接口供 Agent 查询和处理
"""

import json
import sqlite3
import threading
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

_DB_PATH: Optional[Path] = None
_DB_LOCK = threading.Lock()


def _db_path() -> Path:
    """获取队列数据库路径"""
    global _DB_PATH
    if _DB_PATH is None:
        from core.config import get_config
        _DB_PATH = get_config().claude_data_dir / "distill_queue.db"
    return _DB_PATH


def _init_db():
    """初始化 SQLite 数据库（幂等）"""
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS distillation_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                messages_json TEXT NOT NULL,
                meta_json TEXT,
                priority INTEGER NOT NULL DEFAULT 5,
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 3,
                progress REAL DEFAULT 0.0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                output_path TEXT,
                error TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_status_priority 
            ON distillation_tasks(status, priority, created_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_session_id 
            ON distillation_tasks(session_id)
        """)
        conn.commit()


def _row_to_dict(row: sqlite3.Row) -> Dict:
    """将数据库行转换为兼容旧格式的字典"""
    d = dict(row)
    d["messages"] = json.loads(d.pop("messages_json", "[]"))
    d["meta"] = json.loads(d.pop("meta_json", "{}"))
    return d


def enqueue(session_id: str, messages: List[Dict], meta: Dict = None,
            priority: int = 5, max_retries: int = 3) -> str:
    """
    将 session 数据加入蒸馏队列（SQLite，原子写入）

    Args:
        session_id: session 唯一标识
        messages: 消息列表
        meta: 元数据 {"source": "claude", ...}
        priority: 优先级（数字越小越优先，默认 5）
        max_retries: 最大重试次数（默认 3）

    Returns:
        session_id（向后兼容：旧版返回 Path，新版返回 str）
    """
    _init_db()
    with _DB_LOCK:
        with sqlite3.connect(str(_db_path())) as conn:
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO distillation_tasks 
                    (session_id, status, messages_json, meta_json, 
                     priority, max_retries, created_at)
                    VALUES (?, 'pending', ?, ?, ?, ?, ?)
                """, (
                    session_id,
                    json.dumps(messages, ensure_ascii=False),
                    json.dumps(meta or {}, ensure_ascii=False),
                    priority,
                    max_retries,
                    datetime.now().isoformat()
                ))
                conn.commit()
            except Exception as e:
                logger.warning(f"入队失败: {e}")
    return session_id


def list_pending() -> List[Dict]:
    """列出所有 pending 状态的任务（按优先级+创建时间排序）"""
    _init_db()
    with sqlite3.connect(str(_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM distillation_tasks 
            WHERE status = 'pending' 
            ORDER BY priority ASC, created_at ASC
        """).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_next() -> Optional[Dict]:
    """
    原子获取下一个 pending 任务并标记为 processing。
    使用 BEGIN IMMEDIATE + UPDATE 保证多进程安全。
    """
    _init_db()
    with _DB_LOCK:
        with sqlite3.connect(str(_db_path())) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""
                SELECT * FROM distillation_tasks 
                WHERE status = 'pending' AND retry_count < max_retries
                ORDER BY priority ASC, created_at ASC 
                LIMIT 1
            """).fetchone()
            if not row:
                conn.commit()
                return None
            session_id = row["session_id"]
            conn.execute("""
                UPDATE distillation_tasks 
                SET status = 'processing', started_at = ?
                WHERE session_id = ?
            """, (datetime.now().isoformat(), session_id))
            conn.commit()
            result = _row_to_dict(row)
            result["status"] = "processing"
            result["started_at"] = datetime.now().isoformat()
            return result


def mark_done(session_id: str, output_path: str = None) -> bool:
    """标记任务完成"""
    _init_db()
    with sqlite3.connect(str(_db_path())) as conn:
        cur = conn.execute("""
            UPDATE distillation_tasks 
            SET status = 'done', completed_at = ?, output_path = ?
            WHERE session_id = ?
        """, (datetime.now().isoformat(), output_path, session_id))
        conn.commit()
        return cur.rowcount > 0


def mark_failed(session_id: str, error: str) -> bool:
    """
    标记任务失败。自动重试：retry_count < max_retries 时回到 pending，
    否则标记为 archived（永久失败）。
    """
    _init_db()
    with _DB_LOCK:
        with sqlite3.connect(str(_db_path())) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT retry_count, max_retries FROM distillation_tasks 
                WHERE session_id = ?
            """, (session_id,)).fetchone()
            if not row:
                return False
            retry_count = row["retry_count"] + 1
            if retry_count >= row["max_retries"]:
                status = "archived"
                error_msg = f"{error} (final fail after {retry_count} retries)"
            else:
                status = "pending"
                error_msg = f"{error} (retry {retry_count}/{row['max_retries']})"
            conn.execute("""
                UPDATE distillation_tasks 
                SET status = ?, error = ?, completed_at = ?, retry_count = ?
                WHERE session_id = ?
            """, (status, error_msg, datetime.now().isoformat(), retry_count, session_id))
            conn.commit()
            return True


def update_progress(session_id: str, progress: float):
    """更新任务进度（0.0 ~ 1.0）"""
    _init_db()
    with sqlite3.connect(str(_db_path())) as conn:
        conn.execute("""
            UPDATE distillation_tasks SET progress = ? WHERE session_id = ?
        """, (max(0.0, min(1.0, progress)), session_id))
        conn.commit()


def cleanup_old(days: int = 7) -> int:
    """清理 N 天前的已完成/已归档任务"""
    _init_db()
    cutoff_ts = datetime.now().timestamp() - days * 86400
    cutoff = datetime.fromtimestamp(cutoff_ts).isoformat()
    with sqlite3.connect(str(_db_path())) as conn:
        cur = conn.execute("""
            DELETE FROM distillation_tasks 
            WHERE status IN ('done', 'archived') 
            AND completed_at < ?
        """, (cutoff,))
        conn.commit()
        return cur.rowcount


def get_task_count(status: str = None) -> int:
    """获取任务数量（用于监控）"""
    _init_db()
    with sqlite3.connect(str(_db_path())) as conn:
        if status:
            row = conn.execute(
                "SELECT COUNT(*) FROM distillation_tasks WHERE status = ?",
                (status,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM distillation_tasks").fetchone()
        return row[0] if row else 0


def main():
    parser = argparse.ArgumentParser(description="Distillation Queue Manager")
    parser.add_argument("--list", action="store_true", help="列出待处理任务")
    parser.add_argument("--next", action="store_true", help="获取下一个任务")
    parser.add_argument("--done", metavar="SESSION_ID", help="标记任务完成")
    parser.add_argument("--fail", metavar="SESSION_ID", help="标记任务失败")
    parser.add_argument("--output", default=None, help="完成时的输出文件路径")
    parser.add_argument("--error", default=None, help="失败时的错误信息")
    parser.add_argument("--cleanup", action="store_true", help="清理旧任务")
    parser.add_argument("--stats", action="store_true", help="队列统计")
    args = parser.parse_args()

    if args.list:
        pending = list_pending()
        if pending:
            print(f"待蒸馏任务: {len(pending)}")
            for task in pending:
                meta = task.get("meta", {})
                msg_count = len(task.get("messages", []))
                pri = task.get("priority", 5)
                print(f"  - {task['session_id'][:16]}... | "
                      f"消息: {msg_count} | "
                      f"来源: {meta.get('source', 'unknown')} | "
                      f"优先级: {pri} | "
                      f"创建: {task['created_at'][:19]}")
        else:
            print("无待蒸馏任务")
        return

    if args.next:
        task = get_next()
        if task:
            print(json.dumps(task, ensure_ascii=False, indent=2))
        else:
            print("{}")
        return

    if args.done:
        success = mark_done(args.done, args.output)
        print(f"{'已标记完成' if success else '任务不存在'}: {args.done}")
        return

    if args.fail:
        success = mark_failed(args.fail, args.error or "unknown")
        print(f"{'已标记失败' if success else '任务不存在'}: {args.fail}")
        return

    if args.cleanup:
        removed = cleanup_old()
        print(f"清理完成: 移除 {removed} 个旧任务")
        return

    if args.stats:
        total = get_task_count()
        pending = get_task_count("pending")
        processing = get_task_count("processing")
        done = get_task_count("done")
        failed = get_task_count("archived")
        print(f"队列统计: 总计={total}, 待处理={pending}, 处理中={processing}, 完成={done}, 归档={failed}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
