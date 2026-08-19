# -*- coding: utf-8 -*-
"""
CursorSource — Cursor IDE 同步插件

实现 AgentSource 接口，接入 SyncFramework。
Cursor 基于 VS Code，聊天记录可能保存在 SQLite 数据库或 JSON 文件中。

数据位置（调研中）：
- macOS: ~/Library/Application Support/Cursor/
- Linux: ~/.config/Cursor/
- 可能的文件：
  - User/globalStorage/state.vscdb (SQLite)
  - workspaceStorage/*/state.vscdb
  - 或直接在工作区目录下的 .cursor/ 文件夹

⚠️ 当前实现基于公开信息推断，需实际 Cursor 环境验证。
"""

from __future__ import annotations

import base64
import json
import hashlib
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.runtime_environment import environment_get
from core.sync_framework.agent_source import (
    NativeSourceContractError,
    SessionInfo,
    Turn,
)
from core.ops.durable_io import open_native_text, read_native_bytes
from core.sync_framework.native_sqlite import (
    NativeSQLiteReadError,
    connect_native_sqlite_readonly,
)

from integrations.sources.base import (
    BaseAgentSource,
    attach_native_container_residual,
    lossless_text_message_turns,
    native_path_kind,
    stable_path_session_id,
)

logger = logging.getLogger(__name__)

_CURSOR_SQLITE_TABLES = (
    "ItemTable",
    "items",
    "conversations",
    "messages",
)


class CursorSource(BaseAgentSource):
    """Cursor 数据源插件

    ⚠️ EXPERIMENTAL: 当前实现基于 VS Code 结构推断，尚未在真实 Cursor 环境验证。
    """

    experimental = True

    _cap_reasoning = "unknown"
    _cap_attachments = "unknown"
    _cap_source_fidelity = "experimental"

    @property
    def name(self) -> str:
        return "cursor"

    @property
    def model_tag(self) -> str:
        return "cursor"

    @staticmethod
    def _candidate_data_dirs() -> List[Path]:
        configured = str(environment_get("CURSOR_HOME", "")).strip()
        if configured:
            return [Path(configured).expanduser()]
        candidates = []
        if sys.platform == "darwin":
            candidates.append(Path.home() / "Library" / "Application Support" / "Cursor")
        elif sys.platform == "linux":
            candidates.append(Path.home() / ".config" / "Cursor")
        elif sys.platform == "win32":
            candidates.append(Path.home() / "AppData" / "Roaming" / "Cursor")
        return list(dict.fromkeys(candidates))

    @property
    def data_dir(self) -> Optional[Path]:
        return self._resolve_data_dir(self._candidate_data_dirs())

    def observed_roots(self) -> List[Path]:
        """Bind installed data or existing candidate parents for verified empty."""

        configured = str(environment_get("CURSOR_HOME", "")).strip()
        if configured:
            return [Path(configured).expanduser()]
        resolved = self.data_dir
        if resolved is not None:
            return [resolved]
        return list(
            dict.fromkeys(
                candidate.parent
                for candidate in self._candidate_data_dirs()
                if native_path_kind(candidate.parent) == "directory"
            )
        )

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        return {
            "type": "polling",
            "interval": 3600,
            "pattern": "*.json",
        }

    def discover_sessions(self) -> List[SessionInfo]:
        """发现 Cursor 会话文件"""
        base = self.data_dir
        if not base:
            return []
        sessions = self._discover_vscode_workspace_sessions(
            base,
            ["chat_history.json", "session.json", "conversations.json"],
            ["chat_history.json", "conversations.json"],
            prefix="cursor",
        )
        try:
            for database in sorted(base.rglob("*.vscdb")):
                sessions.extend(self._discover_sqlite_conversations(base, database))
        except NativeSourceContractError:
            raise
        except OSError as exc:
            raise NativeSourceContractError.from_storage_failure(
                "native_cursor_session_discovery_failed",
                exc,
            ) from None
        return sessions

    def _discover_sqlite_conversations(
        self,
        base: Path,
        database: Path,
    ) -> List[SessionInfo]:
        sessions: List[SessionInfo] = []
        seen_native_keys: dict[str, bytes] = {}
        session_by_native_key: dict[str, SessionInfo] = {}
        try:
            connection = connect_native_sqlite_readonly(database)
            try:
                present = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                for table in _CURSOR_SQLITE_TABLES:
                    if table not in present:
                        continue
                    columns = {
                        str(row[1])
                        for row in connection.execute(
                            f'PRAGMA table_info("{table}")'  # nosec B608
                        ).fetchall()
                    }
                    if not {"key", "value"} <= columns:
                        continue
                    rows = connection.execute(
                        f'SELECT key, value FROM "{table}" '  # nosec B608 - fixed allowlist
                        "WHERE key LIKE ? OR key LIKE ? ORDER BY key",
                        ("%chat%", "%conversation%"),
                    ).fetchall()
                    for key, value in rows:
                        native_key = str(key)
                        serialized_value = _cursor_sqlite_value_bytes(value)
                        prior_value = seen_native_keys.get(native_key)
                        if prior_value is not None:
                            if prior_value == serialized_value:
                                prior_session = session_by_native_key[native_key]
                                mirror_tables = list(
                                    (prior_session.metadata or {}).get(
                                        "native_sqlite_tables",
                                        [
                                            (prior_session.metadata or {}).get(
                                                "native_sqlite_table"
                                            )
                                        ],
                                    )
                                )
                                mirror_tables.append(table)
                                prior_session.metadata[
                                    "native_sqlite_tables"
                                ] = sorted(set(mirror_tables))
                                continue
                            raise NativeSourceContractError(
                                "native_cursor_sqlite_identity_conflict"
                            )
                        seen_native_keys[native_key] = serialized_value
                        canonical_id = stable_path_session_id(
                            "cursor-db",
                            base,
                            database,
                            native_id=native_key,
                        )
                        session = SessionInfo(
                                session_id=canonical_id,
                                source_path=database,
                                working_dir=str(database.parent),
                                mtime=database.stat().st_mtime,
                                canonical_session_id=canonical_id,
                                session_aliases=[
                                    f"cursor-db-{database.stem}",
                                    hashlib.sha256(
                                        native_key.encode("utf-8")
                                    ).hexdigest()[:16],
                                ],
                                source_kind="sqlite_conversation",
                                metadata={
                                    "native_sqlite_table": table,
                                    "native_sqlite_tables": [table],
                                    "native_sqlite_key": native_key,
                                },
                            )
                        sessions.append(session)
                        session_by_native_key[native_key] = session
            finally:
                connection.close()
        except (NativeSQLiteReadError, OSError, sqlite3.Error) as exc:
            raise NativeSourceContractError.from_storage_failure(
                "native_cursor_sqlite_discovery_failed",
                exc,
            ) from None
        return sessions

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析 Cursor 会话文件为 Turn 列表"""
        if session_path.suffix == ".json":
            return self._parse_json_session(session_path)
        elif session_path.suffix == ".vscdb":
            return self._parse_sqlite_session(session_path)
        return []

    def parse_session(self, session_info: SessionInfo) -> List[Turn]:
        if session_info.source_kind == "sqlite_conversation":
            metadata = dict(session_info.metadata or {})
            return self._parse_sqlite_conversation(
                session_info.source_path,
                str(metadata.get("native_sqlite_table") or ""),
                str(metadata.get("native_sqlite_key") or ""),
            )
        return self.parse_turns(session_info.source_path)

    def session_artifact_evidence_hash(
        self,
        session_info: SessionInfo,
    ) -> str:
        """Hash the exact SQLite row, or the exact JSON artifact bytes."""

        if session_info.source_kind != "sqlite_conversation":
            try:
                payload = read_native_bytes(session_info.source_path)
            except OSError:
                raise NativeSourceContractError(
                    "native_cursor_artifact_evidence_failed"
                ) from None
        else:
            metadata = dict(session_info.metadata or {})
            table = str(metadata.get("native_sqlite_table") or "")
            key = str(metadata.get("native_sqlite_key") or "")
            if table not in _CURSOR_SQLITE_TABLES or not key:
                raise NativeSourceContractError(
                    "native_cursor_session_identity_invalid"
                )
            tables = list(metadata.get("native_sqlite_tables") or [table])
            if (
                not tables
                or table not in tables
                or any(item not in _CURSOR_SQLITE_TABLES for item in tables)
            ):
                raise NativeSourceContractError(
                    "native_cursor_session_identity_invalid"
                )
            try:
                connection = connect_native_sqlite_readonly(
                    session_info.source_path
                )
                try:
                    connection.execute("BEGIN")
                    rows = [
                        (
                            mirror_table,
                            connection.execute(
                                f'SELECT value FROM "{mirror_table}" WHERE key=?',  # nosec B608
                                (key,),
                            ).fetchone(),
                        )
                        for mirror_table in sorted(set(tables))
                    ]
                finally:
                    connection.close()
            except (NativeSQLiteReadError, OSError, sqlite3.Error) as exc:
                raise NativeSourceContractError.from_storage_failure(
                    "native_cursor_artifact_evidence_failed",
                    exc,
                ) from None
            if any(row is None for _mirror_table, row in rows):
                raise NativeSourceContractError(
                    "native_cursor_sqlite_conversation_missing"
                )
            values = [
                _cursor_sqlite_value_bytes(row[0])
                for _mirror_table, row in rows
                if row is not None
            ]
            if len(set(values)) != 1:
                raise NativeSourceContractError(
                    "native_cursor_sqlite_identity_conflict"
                )
            payload = json.dumps(
                {
                    "tables": [mirror_table for mirror_table, _row in rows],
                    "key": key,
                    "value_sha256": hashlib.sha256(values[0]).hexdigest(),
                    "value_size": len(values[0]),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def _parse_json_session(self, session_path: Path) -> List[Turn]:
        """解析 JSON 格式的会话记录"""
        turns = []  # type: ignore[var-annotated]
        try:
            with open_native_text(session_path) as f:
                data = json.load(f)
        except (OSError, IOError, json.JSONDecodeError):
            raise NativeSourceContractError(
                "native_cursor_json_read_failed"
            ) from None

        # Cursor 可能的格式: 数组或对象
        messages: Any = data
        consumed_keys: set[str] = set()
        if isinstance(data, list):
            messages = data
        elif isinstance(data, dict):
            # 可能是 {conversations: [...]} 或 {messages: [...]}。
            for key in ("conversations", "messages"):
                if key in data:
                    messages = data[key]
                    consumed_keys = {key}
                    break

        turns = self._messages_to_turns(messages)
        if consumed_keys:
            return attach_native_container_residual(
                turns,
                data,
                consumed_keys=consumed_keys,
                source_name=self.name,
            )
        return turns

    def _parse_sqlite_session(self, session_path: Path) -> List[Turn]:
        """Compatibility aggregate; formal capture uses ``parse_session``."""
        turns: List[Turn] = []
        base = session_path.parent
        sessions = self._discover_sqlite_conversations(base, session_path)
        for session in sessions:
            for turn in self.parse_session(session):
                turn.turn_number = len(turns)
                turns.append(turn)
        return turns

    def _parse_sqlite_conversation(
        self,
        session_path: Path,
        table: str,
        key: str,
    ) -> List[Turn]:
        if table not in _CURSOR_SQLITE_TABLES or not key:
            raise NativeSourceContractError("native_cursor_session_identity_invalid")
        try:
            connection = connect_native_sqlite_readonly(session_path)
            try:
                row = connection.execute(
                    f'SELECT value FROM "{table}" WHERE key=?',  # nosec B608 - fixed allowlist
                    (key,),
                ).fetchone()
            finally:
                connection.close()
        except (NativeSQLiteReadError, OSError, sqlite3.Error) as exc:
            raise NativeSourceContractError.from_storage_failure(
                "native_cursor_sqlite_read_failed",
                exc,
            ) from None
        if row is None:
            raise NativeSourceContractError("native_cursor_conversation_missing")
        raw_value = _cursor_sqlite_value_bytes(row[0])
        try:
            decoded_value = raw_value.decode("utf-8")
        except UnicodeDecodeError:
            return [
                Turn(
                    turn_number=0,
                    user_content="",
                    assistant_content="",
                    raw_event_refs=[
                        {
                            "event_type": "malformed_sqlite_conversation",
                            "raw_base64": base64.b64encode(raw_value).decode("ascii"),
                            "raw_encoding": "base64",
                            "decode_error": "invalid_utf8",
                        }
                    ],
                    completeness={
                        "visible_text": "unavailable",
                        "tool_results": "unavailable",
                        "reasoning": "unavailable",
                        "attachments": "unavailable",
                        "truncated": False,
                        "loss_reasons": ["native_sqlite_invalid_utf8"],
                    },
                )
            ]
        try:
            data = json.loads(decoded_value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return [
                Turn(
                    turn_number=0,
                    user_content="",
                    assistant_content="",
                    raw_event_refs=[
                        {
                            "event_type": "malformed_sqlite_conversation",
                            "raw": decoded_value,
                            "decode_error": "invalid_json",
                        }
                    ],
                    completeness={
                        "visible_text": "unavailable",
                        "tool_results": "unavailable",
                        "reasoning": "unavailable",
                        "attachments": "unavailable",
                        "truncated": False,
                        "loss_reasons": ["native_sqlite_invalid_json"],
                    },
                )
            ]
        messages: Any = data
        consumed_keys: set[str] = set()
        if isinstance(data, dict) and "messages" in data:
            messages = data["messages"]
            consumed_keys = {"messages"}
        turns = self._messages_to_turns(messages)
        if consumed_keys:
            return attach_native_container_residual(
                turns,
                data,
                consumed_keys=consumed_keys,
                source_name=self.name,
            )
        return turns

    def _messages_to_turns(self, messages: Any) -> List[Turn]:
        """Normalize visible text and preserve every native message as raw."""
        return lossless_text_message_turns(messages, source_name=self.name)

    def get_session_state(self, session_info: SessionInfo) -> Optional[Dict[str, Any]]:  # noqa
        """Track the exact JSON artifact or SQLite conversation row."""

        if session_info.source_kind != "sqlite_conversation":
            return self._compute_session_state([session_info.source_path])
        metadata = dict(session_info.metadata or {})
        table = str(metadata.get("native_sqlite_table") or "")
        key = str(metadata.get("native_sqlite_key") or "")
        if table not in _CURSOR_SQLITE_TABLES or not key:
            raise NativeSourceContractError(
                "native_cursor_session_identity_invalid"
            )
        fingerprint = self.session_artifact_evidence_hash(session_info)
        try:
            connection = connect_native_sqlite_readonly(session_info.source_path)
            try:
                row = connection.execute(
                    f'SELECT value FROM "{table}" WHERE key=?',  # nosec B608
                    (key,),
                ).fetchone()
            finally:
                connection.close()
            if row is None:
                raise NativeSourceContractError(
                    "native_cursor_sqlite_conversation_missing"
                )
            encoded = _cursor_sqlite_value_bytes(row[0])
            stat = session_info.source_path.stat()
            return {
                "mtime": stat.st_mtime,
                "size": len(encoded),
                "file_count": 1,
                "fingerprint": fingerprint,
                "fingerprint_contract": "cursor-sqlite-mirror-set-sha256-v1",
            }
        except NativeSourceContractError:
            raise
        except (NativeSQLiteReadError, OSError, sqlite3.Error) as exc:
            raise NativeSourceContractError.from_storage_failure(
                "native_cursor_session_state_failed",
                exc,
            ) from None


def _cursor_sqlite_value_bytes(value: Any) -> bytes:
    """Return the exact SQLite value bytes without repr coercion."""

    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="surrogatepass")
    return str(value).encode("utf-8", errors="surrogatepass")
