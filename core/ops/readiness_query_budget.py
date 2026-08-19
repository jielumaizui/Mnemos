"""Scoped read-only SQLite deadline support for operational readiness health.

The full cognitive-readiness audit intentionally has no artificial query budget:
it is an evidence-completeness tool.  The interactive health command is
different: it must return a truthful degraded result instead of indefinitely
blocking on a large or locked local database.  This module supplies that
health-only scope without changing ordinary read-only audit behaviour.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from core.ops.durable_io import DurableIOError, inspect_path_kind


READINESS_QUERY_TIMEOUT = "readiness_query_timeout"
READINESS_DB_BUSY = "readiness_db_busy"
_LOCKED_ERROR_MARKERS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "database is busy",
)


class ReadinessQueryDeadlineExceeded(RuntimeError):
    """Raised before a health-scoped SQLite query can start after its deadline."""


@dataclass
class _ReadinessQueryBudget:
    deadline_monotonic: float
    timed_out: bool = False
    busy: bool = False

    def remaining_seconds(self) -> float:
        return self.deadline_monotonic - time.monotonic()

    def progress_handler(self) -> int:
        if self.remaining_seconds() <= 0:
            self.timed_out = True
            return 1
        return 0

    def record_sqlite_error(self, exc: sqlite3.Error) -> None:
        message = str(exc).lower()
        if any(marker in message for marker in _LOCKED_ERROR_MARKERS):
            self.busy = True
        elif "interrupted" in message or self.remaining_seconds() <= 0:
            self.timed_out = True

    def failure_code(self) -> str | None:
        if self.busy:
            return READINESS_DB_BUSY
        if self.timed_out or self.remaining_seconds() <= 0:
            return READINESS_QUERY_TIMEOUT
        return None


_ACTIVE_READINESS_QUERY_BUDGET: ContextVar[_ReadinessQueryBudget | None] = ContextVar(
    "mnemos_health_readiness_query_budget",
    default=None,
)


def _sqlite_file_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(stat.S_IFMT(metadata.st_mode)),
    )


def _canonical_sqlite_path(path: Path) -> Path:
    candidate = Path(path).expanduser()
    try:
        canonical = candidate.parent.resolve(strict=True) / candidate.name
    except OSError:
        raise DurableIOError("readonly_sqlite_parent_unavailable") from None
    kind = inspect_path_kind(canonical)
    if kind == "missing":
        raise FileNotFoundError(canonical)
    if kind != "file":
        raise DurableIOError("readonly_sqlite_path_not_regular")
    return canonical


def _verify_sqlite_anchor(
    descriptor: int,
    path: Path,
    expected_identity: tuple[int, int, int],
) -> None:
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
    except OSError:
        raise DurableIOError("readonly_sqlite_identity_changed") from None
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or _sqlite_file_identity(opened) != expected_identity
        or _sqlite_file_identity(current) != expected_identity
    ):
        raise DurableIOError("readonly_sqlite_identity_changed")


class _VerifiedReadOnlyCursor(sqlite3.Cursor):
    """Revalidate the public pathname around every cursor operation."""

    def _verify(self) -> None:
        connection = self.connection
        if isinstance(connection, _VerifiedReadOnlyConnection):
            connection._verify_readonly_anchor()  # noqa: SLF001

    def execute(self, sql, parameters=(), /):  # type: ignore[override]
        self._verify()
        result = super().execute(sql, parameters)
        self._verify()
        return result

    def executemany(self, sql, seq_of_parameters, /):  # type: ignore[override]
        self._verify()
        result = super().executemany(sql, seq_of_parameters)
        self._verify()
        return result

    def executescript(self, sql_script, /):  # type: ignore[override]
        self._verify()
        result = super().executescript(sql_script)
        self._verify()
        return result

    def fetchone(self):  # type: ignore[override]
        self._verify()
        result = super().fetchone()
        self._verify()
        return result

    def fetchmany(self, size=None):  # type: ignore[override]
        self._verify()
        result = super().fetchmany() if size is None else super().fetchmany(size)
        self._verify()
        return result

    def fetchall(self):  # type: ignore[override]
        self._verify()
        result = super().fetchall()
        self._verify()
        return result


class _VerifiedReadOnlyConnection(sqlite3.Connection):
    """Keep a no-follow inode anchor for the complete read-only connection."""

    _readonly_anchor_fd = -1
    _readonly_anchor_path: Path | None = None
    _readonly_anchor_identity: tuple[int, int, int] | None = None

    def _bind_readonly_anchor(
        self,
        descriptor: int,
        path: Path,
        identity: tuple[int, int, int],
    ) -> None:
        self._readonly_anchor_fd = descriptor
        self._readonly_anchor_path = path
        self._readonly_anchor_identity = identity

    def _verify_readonly_anchor(self) -> None:
        if (
            self._readonly_anchor_fd < 0
            or self._readonly_anchor_path is None
            or self._readonly_anchor_identity is None
        ):
            return
        _verify_sqlite_anchor(
            self._readonly_anchor_fd,
            self._readonly_anchor_path,
            self._readonly_anchor_identity,
        )

    def cursor(self, factory=None):  # type: ignore[override]
        self._verify_readonly_anchor()
        return super().cursor(factory or _VerifiedReadOnlyCursor)

    def execute(self, sql, parameters=(), /):  # type: ignore[override]
        self._verify_readonly_anchor()
        result = super().execute(sql, parameters)
        self._verify_readonly_anchor()
        return result

    def executemany(self, sql, seq_of_parameters, /):  # type: ignore[override]
        self._verify_readonly_anchor()
        result = super().executemany(sql, seq_of_parameters)
        self._verify_readonly_anchor()
        return result

    def executescript(self, sql_script, /):  # type: ignore[override]
        self._verify_readonly_anchor()
        result = super().executescript(sql_script)
        self._verify_readonly_anchor()
        return result

    def close(self) -> None:
        failure: DurableIOError | None = None
        try:
            self._verify_readonly_anchor()
        except DurableIOError as exc:
            failure = exc
        descriptor = self._readonly_anchor_fd
        self._readonly_anchor_fd = -1
        try:
            super().close()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if failure is not None:
            raise failure

    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


class _BudgetedReadOnlyConnection(_VerifiedReadOnlyConnection):
    """Record health-scope SQLite failures without changing normal semantics."""

    def execute(self, sql, parameters=(), /):  # type: ignore[override]
        try:
            return super().execute(sql, parameters)
        except sqlite3.Error as exc:
            budget = _ACTIVE_READINESS_QUERY_BUDGET.get()
            if budget is not None:
                budget.record_sqlite_error(exc)
            raise


@contextmanager
def health_readiness_query_budget(timeout_seconds: float) -> Iterator[None]:
    """Apply one monotonic, read-only SQLite deadline within health only."""
    timeout = max(0.0, float(timeout_seconds))
    budget = _ReadinessQueryBudget(time.monotonic() + timeout)
    token = _ACTIVE_READINESS_QUERY_BUDGET.set(budget)
    try:
        yield
    finally:
        _ACTIVE_READINESS_QUERY_BUDGET.reset(token)


def readiness_query_failure_code() -> str | None:
    """Return the stable health failure category for the active query scope."""
    budget = _ACTIVE_READINESS_QUERY_BUDGET.get()
    return budget.failure_code() if budget is not None else None


def connect_readonly_sqlite(
    db_path: Path,
    *,
    timeout_seconds: float = 5.0,
    immutable: bool = False,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open SQLite read-only and attach the active health deadline if present."""
    budget = _ACTIVE_READINESS_QUERY_BUDGET.get()
    timeout = max(0.0, float(timeout_seconds))
    if budget is not None:
        remaining = budget.remaining_seconds()
        if remaining <= 0:
            budget.timed_out = True
            raise ReadinessQueryDeadlineExceeded(READINESS_QUERY_TIMEOUT)
        timeout = min(timeout, remaining)
    connection: sqlite3.Connection | None = None
    anchor_descriptor = -1
    anchor_transferred = False
    try:
        canonical = _canonical_sqlite_path(Path(db_path))
        anchor_descriptor = os.open(
            canonical,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(anchor_descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise DurableIOError("readonly_sqlite_path_not_regular")
        identity = _sqlite_file_identity(opened)
        _verify_sqlite_anchor(anchor_descriptor, canonical, identity)
        query = "?mode=ro&immutable=1" if immutable else "?mode=ro"
        connection = sqlite3.connect(
            canonical.as_uri() + query,
            uri=True,
            timeout=timeout,
            check_same_thread=check_same_thread,
            factory=(
                _BudgetedReadOnlyConnection
                if budget is not None
                else _VerifiedReadOnlyConnection
            ),
        )
        _verify_sqlite_anchor(anchor_descriptor, canonical, identity)
        if isinstance(connection, _VerifiedReadOnlyConnection):
            connection._bind_readonly_anchor(  # noqa: SLF001
                anchor_descriptor,
                canonical,
                identity,
            )
            anchor_transferred = True
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA query_only").fetchone() != (1,):
            raise DurableIOError("readonly_sqlite_query_only_unverified")
        main_paths = [
            Path(str(row[2])).expanduser().parent.resolve(strict=True)
            / Path(str(row[2])).name
            for row in connection.execute("PRAGMA database_list").fetchall()
            if str(row[1]) == "main"
        ]
        if main_paths != [canonical]:
            raise DurableIOError("readonly_sqlite_identity_unverified")
    except sqlite3.Error as exc:
        if budget is not None:
            budget.record_sqlite_error(exc)
        raise
    except BaseException:
        if connection is not None:
            try:
                connection.close()
            except (DurableIOError, OSError, sqlite3.Error):
                pass
        raise
    finally:
        if anchor_descriptor >= 0 and not anchor_transferred:
            os.close(anchor_descriptor)
    if budget is not None:
        connection.set_progress_handler(budget.progress_handler, 1_000)
    return connection
