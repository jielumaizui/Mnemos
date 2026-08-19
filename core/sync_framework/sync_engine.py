# -*- coding: utf-8 -*-
"""
SyncEngine — 统一同步协调层

AgentSource 和 StorageBackend 之间的统一协调层。
插件只负责：发现会话 + 解析消息。
引擎负责：噪音过滤→内容构建→脱敏→精确去重→标签组装→存储分片→信号采集。

8 步流水线:
  1. 增量判定 — 基于 exact turn + content hash + terminal state，禁止最大序号遮蔽缺洞
  2. 噪音过滤 — 统一 is_noise_message()
  3. 内容构建 — Markdown 格式化
  4. 脱敏 — 复用 sanitize_content()
  5. 去重检查 — content_hash 对比
  6. 标签组装 — 七维标签 + 插件扩展 + 自动检测
  7. 存储分片 — Config 驱动阈值，超长自动分片
  8. 信号采集 — 画像行为信号 + sync_log 状态记录
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_SYNCED_STATUSES = {"new", "updated", "synced", "backfilled", "skipped_backend"}

from core.sync_framework.storage_backend import (  # noqa: E402
    StorageBackend,
    StorageRateLimitError,
    StorageAuthError,
    StorageServerError,
    create_storage_backend,
)
from core.config import ConfigProvider, get_config  # noqa: E402
from core.db_utils import SqlitePool, release_transient_pools  # noqa: E402
from core.ops.durable_io import DurableIOError, inspect_path_kind  # noqa: E402
from core.ops.durable_io import read_native_bytes  # noqa: E402
from core.sync_framework.raw_event_store import RawEventStore  # noqa: E402
from core.sync_framework.sync_log_store import SyncLogStore  # noqa: E402
from core.sync_framework.sync_engine_support import (  # noqa: E402,F401
    BackendDuplicateStateUnavailableError,
    SyncEngineSupportMixin,
    _append_json_section,
    _append_text_section,
    _get_reasoning_mode,
    _json_dumps,
    bind_sanitize_pattern_loader,
    build_turn_markdown,
    compute_content_hash,
    sanitize_content,
)
from core.sync_framework.sync_engine_artifact_support import (  # noqa: E402
    CanonicalRawCommitError,
    SyncEngineArtifactSupportMixin,
    _configured_database_dir,
)

from .agent_source import (  # noqa: E402
    AgentSource,
    SessionInfo,
    SyncResult,
    Turn,
    canonicalize_session_info,
    parse_discovered_session,
)
from .session_handoff import enqueue_complete_session  # noqa: E402
from .source_support import build_native_raw_metadata  # noqa: E402

# Constants extracted from magic numbers
SHARD_THRESHOLD = 819200
SYNC_ENGINE__CLEANUP_OLD_SYNC_LOG_DAYS = 90
_SYNC_PERSISTENCE_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    sqlite3.Error,
    StorageRateLimitError,
    StorageAuthError,
    StorageServerError,
)


class _SyncEventBusConfig:
    """Path-scoped EventBus view that preserves the caller's tuning values."""

    def __init__(self, base: Any, database_dir: Path):
        self._base = base
        self.database_dir = Path(database_dir).expanduser()
        self.mnemos_dir = self.database_dir
        self.data_dir = self.database_dir

    def get(self, key: str, default: Any = None) -> Any:
        getter = getattr(self._base, "get", None)
        if callable(getter):
            return getter(key, default)
        return default


_DEFAULT_SANITIZE_PATTERNS = [
    (r"sk-[a-zA-Z0-9]{20,}", "[API-KEY]"),
    (r"gh[pousr]_[A-Za-z0-9_]{36,}", "[GITHUB-TOKEN]"),
    (r"AKID[0-9a-zA-Z]{10,}", "[CLOUD-KEY]"),
    (r"password[:=]\s*\S+", "password=[HIDDEN]"),
    (r"secret[:=]\s*\S+", "secret=[HIDDEN]"),
    (r"token[:=]\s*\S+", "token=[HIDDEN]"),
]


def _load_sanitize_patterns():
    """从配置文件加载脱敏规则，不存在则用内置默认值"""
    cfg = get_config()
    cfg_dir = cfg.data_dir / "configs"
    patterns_file = cfg_dir / "sanitize_patterns.json"
    try:
        patterns_kind = inspect_path_kind(patterns_file)
    except DurableIOError:
        raise CanonicalRawCommitError("sanitize_pattern_config_unavailable") from None
    if patterns_kind == "missing":
        return list(_DEFAULT_SANITIZE_PATTERNS)
    if patterns_kind != "file":
        raise CanonicalRawCommitError("sanitize_pattern_config_not_regular")
    try:
        data = json.loads(read_native_bytes(patterns_file).decode("utf-8"))
        if not isinstance(data, list) or not data:
            raise ValueError("sanitize pattern list must be non-empty")
        patterns: list[tuple[str, str]] = []
        for item in data:
            if (
                not isinstance(item, (list, tuple))
                or len(item) != 2
                or not isinstance(item[0], str)
                or not item[0]
                or not isinstance(item[1], str)
            ):
                raise ValueError("sanitize pattern entry is invalid")
            re.compile(item[0])
            patterns.append((item[0], item[1]))
        return patterns
    except (OSError, UnicodeError, ValueError, TypeError, re.error):
        raise CanonicalRawCommitError("sanitize_pattern_config_invalid") from None


bind_sanitize_pattern_loader(_load_sanitize_patterns)


class SyncEngine(SyncEngineArtifactSupportMixin, SyncEngineSupportMixin):
    """
    AgentSource 和 StorageBackend 之间的统一协调层。

    设计原则：
    - 插件不可见内部逻辑，只提供原始数据
    - 所有同步数据统一经过此引擎，不绕路
    - 画像信号在同步成功后统一采集
    - 统一防重：一个 SQLite 库管所有 Agent
    """

    def __getattribute__(self, name: str):
        attr = object.__getattribute__(self, name)
        if name.startswith("_") or name == "close" or not callable(attr):
            return attr
        class_attr = getattr(type(self), name, None)
        if not callable(class_attr):
            return attr

        def release_after_call(*args, **kwargs):
            try:
                return attr(*args, **kwargs)
            finally:
                release_transient_pools(self, "_pool")
                raw_store = getattr(self, "raw_store", None)
                if raw_store is not None:
                    release_transient_pools(raw_store, "_pool")

        return release_after_call

    def __init__(
        self,
        backend: Optional[StorageBackend] = None,
        db_path: Optional[str] = None,
        config: Optional[ConfigProvider] = None,
        **kwargs,
    ):
        explicit_config = config is not None
        explicit_db_path = db_path is not None
        self._explicit_config = explicit_config
        self._explicit_db_path = explicit_db_path
        self.config = config or get_config()

        self.backend = backend or self._build_default_backend()
        self.db_path = Path(db_path or self.config.database_dir / "sync_log.db").expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._shard_threshold = self.config.get(
            "storage.obsidian.daily_size_threshold", SHARD_THRESHOLD
        )
        self._pool = SqlitePool(self.db_path)
        self._sync_log = SyncLogStore(self._pool.get_conn, config=self.config)
        configured_event_dir = _configured_database_dir(self.config)
        if explicit_db_path and not explicit_config:
            event_database_dir = self.db_path.parent
        else:
            event_database_dir = configured_event_dir or self.db_path.parent
        self._event_bus_config = _SyncEventBusConfig(
            self.config,
            event_database_dir,
        )
        self._event_publisher: Callable[..., str] | None = kwargs.get(
            "event_publisher"
        )
        try:
            self._init_db()
        except BaseException:
            self._pool.close()
            raise
        release_transient_pools(self, "_pool")
        self.raw_store: Optional[RawEventStore] = kwargs.get("raw_store")
        if self.raw_store is None:
            try:
                self.raw_store = RawEventStore(
                    db_path=self._resolve_raw_event_store_path(),
                    config=self.config,
                )
            except (ImportError, OSError, RuntimeError, ValueError, TypeError, sqlite3.Error):
                logger.warning("[SyncEngine] raw_event_store 初始化失败", exc_info=True)

    def _resolve_raw_event_store_path(self) -> Path:
        """Resolve one real RawStore path without weakening the Raw receipt gate.

        An explicit config owns the configured Raw path. When only ``db_path``
        is explicit, that caller-owned directory must also own canonical Raw
        and EventBus state; consulting the ambient process config in that case
        would split one SyncEngine generation across unrelated database roots.
        """
        if self._explicit_db_path and not self._explicit_config:
            return self.db_path.parent / "raw_events.db"
        configured = (
            self.config.get("raw_event_store.db_path") if hasattr(self.config, "get") else None
        )
        if configured:
            return Path(configured).expanduser()
        database_dir = _configured_database_dir(self.config)
        if database_dir is not None:
            return database_dir / "raw_events.db"
        return self.db_path.parent / "raw_events.db"

    def _publish_sync_event(
        self,
        event_type: str,
        agent: str,
        payload: Dict[str, Any],
    ) -> str:
        if self._event_publisher is not None:
            return self._event_publisher(event_type, agent, payload)
        from core.mnemos_bus import publish_event

        return publish_event(
            event_type,
            agent,
            payload,
            config=self._event_bus_config,
        )

    def _source_metadata_write_is_frozen(self, agent_name: str, session_id: str) -> bool:
        """Fail closed before a sync/backfill flow can recreate deleted metadata."""

        from core.privacy.ownership_freeze import cognitive_write_is_frozen

        try:
            return cognitive_write_is_frozen(
                self.config,
                agent=str(agent_name),
                session_id=str(session_id),
            )
        except PermissionError:
            logger.error(
                "[SyncEngine] source metadata write blocked: ownership freeze state unavailable"
            )
            return True

    def close(self):
        """关闭持久连接"""
        if hasattr(self, "_pool"):
            self._pool.close()
        if hasattr(self, "raw_store") and self.raw_store is not None:
            try:
                self.raw_store.close()
            except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError):
                logger.warning("[SyncEngine] raw_event_store 关闭失败", exc_info=True)

    # ---------- 内部工厂 ----------

    def _build_default_backend(self) -> StorageBackend:
        """根据配置创建默认存储后端（当前仅支持 obsidian）。"""
        return create_storage_backend(config=self.config)

    def _init_db(self):
        """初始化统一防重数据库"""
        conn = self._pool.get_conn()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sync_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_name TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    turn_number INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    backend_uids TEXT,
                    status TEXT DEFAULT 'synced',
                    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    distill_status TEXT DEFAULT 'pending',
                    distill_job_id TEXT,
                    distilled_at TIMESTAMP,
                    wiki_page_paths TEXT,
                    distill_error TEXT,
                    error TEXT,
                    working_dir TEXT,
                    tags TEXT,
                    artifact_path TEXT,
                    UNIQUE(agent_name, session_id, turn_number)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_sync_lookup
                ON sync_log(agent_name, session_id, turn_number)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_sync_content_hash
                ON sync_log(content_hash)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_sync_log_dedup
                ON sync_log(agent_name, session_id, turn_number, content_hash)
            """)
            columns = {
                str(row[1]) for row in cursor.execute("PRAGMA table_info(sync_log)").fetchall()
            }
            for column, ddl in (
                ("backend_uids", "ALTER TABLE sync_log ADD COLUMN backend_uids TEXT"),
                ("artifact_path", "ALTER TABLE sync_log ADD COLUMN artifact_path TEXT"),
                ("error", "ALTER TABLE sync_log ADD COLUMN error TEXT"),
                (
                    "persona_collected",
                    "ALTER TABLE sync_log ADD COLUMN persona_collected INTEGER DEFAULT 0",
                ),
                ("working_dir", "ALTER TABLE sync_log ADD COLUMN working_dir TEXT"),
                ("tags", "ALTER TABLE sync_log ADD COLUMN tags TEXT"),
            ):
                if column not in columns:
                    cursor.execute(ddl)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    turn_number INTEGER,
                    content_length INTEGER,
                    has_code INTEGER,
                    has_tools INTEGER,
                    user_questions INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sync_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    audit_type TEXT DEFAULT 'l1_scan',
                    skipped_missing INTEGER DEFAULT 0,
                    skipped_large INTEGER DEFAULT 0,
                    skipped_stale INTEGER DEFAULT 0,
                    skipped_unchanged INTEGER DEFAULT 0,
                    skipped_over_limit INTEGER DEFAULT 0,
                    selected INTEGER DEFAULT 0,
                    synced_turns INTEGER DEFAULT 0,
                    created_at REAL NOT NULL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_sync_audit_source_time
                ON sync_audit(source, audit_type, created_at)
            """)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        # 启动时清理旧 sync_log，防止表无限增长
        self._cleanup_old_sync_log()

    def _cleanup_old_sync_log(self, days: int = SYNC_ENGINE__CLEANUP_OLD_SYNC_LOG_DAYS):
        """清理超过 N 天的已同步记录"""
        conn: sqlite3.Connection | None = None
        try:
            conn = self._pool.get_conn()
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            cursor = conn.execute(
                "DELETE FROM sync_log WHERE synced_at < ? AND status = 'synced'", (cutoff,)
            )
            conn.commit()
            if cursor.rowcount > 0:
                logger.info("[SyncEngine] 清理 %s 条旧 sync_log", cursor.rowcount)
        except (sqlite3.Error, OSError):
            if conn is not None:
                conn.rollback()
            logging.getLogger(__name__).warning("Unexpected error", exc_info=True)

    # ---------- 公共 API ----------

    def _compute_content_hash(self, turn: Turn, content: str) -> str:
        """优先使用 Turn 中预计算哈希，否则基于内容计算。"""
        full_content_hash = (turn.metadata or {}).get("full_content_hash")
        if full_content_hash:
            return str(full_content_hash)
        return hashlib.md5(content.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]

    def canonicalize_session_info(self, session_info: SessionInfo) -> SessionInfo:
        """Copy ``SessionInfo`` with the canonical id as its primary storage key."""
        return canonicalize_session_info(session_info)

    def _assert_session_identity_activation(
        self,
        source: AgentSource,
        session_info: SessionInfo,
    ) -> None:
        """Fail closed when a parser identity change overlaps historical Raw state."""
        metadata = dict(session_info.metadata or {})
        if metadata.get("identity_reconciliation_required") is not True:
            return
        version = str(metadata.get("identity_contract_version") or "")
        legacy_ids = metadata.get("legacy_canonical_session_ids")
        identities = [
            session_info.session_id,
            *(session_info.session_aliases or []),
            *([str(value) for value in legacy_ids] if isinstance(legacy_ids, list) else []),
        ]
        if not version or self.raw_store is None:
            raise CanonicalRawCommitError("source_session_identity_reconciliation_unavailable")
        if self.raw_store.has_incompatible_session_identity(
            source.name,
            identities,
            identity_contract_version=version,
            canonical_session_id=str(session_info.canonical_session_id or session_info.session_id),
            source_artifact_id=str(metadata.get("source_artifact_id") or ""),
        ):
            raise CanonicalRawCommitError("source_session_identity_reconciliation_required")

    def _is_synced_duplicate(self, existing: Optional[Dict], content_hash: str) -> bool:
        """判断本地 sync_log 记录是否已同步且哈希一致。"""
        if not existing:
            return False
        return (
            existing.get("status") in _SYNCED_STATUSES
            and existing.get("content_hash") == content_hash
        )

    def _lookup_backend_duplicates(
        self,
        source: AgentSource,
        session_info: SessionInfo,
        turn: Turn,
        content_hash: str,
        check_backend_duplicate: bool,
        backend_duplicate_cache: Optional[Dict[Tuple[str, int, str], List[str]]],
    ) -> List[str]:
        """查询后端是否已有重复记录。"""
        if backend_duplicate_cache is not None:
            return backend_duplicate_cache.get(
                (session_info.session_id, turn.turn_number, content_hash),
                [],
            )
        if check_backend_duplicate:
            return self._check_backend_duplicate(
                source.name, session_info.session_id, turn.turn_number, content_hash
            )
        return []

    def _artifact_path(self, turn: Turn) -> str:
        """从 Turn metadata 中提取 artifact_path。"""
        metadata = turn.metadata or {}
        artifact_path = metadata.get("artifact_path", "") or metadata.get(
            "reasoning_artifact_path", ""
        )
        return "" if artifact_path is None else str(artifact_path)

    def _raw_source_metadata(
        self,
        source: AgentSource,
        session_info: SessionInfo,
        turn: Turn,
    ) -> Dict[str, Any]:
        """组装 canonical raw store 需要的来源元数据。"""
        return build_native_raw_metadata(source, session_info, turn)

    def _record_raw_turn(
        self,
        source: AgentSource,
        session_info: SessionInfo,
        turn: Turn,
        *,
        content_hash: Optional[str] = None,
    ) -> Optional[str]:
        """Write canonical Raw and return its durable revision receipt if any."""
        store = self.raw_store
        if store is None:
            return None

        source_files = [str(p) for p in (turn.source_files or [])]
        if not source_files and session_info.source_path:
            source_files = [str(session_info.source_path)]
        try:
            # Capture/document producers may already have committed this exact
            # immutable Raw revision.  Verify it against canonical storage
            # before any parser-specific metadata construction; otherwise an
            # ingestion-only source could be forced through the host-agent
            # support manifest and lose a valid receipt.
            supplied_revision_id = str((turn.metadata or {}).get("raw_event_id") or "")
            if supplied_revision_id:
                existing_header = store.get_revision_header(supplied_revision_id)
                if (
                    existing_header
                    and existing_header.get("source_agent") == source.name
                    and existing_header.get("session_id") == session_info.session_id
                ):
                    if int(existing_header.get("turn_number", -1)) != turn.turn_number or (
                        content_hash and existing_header.get("content_hash") != content_hash
                    ):
                        logger.error(
                            "[SyncEngine] supplied Raw revision identity mismatch: %s",
                            supplied_revision_id,
                        )
                        return None
                    return supplied_revision_id
            metadata = self._raw_source_metadata(source, session_info, turn)
            existing_revision_id = str(metadata.get("raw_event_id") or "")
            if existing_revision_id:
                existing_header = store.get_revision_header(existing_revision_id)
                if existing_header and existing_header.get("support_manifest_hash") == metadata.get(
                    "support_manifest_hash"
                ):
                    if (
                        existing_header.get("source_agent") != source.name
                        or existing_header.get("session_id") != session_info.session_id
                        or int(existing_header.get("turn_number", -1)) != turn.turn_number
                        or (content_hash and existing_header.get("content_hash") != content_hash)
                    ):
                        logger.error(
                            "[SyncEngine] native Raw revision identity mismatch: %s",
                            existing_revision_id,
                        )
                        return None
                    return existing_revision_id
            return store.upsert_turn(
                source_agent=source.name,
                session_id=session_info.session_id,
                turn_number=turn.turn_number,
                user_content=turn.user_content,
                assistant_content=turn.assistant_content,
                model_tag=source.model_tag,
                timestamp=turn.timestamp,
                metadata=metadata,
                tool_calls=turn.tool_calls,
                tool_results=turn.tool_results,
                reasoning=turn.reasoning,
                attachments=turn.attachments,
                raw_event_refs=turn.raw_event_refs,
                source_files=source_files,
                source_path=str(session_info.source_path) if session_info.source_path else None,
                completeness=dict(turn.completeness or {}),
                content_hash=content_hash,
                full_content_hash=(turn.metadata or {}).get("full_content_hash"),
                origin="sync_engine",
            )
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error):
            logger.warning("[SyncEngine] raw_event_store 写入失败", exc_info=True)
            return None

    def _raw_revision_content_hash(self, revision_id: str) -> str:
        """Read the authoritative hash bound to an immutable Raw revision."""
        if self.raw_store is None or not str(revision_id or ""):
            return ""
        try:
            header = self.raw_store.get_revision_header(str(revision_id))
        except (AttributeError, OSError, sqlite3.Error) as exc:
            logger.warning("[SyncEngine] Raw revision header lookup failed", exc_info=True)
            raise CanonicalRawCommitError("canonical_raw_revision_header_unavailable") from exc
        return str((header or {}).get("content_hash") or "")

    def _bind_canonical_raw_identity(self, turn: Turn, revision_id: str) -> str:
        """Bind downstream receipts to the exact immutable Raw revision."""
        if not str(revision_id or ""):
            raise CanonicalRawCommitError("canonical_raw_commit_missing")
        raw_content_hash = self._raw_revision_content_hash(str(revision_id))
        if not raw_content_hash:
            raise CanonicalRawCommitError("canonical_raw_revision_header_missing")
        metadata = dict(turn.metadata or {})
        metadata["raw_event_id"] = str(revision_id)
        metadata["raw_content_hash"] = raw_content_hash
        turn.metadata = metadata
        return raw_content_hash

    def _requires_canonical_raw_receipt(self) -> bool:
        """Formal Sync never advances without a canonical Raw receipt."""
        return True

    def _persist_single_turn(
        self,
        source: AgentSource,
        session_info: SessionInfo,
        turn: Turn,
        content: str,
        content_hash: str,
        existing: Optional[Dict],
        artifact_path: str,
        raw_event_id: Optional[str] = None,
    ) -> SyncResult:
        """组装标签、保存内容、记录状态并采集信号。"""
        tags = self._build_tags(source, turn, session_info)
        tags.extend(source.build_extra_tags(turn))
        tags.append(f"content_hash={content_hash}")

        title = f"{source.name}-{session_info.session_id[:8]}-turn{turn.turn_number + 1}"
        if self._uses_raw_projection_backend():
            memories = []
        else:
            memories = self._save_content(content, tags, title)
        uids = [m.uid for m in memories] if memories else []
        status_str = "updated" if existing else "new"

        sync_record = self._make_sync_record_tuple(
            source.name,
            session_info.session_id,
            turn.turn_number,
            content_hash,
            uids,
            status_str,
            None,
            artifact_path,
        )
        signal = self._make_persona_signal_tuple(
            source,
            turn,
            session_info.session_id,
        )
        committed_turns = self._record_sync_and_persona_batch(
            [sync_record],
            [signal],
        )
        if committed_turns is None:
            raise RuntimeError("sync_persona_commit_failed")
        persona_committed = (
            source.name,
            session_info.session_id,
            int(turn.turn_number),
        ) in committed_turns
        if not persona_committed:
            raise RuntimeError("persona_projection_commit_missing")

        from core.ops.cognitive_pipeline_receipts import record_synced_turn

        record_synced_turn(
            self.db_path.parent,
            source_name=source.name,
            session_id=session_info.session_id,
            turn=turn,
            content_hash=content_hash,
            persona_committed=True,
        )

        return SyncResult(
            session_id=session_info.session_id,
            turn_number=turn.turn_number,
            action=status_str,
            backend_uids=uids,
            content_hash=content_hash,
            raw_event_id=raw_event_id,
        )

    def _uses_raw_projection_backend(self) -> bool:
        """Return True when canonical raw_events.db owns raw vault projection writes."""
        return bool(
            getattr(self, "raw_store", None) is not None
            and self.config.get("raw_projection.enabled", True)
        )

    def sync_single_turn(
        self,
        source: AgentSource,
        session_info: SessionInfo,
        turn: Turn,
        incremental: bool = True,
        check_backend_duplicate: bool = True,
        backend_duplicate_cache: Optional[Dict[Tuple[str, int, str], List[str]]] = None,
    ) -> SyncResult:
        """
        同步单轮对话。

        供 CaptureWorker 调用，复用完整的 8 步流水线。
        """
        session_info = self.canonicalize_session_info(session_info)
        try:
            self._assert_session_identity_activation(source, session_info)
        except CanonicalRawCommitError as exc:
            return SyncResult(
                session_id=session_info.session_id,
                turn_number=turn.turn_number,
                action="failed",
                error=str(exc),
            )
        if self._source_metadata_write_is_frozen(source.name, session_info.session_id):
            return SyncResult(
                session_id=session_info.session_id,
                turn_number=turn.turn_number,
                action="blocked",
                error="data_ownership_freeze",
            )
        try:
            self._ensure_reasoning_artifact(
                turn,
                source.name,
                session_info.session_id,
            )
        except (CanonicalRawCommitError, ValueError) as exc:
            return SyncResult(
                session_id=session_info.session_id,
                turn_number=turn.turn_number,
                action="failed",
                error=str(exc),
            )
        content = self._build_markdown(turn, session_info.session_id, source.model_tag)
        content = self._sanitize_content(content)
        content_hash = self._compute_content_hash(turn, content)
        artifact_path = self._artifact_path(turn)
        raw_event_id = self._record_raw_turn(source, session_info, turn, content_hash=content_hash)
        if self._requires_canonical_raw_receipt() and not raw_event_id:
            return self._make_failure_result(
                CanonicalRawCommitError("canonical_raw_commit_missing"),
                source,
                session_info,
                turn,
                content_hash,
                artifact_path,
            )
        try:
            self._bind_canonical_raw_identity(turn, str(raw_event_id or ""))
        except CanonicalRawCommitError as exc:
            return self._make_failure_result(
                exc,
                source,
                session_info,
                turn,
                content_hash,
                artifact_path,
                raw_event_id,
            )

        # Canonical Raw owns the complete native record, including a turn that
        # the semantic backend intentionally classifies as noise.  A caller
        # that requires lossless Raw capture can therefore wait for this receipt.
        if self._is_noise(turn):
            return SyncResult(
                session_id=session_info.session_id,
                turn_number=turn.turn_number,
                action="noise",
                content_hash=content_hash,
                raw_event_id=raw_event_id,
            )

        existing = self._check_synced(source.name, session_info.session_id, turn.turn_number)
        if self._is_synced_duplicate(existing, content_hash):
            persona_key = (
                source.name,
                session_info.session_id,
                int(turn.turn_number),
            )
            try:
                exact_persona = turn.turn_number in self._sync_log.exact_persona_turns(
                    source.name,
                    session_info.session_id,
                    [turn.turn_number],
                )
                if not exact_persona:
                    committed = self._record_sync_and_persona_batch(
                        [],
                        [
                            self._make_persona_signal_tuple(
                                source,
                                turn,
                                session_info.session_id,
                            )
                        ],
                        existing_sync_bindings=[
                            (
                                source.name,
                                session_info.session_id,
                                int(turn.turn_number),
                                content_hash,
                            )
                        ],
                    )
                    if committed is None or persona_key not in committed:
                        raise RuntimeError("persona_projection_repair_failed")

                from core.ops.cognitive_pipeline_receipts import record_synced_turn

                record_synced_turn(
                    self.db_path.parent,
                    source_name=source.name,
                    session_id=session_info.session_id,
                    turn=turn,
                    content_hash=content_hash,
                    persona_committed=True,
                )
                return SyncResult(
                    session_id=session_info.session_id,
                    turn_number=turn.turn_number,
                    action="skipped",
                    content_hash=content_hash,
                    raw_event_id=raw_event_id,
                )
            except _SYNC_PERSISTENCE_ERRORS as exc:
                return self._make_failure_result(
                    exc,
                    source,
                    session_info,
                    turn,
                    content_hash,
                    artifact_path,
                    raw_event_id,
                )

        backend_dupe = self._lookup_backend_duplicates(
            source,
            session_info,
            turn,
            content_hash,
            check_backend_duplicate,
            backend_duplicate_cache,
        )
        if backend_dupe:
            try:
                sync_record = self._make_sync_record_tuple(
                    source.name,
                    session_info.session_id,
                    turn.turn_number,
                    content_hash,
                    backend_dupe,
                    "skipped_backend",
                    None,
                    artifact_path,
                )
                signal = self._make_persona_signal_tuple(
                    source,
                    turn,
                    session_info.session_id,
                )
                committed = self._record_sync_and_persona_batch(
                    [sync_record],
                    [signal],
                )
                persona_key = (
                    source.name,
                    session_info.session_id,
                    int(turn.turn_number),
                )
                if committed is None or persona_key not in committed:
                    raise RuntimeError("backend_duplicate_persona_commit_failed")

                from core.ops.cognitive_pipeline_receipts import record_synced_turn

                record_synced_turn(
                    self.db_path.parent,
                    source_name=source.name,
                    session_id=session_info.session_id,
                    turn=turn,
                    content_hash=content_hash,
                    persona_committed=True,
                )
                return SyncResult(
                    session_id=session_info.session_id,
                    turn_number=turn.turn_number,
                    action="skipped",
                    backend_uids=backend_dupe,
                    content_hash=content_hash,
                    raw_event_id=raw_event_id,
                )
            except _SYNC_PERSISTENCE_ERRORS as exc:
                return self._make_failure_result(
                    exc,
                    source,
                    session_info,
                    turn,
                    content_hash,
                    artifact_path,
                    raw_event_id,
                )

        try:
            return self._persist_single_turn(
                source,
                session_info,
                turn,
                content,
                content_hash,
                existing,
                artifact_path,
                raw_event_id,
            )
        except _SYNC_PERSISTENCE_ERRORS as exc:
            return self._make_failure_result(
                exc,
                source,
                session_info,
                turn,
                content_hash,
                artifact_path,
                raw_event_id,
            )

    def sync_session(
        self,
        source: AgentSource,
        session_info: SessionInfo,
        incremental: bool = True,
    ) -> List[SyncResult]:
        """
        同步单个会话的所有轮次。

        Args:
            source: AgentSource 实例
            session_info: 会话信息
            incremental: 是否增量同步（只同步新增轮次）

        Returns:
            SyncResult 列表
        """
        session_info = self.canonicalize_session_info(session_info)
        self._assert_session_identity_activation(source, session_info)
        turns = parse_discovered_session(source, session_info)
        results: List[SyncResult] = []

        # 发射 polled 事件
        try:
            self._publish_sync_event(
                "polled",
                source.name,
                {
                    "file_path": str(session_info.source_path),
                    "session_id": session_info.session_id,
                },
            )
        except ImportError:
            logging.getLogger(__name__).warning(
                "Caught unexpected error at sync_engine.py", exc_info=True
            )

        # KIA Hook: session_start
        _ = source.on_session_start(
            session_info.session_id,
            {"working_dir": session_info.working_dir, "agent": source.name},
        )

        for turn in turns:
            result = self.sync_single_turn(source, session_info, turn, incremental)
            # 增量跳过时不加入结果（保持原有行为）
            if incremental and result.action == "skipped":
                continue
            results.append(result)

        # KIA Hook: session_end
        all_messages = []
        for t in turns:
            if t.user_content:
                all_messages.append({"role": "user", "content": t.user_content})
            if t.assistant_content:
                all_messages.append({"role": "assistant", "content": t.assistant_content})
        source.on_session_end(session_info.session_id, all_messages)

        return results

    def _build_backend_duplicate_cache(
        self,
        source: AgentSource,
        session_id: str,
    ) -> Dict[Tuple[str, int, str], List[str]]:
        """批量读取后端该 session 的已有记录（按 session 过滤，避免全库扫描）。"""
        backend_duplicate_cache: Dict[Tuple[str, int, str], List[str]] = {}
        try:
            session_memories = self.backend.list_by_tags(
                [f"source={source.name}", f"session={session_id}"], limit=None
            )
            for memory in session_memories or []:
                tags = list(getattr(memory, "tags", []) or [])
                turn_number: Optional[int] = None
                hashes: List[str] = []
                for tag in tags:
                    if tag.startswith("turn="):
                        try:
                            turn_number = int(tag.split("=", 1)[1]) - 1
                        except ValueError:
                            turn_number = None
                    elif tag.startswith("content_hash="):
                        hashes.append(tag.split("=", 1)[1])
                if turn_number is None:
                    continue
                for h in hashes:
                    key = (session_id, turn_number, h)
                    backend_duplicate_cache.setdefault(key, []).append(getattr(memory, "uid", ""))
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
            logger.debug("[SyncEngine] 构建 session 后端去重缓存失败", exc_info=True)
            raise BackendDuplicateStateUnavailableError(
                "backend_session_duplicate_cache_unavailable"
            ) from None
        return backend_duplicate_cache

    def _handle_sync_turn_error(
        self,
        exc: Exception,
        source_name: str,
        session_id: str,
        turn_number: int,
        content_hash: str,
        artifact_path: Optional[str],
        record_batch: List[tuple],
        raw_event_id: Optional[str] = None,
    ) -> SyncResult:
        """统一处理单 turn 同步异常，生成失败记录。"""
        if isinstance(exc, StorageRateLimitError):
            err_msg = f"rate_limit: {exc}"
        elif isinstance(exc, StorageAuthError):
            err_msg = f"auth_error: {exc}"
        elif isinstance(exc, StorageServerError):
            err_msg = f"server_error: {exc}"
        else:
            err_msg = str(exc)

        record_batch.append(
            self._make_sync_record_tuple(
                source_name,
                session_id,
                turn_number,
                content_hash,
                [],
                "failed",
                err_msg,
                artifact_path,
            )
        )
        return SyncResult(
            session_id=session_id,
            turn_number=turn_number,
            action="failed",
            content_hash=content_hash,
            error=err_msg,
            raw_event_id=raw_event_id,
        )

    def _sync_turn_in_batch(
        self,
        source: AgentSource,
        session_info: SessionInfo,
        turn: Turn,
        sync_state_cache: Dict[int, Dict],
        exact_persona_turns: set[int],
        backend_duplicate_cache: Dict[Tuple[str, int, str], List[str]],
        record_batch: List[tuple],
        signal_batch: List[tuple],
        existing_sync_bindings: List[tuple[str, str, int, str]],
    ) -> SyncResult:
        """同步单个 turn，并将需要写入 sync_log / user_signals 的记录追加到批量列表。"""
        session_id = session_info.session_id
        if self._source_metadata_write_is_frozen(source.name, session_id):
            return SyncResult(
                session_id=session_id,
                turn_number=turn.turn_number,
                action="blocked",
                error="data_ownership_freeze",
            )

        try:
            self._ensure_reasoning_artifact(turn, source.name, session_id)
        except (CanonicalRawCommitError, ValueError) as exc:
            return self._handle_sync_turn_error(
                exc,
                source.name,
                session_id,
                turn.turn_number,
                "",
                "",
                record_batch,
            )

        content = self._build_markdown(turn, session_id, source.model_tag)
        content = self._sanitize_content(content)
        content_hash = self._compute_content_hash(turn, content)

        artifact_path = (turn.metadata or {}).get("artifact_path", "") or (turn.metadata or {}).get(
            "reasoning_artifact_path", ""
        )
        raw_event_id = self._record_raw_turn(source, session_info, turn, content_hash=content_hash)
        if self._requires_canonical_raw_receipt() and not raw_event_id:
            return self._handle_sync_turn_error(
                CanonicalRawCommitError("canonical_raw_commit_missing"),
                source.name,
                session_id,
                turn.turn_number,
                content_hash,
                artifact_path,
                record_batch,
            )
        try:
            self._bind_canonical_raw_identity(turn, str(raw_event_id or ""))
        except CanonicalRawCommitError as exc:
            return self._handle_sync_turn_error(
                exc,
                source.name,
                session_id,
                turn.turn_number,
                content_hash,
                artifact_path,
                record_batch,
                raw_event_id,
            )

        # Keep native Raw lossless even when the semantic backend classifies a
        # turn as noise.  The Raw receipt is what drives continuous cursors.
        if self._is_noise(turn):
            return SyncResult(
                session_id=session_id,
                turn_number=turn.turn_number,
                action="noise",
                content_hash=content_hash,
                raw_event_id=raw_event_id,
            )

        existing = sync_state_cache.get(turn.turn_number)
        if (
            existing
            and existing.get("status") in _SYNCED_STATUSES
            and existing.get("content_hash") == content_hash
        ):
            backend_uids = existing.get("backend_uids", [])
            if turn.turn_number not in exact_persona_turns:
                existing_sync_bindings.append(
                    (
                        source.name,
                        session_id,
                        int(turn.turn_number),
                        content_hash,
                    )
                )
                signal_batch.append(self._make_persona_signal_tuple(source, turn, session_id))
            return SyncResult(
                session_id=session_id,
                turn_number=turn.turn_number,
                action="skipped",
                content_hash=content_hash,
                backend_uids=backend_uids,
                raw_event_id=raw_event_id,
            )

        backend_dupe = backend_duplicate_cache.get((session_id, turn.turn_number, content_hash), [])
        if backend_dupe:
            record_batch.append(
                self._make_sync_record_tuple(
                    source.name,
                    session_id,
                    turn.turn_number,
                    content_hash,
                    backend_dupe,
                    "skipped_backend",
                    None,
                    artifact_path,
                )
            )
            signal_batch.append(self._make_persona_signal_tuple(source, turn, session_id))
            return SyncResult(
                session_id=session_id,
                turn_number=turn.turn_number,
                action="skipped",
                backend_uids=backend_dupe,
                content_hash=content_hash,
                raw_event_id=raw_event_id,
            )

        tags = self._build_tags(source, turn, session_info)
        tags.extend(source.build_extra_tags(turn))
        tags.append(f"content_hash={content_hash}")
        title = f"{source.name}-{session_id[:8]}-turn{turn.turn_number + 1}"

        try:
            # Canonical Raw owns the raw-vault projection.  The single-turn
            # path already avoids the alternate backend in this mode; batch
            # reconciliation must preserve that same ownership boundary.
            memories = (
                []
                if self._uses_raw_projection_backend()
                else self._save_content(content, tags, title)
            )
            uids = [m.uid for m in memories] if memories else []
            status_str = "updated" if existing else "new"
            record_batch.append(
                self._make_sync_record_tuple(
                    source.name,
                    session_id,
                    turn.turn_number,
                    content_hash,
                    uids,
                    status_str,
                    None,
                    artifact_path,
                )
            )
            signal_batch.append(self._make_persona_signal_tuple(source, turn, session_id))
            return SyncResult(
                session_id=session_id,
                turn_number=turn.turn_number,
                action=status_str,
                backend_uids=uids,
                content_hash=content_hash,
                raw_event_id=raw_event_id,
            )
        except _SYNC_PERSISTENCE_ERRORS as e:
            return self._handle_sync_turn_error(
                e,
                source.name,
                session_id,
                turn.turn_number,
                content_hash,
                artifact_path,
                record_batch,
                raw_event_id,
            )

    def sync_turns(
        self,
        source: AgentSource,
        session_info: SessionInfo,
        turns: List[Turn],
        incremental: bool = True,
        enqueue_distillation: bool = True,
    ) -> List[SyncResult]:
        """
        批量同步一个 session 的多个 turn。

        相比逐条调用 ``sync_single_turn``，本方法：
        - 只查询一次 ``sync_log`` 状态；
        - 只查询一次后端该 session 的已有记录；
        - 将所有 ``sync_log`` 写入和用户信号写入合并为批量 ``executemany``。

        这显著降低历史回填 / daemon L1 同步时的数据库 IO。
        """
        results: List[SyncResult] = []
        if not turns:
            return results

        session_info = self.canonicalize_session_info(session_info)
        session_id = session_info.session_id
        # 1. 批量读取本地 sync_log
        turn_numbers = [t.turn_number for t in turns]
        sync_state_cache = self._check_synced_batch(source.name, session_id, turn_numbers)
        exact_persona_turns = self._sync_log.exact_persona_turns(
            source.name,
            session_id,
            turn_numbers,
        )

        # 2. Canonical Raw projection is the only owner of the raw vault.  Do
        # not query the alternate backend for duplicates in that mode: apart from
        # wasting a full session index lookup, that path can re-enter a
        # rejected direct-raw write implementation.  The canonical Raw receipt
        # and sync_log remain the authoritative idempotency evidence.
        backend_duplicate_cache = (
            {}
            if self._uses_raw_projection_backend()
            else self._build_backend_duplicate_cache(source, session_id)
        )

        record_batch: List[tuple] = []
        signal_batch: List[tuple] = []
        existing_sync_bindings: List[tuple[str, str, int, str]] = []

        for turn in turns:
            result = self._sync_turn_in_batch(
                source,
                session_info,
                turn,
                sync_state_cache,
                exact_persona_turns,
                backend_duplicate_cache,
                record_batch,
                signal_batch,
                existing_sync_bindings,
            )
            results.append(result)

        persona_committed_turns = self._record_sync_and_persona_batch(
            record_batch,
            signal_batch,
            existing_sync_bindings=existing_sync_bindings,
        )
        required_persona_keys = {
            (str(signal[1]), str(signal[2]), int(signal[3])) for signal in signal_batch
        }
        committed_persona_keys = frozenset(persona_committed_turns or ())
        sync_batch_committed = (
            persona_committed_turns is not None and required_persona_keys <= committed_persona_keys
        )
        if not sync_batch_committed:
            persona_committed_turns = committed_persona_keys
            for turn, result in zip(turns, results):
                if (
                    source.name,
                    session_id,
                    int(turn.turn_number),
                ) in required_persona_keys:
                    result.action = "failed"
                    result.error = "sync_persona_batch_commit_failed"
        else:
            persona_committed_turns = frozenset(
                set(committed_persona_keys)
                | {
                    (source.name, session_id, int(turn_number))
                    for turn_number in exact_persona_turns
                }
            )
        for turn, result in zip(turns, results):
            if result.action in {"new", "updated", "skipped"}:
                from core.ops.cognitive_pipeline_receipts import record_synced_turn

                try:
                    record_synced_turn(
                        self.db_path.parent,
                        source_name=source.name,
                        session_id=session_id,
                        turn=turn,
                        content_hash=str(result.content_hash or ""),
                        persona_committed=(
                            source.name,
                            session_id,
                            int(turn.turn_number),
                        )
                        in persona_committed_turns,
                    )
                except _SYNC_PERSISTENCE_ERRORS:
                    result.action = "failed"
                    result.error = "cognitive_sync_receipt_commit_failed"
                    self._record_sync(
                        source.name,
                        session_id,
                        turn.turn_number,
                        str(result.content_hash or ""),
                        [],
                        "failed",
                        error=result.error,
                        artifact_path=self._artifact_path(turn),
                    )

        # Direct batch callers re-ack the full input every poll. Exact revisions are
        # idempotent, and a crash between L1 and Amphora therefore repairs on retry.
        if enqueue_distillation and results and not any(r.action == "failed" for r in results):
            self.enqueue_session_for_distillation(source, session_info, turns)

        return results

    def enqueue_session_for_distillation(
        self,
        source: AgentSource,
        session_info: SessionInfo,
        turns: List[Turn],
    ) -> Dict[str, Any]:
        """Enqueue one complete canonical session after Raw coverage is confirmed."""
        return enqueue_complete_session(
            database_dir=self.db_path.parent,
            source=source,
            session_info=self.canonicalize_session_info(session_info),
            turns=turns,
        )

    def bind_session_raw_identities(
        self,
        source: AgentSource,
        session_info: SessionInfo,
        turns: List[Turn],
    ) -> List[Turn]:
        """Re-ack a complete parsed session into Raw and bind immutable revisions.

        Backfill may sync only the missing tail while the complete-session
        handoff includes older turns.  Re-acking every parsed turn is
        idempotent for unchanged Raw content and repairs a missing/stale Raw
        identity before any complete-session receipt can be published.
        """
        canonical_session = self.canonicalize_session_info(session_info)
        self._assert_session_identity_activation(source, canonical_session)
        for turn in turns:
            content = self._build_markdown(
                turn,
                canonical_session.session_id,
                source.model_tag,
            )
            content = self._sanitize_content(content)
            content_hash = self._compute_content_hash(turn, content)
            revision_id = self._record_raw_turn(
                source,
                canonical_session,
                turn,
                content_hash=content_hash,
            )
            try:
                self._bind_canonical_raw_identity(
                    turn,
                    str(revision_id or ""),
                )
            except CanonicalRawCommitError as exc:
                raise CanonicalRawCommitError("complete_session_raw_identity_missing") from exc
        return turns

    # ---------- 流水线步骤 ----------
