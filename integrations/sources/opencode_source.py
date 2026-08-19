"""
OpenCodeSource — OpenCode Agent 被动同步插件

OpenCode 将会话数据存储在本地 SQLite 数据库中：

    ~/.local/share/opencode/opencode.db

核心表：
- session:   会话元数据（id, title, directory, time_created, time_updated, agent, model, ...）
- message:   消息元数据（id, session_id, time_created, data JSON: {role, model, summary, ...}）
- part:      消息内容片段（id, message_id, session_id, data JSON: {type, text, tool, reasoning, ...}）

本插件直接以只读方式连接该数据库，按 session 聚合 message + part，
还原为 Turn 列表后送入 SyncEngine。触发策略使用 watchdog 监控数据目录 +
polling 兜底，保证 OpenCode 聊天记录尽可能实时、全量进入 raw vault。
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.sync_framework.agent_source import (
    NativeSourceContractError,
    SessionInfo,
    Turn,
)
from core.ops.durable_io import read_native_bytes
from core.sync_framework.native_sqlite import (
    NativeSQLiteReadError,
    connect_native_sqlite_readonly,
)
from core.runtime_environment import environment_get

from integrations.sources.base import (
    BaseAgentSource,
    attach_native_container_residual,
    native_path_kind,
    stable_path_session_id,
)


def _sqlite_json_value(value: Any) -> Any:
    """Preserve SQLite scalar type and exact bytes in JSON-safe form."""

    if isinstance(value, bytes):
        return {
            "_mnemos_sqlite_type": "blob",
            "base64": base64.b64encode(value).decode("ascii"),
        }
    return value


def _row_mapping(columns: List[str], row: Any) -> Dict[str, Any]:
    return {
        column: _sqlite_json_value(value)
        for column, value in zip(columns, row, strict=True)
    }


def _table_columns(
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
            f"native_opencode_{table}_schema_incomplete"
        )
    return columns


class OpenCodeSource(BaseAgentSource):
    """OpenCode 被动数据源插件 — 直连 SQLite。"""

    # OpenCode 数据库文件名
    DB_NAME = "opencode.db"

    _cap_tool_calls = True
    _cap_tool_results = True
    _cap_reasoning = True
    _cap_attachments = False
    _cap_source_fidelity = "full"
    _cap_memory_scope = "opencode_db_sessions"
    _cap_host_memory_default = "host_dependent_unknown"
    _cap_host_memory_effect = (
        "host memory may affect prompts; opencode.db remains the canonical passive capture source"
    )
    _cap_transcript_kind = "native_sqlite_message_part_tables"
    _cap_compression = "raw_sqlite_parts_no_mnemos_compression"

    _default_extra_tags = ["source=opencode"]

    @property
    def name(self) -> str:
        return "opencode"

    @property
    def model_tag(self) -> str:
        return "opencode"

    @property
    def data_dir(self) -> Optional[Path]:
        """返回 OpenCode 数据目录，不执行外部 CLI 探针。"""
        configured_db = environment_get("OPENCODE_DB_PATH")
        if configured_db:
            configured_path = Path(configured_db).expanduser()
            if native_path_kind(configured_path) == "file":
                return configured_path.parent

        official = Path.home() / ".local" / "share" / "opencode"
        alts = [
            Path.home() / ".config" / "opencode",
            Path.home() / ".opencode",
        ]
        # Prefer directory where DB_NAME actually exists.
        for p in [official] + alts:
            if native_path_kind(p / self.DB_NAME) != "missing":
                return p
        return self._resolve_data_dir([official] + alts)

    @property
    def db_path(self) -> Optional[Path]:
        """返回 SQLite 数据库路径。"""
        base = self.data_dir
        if base is None:
            return None
        return base / self.DB_NAME

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        """
        使用 hybrid 策略：
        - watchdog 监控 opencode.db 及相关 WAL 文件所在目录，变化即时触发。
        - polling 30s 兜底，防止 watchdog 漏报或数据库快速连续写入。
        """
        return {
            "type": "hybrid",
            "events": ["modified", "created"],
            "debounce": 3.0,
            "recursive": True,
            "interval": 30,
            "pattern": "opencode.db*",
        }

    def _connect(self, db_path: Path):
        """以只读模式连接 SQLite，避免影响 OpenCode 主进程。"""
        return connect_native_sqlite_readonly(db_path)

    def _table_exists(self, conn: sqlite3.Connection, table: str) -> bool:
        cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
        return cur.fetchone() is not None

    def discover_sessions(self) -> List[SessionInfo]:
        """发现 OpenCode 本地可同步的会话。

        SQLite and standalone JSON are both declared OpenCode source formats;
        one format never removes the other from the native denominator.
        """
        sessions: List[SessionInfo] = []
        db_path = self.db_path
        if db_path is not None and native_path_kind(db_path) != "missing":
            sessions.extend(self._discover_from_sqlite(db_path))
        sessions.extend(self._discover_from_json_files())
        sessions.sort(
            key=lambda item: (-(item.mtime or 0.0), item.session_id)
        )
        return sessions

    def _discover_from_sqlite(self, db_path: Path) -> List[SessionInfo]:
        """从 SQLite session 表发现所有可同步的会话。"""
        try:
            conn = self._connect(db_path)
        except (NativeSQLiteReadError, OSError, sqlite3.Error) as exc:
            raise NativeSourceContractError.from_storage_failure(
                "native_opencode_database_open_failed",
                exc,
            ) from None

        try:
            if not self._table_exists(conn, "session"):
                raise NativeSourceContractError(
                    "native_opencode_session_schema_missing"
                )

            sessions: List[SessionInfo] = []
            rows = conn.execute("""
                SELECT id, title, directory, time_created, time_updated
                FROM session
                ORDER BY time_updated DESC
                """).fetchall()

            for sid, title, directory, time_created, time_updated in rows:
                mtime = (time_updated or time_created or 0) / 1000.0
                sessions.append(
                    SessionInfo(
                        session_id=str(sid),
                        source_path=db_path,
                        working_dir=directory or "",
                        mtime=mtime,
                        canonical_session_id=str(sid),
                        session_aliases=[str(sid)],
                        source_kind="sqlite",
                        metadata={"native_session_id": str(sid)},
                    )
                )
            return sessions
        except (OSError, sqlite3.Error) as exc:
            raise NativeSourceContractError.from_storage_failure(
                "native_opencode_session_discovery_failed",
                exc,
            ) from None
        except (ValueError, TypeError):
            raise NativeSourceContractError(
                "native_opencode_session_discovery_failed"
            ) from None
        finally:
            conn.close()

    def _discover_from_json_files(self) -> List[SessionInfo]:
        """旧版兼容：扫描 data_dir 下的 JSON 会话文件。"""
        base = self.data_dir
        if base is None:
            return []

        sessions: List[SessionInfo] = []
        try:
            search_paths = [
                base / "sessions",
                base / "history",
                base / "chats",
                base / "logs",
                base / "mnemos_tasks",
            ]

            for sp in search_paths:
                if native_path_kind(sp) == "missing":
                    continue
                for json_file in sp.rglob("*.json"):
                    if json_file.name.startswith("."):
                        continue
                    canonical_id = stable_path_session_id(
                        "opencode",
                        base,
                        json_file,
                        native_id=json_file.stem,
                    )
                    sessions.append(
                        SessionInfo(
                            session_id=json_file.stem,
                            source_path=json_file,
                            working_dir=str(json_file.parent),
                            mtime=json_file.stat().st_mtime,
                            canonical_session_id=canonical_id,
                            session_aliases=[json_file.stem, json_file.name],
                            source_kind="json_fallback",
                        )
                    )

            config_files = {"opencode.json", "settings.json", "package.json"}
            for json_file in base.glob("*.json"):
                if (
                    json_file.name.startswith(".")
                    or json_file.name.lower() in config_files
                ):
                    continue
                canonical_id = stable_path_session_id(
                    "opencode",
                    base,
                    json_file,
                    native_id=json_file.stem,
                )
                sessions.append(
                    SessionInfo(
                        session_id=json_file.stem,
                        source_path=json_file,
                        working_dir=str(base),
                        mtime=json_file.stat().st_mtime,
                        canonical_session_id=canonical_id,
                        session_aliases=[json_file.stem, json_file.name],
                        source_kind="json_fallback",
                    )
                )
            return sessions
        except OSError:
            raise NativeSourceContractError(
                "native_opencode_json_discovery_failed"
            ) from None

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析 OpenCode 单个会话的所有消息为 Turn 列表。

        SQLite 数据库承载多个会话，单凭物理路径不能安全确定 native
        session identity。生产调用必须使用 ``parse_session(SessionInfo)``；
        这里对 SQLite 输入 fail closed，避免调用顺序把另一个会话的正文
        归到当前 session。JSON fallback 仍可保持原有直接解析能力。
        """
        if native_path_kind(session_path) == "file" and session_path.suffix == ".db":
            raise NativeSourceContractError(
                "native_opencode_session_identity_required"
            )
        return self._parse_turns_from_json(session_path)

    def parse_session(self, session_info: SessionInfo) -> List[Turn]:
        """Parse the exact SQLite session identity returned by discovery."""
        if session_info.source_kind == "sqlite" or session_info.source_path.suffix == ".db":
            native_session_id = str(
                (session_info.metadata or {}).get("native_session_id")
                or session_info.session_id
            )
            if not native_session_id:
                raise NativeSourceContractError(
                    "native_opencode_session_identity_invalid"
                )
            return self._parse_turns_from_sqlite(
                session_info.source_path,
                native_session_id,
            )
        return self._parse_turns_from_json(session_info.source_path)

    def session_artifact_evidence_hash(
        self,
        session_info: SessionInfo,
    ) -> str:
        """Hash only the exact native session rows consumed by parsing."""

        if session_info.source_kind != "sqlite" and session_info.source_path.suffix != ".db":
            try:
                payload = read_native_bytes(session_info.source_path)
            except OSError:
                raise NativeSourceContractError(
                    "native_opencode_artifact_evidence_failed"
                ) from None
            return "sha256:" + hashlib.sha256(payload).hexdigest()
        native_session_id = str(
            (session_info.metadata or {}).get("native_session_id")
            or session_info.session_id
        )
        if not native_session_id:
            raise NativeSourceContractError(
                "native_opencode_session_identity_invalid"
            )
        try:
            connection = self._connect(session_info.source_path)
            try:
                connection.execute("BEGIN")
                session_columns = _table_columns(
                    connection,
                    "session",
                    required={"id"},
                )
                message_columns = _table_columns(
                    connection,
                    "message",
                    required={
                        "id",
                        "session_id",
                        "time_created",
                        "time_updated",
                        "data",
                    },
                )
                part_columns = _table_columns(
                    connection,
                    "part",
                    required={
                        "id",
                        "message_id",
                        "session_id",
                        "time_created",
                        "data",
                    },
                )
                session_row = connection.execute(
                    "SELECT * FROM session WHERE id=?",
                    (native_session_id,),
                ).fetchone()
                message_rows = connection.execute(
                    """
                    SELECT * FROM message WHERE session_id=?
                    ORDER BY time_created, id
                    """,
                    (native_session_id,),
                ).fetchall()
                part_rows_with_owner = connection.execute(
                    """
                    SELECT p.*, m.session_id
                    FROM part p
                    LEFT JOIN message m ON m.id = p.message_id
                    WHERE p.session_id=? OR m.session_id=?
                    ORDER BY p.time_created, p.id
                    """,
                    (native_session_id, native_session_id),
                ).fetchall()
            finally:
                connection.close()
        except (NativeSQLiteReadError, OSError, sqlite3.Error) as exc:
            raise NativeSourceContractError.from_storage_failure(
                "native_opencode_artifact_evidence_failed",
                exc,
            ) from None
        if session_row is None:
            raise NativeSourceContractError(
                "native_opencode_session_missing"
            )
        if any(
            str(row[part_columns.index("session_id")]) != native_session_id
            or str(row[-1] or "") != native_session_id
            for row in part_rows_with_owner
        ):
            raise NativeSourceContractError(
                "native_opencode_part_session_identity_conflict"
            )
        payload = json.dumps(
            {
                "session": _row_mapping(session_columns, session_row),
                "messages": [
                    _row_mapping(message_columns, row)
                    for row in message_rows
                ],
                "parts": [
                    _row_mapping(part_columns, row[:-1])
                    for row in part_rows_with_owner
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def _parse_turns_from_sqlite(self, db_path: Path, session_id: str) -> List[Turn]:
        """Read one explicit native session from the discovered SQLite file."""
        if native_path_kind(db_path) == "missing":
            raise NativeSourceContractError(
                "native_opencode_database_missing"
            )

        try:
            conn = self._connect(db_path)
        except (NativeSQLiteReadError, OSError, sqlite3.Error) as exc:
            raise NativeSourceContractError.from_storage_failure(
                "native_opencode_database_open_failed",
                exc,
            ) from None

        try:
            if not (self._table_exists(conn, "message") and self._table_exists(conn, "part")):
                raise NativeSourceContractError(
                    "native_opencode_message_schema_missing"
                )
            session_columns = _table_columns(
                conn,
                "session",
                required={"id"},
            )
            message_columns = _table_columns(
                conn,
                "message",
                required={"id", "session_id", "time_created", "data"},
            )
            part_columns = _table_columns(
                conn,
                "part",
                required={
                    "id",
                    "message_id",
                    "session_id",
                    "time_created",
                    "data",
                },
            )
            session_row = conn.execute(
                "SELECT * FROM session WHERE id=?",
                (session_id,),
            ).fetchone()
            message_rows = conn.execute(
                """
                SELECT * FROM message
                WHERE session_id = ?
                ORDER BY time_created ASC, id ASC
                """,
                (session_id,),
            ).fetchall()
            part_rows_with_owner = conn.execute(
                """
                SELECT p.*, m.session_id
                FROM part p
                LEFT JOIN message m ON m.id = p.message_id
                WHERE p.session_id=? OR m.session_id=?
                ORDER BY p.time_created ASC, p.id ASC
                """,
                (session_id, session_id),
            ).fetchall()
        except sqlite3.Error as exc:
            raise NativeSourceContractError.from_storage_failure(
                "native_opencode_message_query_failed",
                exc,
            ) from None
        finally:
            conn.close()

        if session_row is None:
            raise NativeSourceContractError(
                "native_opencode_session_missing"
            )
        part_session_index = part_columns.index("session_id")
        if any(
            str(row[part_session_index]) != session_id
            or str(row[-1] or "") != session_id
            for row in part_rows_with_owner
        ):
            raise NativeSourceContractError(
                "native_opencode_part_session_identity_conflict"
            )

        session_record = _row_mapping(session_columns, session_row)
        part_message_index = part_columns.index("message_id")
        parts_by_message: Dict[str, List[Dict[str, Any]]] = {}
        for row in part_rows_with_owner:
            native_row = _row_mapping(part_columns, row[:-1])
            raw_part_data = native_row.get("data")
            if isinstance(raw_part_data, str):
                try:
                    decoded_part_data: Any = json.loads(raw_part_data)
                except json.JSONDecodeError:
                    decoded_part_data = {
                        "type": "raw_native_part",
                        "raw": raw_part_data,
                        "decode_error": "invalid_json",
                    }
            else:
                decoded_part_data = {
                    "type": "raw_native_part",
                    "raw": raw_part_data,
                    "decode_error": "non_text_json",
                }
            part = (
                dict(decoded_part_data)
                if isinstance(decoded_part_data, dict)
                else {
                    "type": "raw_native_part",
                    "raw": decoded_part_data,
                    "decode_error": "non_object_part",
                }
            )
            part["_raw_native_part"] = native_row
            message_id = str(row[part_message_index])
            parts_by_message.setdefault(message_id, []).append(part)

        messages: List[Dict[str, Any]] = []
        message_id_index = message_columns.index("id")
        message_time_index = message_columns.index("time_created")
        message_data_index = message_columns.index("data")
        for index, row in enumerate(message_rows):
            native_message_row = _row_mapping(message_columns, row)
            raw_message_data = row[message_data_index]
            if isinstance(raw_message_data, str):
                try:
                    decoded_message_data = json.loads(raw_message_data)
                except json.JSONDecodeError:
                    decoded_message_data = {
                        "raw": raw_message_data,
                        "decode_error": "invalid_json",
                    }
            else:
                decoded_message_data = {
                    "raw": _sqlite_json_value(raw_message_data),
                    "decode_error": "non_text_json",
                }
            role = (
                str(decoded_message_data.get("role") or "")
                if isinstance(decoded_message_data, dict)
                else ""
            )
            message_id = str(row[message_id_index])
            message = {
                "id": message_id,
                "role": role,
                "time_created": row[message_time_index],
                "parts": parts_by_message.get(message_id, []),
                "_raw_native_message": native_message_row,
            }
            if index == 0:
                message["_raw_native_session"] = session_record
            messages.append(message)
        if not messages:
            messages.append(
                {
                    "id": "",
                    "role": "",
                    "time_created": None,
                    "parts": [],
                    "_raw_native_message": {
                        "event_type": "metadata_only_session"
                    },
                    "_raw_native_session": session_record,
                }
            )

        return self._messages_to_turns(messages)

    def _parse_turns_from_json(self, session_path: Path) -> List[Turn]:
        """Parse one declared standalone JSON session artifact."""
        try:
            native_bytes = read_native_bytes(session_path)
        except OSError:
            raise NativeSourceContractError(
                "native_opencode_json_read_failed"
            ) from None
        try:
            native_text = native_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return [
                Turn(
                    turn_number=0,
                    user_content="",
                    assistant_content="",
                    raw_event_refs=[
                        {
                            "event_type": "native_json_artifact",
                            "raw_base64": base64.b64encode(native_bytes).decode(
                                "ascii"
                            ),
                            "raw_encoding": "base64",
                            "decode_error": "invalid_utf8",
                        }
                    ],
                    source_files=[str(session_path)],
                    completeness={
                        "visible_text": "unavailable",
                        "tool_calls": "unavailable",
                        "tool_results": "unavailable",
                        "reasoning": "unavailable",
                        "attachments": "unavailable",
                        "truncated": False,
                        "loss_reasons": ["native_json_invalid_utf8"],
                    },
                )
            ]
        try:
            data = json.loads(native_text)
        except json.JSONDecodeError:
            raise NativeSourceContractError(
                "native_opencode_json_read_failed"
            ) from None

        messages = _extract_messages(data)
        consumed_keys = {
            key
            for key in ("messages", "conversations", "chats", "history", "turns", "dialogue")
            if isinstance(data, dict) and data.get(key) is messages
        }
        if not messages:
            messages = [data]

        # Normalize the declared JSON message variants for the shared turn parser.
        normalized: List[Dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                normalized.append(
                    {
                        "id": "",
                        "role": "",
                        "time_created": None,
                        "parts": [],
                        "_raw_native_message": msg,
                    }
                )
                continue
            role = msg.get("role") or msg.get("sender") or msg.get("author")
            content_value = msg.get("content", "")
            if isinstance(content_value, list):
                parts = [
                    {"type": "text", "text": str(p.get("text", ""))}
                    if isinstance(p, dict)
                    else {
                        "type": "raw_native_part",
                        "raw": p,
                        "decode_error": "non_object_part",
                    }
                    for p in content_value
                ]
            else:
                parts = [{"type": "text", "text": str(content_value)}]
            normalized.append(
                {
                    "id": msg.get("id", ""),
                    "role": role,
                    "time_created": None,
                    "parts": parts,
                    "_raw_native_message": msg,
                }
            )

        turns = self._messages_to_turns(normalized)
        if consumed_keys:
            return attach_native_container_residual(
                turns,
                data,
                consumed_keys=consumed_keys,
                source_name=self.name,
            )
        return turns

    def _messages_to_turns(self, messages: List[Dict[str, Any]]) -> List[Turn]:
        """将 OpenCode message + part 聚合为 Turn 列表。"""
        turns: List[Turn] = []
        user_content = ""
        assistant_content = ""
        turn_metadata: Dict[str, Any] = {}
        turn_number = 0

        for msg in messages:
            role = str(msg.get("role") or "").strip().lower()
            parts = msg.get("parts", [])
            if not isinstance(parts, list):
                parts = []
            else:
                parts = [
                    part
                    if isinstance(part, dict)
                    else {
                        "type": "raw_native_part",
                        "raw": part,
                        "decode_error": "non_object_part",
                    }
                    for part in parts
                ]

            text = _extract_text(parts)
            reasoning = _extract_reasoning(parts)
            tool_calls = _extract_tool_calls(parts)
            tool_results = _extract_tool_results(parts)
            raw_refs = [
                {
                    "event_type": "native_message",
                    "message_id": str(msg.get("id") or ""),
                    "role": role,
                    "raw": msg.get("_raw_native_message", msg),
                },
                *(
                    [
                        {
                            "event_type": "native_session",
                            "raw": msg["_raw_native_session"],
                        }
                    ]
                    if "_raw_native_session" in msg
                    else []
                ),
                *[
                    {
                        "event_type": "native_part",
                        "raw": part["_raw_native_part"],
                    }
                    for part in parts
                    if "_raw_native_part" in part
                ],
                *[
                    part
                    for part in parts
                    if part.get("type")
                    in ("step-start", "step-finish", "compaction")
                ],
            ]

            ts = msg.get("time_created")
            ts_iso = _ms_to_iso(ts)

            if role == "user":
                if user_content or assistant_content:
                    turns.append(
                        self._build_turn(
                            turn_number,
                            user_content,
                            assistant_content,
                            turn_metadata,
                        )
                    )
                    turn_number += 1
                user_content = text
                assistant_content = ""
                # 保留此前 system/tool 等 raw refs
                existing_refs = turn_metadata.get("raw_event_refs", [])
                turn_metadata = {
                    "source": "opencode",
                    "timestamp": ts_iso,
                    "raw_event_refs": existing_refs + raw_refs,
                    "reasoning": reasoning,
                }
                native_message_id = str(msg.get("id") or "")
                if native_message_id:
                    # `id` is the OpenCode message primary key at this parser
                    # seam, not an unqualified generic metadata value.
                    turn_metadata["native_event_id"] = (
                        f"opencode:message:{native_message_id}"
                    )
            elif role == "assistant":
                assistant_content = text
                if reasoning:
                    turn_metadata["reasoning"] = turn_metadata.get("reasoning", "") + reasoning
                turn_metadata.setdefault("raw_event_refs", []).extend(raw_refs)
                if tool_calls:
                    turn_metadata.setdefault("tool_calls", []).extend(tool_calls)
                if tool_results:
                    turn_metadata.setdefault("tool_results", []).extend(tool_results)
                if ts_iso and "timestamp" not in turn_metadata:
                    turn_metadata["timestamp"] = ts_iso
            else:
                # system / tool / 其他角色保留到 raw_event_refs
                turn_metadata.setdefault("raw_event_refs", []).extend(raw_refs)

        if user_content or assistant_content or turn_metadata:
            turns.append(
                self._build_turn(
                    turn_number,
                    user_content,
                    assistant_content,
                    turn_metadata,
                )
            )

        return turns

    def _build_turn(
        self,
        turn_number: int,
        user_content: str,
        assistant_content: str,
        metadata: Dict[str, Any],
    ) -> Turn:
        reasoning = metadata.pop("reasoning", "") if isinstance(metadata, dict) else ""
        tool_calls = metadata.pop("tool_calls", []) if isinstance(metadata, dict) else []
        tool_results = metadata.pop("tool_results", []) if isinstance(metadata, dict) else []
        return Turn(
            turn_number=turn_number,
            user_content=user_content,
            assistant_content=assistant_content,
            timestamp=metadata.get("timestamp") if isinstance(metadata, dict) else None,
            metadata=metadata,
            tool_calls=tool_calls,
            tool_results=tool_results,
            reasoning=reasoning,
            raw_event_refs=metadata.get("raw_event_refs", []) if isinstance(metadata, dict) else [],
        )

    def get_session_state(self, session_info: SessionInfo) -> Optional[Dict[str, Any]]:  # noqa
        """Return an exact content-bound state for the selected native session."""
        db_path = session_info.source_path
        if native_path_kind(db_path) != "file":
            raise NativeSourceContractError(
                "native_opencode_session_artifact_missing"
            )
        native_session_id = str(
            (session_info.metadata or {}).get("native_session_id")
            or session_info.session_id
        )
        if not native_session_id:
            raise NativeSourceContractError(
                "native_opencode_session_identity_invalid"
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
                "native_opencode_session_state_failed",
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
                "native_opencode_session_state_failed"
            ) from None

        try:
            cur = conn.execute(
                """
                SELECT time_updated, time_created
                FROM session
                WHERE id = ?
                """,
                (native_session_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise NativeSourceContractError(
                    "native_opencode_session_missing"
                )
            mtime = (row[0] or row[1] or 0) / 1000.0

            cur = conn.execute(
                """
                SELECT COUNT(*), COALESCE(MAX(m.time_updated), 0)
                FROM message m
                WHERE m.session_id = ?
                """,
                (native_session_id,),
            )
            msg_count, last_msg_ts = cur.fetchone()
            last_msg_ts = last_msg_ts or 0

            return {
                "mtime": max(mtime, last_msg_ts / 1000.0),
                "size": int(msg_count),
                "file_count": int(msg_count) or 1,
                "fingerprint": fingerprint,
                "fingerprint_contract": "opencode-exact-session-rows-sha256-v1",
            }
        except NativeSourceContractError:
            raise
        except sqlite3.Error as exc:
            raise NativeSourceContractError.from_storage_failure(
                "native_opencode_session_state_failed",
                exc,
            ) from None
        except (ValueError, TypeError):
            raise NativeSourceContractError(
                "native_opencode_session_state_failed"
            ) from None
        finally:
            conn.close()


# ---------- 辅助函数 ----------
def _ms_to_iso(ts: Optional[int]) -> Optional[str]:
    if not ts:
        return None
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).isoformat()
    # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
        return None


def _extract_text(parts: List[Dict[str, Any]]) -> str:
    return "".join(
        str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("type") == "text"
    )


def _extract_reasoning(parts: List[Dict[str, Any]]) -> str:
    return "".join(
        str(p.get("text", ""))
        for p in parts
        if isinstance(p, dict) and p.get("type") == "reasoning"
    )


def _extract_tool_calls(parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """提取正在调用的 tool（对应 tool_calls）。"""
    tools: List[Dict[str, Any]] = []
    for p in parts:
        if not isinstance(p, dict) or p.get("type") != "tool":
            continue
        state = p.get("state") or {}
        if state.get("status") in ("calling", "in_progress"):
            tools.append(
                {
                    "name": p.get("tool", ""),
                    "id": p.get("callID", ""),
                    "arguments": state.get("input", {}),
                }
            )
    return tools


def _extract_tool_results(parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """提取已完成 tool 的输出（对应 tool_results）。"""
    results: List[Dict[str, Any]] = []
    for p in parts:
        if not isinstance(p, dict) or p.get("type") != "tool":
            continue
        state = p.get("state") or {}
        if state.get("status") == "completed":
            results.append(
                {
                    "name": p.get("tool", ""),
                    "id": p.get("callID", ""),
                    "output": state.get("output", ""),
                }
            )
    return results


def _extract_messages(data: Any) -> List[Dict]:
    """从多种可能的 JSON 结构中提取消息列表。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("messages", "conversations", "chats", "history", "turns", "dialogue"):
            if key in data and isinstance(data[key], list):
                return data[key]  # type: ignore[no-any-return]
        if "role" in data or "content" in data:
            return [data]
    return []
