# -*- coding: utf-8 -*-
"""Crush Agent 被动同步插件。

Crush 将会话数据存储在项目本地 SQLite 数据库中：

    ./.crush/crush.db

核心表：
- sessions:    会话元数据（id, title, parent_session_id, message_count, updated_at, ...）
- messages:    消息记录（id, session_id, role, parts JSON 数组, model, created_at, ...）
- files:       文件引用（id, session_id, path, content, version, ...）
- read_files:  会话读取过的文件路径（session_id, path, read_at）

本插件以只读方式连接 crush.db，按 session 聚合 messages，
解析 text / tool_call / tool_result 片段为统一 Turn 后送入 SyncEngine。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from core.agent_kit.source_support_manifest import (
    expand_path_templates,
    get_agent_source_support_manifest,
)
from core.config import get_config
from core.sync_framework.agent_source import (
    NativeSourceContractError,
    SessionInfo,
    Turn,
)
from core.sync_framework.native_sqlite import (
    NativeSQLiteReadError,
    connect_native_sqlite_readonly,
)

from integrations.sources.base import BaseAgentSource, native_path_kind


def _crush_sqlite_json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "_mnemos_sqlite_type": "blob",
            "base64": base64.b64encode(value).decode("ascii"),
        }
    return value


def _crush_row_mapping(columns: List[str], row: Any) -> Dict[str, Any]:
    return {
        column: _crush_sqlite_json_value(value)
        for column, value in zip(columns, row, strict=True)
    }


def _crush_table_columns(
    connection: sqlite3.Connection,
    table: str,
    *,
    required: set[str],
) -> List[str]:
    columns = [
        str(row[1])
        for row in connection.execute(
            f'PRAGMA table_info("{table}")'  # nosec B608 - fixed internal names
        ).fetchall()
    ]
    if not required <= set(columns):
        raise NativeSourceContractError(
            f"native_crush_{table}_schema_incomplete"
        )
    return columns


@dataclass(frozen=True)
class _CrushSessionCandidate:
    """One native session observed in one read-only Crush database."""

    db_path: Path
    database_id: str
    native_session_id: str
    content_hash: str
    title: str
    parent_session_id: str
    message_count: int
    updated_at: Optional[int]
    created_at: Optional[int]
    priority: int

    @property
    def mtime(self) -> float:
        return _ts_to_seconds(self.updated_at or self.created_at)


class CrushSource(BaseAgentSource):
    """Crush 被动数据源插件 — 直连 SQLite。"""

    DB_NAME = "crush.db"

    _cap_tool_calls = True
    _cap_tool_results = True
    _cap_reasoning = False
    _cap_attachments = True
    _cap_source_fidelity = "full"
    _cap_memory_scope = "project_local_crush_db"
    _cap_host_memory_default = "host_dependent_unknown"
    _cap_host_memory_effect = (
        "Crush memory/config may affect prompts; .crush/crush.db remains the passive capture source"
    )
    _cap_transcript_kind = "native_project_sqlite"
    _cap_compression = "raw_sqlite_messages_no_mnemos_compression"

    _default_extra_tags = ["source=crush"]

    @property
    def name(self) -> str:
        return "crush"

    @property
    def model_tag(self) -> str:
        return "crush"

    @property
    def data_dir(self) -> Optional[Path]:
        """Return the representative root required by the AgentSource contract.

        ``discover_sessions()`` is intentionally broader: it reads every
        valid database returned by ``db_paths``.  This representative property
        must never be used as a completeness boundary.
        """
        roots = self._candidate_data_dirs()
        for root in roots:
            db_path = self._database_path_for_root(root)
            if native_path_kind(db_path) == "file":
                return db_path.parent
        for root in roots:
            if native_path_kind(root) != "missing":
                return root.parent if root.name == self.DB_NAME else root
        return None

    @property
    def db_path(self) -> Optional[Path]:
        """Return the representative database for direct source probes."""
        return self.db_paths[0] if self.db_paths else None

    @property
    def db_paths(self) -> List[Path]:
        """Return every declared, readable Crush database in priority order."""
        paths: List[Path] = []
        seen: set[str] = set()
        for root in self._candidate_data_dirs():
            db_path = self._database_path_for_root(root)
            try:
                canonical = str(db_path.resolve(strict=False))
            except OSError:
                canonical = str(db_path)
            if canonical in seen or native_path_kind(db_path) != "file":
                continue
            seen.add(canonical)
            paths.append(db_path)
        return paths

    def observed_roots(self) -> List[Path]:
        """Expose every observed database root for manifest-bound snapshots."""
        return [db_path.parent for db_path in self.db_paths]

    def _candidate_data_dirs(self) -> List[Path]:
        """Collect all configured, environment, project, and global roots.

        The first root expresses owner priority only.  It never removes a
        simultaneously valid root from the passive-capture denominator.
        """
        overrides = getattr(self, "_override_data_dirs", None)
        if overrides is not None:
            return self._normalize_root_values(overrides)
        override: Optional[Path] = getattr(self, "_override_data_dir", None)
        if override is not None:
            return self._normalize_root_values([override])

        resolver = get_agent_source_support_manifest().source(self.name).root_resolver
        config = get_config()
        candidates: List[Path] = []
        for key in resolver.get("configuration_keys", []):
            candidates.extend(self._normalize_root_values(config.get(str(key))))
        for environment in resolver.get("environment", []):
            if not isinstance(environment, Mapping):
                continue
            value = os.getenv(str(environment.get("name") or ""))
            candidates.extend(self._normalize_root_values(value))
        candidates.extend(expand_path_templates(resolver.get("standard_paths", [])))

        multi_root = resolver.get("multi_root", {})
        if isinstance(multi_root, Mapping) and multi_root.get("project_ancestor_search"):
            try:
                cwd = Path.cwd().resolve()
            except OSError:
                cwd = Path.cwd()
            for directory in cwd.parents:
                candidates.append(directory / ".crush")

        roots: List[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            try:
                canonical = str(candidate.expanduser().resolve(strict=False))
            except OSError:
                canonical = str(candidate.expanduser())
            if canonical in seen:
                continue
            seen.add(canonical)
            roots.append(candidate.expanduser())
        return roots

    @staticmethod
    def _normalize_root_values(value: Any) -> List[Path]:
        if value is None or value == "":
            return []
        if isinstance(value, (list, tuple)):
            return [Path(item).expanduser() for item in value if str(item).strip()]
        return [Path(value).expanduser()]

    def _database_path_for_root(self, root: Path) -> Path:
        return root if root.name == self.DB_NAME else root / self.DB_NAME

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        """Hybrid 策略：监控 crush.db 目录，30s polling 兜底。"""
        return {
            "type": "hybrid",
            "events": ["modified", "created"],
            "debounce": 3.0,
            "recursive": True,
            "interval": 30,
            "pattern": "crush.db*",
        }

    def _connect(self, db_path: Path) -> sqlite3.Connection:
        """以只读模式连接 SQLite，避免影响 Crush 主进程。"""
        return connect_native_sqlite_readonly(db_path)

    def _table_exists(self, conn: sqlite3.Connection, table: str) -> bool:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        )
        return cur.fetchone() is not None

    # ------------------------------------------------------------------
    # Session discovery
    # ------------------------------------------------------------------

    def discover_sessions(self) -> List[SessionInfo]:
        """Discover sessions from every valid database without cwd selection loss."""
        candidates: List[_CrushSessionCandidate] = []
        for priority, db_path in enumerate(self.db_paths):
            try:
                candidates.extend(self._discover_database_sessions(db_path, priority))
            except (
                NativeSQLiteReadError,
                OSError,
                sqlite3.Error,
            ) as exc:
                raise NativeSourceContractError.from_storage_failure(
                    "native_crush_session_discovery_failed",
                    exc,
                ) from None
            except (ValueError, TypeError):
                raise NativeSourceContractError(
                    "native_crush_session_discovery_failed"
                ) from None
        return self._canonicalize_database_sessions(candidates)

    def _discover_database_sessions(
        self,
        db_path: Path,
        priority: int,
    ) -> List[_CrushSessionCandidate]:
        conn = self._connect(db_path)
        try:
            if not self._table_exists(conn, "sessions"):
                raise NativeSourceContractError(
                    "native_crush_session_schema_missing"
                )
            rows = conn.execute(
                """
                SELECT id, title, parent_session_id, message_count,
                       updated_at, created_at
                FROM sessions
                ORDER BY updated_at DESC, id ASC
                """
            ).fetchall()
            database_id = _database_identity(db_path)
            return [
                _CrushSessionCandidate(
                    db_path=db_path,
                    database_id=database_id,
                    native_session_id=str(session_id),
                    content_hash=self._session_content_hash(conn, str(session_id)),
                    title=str(title or ""),
                    parent_session_id=str(parent_id or ""),
                    message_count=int(message_count or 0),
                    updated_at=updated_at,
                    created_at=created_at,
                    priority=priority,
                )
                for session_id, title, parent_id, message_count, updated_at, created_at in rows
            ]
        finally:
            conn.close()

    def _session_content_hash(self, conn: sqlite3.Connection, session_id: str) -> str:
        """Hash exact session-owned rows for cross-root de-duplication."""
        session_columns = _crush_table_columns(
            conn,
            "sessions",
            required={"id"},
        )
        session_row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        message_columns: List[str] = []
        messages: List[tuple[Any, ...]] = []
        if self._table_exists(conn, "messages"):
            message_columns = _crush_table_columns(
                conn,
                "messages",
                required={"id", "session_id", "created_at"},
            )
            messages = conn.execute(
                """
                SELECT * FROM messages
                WHERE session_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (session_id,),
            ).fetchall()
        read_file_columns: List[str] = []
        read_files: List[tuple[Any, ...]] = []
        if self._table_exists(conn, "read_files"):
            read_file_columns = _crush_table_columns(
                conn,
                "read_files",
                required={"session_id", "path", "read_at"},
            )
            read_files = conn.execute(
                """
                SELECT * FROM read_files
                WHERE session_id = ?
                ORDER BY read_at ASC, path ASC
                """,
                (session_id,),
            ).fetchall()
        payload = json.dumps(
            {
                "session": (
                    _crush_row_mapping(session_columns, session_row)
                    if session_row is not None
                    else None
                ),
                "messages": [
                    _crush_row_mapping(message_columns, row)
                    for row in messages
                ],
                "read_files": [
                    _crush_row_mapping(read_file_columns, row)
                    for row in read_files
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _canonicalize_database_sessions(
        self,
        candidates: List[_CrushSessionCandidate],
    ) -> List[SessionInfo]:
        """Group clone databases by native id+content without dropping conflicts."""
        by_native_id: Dict[str, List[_CrushSessionCandidate]] = {}
        for candidate in candidates:
            by_native_id.setdefault(candidate.native_session_id, []).append(candidate)

        sessions: List[SessionInfo] = []
        for native_session_id, same_native_id in by_native_id.items():
            by_content_hash: Dict[str, List[_CrushSessionCandidate]] = {}
            for candidate in same_native_id:
                by_content_hash.setdefault(candidate.content_hash, []).append(candidate)
            divergent = len(by_content_hash) > 1
            for matching_content in by_content_hash.values():
                representative = min(
                    matching_content,
                    key=lambda item: item.database_id,
                )
                canonical_session_id = native_session_id
                if divergent:
                    canonical_session_id = (
                        f"{native_session_id}::db::"
                        f"{representative.database_id.removeprefix('crush-db-')}"
                    )
                metadata: Dict[str, Any] = {
                    "title": representative.title,
                    "message_count": representative.message_count,
                    "created_at": _ts_to_iso(representative.created_at),
                    "updated_at": _ts_to_iso(representative.updated_at),
                    "native_session_id": native_session_id,
                    "native_session_content_hash": representative.content_hash,
                    "source_database_id": representative.database_id,
                    "source_database_ids": sorted(
                        item.database_id for item in matching_content
                    ),
                    "source_database_count": len(matching_content),
                    "canonical_identity_mode": (
                        "native_session_id" if not divergent else "divergent_database"
                    ),
                    "canonical_representative_policy": "min_database_id",
                }
                if representative.parent_session_id:
                    metadata["parent_session_id"] = representative.parent_session_id
                sessions.append(
                    SessionInfo(
                        session_id=canonical_session_id,
                        source_path=representative.db_path,
                        working_dir=str(representative.db_path.parent),
                        mtime=max(item.mtime for item in matching_content),
                        canonical_session_id=canonical_session_id,
                        session_aliases=[native_session_id],
                        source_kind="sqlite",
                        metadata=metadata,
                        source_paths=sorted(
                            {item.db_path for item in matching_content},
                            key=lambda path: _database_identity(path),
                        ),
                    )
                )
        sessions.sort(key=lambda item: (-(item.mtime or 0.0), item.session_id))
        return sessions

    # ------------------------------------------------------------------
    # Turn parsing
    # ------------------------------------------------------------------

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """Implement the AgentSource direct-path parser for a Crush database."""
        db_path = session_path if session_path.name == self.DB_NAME else self.db_path
        if db_path is None or native_path_kind(db_path) != "file":
            raise NativeSourceContractError(
                "native_crush_database_missing"
            )
        try:
            native_session_id = self._latest_session_id(db_path)
        except (OSError, ValueError, TypeError, sqlite3.Error):
            raise NativeSourceContractError(
                "native_crush_latest_session_failed"
            ) from None
        return self._parse_database_session(db_path, native_session_id)

    def parse_session(self, session_info: SessionInfo) -> List[Turn]:
        """Parse a discovered database session without mutable queue ordering."""
        metadata = session_info.metadata or {}
        native_session_id = str(metadata.get("native_session_id") or session_info.session_id)
        return self._parse_database_session(session_info.source_path, native_session_id)

    def native_artifact_paths(self, session_info: SessionInfo) -> List[Path]:
        """Declare every exact clone database represented by one session."""

        paths = list(session_info.source_paths or [session_info.source_path])
        if not paths or any(native_path_kind(path) != "file" for path in paths):
            raise NativeSourceContractError(
                "native_crush_artifact_set_incomplete"
            )
        return sorted(
            dict.fromkeys(paths),
            key=lambda path: _database_identity(path),
        )

    def session_artifact_evidence_hash(
        self,
        session_info: SessionInfo,
    ) -> str:
        """Hash only the exact native rows consumed by one Crush session."""

        native_session_id = str(
            (session_info.metadata or {}).get("native_session_id")
            or session_info.session_id
        )
        if not native_session_id:
            raise NativeSourceContractError(
                "native_crush_session_identity_invalid"
            )
        evidence: List[Dict[str, Any]] = []
        try:
            for database in self.native_artifact_paths(session_info):
                connection = self._connect(database)
                try:
                    connection.execute("BEGIN")
                    session_columns = _crush_table_columns(
                        connection,
                        "sessions",
                        required={"id"},
                    )
                    message_columns = _crush_table_columns(
                        connection,
                        "messages",
                        required={"id", "session_id", "created_at"},
                    )
                    session_row = connection.execute(
                        "SELECT * FROM sessions WHERE id = ?",
                        (native_session_id,),
                    ).fetchone()
                    message_rows = connection.execute(
                        """
                        SELECT * FROM messages
                        WHERE session_id = ?
                        ORDER BY created_at ASC, id ASC
                        """,
                        (native_session_id,),
                    ).fetchall()
                    if self._table_exists(connection, "read_files"):
                        read_file_columns = _crush_table_columns(
                            connection,
                            "read_files",
                            required={"session_id", "path", "read_at"},
                        )
                        read_file_rows = connection.execute(
                            """
                            SELECT * FROM read_files
                            WHERE session_id = ?
                            ORDER BY read_at ASC, path ASC
                            """,
                            (native_session_id,),
                        ).fetchall()
                    else:
                        read_file_columns = []
                        read_file_rows = []
                finally:
                    connection.close()
                if session_row is None:
                    raise NativeSourceContractError(
                        "native_crush_session_missing"
                    )
                evidence.append(
                    {
                        "database_id": _database_identity(database),
                        "session": _crush_row_mapping(
                            session_columns,
                            session_row,
                        ),
                        "messages": [
                            _crush_row_mapping(message_columns, row)
                            for row in message_rows
                        ],
                        "read_files": [
                            _crush_row_mapping(read_file_columns, row)
                            for row in read_file_rows
                        ],
                    }
                )
        except NativeSourceContractError:
            raise
        except (NativeSQLiteReadError, OSError, sqlite3.Error) as exc:
            raise NativeSourceContractError.from_storage_failure(
                "native_crush_artifact_evidence_failed",
                exc,
            ) from None
        payload = json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def _latest_session_id(self, db_path: Path) -> str:
        conn = self._connect(db_path)
        try:
            if not self._table_exists(conn, "sessions"):
                return ""
            row = conn.execute(
                "SELECT id FROM sessions ORDER BY updated_at DESC, id ASC LIMIT 1"
            ).fetchone()
            return str(row[0]) if row else ""
        finally:
            conn.close()

    def _parse_database_session(self, db_path: Path, session_id: str) -> List[Turn]:
        if not session_id or native_path_kind(db_path) != "file":
            raise NativeSourceContractError(
                "native_crush_session_identity_or_database_missing"
            )
        try:
            conn = self._connect(db_path)
        except (NativeSQLiteReadError, OSError, sqlite3.Error) as exc:
            raise NativeSourceContractError.from_storage_failure(
                "native_crush_database_open_failed",
                exc,
            ) from None
        except ValueError:
            raise NativeSourceContractError(
                "native_crush_database_open_failed"
            ) from None

        try:
            if not self._table_exists(conn, "messages"):
                raise NativeSourceContractError(
                    "native_crush_message_schema_missing"
                )
            session_columns = _crush_table_columns(
                conn,
                "sessions",
                required={"id"},
            )
            message_columns = _crush_table_columns(
                conn,
                "messages",
                required={
                    "id",
                    "session_id",
                    "role",
                    "parts",
                    "model",
                    "created_at",
                    "finished_at",
                },
            )
            session_row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE session_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (session_id,),
            ).fetchall()
            if session_row is None:
                raise NativeSourceContractError(
                    "native_crush_session_missing"
                )
            messages: List[Dict[str, Any]] = []
            for row in rows:
                native_message_row = _crush_row_mapping(
                    message_columns,
                    row,
                )
                msg_id = native_message_row["id"]
                role = native_message_row["role"]
                parts_json = row[message_columns.index("parts")]
                model = native_message_row["model"]
                created_at = native_message_row["created_at"]
                finished_at = native_message_row["finished_at"]
                if isinstance(parts_json, bytes):
                    try:
                        raw_parts_json = parts_json.decode("utf-8")
                    except UnicodeDecodeError:
                        decoded_parts = None
                        decode_error = "invalid_utf8"
                        opaque_data = {
                            "raw_base64": base64.b64encode(parts_json).decode("ascii"),
                            "raw_encoding": "base64",
                            "decode_error": decode_error,
                        }
                    else:
                        decode_error = ""
                        opaque_data = {}
                else:
                    raw_parts_json = str(parts_json or "[]")
                    decode_error = ""
                    opaque_data = {}
                if not decode_error:
                    try:
                        decoded_parts = json.loads(raw_parts_json)
                    except json.JSONDecodeError:
                        decoded_parts = None
                        decode_error = "invalid_json"
                        opaque_data = {
                            "raw": raw_parts_json,
                            "decode_error": decode_error,
                        }
                if not decode_error:
                    if not isinstance(decoded_parts, list):
                        decode_error = "non_array_json"
                        opaque_data = {
                            "raw": raw_parts_json,
                            "decode_error": decode_error,
                        }
                if decode_error:
                    parts = [
                        {
                            "type": "raw_parts_json",
                            "data": opaque_data,
                        }
                    ]
                else:
                    parts = (
                        decoded_parts
                        if isinstance(decoded_parts, list)
                        else []
                    )
                messages.append(
                    {
                        "id": msg_id,
                        "role": role,
                        "parts": parts,
                        "model": model,
                        "created_at": created_at,
                        "finished_at": finished_at,
                        "_raw_native_message": native_message_row,
                    }
                )
            native_read_file_records: List[Dict[str, Any]] = []
            if self._table_exists(conn, "read_files"):
                read_file_columns = _crush_table_columns(
                    conn,
                    "read_files",
                    required={"session_id", "path", "read_at"},
                )
                native_read_file_records = [
                    _crush_row_mapping(read_file_columns, row)
                    for row in conn.execute(
                        """
                        SELECT * FROM read_files
                        WHERE session_id = ?
                        ORDER BY read_at ASC, path ASC
                        """,
                        (session_id,),
                    ).fetchall()
                ]
            read_files = [
                str(record["path"])
                for record in native_read_file_records
                if record.get("path")
            ]
            if not messages:
                messages.append(
                    {
                        "id": "",
                        "role": "",
                        "parts": [],
                        "model": "",
                        "created_at": None,
                        "finished_at": None,
                        "_raw_native_message": {
                            "event_type": "metadata_only_session"
                        },
                    }
                )
            return self._messages_to_turns(
                messages,
                read_files,
                db_path,
                native_session_record=_crush_row_mapping(
                    session_columns,
                    session_row,
                ),
                native_read_file_records=native_read_file_records,
            )
        except (OSError, sqlite3.Error) as exc:
            raise NativeSourceContractError.from_storage_failure(
                "native_crush_message_query_failed",
                exc,
            ) from None
        except (ValueError, TypeError):
            raise NativeSourceContractError(
                "native_crush_message_query_failed"
            ) from None
        finally:
            conn.close()

    def _messages_to_turns(
        self,
        messages: List[Dict[str, Any]],
        read_files: List[str],
        db_path: Path,
        *,
        native_session_record: Optional[Dict[str, Any]] = None,
        native_read_file_records: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Turn]:
        """将 Crush message + parts 聚合为 Turn 列表。"""
        turns: List[Turn] = []
        user_content = ""
        assistant_content = ""
        turn_metadata: Dict[str, Any] = {}
        turn_number = 0

        def ensure_turn_metadata(msg: Mapping[str, Any], ts_iso: Optional[str]) -> None:
            if not turn_metadata:
                turn_metadata.update(
                    {
                        "source": "crush",
                        "timestamp": ts_iso,
                        "model": msg.get("model") or "",
                    }
                )
                if read_files:
                    turn_metadata["read_files"] = read_files
            elif ts_iso and not turn_metadata.get("timestamp"):
                turn_metadata["timestamp"] = ts_iso

        for message_index, msg in enumerate(messages):
            role = str(msg.get("role") or "").strip().lower()
            parts = msg.get("parts") or []
            if not isinstance(parts, list):
                parts = []

            text = _extract_text(parts)
            tool_calls = _extract_tool_calls(parts)
            tool_results = _extract_tool_results(parts)
            ts_iso = _ts_to_iso(msg.get("created_at"))
            unhandled_parts = [
                part
                for part in parts
                if not _part_is_losslessly_normalized(role, part)
            ]
            native_refs: List[Dict[str, Any]] = [
                {
                    "event_type": "native_message",
                    "raw": msg.get("_raw_native_message", msg),
                }
            ]
            if message_index == 0 and native_session_record is not None:
                native_refs.append(
                    {
                        "event_type": "native_session",
                        "raw": native_session_record,
                    }
                )
                native_refs.extend(
                    {
                        "event_type": "native_read_file",
                        "raw": record,
                    }
                    for record in (native_read_file_records or [])
                )

            if role == "user":
                if user_content or assistant_content or turn_metadata:
                    turns.append(
                        self._build_turn(
                            turn_number,
                            user_content,
                            assistant_content,
                            turn_metadata,
                            read_files,
                            db_path,
                        )
                    )
                    turn_number += 1
                user_content = text
                assistant_content = ""
                turn_metadata = {}
                ensure_turn_metadata(msg, ts_iso)
                turn_metadata.setdefault("raw_event_refs", []).extend(
                    native_refs
                )
                if tool_calls:
                    turn_metadata.setdefault("tool_calls", []).extend(tool_calls)
                if tool_results:
                    turn_metadata.setdefault("tool_results", []).extend(tool_results)
                if unhandled_parts:
                    turn_metadata.setdefault("raw_event_refs", []).append(
                        {
                            "message_id": str(msg.get("id") or ""),
                            "role": role,
                            "parts": unhandled_parts,
                        }
                    )
            elif role == "assistant":
                ensure_turn_metadata(msg, ts_iso)
                turn_metadata.setdefault("raw_event_refs", []).extend(
                    native_refs
                )
                if text:
                    assistant_content = (
                        f"{assistant_content}\n{text}"
                        if assistant_content
                        else text
                    )
                if tool_calls:
                    turn_metadata.setdefault("tool_calls", []).extend(tool_calls)
                if tool_results:
                    turn_metadata.setdefault("tool_results", []).extend(tool_results)
                if unhandled_parts:
                    turn_metadata.setdefault("raw_event_refs", []).append(
                        {
                            "message_id": str(msg.get("id") or ""),
                            "role": role,
                            "parts": unhandled_parts,
                        }
                    )
            elif role == "tool":
                ensure_turn_metadata(msg, ts_iso)
                turn_metadata.setdefault("raw_event_refs", []).extend(
                    native_refs
                )
                if tool_results:
                    turn_metadata.setdefault("tool_results", []).extend(tool_results)
                if unhandled_parts:
                    turn_metadata.setdefault("raw_event_refs", []).append(
                        {
                            "message_id": str(msg.get("id") or ""),
                            "role": role,
                            "parts": unhandled_parts,
                        }
                    )
            else:
                # 其他角色保留到 raw_event_refs
                ensure_turn_metadata(msg, ts_iso)
                turn_metadata.setdefault("raw_event_refs", []).extend(
                    native_refs
                )
                turn_metadata.setdefault("raw_event_refs", []).append(
                    {"role": role, "parts": parts}
                )

        if user_content or assistant_content or turn_metadata:
            turns.append(
                self._build_turn(
                    turn_number,
                    user_content,
                    assistant_content,
                    turn_metadata,
                    read_files,
                    db_path,
                )
            )

        return turns

    def _build_turn(
        self,
        turn_number: int,
        user_content: str,
        assistant_content: str,
        metadata: Dict[str, Any],
        read_files: List[str],
        db_path: Path,
    ) -> Turn:
        tool_calls = metadata.pop("tool_calls", []) if isinstance(metadata, dict) else []
        tool_results = (
            metadata.pop("tool_results", []) if isinstance(metadata, dict) else []
        )
        attachments = [{"type": "read_file", "path": path} for path in read_files]
        return Turn(
            turn_number=turn_number,
            user_content=user_content,
            assistant_content=assistant_content,
            timestamp=metadata.get("timestamp") if isinstance(metadata, dict) else None,
            metadata=metadata,
            tool_calls=tool_calls,
            tool_results=tool_results,
            attachments=attachments,
            source_files=[str(db_path), *read_files],
            raw_event_refs=metadata.get("raw_event_refs", [])
            if isinstance(metadata, dict)
            else [],
            completeness={
                "visible_text": "full",
                "tool_calls": "full" if tool_calls else "unavailable",
                "tool_results": "full" if tool_results else "unavailable",
                "reasoning": "unavailable",
                "attachments": "full" if attachments else "unavailable",
                "truncated": False,
                "loss_reasons": [],
            },
        )

    # ------------------------------------------------------------------
    # Incremental sync state
    # ------------------------------------------------------------------

    def get_session_state(self, session_info: SessionInfo) -> Optional[Dict[str, Any]]:
        """返回 session 聚合状态，用于增量同步判断。"""
        db_path = session_info.source_path
        if native_path_kind(db_path) != "file":
            raise NativeSourceContractError(
                "native_crush_session_artifact_missing"
            )
        native_session_id = str(
            (session_info.metadata or {}).get("native_session_id") or session_info.session_id
        )
        fingerprint = self.session_artifact_evidence_hash(session_info)

        try:
            conn = self._connect(db_path)
        except (
            NativeSQLiteReadError,
            OSError,
            sqlite3.Error,
        ) as exc:
            raise NativeSourceContractError.from_storage_failure(
                "native_crush_session_state_failed",
                exc,
            ) from None
        except (
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            raise NativeSourceContractError(
                "native_crush_session_state_failed"
            ) from None

        try:
            row = conn.execute(
                "SELECT updated_at, created_at FROM sessions WHERE id = ?",
                (native_session_id,),
            ).fetchone()
            mtime = _ts_to_seconds(row[0] or row[1]) if row else 0

            row = conn.execute(
                """
                SELECT COUNT(*), COALESCE(MAX(updated_at), 0)
                FROM messages
                WHERE session_id = ?
                """,
                (native_session_id,),
            ).fetchone()
            msg_count, last_msg_ts = row
            last_msg_ts = last_msg_ts or 0

            return {
                "mtime": max(mtime, last_msg_ts / 1000.0),
                "size": int(msg_count),
                "file_count": int(msg_count) or 1,
                "fingerprint": fingerprint,
                "fingerprint_contract": "crush-exact-session-rows-sha256-v1",
            }
        except (OSError, sqlite3.Error) as exc:
            raise NativeSourceContractError.from_storage_failure(
                "native_crush_session_state_failed",
                exc,
            ) from None
        except (ValueError, TypeError):
            raise NativeSourceContractError(
                "native_crush_session_state_failed"
            ) from None
        finally:
            conn.close()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _database_identity(db_path: Path) -> str:
    """Return an opaque stable identity for one configured local database path."""
    try:
        canonical_path = str(db_path.resolve(strict=False))
    except OSError:
        canonical_path = str(db_path)
    digest = hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()[:24]
    return f"crush-db-{digest}"


def _ts_to_seconds(ts: Optional[int]) -> float:
    """把 Crush 时间戳统一成秒。

    表中注释称是毫秒，但实际观察到的值是 Unix 秒；这里做兼容处理：
    - 大于等于 1e10 视为毫秒
    - 否则视为秒
    """
    if not ts:
        return 0.0
    if ts >= 10_000_000_000:  # milliseconds
        return ts / 1000.0
    return float(ts)


def _ts_to_iso(ts: Optional[int]) -> Optional[str]:
    if not ts:
        return None
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(_ts_to_seconds(ts), tz=timezone.utc).isoformat()
    # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
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
        return None


def _extract_text(parts: List[Dict[str, Any]]) -> str:
    texts: List[str] = []
    for p in parts:
        if not isinstance(p, dict) or p.get("type") != "text":
            continue
        data = p.get("data") or {}
        if isinstance(data, dict):
            texts.append(str(data.get("text", "")))
    return "".join(texts)


def _part_is_losslessly_normalized(role: str, part: Any) -> bool:
    """Return true only when the normalized fields consume the complete part."""
    if not isinstance(part, dict) or set(part) - {"type", "data"}:
        return False
    part_type = str(part.get("type") or "")
    data = part.get("data")
    if not isinstance(data, dict):
        return False
    if part_type == "text" and role in {"user", "assistant"}:
        return (
            not set(data) - {"text"}
            and "text" in data
            and isinstance(data["text"], str)
        )
    if part_type == "tool_call" and role in {"user", "assistant"}:
        return (
            not set(data) - {"id", "name", "input"}
            and isinstance(data.get("id"), str)
            and isinstance(data.get("name"), str)
            and isinstance(data.get("input"), dict)
        )
    if part_type == "tool_result" and role in {"user", "assistant", "tool"}:
        allowed = {
            "tool_call_id",
            "name",
            "content",
            "data",
            "mime_type",
            "metadata",
            "is_error",
        }
        string_fields = ("tool_call_id", "name", "content", "mime_type")
        return (
            not set(data) - allowed
            and all(
                key not in data or isinstance(data[key], str)
                for key in string_fields
            )
            and (
                "metadata" not in data or isinstance(data["metadata"], dict)
            )
            and (
                "is_error" not in data or isinstance(data["is_error"], bool)
            )
        )
    return False


def _extract_tool_calls(parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """提取 assistant 消息中的 tool_call。"""
    calls: List[Dict[str, Any]] = []
    for p in parts:
        if not isinstance(p, dict) or p.get("type") != "tool_call":
            continue
        data = p.get("data") or {}
        if not isinstance(data, dict):
            continue
        arguments = data.get("input") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"raw": arguments}
        calls.append(
            {
                "id": data.get("id", ""),
                "name": data.get("name", ""),
                "arguments": arguments,
            }
        )
    return calls


def _extract_tool_results(parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """提取 tool 消息中的 tool_result。"""
    results: List[Dict[str, Any]] = []
    for p in parts:
        if not isinstance(p, dict) or p.get("type") != "tool_result":
            continue
        data = p.get("data") or {}
        if not isinstance(data, dict):
            continue
        metadata = data.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {"raw": metadata}
        results.append(
            {
                "id": data.get("tool_call_id", ""),
                "name": data.get("name", ""),
                "output": data.get("content", ""),
                "data": data.get("data", ""),
                "mime_type": data.get("mime_type", ""),
                "metadata": metadata,
                "is_error": bool(data.get("is_error", False)),
            }
        )
    return results
