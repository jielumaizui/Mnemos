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

import hashlib
import json
import logging
import sqlite3
import stat
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Literal, Optional, overload

from core.kia.amphora_provenance_support import (
    AmphoraProvenanceContext,
    _backup_historical_provenance_object as _backup_historical_provenance_object_support,
    _historical_provenance_inventory_in_connection as _historical_provenance_inventory_support,
    build_historical_provenance_inventory as _build_historical_provenance_inventory,
)
from core.kia.amphora_types import (
    DistillProgress,
    PROVENANCE_MIGRATION_SCHEMA,
    SYSTEM_OWNED_META_KEYS,
)
from core.kia.amphora_queue_support import (
    TaskPriority as _TaskPriority,
    normalize_priority as _normalize_priority,
    physical_message_path_kind as _physical_message_path_kind,
    task_id as _task_id,
)
from core.kia.amphora_queries import AmphoraQueries
from core import pipeline_receipts as _pipeline_receipts
from core.ops.durable_io import (
    DurableIOError,
    SecureImmutablePublishReceipt,
    fsync_directory,
    secure_publish_immutable_text,
    secure_read_bytes,
    secure_remove_regular_file,
)
from core.pipeline_receipts import (
    DistillationEnqueueReceipt,
)

# Constants extracted from magic numbers
CONN_SECONDS = 30
CLEANUP_OLD_DAYS = 7
LEGACY_PROVENANCE_REASON = "legacy_done_without_typed_terminal_receipt"

logger = logging.getLogger(__name__)

_DB_PATH: Optional[Path] = None
_DB_LOCK = threading.Lock()
DistillationWriteReceipt = _pipeline_receipts.DistillationWriteReceipt
TaskPriority = _TaskPriority


def _db_path() -> Path:
    """获取队列数据库路径（基于 database_dir，不再绑定 Claude 目录）。"""
    global _DB_PATH
    if _DB_PATH is None:
        from core.config import get_config

        _DB_PATH = get_config().database_dir / "distill_queue.db"
    return _DB_PATH


def _messages_dir() -> Path:
    path = (_db_path().parent / "distill_messages").absolute()
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return path
    except OSError:
        raise RuntimeError(
            "amphora_messages_directory_unavailable"
        ) from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("amphora_messages_directory_unsafe")
    return path


def _now() -> str:
    return datetime.now().isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=CONN_SECONDS)
    conn.row_factory = sqlite3.Row  # noqa
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _create_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS distillation_tasks (
            task_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            source_agent TEXT NOT NULL DEFAULT 'unknown',
            input_revision TEXT NOT NULL DEFAULT '',
            generation INTEGER NOT NULL DEFAULT 1,
            receipt_id TEXT NOT NULL DEFAULT '',
            handoff_receipt_id TEXT NOT NULL DEFAULT '',
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
            terminal_reason TEXT DEFAULT '',
            written_count INTEGER NOT NULL DEFAULT 0,
            written_paths TEXT NOT NULL DEFAULT '[]',
            proposal_ids TEXT NOT NULL DEFAULT '[]',
            required_consumer_receipts TEXT NOT NULL DEFAULT '[]',
            terminal_outbox_anchor_sha256 TEXT NOT NULL DEFAULT '',
            error TEXT,
            next_retry_at TEXT,
            updated_at TEXT
        )
    """)
    _create_terminal_outbox_anchor_trigger(conn)


_TERMINAL_OUTBOX_ANCHOR_TRIGGER_NAME = (
    "distillation_tasks_terminal_outbox_anchor_immutable"
)
_TERMINAL_OUTBOX_ANCHOR_TRIGGER_SQL = f"""
CREATE TRIGGER {_TERMINAL_OUTBOX_ANCHOR_TRIGGER_NAME}
BEFORE UPDATE OF terminal_outbox_anchor_sha256 ON distillation_tasks
FOR EACH ROW
WHEN (
    OLD.terminal_outbox_anchor_sha256 <> ''
    AND NEW.terminal_outbox_anchor_sha256
        <> OLD.terminal_outbox_anchor_sha256
) OR (
    OLD.terminal_outbox_anchor_sha256 = ''
    AND NEW.terminal_outbox_anchor_sha256 <> ''
    AND NEW.status NOT IN ('committed', 'intentional_skip', 'failed')
)
BEGIN
    SELECT RAISE(ABORT, 'terminal outbox anchor is immutable');
END
"""


def _normalized_schema_sql(value: str) -> str:
    return " ".join(str(value or "").rstrip(";").split())


def _create_terminal_outbox_anchor_trigger(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(_TERMINAL_OUTBOX_ANCHOR_TRIGGER_SQL)


def _validate_terminal_outbox_anchor_trigger(
    conn: sqlite3.Connection,
) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (_TERMINAL_OUTBOX_ANCHOR_TRIGGER_NAME,),
    ).fetchone()
    if (
        row is None
        or _normalized_schema_sql(str(row[0] or ""))
        != _normalized_schema_sql(_TERMINAL_OUTBOX_ANCHOR_TRIGGER_SQL)
    ):
        raise RuntimeError(
            "canonical_terminal_outbox_anchor_upgrade_required; "
            "run scripts/reconcile_distill_runtime_receipts.py"
        )


def _reconcile_terminal_outbox_anchor_schema(
    conn: sqlite3.Connection,
) -> dict[str, bool]:
    """Install the canonical terminal-anchor column and trigger.

    This module is the sole DDL owner. Migration commands may call this
    function only inside their reviewed backup and transaction boundary.
    """
    columns = {
        str(info[1])
        for info in conn.execute("PRAGMA table_info(distillation_tasks)")
    }
    column_added = "terminal_outbox_anchor_sha256" not in columns
    if column_added:
        conn.execute(
            "ALTER TABLE distillation_tasks ADD COLUMN "
            "terminal_outbox_anchor_sha256 TEXT NOT NULL DEFAULT ''"
        )
    trigger = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
        (_TERMINAL_OUTBOX_ANCHOR_TRIGGER_NAME,),
    ).fetchone()
    trigger_created = trigger is None
    if trigger_created:
        _create_terminal_outbox_anchor_trigger(conn)
    _validate_terminal_outbox_anchor_trigger(conn)
    return {
        "column_added": column_added,
        "trigger_created": trigger_created,
        "canonical": True,
    }


def _create_provenance_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS amphora_provenance_migrations (
            migration_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            legacy_task_id TEXT NOT NULL UNIQUE,
            legacy_input_revision TEXT NOT NULL,
            legacy_object_hash TEXT NOT NULL,
            inventory_hash TEXT NOT NULL,
            backup_manifest_hash TEXT NOT NULL,
            backup_manifest_path TEXT NOT NULL,
            canonical_task_id TEXT NOT NULL UNIQUE,
            canonical_input_revision TEXT NOT NULL,
            handoff_receipt_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(legacy_task_id) REFERENCES distillation_tasks(task_id),
            FOREIGN KEY(canonical_task_id) REFERENCES distillation_tasks(task_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS amphora_provenance_migrations_no_update
        BEFORE UPDATE ON amphora_provenance_migrations BEGIN
            SELECT RAISE(ABORT, 'amphora provenance migrations are immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS amphora_provenance_migrations_no_delete
        BEFORE DELETE ON amphora_provenance_migrations BEGIN
            SELECT RAISE(ABORT, 'amphora provenance migrations are immutable');
        END
        """
    )


def _create_source_span_migration_table(conn: sqlite3.Connection) -> None:
    """Own append-only historical-task to exact-Raw generation receipts."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS amphora_source_span_migrations (
            migration_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            legacy_task_id TEXT NOT NULL UNIQUE,
            legacy_input_revision TEXT NOT NULL,
            legacy_object_hash TEXT NOT NULL,
            raw_preimage_hash TEXT NOT NULL,
            inventory_hash TEXT NOT NULL,
            canonical_task_id TEXT NOT NULL,
            canonical_input_revision TEXT NOT NULL,
            backup_manifest_path TEXT NOT NULL,
            backup_manifest_hash TEXT NOT NULL,
            backup_manifest_file_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(legacy_task_id) REFERENCES distillation_tasks(task_id),
            FOREIGN KEY(canonical_task_id) REFERENCES distillation_tasks(task_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS amphora_source_span_migrations_no_update
        BEFORE UPDATE ON amphora_source_span_migrations BEGIN
            SELECT RAISE(ABORT, 'amphora source span migrations are immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS amphora_source_span_migrations_no_delete
        BEFORE DELETE ON amphora_source_span_migrations BEGIN
            SELECT RAISE(ABORT, 'amphora source span migrations are immutable');
        END
        """
    )


def _init_db():
    """初始化或迁移 SQLite 数据库（幂等）。"""
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        # SQLite does not implicitly begin a transaction for DDL.  Keep every
        # table, trigger, index, and data-copy step inside one explicit
        # boundary so a payload/schema failure restores the exact preimage.
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute("""
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'distillation_tasks'
        """).fetchone()

        if not exists:
            _create_table(conn)
        else:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(distillation_tasks)")}
            table_sql = str(
                conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='distillation_tasks'"
                ).fetchone()[0]
                or ""
            )
            if "task_id" not in columns:
                raise RuntimeError(
                    "distillation_tasks schema predates typed task identity; "
                    "automatic reconstruction is forbidden"
                )
            elif "SESSION_ID TEXT UNIQUE" in table_sql.upper().replace("\n", " "):
                _migrate_revision_table(conn)
            else:
                _add_missing_columns(conn, columns)
                _validate_terminal_outbox_anchor_trigger(conn)

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
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_distill_input_revision
            ON distillation_tasks(source_agent, session_id, input_revision)
        """)
        _create_provenance_migration_table(conn)
        _create_source_span_migration_table(conn)


def _add_missing_columns(conn: sqlite3.Connection, columns: set):
    if "terminal_outbox_anchor_sha256" not in columns:
        raise RuntimeError(
            "canonical_terminal_outbox_anchor_upgrade_required; "
            "run scripts/reconcile_distill_runtime_receipts.py"
        )
    missing = {
        "messages_path": "TEXT",
        "meta": "TEXT",
        "progress_step": "TEXT",
        "progress_detail": "TEXT",
        "progress": "REAL DEFAULT 0.0",
        "next_retry_at": "TEXT",
        "updated_at": "TEXT",
        "source_agent": "TEXT NOT NULL DEFAULT 'unknown'",
        "input_revision": "TEXT NOT NULL DEFAULT ''",
        "generation": "INTEGER NOT NULL DEFAULT 1",
        "receipt_id": "TEXT NOT NULL DEFAULT ''",
        "handoff_receipt_id": "TEXT NOT NULL DEFAULT ''",
        "terminal_reason": "TEXT DEFAULT ''",
        "written_count": "INTEGER NOT NULL DEFAULT 0",
        "written_paths": "TEXT NOT NULL DEFAULT '[]'",
        "proposal_ids": "TEXT NOT NULL DEFAULT '[]'",
        "required_consumer_receipts": "TEXT NOT NULL DEFAULT '[]'",
    }
    added: set[str] = set()
    for name, ddl in missing.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE distillation_tasks ADD COLUMN {name} {ddl}")
            columns.add(name)
            added.add(name)
    if "updated_at" in added:
        conn.execute(
            """
            UPDATE distillation_tasks
            SET updated_at = COALESCE(completed_at, started_at, created_at)
            WHERE updated_at IS NULL
              AND COALESCE(completed_at, started_at, created_at) IS NOT NULL
            """
        )
    if "receipt_id" in added:
        conn.execute(
            "UPDATE distillation_tasks SET receipt_id='amphora-' || task_id WHERE receipt_id=''"
        )
    if not ({"source_agent", "input_revision", "generation"} & added):
        return
    rows = conn.execute(
        """
        SELECT task_id, session_id, source_agent, input_revision, messages_path, meta
        FROM distillation_tasks ORDER BY created_at, task_id
        """
    ).fetchall()
    generations: dict[tuple[str, str], int] = {}
    for row in rows:
        meta = json.loads(row["meta"] or "{}")
        if "source_agent" in added:
            source_agent = str(meta.get("source") or row["source_agent"] or "unknown")
        else:
            source_agent = str(row["source_agent"] or meta.get("source") or "unknown")
        messages = _read_messages(
            row["messages_path"],
            required=bool(row["messages_path"]),
        )
        messages_revision = _messages_revision(messages)
        revision = str(
            row["input_revision"]
            or meta.get("input_revision")
            or messages_revision
        )
        meta["messages_revision"] = messages_revision
        session_id = str(row["session_id"])
        key = (source_agent, session_id)
        generations[key] = generations.get(key, 0) + 1
        conn.execute(
            """
            UPDATE distillation_tasks
            SET source_agent=?, input_revision=?, generation=?, meta=?
            WHERE task_id=?
            """,
            (
                source_agent,
                revision,
                generations[key],
                json.dumps(meta, ensure_ascii=False),
                row["task_id"],
            ),
        )


def _migrate_revision_table(conn: sqlite3.Connection) -> None:
    """Remove the session-only UNIQUE contract while preserving every historical task."""
    legacy_name = f"distillation_tasks_session_unique_{int(datetime.now().timestamp())}"
    _sanitize_table_name(legacy_name)
    conn.execute(
        f"DROP TRIGGER IF EXISTS {_TERMINAL_OUTBOX_ANCHOR_TRIGGER_NAME}"
    )
    conn.execute(f"ALTER TABLE distillation_tasks RENAME TO {legacy_name}")
    _create_table(conn)
    rows = conn.execute(f"SELECT * FROM {legacy_name}").fetchall()  # nosec B608
    for row in rows:
        data = dict(row)
        messages = _read_messages(
            data.get("messages_path"),
            required=bool(data.get("messages_path")),
        )
        meta = json.loads(data.get("meta") or "{}")
        source_agent = str(meta.get("source") or "unknown")
        messages_revision = _messages_revision(messages)
        revision = str(meta.get("input_revision") or messages_revision)
        meta["messages_revision"] = messages_revision
        legacy_done = data.get("status") == "done"
        durable_output = bool(
            data.get("output_path") and Path(str(data["output_path"])).exists()
        )
        migrated_status = data.get("status", "pending")
        if legacy_done:
            migrated_status = "committed" if durable_output else "reconciliation_required"
        terminal_reason = ""
        if legacy_done:
            terminal_reason = (
                "legacy_output_path_verified"
                if durable_output
                else "legacy_done_without_typed_terminal_receipt"
            )
        conn.execute(
            """
            INSERT INTO distillation_tasks (
                task_id, session_id, source_agent, input_revision, generation,
                receipt_id, handoff_receipt_id, status, priority, retry_count,
                max_retries, messages_path, meta, progress_step, progress_detail,
                progress, created_at, started_at, completed_at, output_path,
                terminal_reason, written_count, written_paths, proposal_ids,
                required_consumer_receipts, error, next_retry_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["task_id"],
                data["session_id"],
                source_agent,
                revision,
                f"amphora-{data['task_id']}",
                str(meta.get("handoff_receipt_id") or ""),
                migrated_status,
                data.get("priority", 0),
                data.get("retry_count", 0),
                data.get("max_retries", 3),
                data.get("messages_path"),
                json.dumps(meta, ensure_ascii=False),
                data.get("progress_step"),
                data.get("progress_detail"),
                data.get("progress", 0.0),
                data.get("created_at") or _now(),
                data.get("started_at"),
                data.get("completed_at") if migrated_status != "reconciliation_required" else None,
                data.get("output_path"),
                terminal_reason,
                1 if durable_output else 0,
                json.dumps([str(data["output_path"])]) if durable_output else "[]",
                "[]",
                "[]",
                data.get("error"),
                data.get("next_retry_at"),
                data.get("updated_at") or data.get("completed_at") or data.get("created_at"),
            ),
        )


def _sanitize_table_name(name: str) -> str:
    """校验表名只包含合法字符（字母、数字、下划线），防止 SQL 注入。"""
    if not name or not all(c.isalnum() or c == "_" for c in name):
        raise ValueError(f"非法表名: {name!r}")
    return name


def _normalize_messages(messages) -> List[Dict]:
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    elif isinstance(messages, dict):
        return [messages]
    elif not isinstance(messages, list):
        return []
    return messages


@overload
def _write_messages(
    task_id: str,
    messages: List[Dict],
    *,
    return_receipt: Literal[True],
) -> SecureImmutablePublishReceipt: ...


@overload
def _write_messages(
    task_id: str,
    messages: List[Dict],
    *,
    return_receipt: Literal[False] = False,
) -> Path: ...


def _write_messages(
    task_id: str,
    messages: List[Dict],
    *,
    return_receipt: bool = False,
):
    messages = _normalize_messages(messages)
    path = _messages_dir() / f"{task_id}.json"
    content = json.dumps(
        messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    # trusted-scan: artifact owner=kia target=distillation_task_payload expires=never
    return secure_publish_immutable_text(
        path.parent,
        path.name,
        content,
        return_receipt=return_receipt,
    )


def _messages_revision(messages: List[Dict]) -> str:
    normalized = json.dumps(
        messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _provenance_context() -> AmphoraProvenanceContext:
    return AmphoraProvenanceContext(
        db_path=_db_path,
        normalize_messages=_normalize_messages,
        messages_revision=_messages_revision,
        conn_seconds=CONN_SECONDS,
        legacy_provenance_reason=LEGACY_PROVENANCE_REASON,
        migration_schema=PROVENANCE_MIGRATION_SCHEMA,
    )


def build_historical_provenance_inventory() -> dict:
    """Inventory exact historical objects through explicit Amphora dependencies."""

    return _build_historical_provenance_inventory(context=_provenance_context())


class AmphoraTaskPayloadUnavailableError(RuntimeError):
    """A durable task payload cannot be read and must never become empty."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def _read_messages(
    path_value: Optional[str],
    *,
    required: bool = False,
) -> List[Dict]:
    if not path_value:
        if required:
            raise AmphoraTaskPayloadUnavailableError(
                "amphora_task_messages_path_missing"
            )
        return []
    path = Path(path_value).expanduser().absolute()
    root = _messages_dir().absolute()
    try:
        relative = path.relative_to(root)
        if len(relative.parts) != 1:
            raise DurableIOError("amphora_message_path_outside_owner")
        content = secure_read_bytes(root, relative)
        if content is None:
            raise FileNotFoundError(path)
        payload = json.loads(content.decode("utf-8"))
    except FileNotFoundError:
        if not required:
            return []
        raise AmphoraTaskPayloadUnavailableError(
            "amphora_task_messages_file_missing"
        ) from None
    except (OSError, UnicodeError, ValueError):
        if not required:
            return []
        raise AmphoraTaskPayloadUnavailableError(
            "amphora_task_messages_unreadable"
        ) from None
    except json.JSONDecodeError:
        if not required:
            return []
        raise AmphoraTaskPayloadUnavailableError(
            "amphora_task_messages_malformed"
        ) from None
    if not isinstance(payload, list) or any(
        not isinstance(message, dict) for message in payload
    ):
        if not required:
            return []
        raise AmphoraTaskPayloadUnavailableError(
            "amphora_task_messages_invalid"
        )
    return payload


def _row_to_dict(row: sqlite3.Row) -> Dict:
    """Convert a queue row into the stable public task representation."""
    d = dict(row)
    active_status = str(d.get("status") or "") in {
        "pending",
        "retryable_failed",
        "partial",
        "processing",
        "proposal_pending",
    }
    d["messages"] = _read_messages(
        d.get("messages_path"),
        required=active_status,
    )
    d["meta"] = json.loads(d.get("meta") or "{}")
    d["written_paths"] = json.loads(d.get("written_paths") or "[]")
    d["proposal_ids"] = json.loads(d.get("proposal_ids") or "[]")
    d["required_consumer_receipts"] = json.loads(
        d.get("required_consumer_receipts") or "[]"
    )
    return d


from core.kia import amphora_terminal_contract as _terminal_contract
_terminal_contract.bind_terminal_contract_runtime(
    db_path=_db_path,
    row_to_dict=_row_to_dict,
)
_validated_failed_terminal_outbox = (
    _terminal_contract._validated_failed_terminal_outbox
)
_failed_terminal_outbox_matches_row = (
    _terminal_contract._failed_terminal_outbox_matches_row
)
_terminal_receipt_payload = _terminal_contract._terminal_receipt_payload
_terminal_receipt_payload_sha256 = (
    _terminal_contract._terminal_receipt_payload_sha256
)
_terminal_receipt_matches_row = _terminal_contract._terminal_receipt_matches_row
_distillation_write_receipt_from_payload = (
    _terminal_contract._distillation_write_receipt_from_payload
)
_validated_terminal_receipt_outbox = (
    _terminal_contract._validated_terminal_receipt_outbox
)
_normalized_cognitive_event_ids = (
    _terminal_contract._normalized_cognitive_event_ids
)
_cognitive_event_ids_sha256 = _terminal_contract._cognitive_event_ids_sha256
_terminal_outbox_anchor_sha256 = (
    _terminal_contract._terminal_outbox_anchor_sha256
)
_terminal_outbox_anchor_matches_row = (
    _terminal_contract._terminal_outbox_anchor_matches_row
)
_task_with_frozen_terminal_denominator = (
    _terminal_contract._task_with_frozen_terminal_denominator
)
_require_canonical_task_database_config = (
    _terminal_contract._require_canonical_task_database_config
)
_validated_message_cleanup_outbox = (
    _terminal_contract._validated_message_cleanup_outbox
)
_identifier_filter = _terminal_contract._identifier_filter
_retry_time = _terminal_contract._retry_time


def enqueue_with_receipt(
    session_id: str,
    messages: List[Dict],
    meta: Dict | None = None,
    priority: Optional[int] = None,
    max_retries: int = 3,
) -> DistillationEnqueueReceipt:
    """Durably own one source/session/input revision and return its typed receipt."""
    _init_db()
    meta = dict(meta or {})
    present_reserved = sorted(SYSTEM_OWNED_META_KEYS.intersection(meta))
    if present_reserved:
        raise ValueError(f"{present_reserved[0]}_is_reserved")
    messages = _normalize_messages(messages)
    messages_revision = _messages_revision(messages)
    source_agent = str(meta.get("source") or "unknown")
    declared_input_revision = meta.get("input_revision")
    if declared_input_revision is not None and (
        not isinstance(declared_input_revision, str)
        or not declared_input_revision.strip()
    ):
        raise ValueError("input_revision_must_be_nonempty_text")
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or max_retries < 1
    ):
        raise ValueError("max_retries_must_be_positive_integer")
    input_revision = str(declared_input_revision or messages_revision)
    meta["messages_revision"] = messages_revision
    task_id = _task_id(session_id, source_agent, input_revision)
    receipt_id = f"amphora-{task_id}"
    priority_value = _normalize_priority(priority, meta)

    with _DB_LOCK:
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM distillation_tasks
                WHERE source_agent=? AND session_id=? AND input_revision=?
                """,
                (source_agent, session_id, input_revision),
            ).fetchone()
            if existing:
                existing_messages = _read_messages(
                    str(existing["messages_path"] or ""),
                    required=True,
                )
                try:
                    existing_meta = json.loads(existing["meta"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    raise RuntimeError("amphora_existing_task_meta_invalid") from None
                if not isinstance(existing_meta, dict):
                    raise RuntimeError("amphora_existing_task_meta_invalid")
                declared_messages_revision = str(
                    existing_meta.get("messages_revision") or ""
                )
                if (
                    _messages_revision(existing_messages) != messages_revision
                    or (
                        declared_messages_revision
                        and declared_messages_revision != messages_revision
                    )
                    or str(existing["task_id"])
                    != _task_id(session_id, source_agent, input_revision)
                ):
                    raise RuntimeError(
                        "amphora_existing_task_payload_identity_mismatch"
                    )
                conn.commit()
                return DistillationEnqueueReceipt(
                    receipt_id=str(existing["receipt_id"] or f"amphora-{existing['task_id']}"),
                    task_id=str(existing["task_id"]),
                    source_agent=source_agent,
                    session_id=session_id,
                    input_revision=input_revision,
                    status=str(existing["status"]),
                    created=False,
                )

            messages_path = _write_messages(task_id, messages)
            now = _now()
            generation_row = conn.execute(
                """
                SELECT COALESCE(MAX(generation), 0) + 1 FROM distillation_tasks
                WHERE source_agent=? AND session_id=?
                """,
                (source_agent, session_id),
            ).fetchone()
            generation = int(generation_row[0] or 1)
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO distillation_tasks
                (task_id, session_id, source_agent, input_revision, generation,
                 receipt_id, handoff_receipt_id, status, priority, retry_count,
                 max_retries, messages_path, meta, progress_step, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0, ?, ?, ?, ?, ?, ?)
            """,
                (
                    task_id,
                    session_id,
                    source_agent,
                    input_revision,
                    generation,
                    receipt_id,
                    str(meta.get("handoff_receipt_id") or ""),
                    priority_value,
                    max_retries,
                    str(messages_path),
                    json.dumps(meta, ensure_ascii=False),
                    DistillProgress.PENDING.value,
                    now,
                    now,
                ),
            )
            if inserted.rowcount != 1:
                raise RuntimeError("amphora_task_insert_lost_ownership")
            conn.commit()
    return DistillationEnqueueReceipt(
        receipt_id=receipt_id,
        task_id=task_id,
        source_agent=source_agent,
        session_id=session_id,
        input_revision=input_revision,
        status="pending",
        created=True,
    )


from core.kia import (
    amphora_provenance_reconciliation as _provenance_reconciliation,
)

_provenance_reconciliation.bind_provenance_reconciliation_runtime(
    _DB_LOCK=_DB_LOCK,
    _backup_historical_provenance_object_support=(
        _backup_historical_provenance_object_support
    ),
    _connect=_connect,
    _historical_provenance_inventory_support=(
        _historical_provenance_inventory_support
    ),
    _init_db=_init_db,
    _messages_dir=_messages_dir,
    _messages_revision=_messages_revision,
    _normalize_messages=_normalize_messages,
    _normalize_priority=_normalize_priority,
    _now=_now,
    _provenance_context=_provenance_context,
    _task_id=_task_id,
    _write_messages=_write_messages,
    build_historical_provenance_inventory=(
        build_historical_provenance_inventory
    ),
)

reconcile_historical_task_provenance = (
    _provenance_reconciliation.reconcile_historical_task_provenance
)


def list_pending(include_future_retry: bool = True) -> List[Dict]:
    """列出 pending 状态任务；默认包括尚未到重试时间的任务，方便监控。"""
    _init_db()
    retry_clause = (
        "" if include_future_retry else "AND (next_retry_at IS NULL OR next_retry_at <= ?)"
    )
    params = () if include_future_retry else (_now(),)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM distillation_tasks
            WHERE status IN ('pending', 'retryable_failed', 'partial')
            {retry_clause}
            ORDER BY priority DESC,
                     retry_count ASC,
                     COALESCE(next_retry_at, created_at) ASC,
                     created_at ASC
        """,  # nosec B608
            params,
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def list_tasks(status: str | None = None, limit: int = 50) -> List[Dict]:
    """List queue tasks for operations and audits."""
    _init_db()
    limit = max(1, int(limit or 50))
    where = ""
    params: tuple = ()
    if status:
        where = "WHERE status = ?"
        params = (status,)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM distillation_tasks
            {where}
            ORDER BY
                CASE status
                    WHEN 'failed' THEN 1
                    WHEN 'processing' THEN 2
                    WHEN 'pending' THEN 3
                    WHEN 'done' THEN 4
                    ELSE 5
                END,
                COALESCE(completed_at, started_at, created_at) DESC
            LIMIT ?
            """,  # nosec B608
            params + (limit,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_next() -> Optional[Dict]:
    """原子获取下一个可处理任务并标记为 processing。"""
    _init_db()
    with _DB_LOCK:
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM distillation_tasks
                WHERE status IN ('pending', 'retryable_failed', 'partial')
                  AND retry_count < max_retries
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY priority DESC,
                         retry_count ASC,
                         COALESCE(next_retry_at, created_at) ASC,
                         created_at ASC
                LIMIT 1
            """,
                (_now(),),
            ).fetchone()
            if not row:
                conn.commit()
                return None

            result = _row_to_dict(row)
            started_at = _now()
            conn.execute(
                """
                UPDATE distillation_tasks
                SET status = 'processing',
                    started_at = ?,
                    updated_at = ?,
                    progress_step = ?,
                    progress_detail = ''
                WHERE task_id = ?
            """,
                (started_at, started_at, DistillProgress.EXTRACTING.value, row["task_id"]),
            )
            conn.commit()

            result["status"] = "processing"
            result["started_at"] = started_at
            result["updated_at"] = started_at
            result["progress_step"] = DistillProgress.EXTRACTING.value
            return result


def claim_task(identifier: str) -> Optional[Dict]:
    """Atomically claim one exact task, or the newest eligible session generation."""

    _init_db()
    with _DB_LOCK:
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM distillation_tasks
                WHERE (
                        task_id = ?
                        OR (
                            session_id = ?
                            AND task_id = (
                                SELECT candidate.task_id
                                FROM distillation_tasks AS candidate
                                WHERE candidate.session_id = ?
                                  AND candidate.status IN (
                                      'pending', 'retryable_failed', 'partial'
                                  )
                                  AND candidate.retry_count < candidate.max_retries
                                  AND (
                                      candidate.next_retry_at IS NULL
                                      OR candidate.next_retry_at <= ?
                                  )
                                ORDER BY candidate.generation DESC,
                                         candidate.created_at DESC
                                LIMIT 1
                            )
                        )
                      )
                  AND status IN ('pending', 'retryable_failed', 'partial')
                  AND retry_count < max_retries
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY CASE WHEN task_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (identifier, identifier, identifier, _now(), _now(), identifier),
            ).fetchone()
            if not row:
                conn.commit()
                return None

            result = _row_to_dict(row)
            started_at = _now()
            cur = conn.execute(
                """
                UPDATE distillation_tasks
                SET status = 'processing',
                    started_at = ?,
                    updated_at = ?,
                    progress_step = ?,
                    progress_detail = ''
                WHERE task_id = ?
                  AND status IN ('pending', 'retryable_failed', 'partial')
                """,
                (
                    started_at,
                    started_at,
                    DistillProgress.EXTRACTING.value,
                    row["task_id"],
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                raise RuntimeError("amphora_task_claim_failed")
            conn.commit()

            result["status"] = "processing"
            result["started_at"] = started_at
            result["updated_at"] = started_at
            result["progress_step"] = DistillProgress.EXTRACTING.value
            return result


from core.kia import amphora_terminal_operations as _terminal_operations

_terminal_operations.bind_terminal_operations_runtime(
    db_lock=_DB_LOCK,
    connect=_connect,
    init_db=_init_db,
    now=_now,
    row_to_dict=_row_to_dict,
)

mark_terminal = _terminal_operations.mark_terminal
mark_done = _terminal_operations.mark_done
mark_intentional_skip = _terminal_operations.mark_intentional_skip
reset_timeouts = _terminal_operations.reset_timeouts
mark_failed_with_transition = _terminal_operations.mark_failed_with_transition
mark_failed = _terminal_operations.mark_failed
list_terminal_receipt_outbox = (
    _terminal_operations.list_terminal_receipt_outbox
)
mark_terminal_receipt_outbox_committed = (
    _terminal_operations.mark_terminal_receipt_outbox_committed
)
list_failed_terminal_receipt_outbox = (
    _terminal_operations.list_failed_terminal_receipt_outbox
)
mark_failed_terminal_receipt_outbox_committed = (
    _terminal_operations.mark_failed_terminal_receipt_outbox_committed
)
_failed_task_ids = _terminal_operations._failed_task_ids
retry_failed = _terminal_operations.retry_failed
archive_failed = _terminal_operations.archive_failed
update_progress = _terminal_operations.update_progress


def list_processing() -> List[Dict]:
    """列出 processing 状态的任务（供 HephaestusWorker 收集结果）"""
    return AmphoraQueries(_connect, _init_db, _row_to_dict).list_processing()


def cleanup_old(days: int = CLEANUP_OLD_DAYS) -> int:
    """
    归档 N 天前的已证明成功/跳过任务。

    ``failed`` 永远由 failed-terminal receipt 工作流持有，不能由维护任务
    绕过。每个消息文件先在任务 metadata 中持久化 cleanup intent，再删除
    文件并以 CAS 清除数据库引用。删除失败保留路径和 intent 供下轮重试；
    删除后进程崩溃则下轮通过 ``missing_ok`` 完成同一 intent。
    """
    _init_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    finalized = 0
    with _DB_LOCK:
        with _connect() as conn:
            task_ids = [
                str(row["task_id"])
                for row in conn.execute(
                    """
                    SELECT task_id FROM distillation_tasks
                    WHERE status IN (
                        'committed',
                        'intentional_skip',
                        'archived'
                    )
                      AND completed_at < ?
                      AND messages_path IS NOT NULL
                    ORDER BY completed_at, task_id
                    """,
                    (cutoff,),
                ).fetchall()
            ]
            messages_root = _messages_dir()

            for task_id in task_ids:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """
                    SELECT task_id, session_id, input_revision, status,
                           messages_path, meta, terminal_reason,
                           written_count, written_paths, proposal_ids,
                           required_consumer_receipts, retry_count, max_retries,
                           progress_detail, terminal_outbox_anchor_sha256
                    FROM distillation_tasks
                    WHERE task_id=?
                    """,
                    (task_id,),
                ).fetchone()
                if (
                    row is None
                    or str(row["status"])
                    not in {"committed", "intentional_skip", "archived"}
                    or not row["messages_path"]
                ):
                    conn.commit()
                    continue

                raw_path = str(row["messages_path"])
                path = Path(raw_path)
                quarantine_reason = ""
                try:
                    meta = json.loads(str(row["meta"] or "{}"))
                    if not isinstance(meta, dict):
                        raise TypeError("task meta must be an object")
                    terminal_outbox = _validated_terminal_receipt_outbox(
                        meta.get("terminal_receipt_outbox"),
                        task_id=task_id,
                    )
                    failed_outbox = _validated_failed_terminal_outbox(
                        meta.get("failed_terminal_receipt_outbox"),
                        task_id=task_id,
                    )
                    if terminal_outbox is not None:
                        if not _terminal_outbox_anchor_matches_row(
                            row,
                            terminal_outbox,
                        ):
                            raise RuntimeError(
                                "terminal_receipt_anchor_mismatch_before_cleanup"
                            )
                        _task_with_frozen_terminal_denominator(
                            row,
                            terminal_outbox,
                        )
                    if failed_outbox is not None:
                        if not _terminal_outbox_anchor_matches_row(
                            row,
                            failed_outbox,
                        ):
                            raise RuntimeError(
                                "failed_terminal_receipt_anchor_mismatch_before_cleanup"
                            )
                        _task_with_frozen_terminal_denominator(
                            row,
                            failed_outbox,
                        )
                        if not _failed_terminal_outbox_matches_row(
                            row,
                            failed_outbox,
                            allow_archived=True,
                        ):
                            raise RuntimeError(
                                "failed_terminal_receipt_payload_drift_before_cleanup"
                            )
                    if str(row["status"]) in {
                        "committed",
                        "intentional_skip",
                    }:
                        if (
                            terminal_outbox is None
                            or terminal_outbox.get("status") != "committed"
                        ):
                            raise RuntimeError(
                                "terminal_receipt_must_commit_before_cleanup"
                            )
                        receipt = _distillation_write_receipt_from_payload(
                            terminal_outbox.get("receipt")
                        )
                        if not _terminal_receipt_matches_row(row, receipt):
                            raise RuntimeError(
                                "terminal_receipt_payload_drift_before_cleanup"
                            )
                    elif not (
                        (
                            terminal_outbox is not None
                            and terminal_outbox.get("status") == "committed"
                        )
                        or (
                            failed_outbox is not None
                            and failed_outbox.get("status") == "committed"
                        )
                    ):
                        raise RuntimeError(
                            "terminal_receipt_must_commit_before_cleanup"
                        )
                    elif terminal_outbox is not None:
                        receipt = _distillation_write_receipt_from_payload(
                            terminal_outbox.get("receipt")
                        )
                        if not _terminal_receipt_matches_row(
                            row,
                            receipt,
                            allow_archived=True,
                        ):
                            raise RuntimeError(
                                "terminal_receipt_payload_drift_before_cleanup"
                            )
                    path_kind = _physical_message_path_kind(path)
                    if (
                        path.name != f"{task_id}.json"
                        or path.parent.absolute() != messages_root
                        or path_kind not in {"missing", "file"}
                    ):
                        raise RuntimeError(
                            "amphora_cleanup_message_path_invalid"
                        )
                    messages_revision = str(meta.get("messages_revision") or "")
                    if not messages_revision:
                        raise RuntimeError(
                            "amphora_cleanup_messages_revision_missing"
                        )
                    if path_kind == "file" and (
                        _messages_revision(
                            _read_messages(raw_path, required=True)
                        )
                        != messages_revision
                    ):
                        raise RuntimeError(
                            "amphora_cleanup_messages_revision_mismatch"
                        )
                    cleanup_outbox = _validated_message_cleanup_outbox(
                        meta.get("message_cleanup_outbox"),
                        task_id=task_id,
                        messages_path=raw_path,
                    )
                    if cleanup_outbox is None:
                        cleanup_outbox = {
                            "schema_version": (
                                "mnemos.amphora_message_cleanup_outbox.v1"
                            ),
                            "task_id": task_id,
                            "status": "pending",
                            "messages_path": raw_path,
                            "messages_path_sha256": hashlib.sha256(
                                raw_path.encode("utf-8")
                            ).hexdigest(),
                            "created_at": _now(),
                        }
                        meta["message_cleanup_outbox"] = cleanup_outbox
                        updated = conn.execute(
                            """
                            UPDATE distillation_tasks
                            SET meta=?, updated_at=?
                            WHERE task_id=? AND messages_path=? AND meta=?
                            """,
                            (
                                json.dumps(
                                    meta,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                                cleanup_outbox["created_at"],
                                task_id,
                                raw_path,
                                str(row["meta"] or "{}"),
                            ),
                        )
                        if updated.rowcount != 1:
                            conn.rollback()
                            continue
                    conn.commit()
                except (
                    json.JSONDecodeError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as exc:
                    conn.rollback()
                    quarantine_reason = (
                        "message_cleanup_quarantined:"
                        f"{type(exc).__name__}:{exc}"
                    )
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        """
                        UPDATE distillation_tasks
                        SET progress_detail=?, updated_at=?
                        WHERE task_id=? AND messages_path=?
                          AND COALESCE(progress_detail, '') != ?
                        """,
                        (
                            quarantine_reason,
                            _now(),
                            task_id,
                            raw_path,
                            quarantine_reason,
                        ),
                    )
                    conn.commit()
                    continue

                try:
                    secure_remove_regular_file(
                        messages_root,
                        path.name,
                        missing_ok=True,
                    )
                    fsync_directory(messages_root)
                except OSError:
                    logger.error(
                        "Amphora message cleanup remains pending: %s",
                        path,
                        exc_info=True,
                    )
                    continue

                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    """
                    SELECT status, messages_path, meta
                    FROM distillation_tasks
                    WHERE task_id=?
                    """,
                    (task_id,),
                ).fetchone()
                if (
                    current is None
                    or str(current["status"])
                    not in {"committed", "intentional_skip", "archived"}
                    or str(current["messages_path"] or "") != raw_path
                ):
                    conn.rollback()
                    continue
                try:
                    current_meta = json.loads(str(current["meta"] or "{}"))
                    if not isinstance(current_meta, dict):
                        raise TypeError("task meta must be an object")
                    pending_outbox = _validated_message_cleanup_outbox(
                        current_meta.get("message_cleanup_outbox"),
                        task_id=task_id,
                        messages_path=raw_path,
                    )
                    if (
                        pending_outbox is None
                        or pending_outbox.get("status") != "pending"
                    ):
                        raise RuntimeError(
                            "amphora_message_cleanup_outbox_invalid"
                        )
                except (
                    json.JSONDecodeError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ):
                    conn.rollback()
                    logger.error(
                        "Amphora cleanup finalization lost its durable intent: %s",
                        task_id,
                        exc_info=True,
                    )
                    continue

                now = _now()
                committed_outbox = {
                    "schema_version": (
                        "mnemos.amphora_message_cleanup_outbox.v1"
                    ),
                    "task_id": task_id,
                    "status": "committed",
                    "messages_path_sha256": pending_outbox[
                        "messages_path_sha256"
                    ],
                    "created_at": pending_outbox["created_at"],
                    "committed_at": now,
                }
                current_meta["message_cleanup_outbox"] = committed_outbox
                updated = conn.execute(
                    """
                    UPDATE distillation_tasks
                    SET status = 'archived',
                        messages_path = NULL,
                        meta = ?,
                        progress_detail = CASE
                            WHEN progress_detail LIKE
                                'message_cleanup_quarantined:%'
                            THEN ''
                            ELSE progress_detail
                        END,
                        updated_at = ?
                    WHERE task_id=? AND messages_path=? AND meta=?
                    """,
                    (
                        json.dumps(
                            current_meta,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                        task_id,
                        raw_path,
                        str(current["meta"] or "{}"),
                    ),
                )
                if updated.rowcount != 1:
                    conn.rollback()
                    continue
                conn.commit()
                finalized += 1
    return finalized


def get_task_count(status: str | None = None) -> int:
    """获取任务数量（用于监控）。"""
    return AmphoraQueries(_connect, _init_db, _row_to_dict).task_count(status)


def main():
    from core.kia.amphora_cli import AmphoraCliDependencies, main as cli_main

    cli_main(
        AmphoraCliDependencies(
            cleanup_old=cleanup_old,
            get_next=get_next,
            get_task_count=get_task_count,
            list_pending=list_pending,
            mark_done=mark_done,
            mark_failed=mark_failed,
            mark_intentional_skip=mark_intentional_skip,
        )
    )


if __name__ == "__main__":
    main()
