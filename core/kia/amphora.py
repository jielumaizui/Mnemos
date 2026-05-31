"""
Distillation Queue - 子 Agent 蒸馏任务队列管理

Amphora — 双耳瓶 — 蒸馏队列，存放待提炼的原始材料。

职责：
- 接收待蒸馏的 session 数据
- 使用 SQLite 保存任务元数据，messages 单独落盘
- 管理 pending / processing / done / failed / archived 生命周期
- 支持优先级、指数退避重试、进度阶段追踪
- 用 BEGIN IMMEDIATE 保证 get_next() 原子消费
"""

import argparse
import hashlib
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Union

logger = logging.getLogger(__name__)

_DB_PATH: Optional[Path] = None
_DB_LOCK = threading.Lock()


class TaskPriority(Enum):
    NORMAL = 0
    HIGH = 1
    URGENT = 2


class DistillProgress(Enum):
    PENDING = "pending"
    EXTRACTING = "extracting"
    STRUCTURING = "structuring"
    VERIFYING = "verifying"
    WRITING = "writing"
    DONE = "done"


def _db_path() -> Path:
    """获取队列数据库路径。"""
    global _DB_PATH
    if _DB_PATH is None:
        from core.config import get_config
        _DB_PATH = get_config().claude_data_dir / "distill_queue.db"
    return _DB_PATH


def _messages_dir() -> Path:
    path = _db_path().parent / "distill_messages"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _task_id(session_id: str) -> str:
    return hashlib.md5(session_id.encode("utf-8")).hexdigest()[:12]


def _now() -> str:
    return datetime.now().isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _create_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS distillation_tasks (
            task_id TEXT PRIMARY KEY,
            session_id TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            priority INTEGER NOT NULL DEFAULT 0,
            retry_count INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 3,
            messages_path TEXT,
            meta TEXT,
            progress_step TEXT,
            progress_detail TEXT,
            progress REAL DEFAULT 0.0,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            output_path TEXT,
            error TEXT,
            next_retry_at TEXT
        )
    """)


def _init_db():
    """初始化或迁移 SQLite 数据库（幂等）。"""
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        exists = conn.execute("""
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'distillation_tasks'
        """).fetchone()

        if not exists:
            _create_table(conn)
        else:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(distillation_tasks)")}
            if "task_id" not in columns:
                _migrate_legacy_table(conn)
            else:
                _add_missing_columns(conn, columns)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_status_priority
            ON distillation_tasks(status, priority, created_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_retry
            ON distillation_tasks(status, retry_count, next_retry_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_session_id
            ON distillation_tasks(session_id)
        """)


def _add_missing_columns(conn: sqlite3.Connection, columns: set):
    missing = {
        "messages_path": "TEXT",
        "meta": "TEXT",
        "progress_step": "TEXT",
        "progress_detail": "TEXT",
        "progress": "REAL DEFAULT 0.0",
        "next_retry_at": "TEXT",
    }
    for name, ddl in missing.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE distillation_tasks ADD COLUMN {name} {ddl}")


def _migrate_legacy_table(conn: sqlite3.Connection):
    legacy_name = f"distillation_tasks_legacy_{int(datetime.now().timestamp())}"
    conn.execute(f"ALTER TABLE distillation_tasks RENAME TO {legacy_name}")
    _create_table(conn)

    rows = conn.execute(f"SELECT * FROM {legacy_name}").fetchall()
    for row in rows:
        task_id = _task_id(row["session_id"])
        messages = json.loads(row["messages_json"] or "[]")
        messages_path = _write_messages(task_id, messages)
        conn.execute("""
            INSERT OR IGNORE INTO distillation_tasks
            (task_id, session_id, status, priority, retry_count, max_retries,
             messages_path, meta, progress, created_at, started_at,
             completed_at, output_path, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id,
            row["session_id"],
            row["status"],
            _normalize_priority(row["priority"], None),
            row["retry_count"],
            row["max_retries"],
            str(messages_path),
            row["meta_json"],
            row["progress"],
            row["created_at"],
            row["started_at"],
            row["completed_at"],
            row["output_path"],
            row["error"],
        ))


def _write_messages(task_id: str, messages: List[Dict]) -> Path:
    path = _messages_dir() / f"{task_id}.json"
    path.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
    return path


def _read_messages(path_value: Optional[str]) -> List[Dict]:
    if not path_value:
        return []
    path = Path(path_value)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _infer_priority(meta: Optional[Dict]) -> int:
    meta = meta or {}
    if meta.get("urgent") or meta.get("deadline"):
        return TaskPriority.URGENT.value
    if meta.get("important") or meta.get("user_requested"):
        return TaskPriority.HIGH.value
    return TaskPriority.NORMAL.value


def _normalize_priority(priority: Optional[int], meta: Optional[Dict]) -> int:
    if priority is None or priority == 5:
        return _infer_priority(meta)
    if priority in (0, 1, 2):
        return int(priority)
    # 兼容旧调用：旧版数字越小越优先，常见 10 表示低优先级。
    return TaskPriority.HIGH.value if priority <= 3 else TaskPriority.NORMAL.value


def _row_to_dict(row: sqlite3.Row) -> Dict:
    """将数据库行转换为兼容旧格式的字典。"""
    d = dict(row)
    d["messages"] = _read_messages(d.get("messages_path"))
    d["meta"] = json.loads(d.get("meta") or "{}")
    return d


def _identifier_filter(conn: sqlite3.Connection, identifier: str) -> tuple:
    if conn.execute("SELECT 1 FROM distillation_tasks WHERE task_id = ?", (identifier,)).fetchone():
        return "task_id", identifier
    return "session_id", identifier


def _retry_time(retry_count: int) -> str:
    return (datetime.now() + timedelta(minutes=2 ** retry_count)).isoformat()


def enqueue(
    session_id: str,
    messages: List[Dict],
    meta: Dict = None,
    priority: Optional[int] = None,
    max_retries: int = 3,
) -> str:
    """
    将 session 数据加入蒸馏队列。

    返回 session_id 以兼容旧调用；内部 task_id 可从 get_next/list_pending 的结果读取。
    """
    _init_db()
    task_id = _task_id(session_id)
    meta = meta or {}
    priority_value = _normalize_priority(priority, meta)

    with _DB_LOCK:
        with _connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM distillation_tasks WHERE task_id = ? OR session_id = ?",
                (task_id, session_id),
            ).fetchone()
            if existing:
                return session_id

            messages_path = _write_messages(task_id, messages)
            conn.execute("""
                INSERT OR IGNORE INTO distillation_tasks
                (task_id, session_id, status, priority, retry_count, max_retries,
                 messages_path, meta, progress_step, created_at)
                VALUES (?, ?, 'pending', ?, 0, ?, ?, ?, ?, ?)
            """, (
                task_id,
                session_id,
                priority_value,
                max_retries,
                str(messages_path),
                json.dumps(meta, ensure_ascii=False),
                DistillProgress.PENDING.value,
                _now(),
            ))
    return session_id


def list_pending(include_future_retry: bool = True) -> List[Dict]:
    """列出 pending 状态任务；默认包括尚未到重试时间的任务，方便监控。"""
    _init_db()
    retry_clause = "" if include_future_retry else "AND (next_retry_at IS NULL OR next_retry_at <= ?)"
    params = () if include_future_retry else (_now(),)
    with _connect() as conn:
        rows = conn.execute(f"""
            SELECT * FROM distillation_tasks
            WHERE status = 'pending'
            {retry_clause}
            ORDER BY priority DESC, created_at ASC
        """, params).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_next() -> Optional[Dict]:
    """原子获取下一个可处理任务并标记为 processing。"""
    _init_db()
    with _DB_LOCK:
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""
                SELECT * FROM distillation_tasks
                WHERE status = 'pending'
                  AND retry_count < max_retries
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
            """, (_now(),)).fetchone()
            if not row:
                conn.commit()
                return None

            started_at = _now()
            conn.execute("""
                UPDATE distillation_tasks
                SET status = 'processing',
                    started_at = ?,
                    progress_step = ?,
                    progress_detail = ''
                WHERE task_id = ?
            """, (started_at, DistillProgress.EXTRACTING.value, row["task_id"]))
            conn.commit()

            result = _row_to_dict(row)
            result["status"] = "processing"
            result["started_at"] = started_at
            result["progress_step"] = DistillProgress.EXTRACTING.value
            return result


def mark_done(identifier: str, output_path: str = None) -> bool:
    """标记任务完成；identifier 可为 task_id 或 session_id。"""
    _init_db()
    with _connect() as conn:
        column, value = _identifier_filter(conn, identifier)
        cur = conn.execute(f"""
            UPDATE distillation_tasks
            SET status = 'done',
                completed_at = ?,
                output_path = ?,
                progress_step = ?,
                progress_detail = '',
                progress = 1.0
            WHERE {column} = ?
            """, (_now(), output_path, DistillProgress.DONE.value, value))
        return cur.rowcount > 0


def reset_timeouts(timeout_minutes: int = 30) -> int:
    """
    将超时卡住的 processing 任务重置为 pending。

    这是消费端健康检查的轻量降级：Worker 崩溃或无响应时，任务不会永久停在
    processing。重置后仍遵守 retry_count/max_retries 和优先级排序。
    """
    _init_db()
    cutoff = (datetime.now() - timedelta(minutes=timeout_minutes)).isoformat()
    with _connect() as conn:
        cur = conn.execute("""
            UPDATE distillation_tasks
            SET status = 'pending',
                error = ?,
                progress_detail = ?,
                next_retry_at = NULL
            WHERE status = 'processing'
              AND started_at IS NOT NULL
              AND started_at < ?
        """, (
            f"processing timeout after {timeout_minutes} minutes",
            "reset by timeout watchdog",
            cutoff,
        ))
        return cur.rowcount


def mark_failed(identifier: str, error: str) -> bool:
    """
    标记任务失败。
    未耗尽重试次数时回到 pending，并写入 next_retry_at；耗尽后标记 failed。
    """
    _init_db()
    with _DB_LOCK:
        with _connect() as conn:
            column, value = _identifier_filter(conn, identifier)
            row = conn.execute(
                f"SELECT task_id, retry_count, max_retries FROM distillation_tasks WHERE {column} = ?",
                (value,),
            ).fetchone()
            if not row:
                return False

            retry_count = row["retry_count"] + 1
            if retry_count < row["max_retries"]:
                status = "pending"
                next_retry_at = _retry_time(retry_count)
                completed_at = None
                error_msg = f"{error} (retry {retry_count}/{row['max_retries']})"
            else:
                status = "failed"
                next_retry_at = None
                completed_at = _now()
                error_msg = f"{error} (final fail after {retry_count} retries)"

            conn.execute("""
                UPDATE distillation_tasks
                SET status = ?,
                    error = ?,
                    completed_at = ?,
                    retry_count = ?,
                    next_retry_at = ?,
                    progress_detail = ?
                WHERE task_id = ?
            """, (status, error_msg, completed_at, retry_count, next_retry_at, error, row["task_id"]))
            return True


def update_progress(
    identifier: str,
    step_or_progress: Union[str, float],
    detail: str = "",
) -> bool:
    """
    更新任务进度。
    兼容旧接口：第二参数为 float 时写入 progress；新接口使用 step/detail。
    """
    _init_db()
    with _connect() as conn:
        column, value = _identifier_filter(conn, identifier)
        if isinstance(step_or_progress, (float, int)):
            progress = max(0.0, min(1.0, float(step_or_progress)))
            cur = conn.execute(
                f"UPDATE distillation_tasks SET progress = ? WHERE {column} = ?",
                (progress, value),
            )
        else:
            cur = conn.execute(f"""
                UPDATE distillation_tasks
                SET progress_step = ?, progress_detail = ?
                WHERE {column} = ?
            """, (str(step_or_progress), detail, value))
        return cur.rowcount > 0


def list_processing() -> List[Dict]:
    """列出 processing 状态的任务（供 HephaestusWorker 收集结果）"""
    _init_db()
    with _connect() as conn:
        rows = conn.execute("""
            SELECT * FROM distillation_tasks
            WHERE status = 'processing'
            ORDER BY started_at ASC
        """).fetchall()
        return [_row_to_dict(r) for r in rows]


def cleanup_old(days: int = 7) -> int:
    """
    归档 N 天前的完成/失败任务。
    保留 DB 审计记录，只清理 messages_path 指向的大消息文件。
    """
    _init_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    archived = 0
    with _connect() as conn:
        rows = conn.execute("""
            SELECT task_id, messages_path FROM distillation_tasks
            WHERE status IN ('done', 'failed', 'archived')
              AND completed_at < ?
        """, (cutoff,)).fetchall()
        for row in rows:
            if row["messages_path"]:
                Path(row["messages_path"]).unlink(missing_ok=True)
            conn.execute("""
                UPDATE distillation_tasks
                SET status = 'archived',
                    messages_path = NULL,
                    meta = NULL
                WHERE task_id = ?
            """, (row["task_id"],))
            archived += 1
    return archived


def get_task_count(status: str = None) -> int:
    """获取任务数量（用于监控）。"""
    _init_db()
    with _connect() as conn:
        if status:
            row = conn.execute(
                "SELECT COUNT(*) FROM distillation_tasks WHERE status = ?",
                (status,),
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
            logger.info(f"待蒸馏任务: {len(pending)}")
            for task in pending:
                meta = task.get("meta", {})
                print(f"  - {task['session_id'][:16]}... | "
                      f"消息: {len(task.get('messages', []))} | "
                      f"来源: {meta.get('source', 'unknown')} | "
                      f"优先级: {task.get('priority', 0)} | "
                      f"创建: {task['created_at'][:19]}")
        else:
            logger.info("无待蒸馏任务")
        return

    if args.next:
        task = get_next()
        logger.info(json.dumps(task or {}, ensure_ascii=False, indent=2))
        return

    if args.done:
        success = mark_done(args.done, args.output)
        logger.info(f"{'已标记完成' if success else '任务不存在'}: {args.done}")
        return

    if args.fail:
        success = mark_failed(args.fail, args.error or "unknown")
        logger.warning(f"{'已标记失败' if success else '任务不存在'}: {args.fail}")
        return

    if args.cleanup:
        archived = cleanup_old()
        logger.info(f"清理完成: 归档 {archived} 个旧任务")
        return

    if args.stats:
        total = get_task_count()
        pending = get_task_count("pending")
        processing = get_task_count("processing")
        done = get_task_count("done")
        failed = get_task_count("failed")
        archived = get_task_count("archived")
        print(
            "队列统计: "
            f"总计={total}, 待处理={pending}, 处理中={processing}, "
            f"完成={done}, 失败={failed}, 归档={archived}"
        )
        return

    parser.print_help()


if __name__ == "__main__":
    main()
