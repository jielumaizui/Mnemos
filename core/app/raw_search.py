# -*- coding: utf-8 -*-
"""
RawContextSearch — 轻量索引 + 上下文感知搜索

职责：
- 为 Obsidian raw/ 目录建立 SQLite 倒排索引
- 支持全文搜索、时间范围过滤、session_id 精确匹配
- 返回结构化片段（匹配行前后上下文），而非整文件内容

索引维护策略：
- 首次使用：全量扫描 raw 目录建索引
- 增量更新：文件 mtime 变化时重新索引
- 后台同步：ObsidianBackend.save() 后触发更新
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, cast

from core.app.raw_search_support import RawIndexSupportMixin, RawSearchResult
from core.config import ConfigProvider, get_config
from core.frontmatter import normalize_frontmatter, parse_frontmatter
from core.ops.readiness_query_budget import connect_readonly_sqlite

logger = logging.getLogger(__name__)

RAW_INDEX_SCHEMA_VERSION = "mnemos.raw_index_schema.v1"
RAW_INDEX_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS raw_index (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_path TEXT NOT NULL UNIQUE,
        abs_path TEXT NOT NULL,
        session_id TEXT,
        date TEXT,
        created_at TEXT,
        content TEXT,
        frontmatter TEXT,
        turn_number INTEGER,
        source TEXT,
        tags TEXT,
        mtime REAL,
        indexed_at REAL DEFAULT (unixepoch())
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_raw_session ON raw_index(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_raw_date ON raw_index(date)",
    "CREATE INDEX IF NOT EXISTS idx_raw_mtime ON raw_index(mtime)",
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS raw_fts USING fts5(
        content, session_id, source,
        content_rowid='id',
        tokenize='porter'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_tags (
        file_path TEXT NOT NULL,
        tag TEXT NOT NULL,
        PRIMARY KEY (file_path, tag)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_raw_tags_tag ON raw_tags(tag)",
    "CREATE INDEX IF NOT EXISTS idx_raw_tags_file ON raw_tags(file_path)",
)
_RAW_INDEX_REQUIRED_TABLES = ("raw_index", "raw_fts", "raw_tags")
_RAW_INDEX_REQUIRED_INDEXES = {
    "idx_raw_session": ("raw_index", ("session_id",)),
    "idx_raw_date": ("raw_index", ("date",)),
    "idx_raw_mtime": ("raw_index", ("mtime",)),
    "idx_raw_tags_tag": ("raw_tags", ("tag",)),
    "idx_raw_tags_file": ("raw_tags", ("file_path",)),
}
_RAW_INDEX_EXPECTED_COLUMNS = {
    "raw_index": (
        (0, "id", "INTEGER", 0, None, 1),
        (1, "file_path", "TEXT", 1, None, 0),
        (2, "abs_path", "TEXT", 1, None, 0),
        (3, "session_id", "TEXT", 0, None, 0),
        (4, "date", "TEXT", 0, None, 0),
        (5, "created_at", "TEXT", 0, None, 0),
        (6, "content", "TEXT", 0, None, 0),
        (7, "frontmatter", "TEXT", 0, None, 0),
        (8, "turn_number", "INTEGER", 0, None, 0),
        (9, "source", "TEXT", 0, None, 0),
        (10, "tags", "TEXT", 0, None, 0),
        (11, "mtime", "REAL", 0, None, 0),
        (12, "indexed_at", "REAL", 0, "unixepoch()", 0),
    ),
    "raw_fts": (
        (0, "content", "", 0, None, 0),
        (1, "session_id", "", 0, None, 0),
        (2, "source", "", 0, None, 0),
    ),
    "raw_tags": (
        (0, "file_path", "TEXT", 1, None, 1),
        (1, "tag", "TEXT", 1, None, 2),
    ),
}
_RAW_INDEX_EXPECTED_UNIQUE_CONSTRAINTS = {
    "raw_index": ((("file_path",), 1, "u", 0),),
    "raw_tags": ((("file_path", "tag"), 1, "pk", 0),),
}


def _canonical_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _normalized_schema_sql(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _stored_schema_sql(statement: str) -> str:
    """Normalize canonical DDL as SQLite stores it in ``sqlite_master``."""

    return _normalized_schema_sql(statement).replace("ifnotexists", "")


_RAW_INDEX_EXPECTED_OBJECT_SQL = {
    "raw_index": _stored_schema_sql(RAW_INDEX_SCHEMA_STATEMENTS[0]),
    "idx_raw_session": _stored_schema_sql(RAW_INDEX_SCHEMA_STATEMENTS[1]),
    "idx_raw_date": _stored_schema_sql(RAW_INDEX_SCHEMA_STATEMENTS[2]),
    "idx_raw_mtime": _stored_schema_sql(RAW_INDEX_SCHEMA_STATEMENTS[3]),
    "raw_fts": _stored_schema_sql(RAW_INDEX_SCHEMA_STATEMENTS[4]),
    "raw_tags": _stored_schema_sql(RAW_INDEX_SCHEMA_STATEMENTS[5]),
    "idx_raw_tags_tag": _stored_schema_sql(RAW_INDEX_SCHEMA_STATEMENTS[6]),
    "idx_raw_tags_file": _stored_schema_sql(RAW_INDEX_SCHEMA_STATEMENTS[7]),
}


def missing_raw_index_schema_contract() -> Dict[str, Any]:
    """Return the path-level signature for a database that does not exist."""

    payload = {
        "schema_version": RAW_INDEX_SCHEMA_VERSION,
        "state": "missing_database",
        "objects": {},
        "missing_objects": [],
        "mismatches": [],
    }
    return {**payload, "signature_hash": _canonical_hash(payload)}


def inspect_raw_index_schema(connection: sqlite3.Connection) -> Dict[str, Any]:
    """Inspect the exact RawIndex DDL contract without mutating it."""

    object_rows = {
        str(name): {
            "type": str(object_type),
            "table": str(table_name),
            "sql": str(sql or ""),
        }
        for object_type, name, table_name, sql in connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name IN (
                'raw_index', 'raw_fts', 'raw_tags',
                'idx_raw_session', 'idx_raw_date', 'idx_raw_mtime',
                'idx_raw_tags_tag', 'idx_raw_tags_file'
            )
            ORDER BY type, name
            """
        )
    }
    missing_objects: list[str] = []
    mismatches: list[str] = []
    objects: Dict[str, Any] = {}

    for table_name in _RAW_INDEX_REQUIRED_TABLES:
        object_row = object_rows.get(table_name)
        if object_row is None:
            missing_objects.append(f"table:{table_name}")
            continue
        if object_row["type"] != "table":
            mismatches.append(f"{table_name}:object_type")
            continue
        table_columns = tuple(
            tuple(row)
            for row in connection.execute(f"PRAGMA table_info('{table_name}')")
        )
        objects[f"table:{table_name}"] = {
            "columns": [list(row) for row in table_columns],
            "sql": _normalized_schema_sql(object_row["sql"]),
        }
        if table_columns != _RAW_INDEX_EXPECTED_COLUMNS[table_name]:
            mismatches.append(f"{table_name}:columns")
        if (
            _normalized_schema_sql(object_row["sql"])
            != _RAW_INDEX_EXPECTED_OBJECT_SQL[table_name]
        ):
            mismatches.append(
                "raw_fts:not_canonical_fts5"
                if table_name == "raw_fts"
                else f"{table_name}:definition"
            )
        if table_name != "raw_fts":
            foreign_keys = [
                list(row)
                for row in connection.execute(
                    f"PRAGMA foreign_key_list('{table_name}')"
                )
            ]
            objects[f"table:{table_name}"]["foreign_keys"] = foreign_keys
            if foreign_keys:
                mismatches.append(f"{table_name}:foreign_keys")

    for index_name, (table_name, expected_columns) in sorted(
        _RAW_INDEX_REQUIRED_INDEXES.items()
    ):
        object_row = object_rows.get(index_name)
        if object_row is None:
            missing_objects.append(f"index:{index_name}")
            continue
        index_columns = tuple(
            str(row[2])
            for row in cast(
                Iterable[Tuple[Any, ...]],
                connection.execute(f"PRAGMA index_info('{index_name}')"),
            )
        )
        objects[f"index:{index_name}"] = {
            "table": object_row["table"],
            "columns": list(index_columns),
            "sql": _normalized_schema_sql(object_row["sql"]),
        }
        if (
            object_row["type"] != "index"
            or object_row["table"] != table_name
            or index_columns != expected_columns
            or _normalized_schema_sql(object_row["sql"])
            != _RAW_INDEX_EXPECTED_OBJECT_SQL[index_name]
        ):
            mismatches.append(f"{index_name}:definition")

    for table_name, expected_constraints in (
        _RAW_INDEX_EXPECTED_UNIQUE_CONSTRAINTS.items()
    ):
        if object_rows.get(table_name, {}).get("type") != "table":
            continue
        constraints = []
        explicit_indexes = []
        for _seq, index_name, unique, origin, partial in connection.execute(
            f"PRAGMA index_list('{table_name}')"
        ):
            constraint_columns = tuple(
                str(row[2])
                for row in cast(
                    Iterable[Tuple[Any, ...]],
                    connection.execute(
                        f"PRAGMA index_info('{str(index_name)}')"
                    ),
                )
            )
            descriptor = (
                constraint_columns,
                int(unique),
                str(origin),
                int(partial),
            )
            if str(origin) in {"u", "pk"}:
                constraints.append(descriptor)
            elif str(index_name) not in _RAW_INDEX_REQUIRED_INDEXES:
                explicit_indexes.append(str(index_name))
        objects[f"constraints:{table_name}"] = [
            [list(columns), unique, origin, partial]
            for columns, unique, origin, partial in sorted(constraints)
        ]
        if tuple(sorted(constraints)) != expected_constraints:
            mismatches.append(f"{table_name}:unique_constraints")
        if explicit_indexes:
            mismatches.append(
                f"{table_name}:unexpected_indexes:{','.join(sorted(explicit_indexes))}"
            )

    unexpected_triggers = sorted(
        str(row[0])
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'trigger'
              AND tbl_name IN ('raw_index', 'raw_fts', 'raw_tags')
            """
        )
    )
    if unexpected_triggers:
        mismatches.append(
            "unexpected_triggers:" + ",".join(unexpected_triggers)
        )

    if mismatches:
        state = "incompatible"
    elif len(missing_objects) == (
        len(_RAW_INDEX_REQUIRED_TABLES) + len(_RAW_INDEX_REQUIRED_INDEXES)
    ):
        state = "absent"
    elif missing_objects:
        state = "partial"
    else:
        state = "canonical"
    payload = {
        "schema_version": RAW_INDEX_SCHEMA_VERSION,
        "state": state,
        "objects": objects,
        "missing_objects": sorted(missing_objects),
        "mismatches": sorted(mismatches),
    }
    return {**payload, "signature_hash": _canonical_hash(payload)}


def raw_index_content_state(
    raw_dir: Path,
    relative_path: str,
    text: str,
) -> Dict[str, Any]:
    """Derive every deterministic RawIndex field from canonical file bytes."""

    frontmatter, _body = parse_frontmatter(text)
    normalized = normalize_frontmatter(frontmatter or {})
    raw_date = normalized.get("date", "")
    date = raw_date.isoformat() if hasattr(raw_date, "isoformat") else str(raw_date or "")
    raw_time = normalized.get("time", "00:00")
    if isinstance(raw_time, int) and 0 <= raw_time < 24 * 60:
        hours, minutes = divmod(raw_time, 60)
        time_text = f"{hours:02d}:{minutes:02d}"
    else:
        time_text = str(raw_time or "00:00")
    turn_number = normalized.get("turn", 0)
    if isinstance(turn_number, str):
        try:
            turn_number = int(turn_number)
        except ValueError:
            turn_number = 0
    tags = normalized.get("tags", [])
    if isinstance(tags, str):
        tags = [tags]
    tags_json = json.dumps(tags, ensure_ascii=False)
    frontmatter_text = (
        text[: text.find("---", 3) + 3] if text.startswith("---") else ""
    )
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "abs_path": str((Path(raw_dir).expanduser().absolute() / relative_path)),
        "session_id": str(normalized.get("session_id") or ""),
        "date": date,
        "created_at": f"{date}T{time_text}",
        "content_hash": content_hash,
        "frontmatter_hash": hashlib.sha256(
            frontmatter_text.encode("utf-8")
        ).hexdigest(),
        "turn_number": int(turn_number or 0),
        "source": str(normalized.get("source") or ""),
        "tags_json": tags_json,
        "fts_content_hash": content_hash,
        "fts_session_id": str(normalized.get("session_id") or ""),
        "fts_source": str(normalized.get("source") or ""),
        "normalized_tags": sorted(
            {str(tag).strip().lower() for tag in tags if str(tag).strip()}
        ),
    }


def raw_index_projection_snapshot(
    connection: sqlite3.Connection,
) -> Dict[str, Any]:
    """Read the complete RawIndex data preimage under the caller's transaction."""

    schema = inspect_raw_index_schema(connection)
    if schema["state"] == "incompatible":
        raise RuntimeError(
            "raw_index_schema_incompatible:" + ",".join(schema["mismatches"])
        )
    present_tables = {
        name.split(":", 1)[1]
        for name in schema["objects"]
        if name.startswith("table:")
    }
    raw_index_rows: List[Dict[str, Any]] = []
    raw_fts_rows: List[Dict[str, Any]] = []
    raw_tag_rows: List[List[str]] = []
    tags_by_path: Dict[str, List[str]] = {}
    fts_by_rowid: Dict[int, Dict[str, str]] = {}
    indexed_states: Dict[str, Dict[str, Any]] = {}
    indexed_managed_paths: set[str] = set()

    if "raw_tags" in present_tables:
        for relative_path, tag in connection.execute(
            "SELECT file_path, tag FROM raw_tags ORDER BY file_path, tag"
        ):
            path_text = str(relative_path or "")
            tag_text = str(tag or "")
            tags_by_path.setdefault(path_text, []).append(tag_text)
            raw_tag_rows.append([path_text, tag_text])
    if "raw_fts" in present_tables:
        for rowid, content, session_id, source in connection.execute(
            """
            SELECT rowid, content, session_id, source
            FROM raw_fts
            ORDER BY rowid
            """
        ):
            normalized_rowid = int(rowid)
            fts_state = {
                "content_hash": hashlib.sha256(
                    str(content or "").encode("utf-8")
                ).hexdigest(),
                "session_id": str(session_id or ""),
                "source": str(source or ""),
            }
            fts_by_rowid[normalized_rowid] = fts_state
            raw_fts_rows.append({"rowid": normalized_rowid, **fts_state})

    raw_index_ids: set[int] = set()
    raw_index_paths: set[str] = set()
    if "raw_index" in present_tables:
        for row in connection.execute(
            """
            SELECT
                id, file_path, abs_path, session_id, date, created_at,
                content, frontmatter, turn_number, source, tags, mtime,
                indexed_at
            FROM raw_index
            ORDER BY file_path, id
            """
        ):
            (
                row_id,
                relative_path,
                abs_path,
                session_id,
                date,
                created_at,
                content,
                frontmatter,
                turn_number,
                source,
                tags_json,
                mtime,
                indexed_at,
            ) = row
            normalized_rowid = int(row_id)
            path_text = str(relative_path or "")
            content_text = str(content or "")
            frontmatter_text = str(frontmatter or "")
            raw_index_ids.add(normalized_rowid)
            raw_index_paths.add(path_text)
            raw_index_rows.append(
                {
                    "id": normalized_rowid,
                    "file_path": path_text,
                    "abs_path": str(abs_path or ""),
                    "session_id": str(session_id or ""),
                    "date": str(date or ""),
                    "created_at": str(created_at or ""),
                    "content_hash": hashlib.sha256(
                        content_text.encode("utf-8")
                    ).hexdigest(),
                    "frontmatter_hash": hashlib.sha256(
                        frontmatter_text.encode("utf-8")
                    ).hexdigest(),
                    "turn_number": int(turn_number or 0),
                    "source": str(source or ""),
                    "tags_json": str(tags_json or ""),
                    "mtime": float(mtime or 0.0),
                    "indexed_at": float(indexed_at or 0.0),
                }
            )
            row_fts_state = fts_by_rowid.get(normalized_rowid)
            indexed_states[path_text] = {
                "abs_path": str(abs_path or ""),
                "session_id": str(session_id or ""),
                "date": str(date or ""),
                "created_at": str(created_at or ""),
                "content_hash": hashlib.sha256(
                    content_text.encode("utf-8")
                ).hexdigest(),
                "frontmatter_hash": hashlib.sha256(
                    frontmatter_text.encode("utf-8")
                ).hexdigest(),
                "turn_number": int(turn_number or 0),
                "source": str(source or ""),
                "tags_json": str(tags_json or ""),
                "fts_content_hash": (
                    str(row_fts_state["content_hash"]) if row_fts_state else ""
                ),
                "fts_session_id": (
                    str(row_fts_state["session_id"]) if row_fts_state else ""
                ),
                "fts_source": str(row_fts_state["source"]) if row_fts_state else "",
                "normalized_tags": tags_by_path.get(path_text, []),
            }
            if re.search(
                r'^mnemos_type:\s+["\']?raw_retention_projection(?:_index)?["\']?\s*$',
                content_text[:4096],
                flags=re.MULTILINE,
            ):
                indexed_managed_paths.add(path_text)

    preimage = {
        "raw_index": raw_index_rows,
        "raw_fts": raw_fts_rows,
        "raw_tags": raw_tag_rows,
    }
    return {
        "schema": schema,
        "preimage": preimage,
        "preimage_hash": _canonical_hash(preimage),
        "indexed_states": indexed_states,
        "indexed_managed_paths": sorted(indexed_managed_paths),
        "orphan_counts": {
            "raw_fts": len(set(fts_by_rowid) - raw_index_ids),
            "raw_tags": sum(
                1
                for relative_path, _tag in raw_tag_rows
                if relative_path not in raw_index_paths
            ),
        },
    }


def _raw_index_unplanned_preimage(
    snapshot: Dict[str, Any],
    *,
    touched_paths: set[str],
    allow_orphan_cleanup: bool,
) -> Dict[str, Any]:
    """Project the rows an exact write set is not authorized to change."""

    preimage = dict(snapshot["preimage"])
    raw_index_rows = list(preimage["raw_index"])
    all_row_ids = {int(row["id"]) for row in raw_index_rows}
    touched_row_ids = {
        int(row["id"])
        for row in raw_index_rows
        if str(row["file_path"]) in touched_paths
    }
    all_paths = {str(row["file_path"]) for row in raw_index_rows}
    return {
        "raw_index": [
            row
            for row in raw_index_rows
            if str(row["file_path"]) not in touched_paths
        ],
        "raw_fts": [
            row
            for row in preimage["raw_fts"]
            if int(row["rowid"]) not in touched_row_ids
            and not (
                allow_orphan_cleanup
                and int(row["rowid"]) not in all_row_ids
            )
        ],
        "raw_tags": [
            row
            for row in preimage["raw_tags"]
            if str(row[0]) not in touched_paths
            and not (
                allow_orphan_cleanup
                and str(row[0]) not in all_paths
            )
        ],
    }


def _raw_index_database_path_state(path: Path) -> str:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "missing_database"
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("raw_index_database_not_regular")
    return "present"


def _fts_literal_query(query: str) -> str:
    """Treat user input as text, not as executable FTS5 query syntax."""
    return '"' + str(query).replace('"', '""') + '"'


class RawIndex(RawIndexSupportMixin):
    """Raw 目录轻量索引管理器"""

    def __init__(
        self,
        raw_dir: Optional[Path] = None,
        db_path: Optional[Path] = None,
        config: Optional[ConfigProvider] = None,
        raw_event_store: Optional[Any] = None,
        read_only: bool = False,
        initialize_schema: bool = True,
    ):
        cfg = config or get_config()
        self.raw_dir = Path(raw_dir or cfg.obsidian_vault_path).expanduser()
        # 索引库放在 database_dir 下（可独立配置到数据盘）
        db_dir = Path(db_path).parent if db_path else cfg.database_dir
        self.db_path = db_path or (db_dir / "raw_index.db")
        self.read_only = bool(read_only)
        if not self.read_only:
            db_dir.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self.raw_event_store = raw_event_store
        self._owns_raw_event_store = False
        if (
            not self.read_only
            and self.raw_event_store is None
            and cfg.get("raw_event_store.enabled", True)
        ):
            try:
                from core.sync_framework.raw_event_store import RawEventStore

                self.raw_event_store = RawEventStore(
                    db_path=db_dir / "raw_events.db",
                    config=cfg,
                )
                self._owns_raw_event_store = True
            except (ImportError, OSError, sqlite3.Error, ValueError):
                logger.debug("[RawIndex] raw_event_store 不可用，跳过 metrics 记录", exc_info=True)
        if not self.read_only and initialize_schema:
            try:
                self._ensure_schema()
            except BaseException:
                self.close()
                raise

    def __enter__(self) -> "RawIndex":
        return self

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        self.close()

    def __del__(self):
        # [P1-39] 确保未显式关闭时释放连接，避免 WAL/锁泄漏
        self.close()

    # ---------- 数据库连接 ----------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            if self.read_only:
                self._conn = connect_readonly_sqlite(
                    self.db_path,
                    timeout_seconds=5,
                    check_same_thread=False,
                )
            else:
                self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._ensure_schema_in_transaction(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def _ensure_schema_in_transaction(self, conn: sqlite3.Connection) -> None:
        before = inspect_raw_index_schema(conn)
        if before["state"] == "incompatible":
            raise RuntimeError(
                "raw_index_schema_incompatible:"
                + ",".join(before["mismatches"])
            )
        self._execute_schema_statements(
            conn,
            ";\n".join(RAW_INDEX_SCHEMA_STATEMENTS),
        )
        after = inspect_raw_index_schema(conn)
        if after["state"] != "canonical":
            details = after["mismatches"] or after["missing_objects"]
            raise RuntimeError(
                "raw_index_schema_initialization_incomplete:"
                + ",".join(details)
            )

    @staticmethod
    def _execute_schema_statements(
        conn: sqlite3.Connection,
        script: str,
    ) -> None:
        """Execute fixed DDL without ``executescript``'s implicit commit."""
        for statement in script.split(";"):
            if statement.strip():
                conn.execute(statement)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
        if self._owns_raw_event_store and self.raw_event_store is not None:
            try:
                self.raw_event_store.close()
            finally:
                self.raw_event_store = None
                self._owns_raw_event_store = False

    # ---------- 索引构建 ----------

    def _parse_frontmatter(self, text: str) -> Tuple[Dict[str, Any], str]:
        """Parse Markdown through the canonical typed YAML frontmatter seam."""
        frontmatter, body = parse_frontmatter(text)
        return normalize_frontmatter(frontmatter or {}), body

    def _read_stable_source(
        self,
        file_path: Path,
    ) -> Tuple[Path, str, os.stat_result]:
        """Read one regular in-root file and bind the bytes to one descriptor."""
        normalized_path = Path(file_path).expanduser().absolute()
        lexical_root = self.raw_dir.expanduser().absolute()
        normalized_path.relative_to(lexical_root)
        resolved_root = lexical_root.resolve(strict=True)
        resolved_parent = normalized_path.parent.resolve(strict=True)
        if (
            resolved_parent != resolved_root
            and resolved_root not in resolved_parent.parents
        ):
            raise ValueError("raw_index_path_escapes_raw_root")
        descriptor_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            descriptor_flags |= os.O_NOFOLLOW
        descriptor = os.open(normalized_path, descriptor_flags)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("raw_index_source_not_regular")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity:
            raise OSError("raw_index_source_changed_during_read")
        return (
            normalized_path,
            b"".join(chunks).decode("utf-8"),
            after,
        )

    def _index_file(
        self,
        file_path: Path,
        cursor: sqlite3.Cursor,
        *,
        stable_source: Tuple[Path, str, os.stat_result] | None = None,
    ) -> bool:
        """索引单个文件，返回是否成功"""
        try:
            file_path, text, source_stat = (
                stable_source
                if stable_source is not None
                else self._read_stable_source(file_path)
            )
            lexical_root = self.raw_dir.expanduser().absolute()
            rel_path = str(file_path.relative_to(lexical_root))
            abs_path = str(file_path)
            mtime = source_stat.st_mtime

            state = raw_index_content_state(self.raw_dir, rel_path, text)
            session_id = state["session_id"]
            date = state["date"]
            created_at = state["created_at"]
            turn_number = state["turn_number"]
            source = state["source"]
            tags_json = state["tags_json"]
            tags = json.loads(tags_json)

            # 删除旧记录：先删 FTS 与 tags，再删 raw_index（否则子查询查不到 rowid）
            cursor.execute(
                "DELETE FROM raw_fts WHERE rowid IN (SELECT id FROM raw_index WHERE file_path = ?)",
                (rel_path,),
            )
            cursor.execute("DELETE FROM raw_tags WHERE file_path = ?", (rel_path,))
            cursor.execute("DELETE FROM raw_index WHERE file_path = ?", (rel_path,))

            # 插入新记录
            cursor.execute(
                """INSERT INTO raw_index
                   (file_path, abs_path, session_id, date, created_at, content,
                    frontmatter, turn_number, source, tags, mtime)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rel_path,
                    abs_path,
                    session_id,
                    date,
                    created_at,
                    text,
                    (
                        text[: text.find("---", 3) + 3]
                        if text.startswith("---")
                        else ""
                    ),
                    turn_number,
                    source,
                    tags_json,
                    mtime,
                ),
            )
            row_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO raw_fts (rowid, content, session_id, source) VALUES (?, ?, ?, ?)",
                (row_id, text, session_id or "", source or ""),
            )
            # 归一化标签表
            for tag in tags:
                tag_norm = str(tag).strip().lower()
                if tag_norm:
                    cursor.execute(
                        "INSERT OR IGNORE INTO raw_tags (file_path, tag) VALUES (?, ?)",
                        (rel_path, tag_norm),
                    )
            return True
        except (OSError, UnicodeError, ValueError, TypeError, sqlite3.Error) as e:
            logger.warning("[RawIndex] 索引失败 %s: %s", file_path, e, exc_info=True)
            return False

    def _remove_indexed_rows(self, cursor: sqlite3.Cursor, rel_path: str) -> None:
        """从 raw_index / raw_fts / raw_tags 中删除指定文件的所有索引行。"""
        cursor.execute(
            "DELETE FROM raw_fts WHERE rowid IN (SELECT id FROM raw_index WHERE file_path = ?)",
            (rel_path,),
        )
        cursor.execute("DELETE FROM raw_tags WHERE file_path = ?", (rel_path,))
        cursor.execute("DELETE FROM raw_index WHERE file_path = ?", (rel_path,))

    def _remove_stale_by_age(
        self,
        cursor: sqlite3.Cursor,
        cutoff_days: int,
    ) -> set[str]:
        """Remove stale projections and return paths that must be re-indexed."""
        if cutoff_days <= 0:
            return set()
        cutoff_ts = (datetime.now() - timedelta(days=cutoff_days)).timestamp()
        cursor.execute(
            "SELECT file_path FROM raw_index WHERE indexed_at < ?",
            (cutoff_ts,),
        )
        removed_paths: set[str] = set()
        for (rel_path,) in cursor.fetchall():
            self._remove_indexed_rows(cursor, rel_path)
            removed_paths.add(str(rel_path))
        return removed_paths

    def vacuum(self) -> None:
        """执行 VACUUM 回收空间，应在连接关闭或低峰期调用。"""
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            conn.execute("VACUUM")
            conn.commit()
            logger.info("[RawIndex] VACUUM 完成")
        except sqlite3.Error as e:
            if conn is not None:
                conn.rollback()
            logger.warning("[RawIndex] VACUUM 失败: %s", e, exc_info=True)

    def sync_index(self, force_full: bool = False) -> Dict[str, int]:
        """
        同步索引。

        - force_full=True：删除旧索引，全量重建
        - 默认：只增量更新 mtime 变化的文件

        Returns:
            {"indexed": N, "removed": M, "skipped": K}
        """
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.cursor()

            if force_full:
                cursor.execute("DELETE FROM raw_fts")
                cursor.execute("DELETE FROM raw_tags")
                cursor.execute("DELETE FROM raw_index")
                indexed_files = set()
                indexed_mtime: Dict[str, float] = {}
                removed = 0
            else:
                # 获取已索引文件的 mtime
                cursor.execute("SELECT file_path, mtime FROM raw_index")
                indexed_mtime = {row[0]: row[1] for row in cursor.fetchall()}

                # 删除不存在的文件
                removed = 0
                for rel_path in list(indexed_mtime.keys()):
                    abs_path = self.raw_dir / rel_path
                    if not abs_path.exists():
                        self._remove_indexed_rows(cursor, rel_path)
                        removed += 1
                indexed_files = set(indexed_mtime.keys())

                # 按索引时间清理超旧记录（默认 180 天，可通过配置 raw.index.retention_days 关闭/调整）
                try:
                    cfg = get_config()
                    retention_days = int(cfg.get("raw.index.retention_days", 180))
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    ImportError,
                    AttributeError,
                    RuntimeError,
                    sqlite3.Error,
                ):
                    retention_days = 180
                stale_paths = self._remove_stale_by_age(cursor, retention_days)
                removed += len(stale_paths)

            indexed = 0
            skipped = 0
            failed_paths: list[str] = []
            for md_file in self.raw_dir.rglob("*.md"):
                rel = str(md_file.relative_to(self.raw_dir))
                stable_source: Tuple[Path, str, os.stat_result] | None = None
                if not force_full and rel in indexed_files:
                    try:
                        current_mtime = md_file.stat().st_mtime
                        if abs(current_mtime - indexed_mtime.get(rel, 0)) < 1:
                            stable_source = self._read_stable_source(md_file)
                            indexed_content = cursor.execute(
                                "SELECT content FROM raw_index WHERE file_path = ?",
                                (rel,),
                            ).fetchone()
                            if (
                                indexed_content is not None
                                and str(indexed_content[0] or "") == stable_source[1]
                            ):
                                skipped += 1
                                continue
                    except (OSError, UnicodeError, ValueError, TypeError, sqlite3.Error):
                        failed_paths.append(rel)
                        continue
                if self._index_file(
                    md_file,
                    cursor,
                    stable_source=stable_source,
                ):
                    indexed += 1
                else:
                    failed_paths.append(rel)
            if failed_paths:
                raise RuntimeError(
                    "raw_index_sync_incomplete:"
                    f"{len(failed_paths)}"
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        logger.info(
            "[RawIndex] 同步完成: indexed=%s, removed=%s, skipped=%s",
            indexed,
            removed,
            skipped,
        )

        # 如果删除了较多数据，执行 VACUUM 回收空间
        if removed > 10:
            self.close()
            self.vacuum()

        return {
            "indexed": indexed,
            "removed": removed,
            "skipped": skipped,
        }

    # ---------- 单文件维护 ----------

    def index_file(self, file_path: Path) -> bool:
        """增量索引/更新单个文件，避免全目录扫描。"""
        try:
            file_path = Path(file_path)
            if not file_path.exists():
                return False
            conn = self._connect()
            cursor = conn.cursor()
            result = self._index_file(file_path, cursor)
            if result:
                conn.commit()
            else:
                conn.rollback()
            return result
        except (OSError, UnicodeError, ValueError, TypeError, sqlite3.Error) as e:
            if self._conn is not None:
                self._conn.rollback()
            logger.warning("[RawIndex] 单文件索引失败 %s: %s", file_path, e, exc_info=True)
            return False

    def remove_file(self, rel_path: str) -> bool:
        """从索引中移除单个文件（如文件被删除时）。"""
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM raw_fts WHERE rowid IN (SELECT id FROM raw_index WHERE file_path = ?)",
                (rel_path,),
            )
            cursor.execute("DELETE FROM raw_tags WHERE file_path = ?", (rel_path,))
            cursor.execute("DELETE FROM raw_index WHERE file_path = ?", (rel_path,))
            conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as e:
            if conn is not None:
                conn.rollback()
            logger.warning("[RawIndex] 移除索引失败 %s: %s", rel_path, e, exc_info=True)
            return False

    @staticmethod
    def _remove_orphan_rows_in_transaction(
        conn: sqlite3.Connection,
    ) -> Dict[str, int]:
        orphan_fts = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM raw_fts
                WHERE rowid NOT IN (SELECT id FROM raw_index)
                """
            ).fetchone()[0]
        )
        orphan_tags = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM raw_tags
                WHERE file_path NOT IN (SELECT file_path FROM raw_index)
                """
            ).fetchone()[0]
        )
        conn.execute(
            "DELETE FROM raw_fts WHERE rowid NOT IN (SELECT id FROM raw_index)"
        )
        conn.execute(
            """
            DELETE FROM raw_tags
            WHERE file_path NOT IN (SELECT file_path FROM raw_index)
            """
        )
        remaining_fts = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM raw_fts
                WHERE rowid NOT IN (SELECT id FROM raw_index)
                """
            ).fetchone()[0]
        )
        remaining_tags = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM raw_tags
                WHERE file_path NOT IN (SELECT file_path FROM raw_index)
                """
            ).fetchone()[0]
        )
        if remaining_fts or remaining_tags:
            raise RuntimeError("raw_index_orphan_cleanup_incomplete")
        return {
            "orphan_fts_removed": orphan_fts,
            "orphan_tags_removed": orphan_tags,
        }

    def remove_orphan_rows(self) -> Dict[str, int]:
        """Atomically remove FTS/tag consumers that have no raw_index owner."""
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = self._remove_orphan_rows_in_transaction(conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise

    def apply_projection_write_set(
        self,
        *,
        changed_paths: Iterable[str],
        deleted_paths: Iterable[str],
        cleanup_orphans: bool,
        expected_preimage_hash: str | None = None,
        expected_schema_state: str | None = None,
        expected_schema_signature_hash: str | None = None,
        expected_orphan_counts: Dict[str, int] | None = None,
        expected_post_state_hashes: Dict[str, str] | None = None,
    ) -> Dict[str, int]:
        """Apply one projection generation in one SQLite transaction.

        The reviewed complete preimage and schema are rechecked after
        ``BEGIN IMMEDIATE`` and before any DDL or DML. Schema initialization,
        orphan repair, every changed path, and every deleted path then commit
        together or roll back together.
        """

        changed_path_set = {str(path) for path in changed_paths}
        deleted_path_set = {str(path) for path in deleted_paths}
        if changed_path_set & deleted_path_set:
            raise ValueError("raw_index_projection_path_sets_overlap")
        touched_paths = changed_path_set | deleted_path_set
        database_state = _raw_index_database_path_state(Path(self.db_path))
        if (
            expected_schema_state == "missing_database"
            and database_state != "missing_database"
        ):
            raise RuntimeError("raw_index_schema_state_changed_before_apply")
        if (
            expected_schema_state not in {None, "missing_database"}
            and database_state != "present"
        ):
            raise RuntimeError("raw_index_schema_state_changed_before_apply")
        if expected_schema_state == "missing_database":
            missing_contract = missing_raw_index_schema_contract()
            if (
                expected_schema_signature_hash
                != missing_contract["signature_hash"]
            ):
                raise RuntimeError("raw_index_schema_signature_changed_before_apply")

        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            before = raw_index_projection_snapshot(conn)
            before_schema = before["schema"]
            unplanned_preimage = _raw_index_unplanned_preimage(
                before,
                touched_paths=touched_paths,
                allow_orphan_cleanup=cleanup_orphans,
            )
            if expected_schema_state == "missing_database":
                if before_schema["state"] != "absent":
                    raise RuntimeError(
                        "raw_index_schema_state_changed_before_apply"
                    )
            else:
                if (
                    expected_schema_state is not None
                    and before_schema["state"] != expected_schema_state
                ):
                    raise RuntimeError(
                        "raw_index_schema_state_changed_before_apply"
                    )
                if (
                    expected_schema_signature_hash is not None
                    and before_schema["signature_hash"]
                    != expected_schema_signature_hash
                ):
                    raise RuntimeError(
                        "raw_index_schema_signature_changed_before_apply"
                    )
            if (
                expected_preimage_hash is not None
                and before["preimage_hash"] != expected_preimage_hash
            ):
                raise RuntimeError(
                    "raw_index_complete_preimage_changed_before_apply"
                )
            if (
                expected_orphan_counts is not None
                and before["orphan_counts"] != expected_orphan_counts
            ):
                raise RuntimeError(
                    "raw_index_orphan_denominator_changed_before_apply"
                )

            self._ensure_schema_in_transaction(conn)
            orphan_stats = {
                "orphan_fts_removed": 0,
                "orphan_tags_removed": 0,
            }
            if cleanup_orphans:
                orphan_stats = self._remove_orphan_rows_in_transaction(conn)
                if expected_orphan_counts is not None and (
                    orphan_stats["orphan_fts_removed"]
                    != int(expected_orphan_counts["raw_fts"])
                    or orphan_stats["orphan_tags_removed"]
                    != int(expected_orphan_counts["raw_tags"])
                ):
                    raise RuntimeError(
                        "raw_index_orphan_cleanup_diverged_from_plan"
                    )

            indexed = 0
            for relative_path in sorted(changed_path_set):
                relative = Path(relative_path)
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or relative.as_posix() != relative_path
                ):
                    raise ValueError("raw_index_projection_path_invalid")
                target = self.raw_dir / relative
                if not self._index_file(target, conn.cursor()):
                    raise RuntimeError(
                        f"raw_index_projection_path_failed:{relative_path}"
                    )
                indexed += 1

            removed = 0
            for relative_path in sorted(deleted_path_set):
                relative = Path(relative_path)
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or relative.as_posix() != relative_path
                ):
                    raise ValueError("raw_index_projection_path_invalid")
                existed = conn.execute(
                    "SELECT 1 FROM raw_index WHERE file_path = ?",
                    (relative_path,),
                ).fetchone()
                self._remove_indexed_rows(conn.cursor(), relative_path)
                removed += int(existed is not None)

            after = raw_index_projection_snapshot(conn)
            if after["schema"]["state"] != "canonical":
                raise RuntimeError("raw_index_schema_not_canonical_after_apply")
            if any(after["orphan_counts"].values()):
                raise RuntimeError("raw_index_orphans_remain_after_apply")
            if expected_post_state_hashes is not None:
                actual_hashes = {
                    path: (
                        _canonical_hash(after["indexed_states"][path])
                        if path in after["indexed_states"]
                        else ""
                    )
                    for path in expected_post_state_hashes
                }
                if actual_hashes != expected_post_state_hashes:
                    raise RuntimeError(
                        "raw_index_post_state_diverged_from_plan"
                    )
            if _raw_index_unplanned_preimage(
                after,
                touched_paths=touched_paths,
                allow_orphan_cleanup=False,
            ) != unplanned_preimage:
                raise RuntimeError(
                    "raw_index_unplanned_preimage_changed_during_apply"
                )
            conn.commit()
            return {
                "indexed": indexed,
                "removed": removed,
                "failed": 0,
                **orphan_stats,
            }
        except BaseException:
            conn.rollback()
            raise

    def search(
        self,
        query: str,
        days: Optional[int] = None,
        session_id: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 10,
        allowed_identities: Optional[set[tuple[str, str]]] = None,
    ) -> List[RawSearchResult]:
        """
        搜索 raw 目录。

        Args:
            query: 搜索关键词
            days: 最近 N 天（None 表示全部）
            session_id: 精确匹配 session
            source: 按 Agent 来源过滤（如 "claude", "hermes", "openclaw"）
            limit: 最大返回数
        """
        if not query and not session_id:
            return []
        if self.read_only and not self.db_path.exists():
            return []
        if allowed_identities is not None and not allowed_identities:
            return []

        conn = self._connect()
        cursor = conn.cursor()

        # 构建 WHERE 条件
        conditions: List[str] = []
        params: List = []

        if query:
            # 使用 FTS5 全文搜索
            conditions.append("raw_index.id IN (SELECT rowid FROM raw_fts WHERE raw_fts MATCH ?)")
            params.append(_fts_literal_query(query))

        if session_id:
            conditions.append("raw_index.session_id = ?")
            params.append(session_id)

        if source:
            conditions.append("raw_index.source = ?")
            params.append(source)

        if allowed_identities is not None:
            identity_conditions: List[str] = []
            for allowed_source, allowed_session in sorted(allowed_identities):
                identity_conditions.append(
                    "(raw_index.source = ? AND raw_index.session_id = ?)"
                )
                params.extend([allowed_source, allowed_session])
            conditions.append("(" + " OR ".join(identity_conditions) + ")")

        if days is not None:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            conditions.append("raw_index.date >= ?")
            params.append(cutoff)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        cursor.execute(
            " ".join([
                "SELECT file_path, session_id, date, created_at, content,",
                "turn_number, source, tags, abs_path",
                "FROM raw_index",
                "WHERE",
                where_clause,
                "ORDER BY date DESC, turn_number DESC",
                "LIMIT ?",
            ]),
            params + [limit * 3],  # 多取一些用于片段打分筛选
        )
        rows = cursor.fetchall()

        results: List[RawSearchResult] = []
        query_lower = query.lower() if query else ""

        for row in rows:
            file_path, sess_id, date, created_at, content, turn, source, tags_json, abs_path = row
            tags = json.loads(tags_json) if tags_json else []
            # 提取匹配片段
            snippet, matched_line, line_no, score = self._extract_snippet(
                content, query_lower, abs_path
            )
            if not snippet:
                continue

            frontmatter, _ = self._parse_frontmatter(content or "")
            source_agent = str(frontmatter.get("source_agent") or source or "")
            results.append(
                RawSearchResult(
                    file_path=file_path,
                    session_id=sess_id or "",
                    date=date or "",
                    snippet=snippet,
                    matched_line=matched_line,
                    line_number=line_no,
                    score=score,
                    source=source or "",
                    turn_number=turn or 0,
                    tags=tags,
                    created_at=created_at or "",
                    scope=str(frontmatter.get("scope") or ""),
                    source_agent=source_agent,
                    project=str(frontmatter.get("project") or ""),
                    acl_schema_version=int(
                        frontmatter.get("acl_schema_version") or 0
                    ),
                    acl_metadata_complete=(
                        frontmatter.get("acl_metadata_complete") is True
                    ),
                    acl_reconciliation_status=str(
                        frontmatter.get("acl_reconciliation_status") or ""
                    ),
                )
            )

        # 按分数排序，取前 limit
        results.sort(key=lambda x: x.score, reverse=True)
        return results[:limit]
