# -*- coding: utf-8 -*-
"""
SQLite 连接安全工具

问题：Python 内置 sqlite3.connect 的上下文管理器只处理事务（commit/rollback），
      不会自动调用 close()。在高频循环中（如 daemon 的 CaptureQueue 每几秒 tick
      一次），重复打开不关闭的连接会导致文件描述符泄漏，最终触发
      "OSError: [Errno 24] Too many open files"。

本模块提供 drop-in 替代：
  - sqlite_conn()  — with 退出时自动 close()，并默认启用 WAL/busy_timeout/锁重试
  - SqlitePool    — 为高频组件（CaptureQueue / SyncEngine）提供持久连接复用

用法：
    # 旧代码（泄漏）
    with sqlite3.connect(path, timeout=10) as conn:
        ...

    # 新代码（安全）
    from core.db_utils import sqlite_conn
    with sqlite_conn(path, timeout=10) as conn:
        ...

    # 高频组件（持久连接）
    pool = SqlitePool(path)
    conn = pool.get_conn()
    ...
    pool.close()
"""

from __future__ import annotations

import logging
import re
import sqlite3
import string
import sys
import threading
import time
from collections.abc import Collection, Mapping, Sequence
from contextlib import contextmanager
from functools import wraps
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict


DEFAULT_SQLITE_TIMEOUT_SECONDS = 30.0
DEFAULT_BUSY_TIMEOUT_MS = 30_000
DEFAULT_LOCK_RETRIES = 3
DEFAULT_LOCK_RETRY_BASE_SECONDS = 0.05
_LOCKED_ERROR_MARKERS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
)

_SQL_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def validate_sql_identifier(name: str) -> str:
    """Validate that *name* is a safe SQL identifier.

    Only ASCII letters, digits, and underscores are allowed, and the name
    must not start with a digit.  This covers table names, column names,
    and other identifier-like tokens that cannot be passed as query
    parameters.

    Args:
        name: Identifier candidate.

    Returns:
        The unchanged identifier (useful for fluent composition).

    Raises:
        ValueError: If *name* is not a valid SQL identifier.
    """
    if not _SQL_IDENTIFIER_RE.fullmatch(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def _quote_sql_identifier_path(name: str) -> str:
    parts = name.split(".")
    if not parts:
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return ".".join(f'"{validate_sql_identifier(part)}"' for part in parts)


def render_sql(
    template: str,
    *,
    identifiers: Mapping[str, str] | None = None,
    identifier_lists: Mapping[str, Sequence[str]] | None = None,
    placeholder_counts: Mapping[str, int] | None = None,
    fixed_fragments: Mapping[str, tuple[str, Collection[str]]] | None = None,
) -> str:
    """Render the only dynamic SQL shapes allowed at SQLite call sites.

    Bound parameters remain the authority for values.  This renderer exists
    solely for syntax that SQLite cannot bind: identifiers, identifier lists,
    generated ``?`` lists, and fragments selected from a caller-owned finite
    contract.  Every template token must be supplied exactly once and every
    identifier is quoted after strict validation.
    """

    identifier_values = dict(identifiers or {})
    identifier_list_values = dict(identifier_lists or {})
    placeholder_values = dict(placeholder_counts or {})
    fixed_values = dict(fixed_fragments or {})
    groups = (
        set(identifier_values),
        set(identifier_list_values),
        set(placeholder_values),
        set(fixed_values),
    )
    supplied = set().union(*groups)
    if sum(len(group) for group in groups) != len(supplied):
        raise ValueError("SQL template tokens must be supplied exactly once")

    expected: set[str] = set()
    for _literal, field_name, format_spec, conversion in string.Formatter().parse(template):
        if field_name is None:
            continue
        if not _SQL_IDENTIFIER_RE.fullmatch(field_name) or format_spec or conversion:
            raise ValueError(f"Invalid SQL template token: {field_name!r}")
        expected.add(field_name)
    if expected != supplied:
        raise ValueError(
            "SQL template tokens do not match supplied tokens: "
            f"expected={sorted(expected)!r}, supplied={sorted(supplied)!r}"
        )

    rendered: dict[str, str] = {
        key: _quote_sql_identifier_path(value)
        for key, value in identifier_values.items()
    }
    for key, values in identifier_list_values.items():
        if not values:
            raise ValueError(f"SQL identifier list {key!r} must not be empty")
        rendered[key] = ", ".join(_quote_sql_identifier_path(value) for value in values)
    for key, count in placeholder_values.items():
        if isinstance(count, bool) or int(count) != count or count <= 0:
            raise ValueError(f"SQL placeholder count {key!r} must be a positive integer")
        rendered[key] = ", ".join("?" for _ in range(int(count)))
    for key, (value, allowed) in fixed_values.items():
        if value not in allowed:
            raise ValueError(f"SQL fragment {key!r} is not in its fixed contract")
        rendered[key] = value
    return template.format_map(rendered)


def sqlite_artifact_path(db_path: Path) -> Path:
    """Return the canonical SQLite artifact path for a database."""
    return Path(db_path)


def sqlite_artifact_exists(db_path: Path) -> bool:
    """True when the canonical SQLite database exists."""
    return sqlite_artifact_path(db_path).exists()


def sqlite_artifact_size(db_path: Path) -> tuple[int, bool]:
    """Return artifact size and the encrypted-compat flag, now always False."""
    path = Path(db_path)
    if path.exists():
        return path.stat().st_size, False
    return 0, False


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _LOCKED_ERROR_MARKERS)


class RetryingConnection(sqlite3.Connection):
    """sqlite3.Connection subclass that retries transient lock errors."""

    def _configure_lock_retry(self, retries: int, base_seconds: float) -> None:
        self._mnemos_lock_retries = max(0, int(retries))
        self._mnemos_lock_retry_base = max(0.0, float(base_seconds))

    def _with_lock_retry(self, operation, *args, **kwargs):
        retries = int(getattr(self, "_mnemos_lock_retries", DEFAULT_LOCK_RETRIES))
        base = float(getattr(self, "_mnemos_lock_retry_base", DEFAULT_LOCK_RETRY_BASE_SECONDS))
        logger = logging.getLogger(__name__)
        for attempt in range(retries + 1):
            try:
                return operation(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not _is_lock_error(exc) or attempt >= retries:
                    raise
                delay = base * (2**attempt)
                logger.info(
                    "[SQLite] database locked; retrying operation in %.3fs "
                    "(attempt %s/%s)",
                    delay,
                    attempt + 1,
                    retries,
                )
                time.sleep(delay)
        raise RuntimeError("unreachable sqlite lock retry state")

    def execute(self, sql, parameters=(), /):  # type: ignore[override]
        return self._with_lock_retry(super().execute, sql, parameters)

    def executemany(self, sql, seq_of_parameters, /):  # type: ignore[override]
        return self._with_lock_retry(super().executemany, sql, seq_of_parameters)

    def executescript(self, sql, /):  # type: ignore[override]
        return self._with_lock_retry(super().executescript, sql)

    def commit(self):  # type: ignore[override]
        return self._with_lock_retry(super().commit)


def delete_older_than(
    conn: sqlite3.Connection,
    table: str,
    timestamp_col: str,
    days: int,
    limit: int = 1000,
    dry_run: bool = False,
) -> int:
    """按时间戳批量删除/统计过期行，避免单个大事务。

    使用 ``DELETE ... WHERE rowid IN (SELECT rowid FROM ... WHERE ts < ? LIMIT ?)``
    控制每次事务规模。``dry_run=True`` 时只返回将要清理的行数，不实际删除。

    Args:
        conn: SQLite 连接（需已打开且未关闭）。
        table: 目标表名。
        timestamp_col: 时间戳列名。
        days: 保留天数，早于 ``now - days`` 的行会被清理。
        limit: 每批最大删除/统计行数。
        dry_run: 为 True 时仅统计，不执行 DELETE。

    Returns:
        删除/统计到的过期行总数。
    """
    validate_sql_identifier(table)
    validate_sql_identifier(timestamp_col)
    if days < 0:
        days = 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    total = 0
    while True:
        if dry_run:
            cursor = conn.execute(
                " ".join([
                    "SELECT COUNT(*) FROM",
                    table,
                    "WHERE",
                    timestamp_col,
                    "< ?",
                ]),
                (cutoff,),
            )
            total += cursor.fetchone()[0]
            break
        cursor = conn.execute(
            " ".join([
                "DELETE FROM",
                table,
                "WHERE rowid IN (",
                "SELECT rowid FROM",
                table,
                "WHERE",
                timestamp_col,
                "< ?",
                "LIMIT ?",
                ")",
            ]),
            (cutoff, limit),
        )
        conn.commit()
        total += cursor.rowcount
        if cursor.rowcount < limit:
            break
    return total


def configure_sqlite_connection(
    conn: sqlite3.Connection,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    wal: bool = True,
) -> sqlite3.Connection:
    """Apply Mnemos' default SQLite runtime pragmas to a connection."""
    try:
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        if wal and not bool(getattr(conn, "_mnemos_memory_backed", False)):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        logging.getLogger(__name__).warning(
            "[SQLite] Failed to configure connection pragmas", exc_info=True
        )
    return conn


def _connect_with_defaults(*args: Any, **kwargs: Any) -> sqlite3.Connection:
    lock_retries = int(kwargs.pop("lock_retries", DEFAULT_LOCK_RETRIES))
    lock_retry_base = float(kwargs.pop("lock_retry_base", DEFAULT_LOCK_RETRY_BASE_SECONDS))
    busy_timeout_ms = int(kwargs.pop("busy_timeout_ms", DEFAULT_BUSY_TIMEOUT_MS))
    wal = bool(kwargs.pop("wal", True))
    kwargs.setdefault("timeout", DEFAULT_SQLITE_TIMEOUT_SECONDS)
    kwargs.setdefault("factory", RetryingConnection)

    conn = sqlite3.connect(*args, **kwargs)
    if isinstance(conn, RetryingConnection):
        conn._configure_lock_retry(lock_retries, lock_retry_base)
    return configure_sqlite_connection(conn, busy_timeout_ms=busy_timeout_ms, wal=wal)


def _should_force_transient_pool(db_path: Path) -> bool:
    """Return whether this path must use short-lived pool connections."""
    return False


def release_transient_pools(owner: Any, *names: str) -> None:
    """Release transient SQLite connections held by attributes on *owner*."""
    for name in names:
        pool = getattr(owner, name, None)
        release = getattr(pool, "release_transient_connections", None)
        if callable(release):
            release()


def auto_release_transient_pools(*names: str):
    """Decorator for classes using transient SqlitePool connections."""

    def decorate(method):
        @wraps(method)
        def wrapped(self, *args, **kwargs):
            try:
                return method(self, *args, **kwargs)
            finally:
                release_transient_pools(self, *names)
                raw_store = getattr(self, "raw_store", None)
                if raw_store is not None:
                    release_transient_pools(raw_store, "_pool")

        return wrapped

    return decorate


@contextmanager
def sqlite_conn(*args, **kwargs):
    """sqlite3.connect 的安全替代：with 块退出时自动 close()。

    事务行为与原始 sqlite3.connect 上下文管理器完全一致：
    - 无异常时 commit
    - 有异常时 rollback
    - 最终无论是否异常都 close
    """
    conn = _connect_with_defaults(*args, **kwargs)
    try:
        yield conn
    finally:
        try:
            if sys.exc_info()[0] is None:
                conn.commit()
            else:
                conn.rollback()
        finally:
            conn.close()


class SqlitePool:
    """SQLite 持久连接池（按线程隔离连接）。

    适用于高频访问场景（如 CaptureQueue、SyncEngine），避免每操作一次
    都新建/销毁连接。每个线程拥有独立连接，避免 SQLite 线程限制。
    """

    # [P2-27] 清理最小间隔（秒），避免每次 get_conn 都遍历连接池
    _CLEANUP_INTERVAL = 60.0

    def __init__(
        self,
        db_path: Path,
        timeout: float = DEFAULT_SQLITE_TIMEOUT_SECONDS,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        wal: bool = True,
        lock_retries: int = DEFAULT_LOCK_RETRIES,
        lock_retry_base: float = DEFAULT_LOCK_RETRY_BASE_SECONDS,
        persistent: bool = True,
    ):
        self.db_path = Path(db_path)
        self._timeout = timeout
        self._busy_timeout_ms = busy_timeout_ms
        self._wal = wal
        self._lock_retries = lock_retries
        self._lock_retry_base = lock_retry_base
        self._persistent = bool(persistent and not _should_force_transient_pool(self.db_path))
        self._conns: Dict[int, sqlite3.Connection] = {}
        self._transient_local = threading.local()
        self._transient_conns: set[sqlite3.Connection] = set()
        self._lock = threading.Lock()
        self._last_cleanup = 0.0

    def _open_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=self._timeout,
            check_same_thread=False,
            factory=RetryingConnection,
        )
        conn._configure_lock_retry(self._lock_retries, self._lock_retry_base)
        return configure_sqlite_connection(
            conn,
            busy_timeout_ms=self._busy_timeout_ms,
            wal=self._wal,
        )

    def _cleanup_dead_connections(self):
        """清理已终止线程的遗留连接，防止 ident 重用导致泄漏。

        [P2-27] 增加时间间隔限制，避免频繁遍历连接池。
        """
        now = time.time()
        if now - self._last_cleanup < self._CLEANUP_INTERVAL:
            return
        self._last_cleanup = now

        live_tids = {th.ident for th in threading.enumerate() if th.ident is not None}
        dead_tids = [t for t in list(self._conns.keys()) if t not in live_tids]
        for t in dead_tids:
            conn = self._conns.pop(t, None)
            if conn is not None:
                try:
                    conn.close()
                except (
                    OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError,
                    sqlite3.Error
                ):
                    logging.getLogger(__name__).warning("Unexpected error", exc_info=True)

    def get_conn(self) -> sqlite3.Connection:
        """获取（或创建）当前线程的持久连接。"""
        if not self._persistent:
            conn = self._open_conn()
            conn.row_factory = None  # noqa
            local_conns = getattr(self._transient_local, "conns", None)
            if local_conns is None:
                local_conns = []
                self._transient_local.conns = local_conns
            local_conns.append(conn)
            with self._lock:
                self._transient_conns.add(conn)
            return conn

        tid = threading.current_thread().ident
        with self._lock:
            self._cleanup_dead_connections()
            if tid not in self._conns:
                self._conns[tid] = self._open_conn()  # type: ignore[index]
            conn = self._conns[tid]  # type: ignore[index,assignment]
        conn.row_factory = None  # noqa
        # 防御：如果上次异常退出留下挂起事务，自动回滚
        try:
            if conn.in_transaction:
                conn.rollback()
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logging.getLogger(__name__).warning("Unexpected error", exc_info=True)
        return conn

    def close(self) -> None:
        """关闭所有线程的持久连接。"""
        self._close_all_transient_connections()
        with self._lock:
            for conn in list(self._conns.values()):
                try:
                    conn.close()
                except (
                    OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError,
                    sqlite3.Error
                ):
                    logging.getLogger(__name__).warning("Unexpected error", exc_info=True)
            self._conns.clear()

    def release_transient_connections(self) -> None:
        """Close short-lived connections opened in non-persistent mode."""
        if self._persistent:
            return
        local_conns = list(getattr(self._transient_local, "conns", []))
        self._transient_local.conns = []
        with self._lock:
            for conn in local_conns:
                self._transient_conns.discard(conn)
        for conn in local_conns:
            try:
                conn.close()
            except (
                OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError,
                sqlite3.Error
            ):
                logging.getLogger(__name__).warning("Unexpected error", exc_info=True)

    def _close_all_transient_connections(self) -> None:
        """Close transient connections left by worker threads during pool shutdown."""
        if self._persistent:
            return
        with self._lock:
            conns = list(self._transient_conns)
            self._transient_conns.clear()
        self._transient_local.conns = []
        for conn in conns:
            try:
                conn.close()
            except (
                OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError,
                sqlite3.Error
            ):
                logging.getLogger(__name__).warning("Unexpected error", exc_info=True)

    def __enter__(self) -> "SqlitePool":
        return self

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        self.close()
