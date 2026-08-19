"""Exact read-only SQLite capability for native AgentSource artifacts.

CPython's ``sqlite3.connect`` audit event exposes only the database target, not
the ``uri=True`` argument.  A write guard therefore cannot infer read-only
intent from the URI string.  This module is the single owner that proves that
intent in-process while SQLite itself enforces ``mode=ro`` and ``query_only``.
"""

from __future__ import annotations

import errno
import os
import re
import sqlite3
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import parse_qsl, unquote, urlsplit

from core.ops.durable_io import canonical_native_path


class NativeSQLiteReadError(RuntimeError):
    """Stable, content-free native SQLite read failure."""

    code = "native_sqlite_read_failed"

    def __init__(
        self,
        code: str = "native_sqlite_read_failed",
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = str(code)
        self.details = dict(details or {})
        self.retryable = self.details.get("retryable") is True
        super().__init__(self.code)

    @classmethod
    def from_storage_failure(
        cls,
        failure: BaseException,
    ) -> "NativeSQLiteReadError":
        return cls(
            details=native_storage_failure_evidence(failure),
        )


_RETRYABLE_SQLITE_PRIMARY_CODES = frozenset(
    {
        int(getattr(sqlite3, "SQLITE_BUSY", 5)),
        int(getattr(sqlite3, "SQLITE_LOCKED", 6)),
        int(getattr(sqlite3, "SQLITE_NOMEM", 7)),
        int(getattr(sqlite3, "SQLITE_PROTOCOL", 15)),
        int(getattr(sqlite3, "SQLITE_SCHEMA", 17)),
    }
)
_RETRYABLE_OS_ERRNOS = frozenset(
    value
    for value in (
        errno.EAGAIN,
        errno.EBUSY,
        errno.EINTR,
        errno.EMFILE,
        errno.ENFILE,
        errno.ENOMEM,
        getattr(errno, "ESTALE", None),
        getattr(errno, "ETIMEDOUT", None),
    )
    if isinstance(value, int)
)


def native_storage_failure_evidence(
    failure: BaseException,
) -> dict[str, Any]:
    """Return an exact, content-free retry classification for local storage IO."""

    if isinstance(failure, NativeSQLiteReadError):
        return dict(failure.details)
    if isinstance(failure, sqlite3.Error):
        raw_code = getattr(failure, "sqlite_errorcode", None)
        raw_name = str(getattr(failure, "sqlite_errorname", "") or "")
        code = (
            int(raw_code)
            if isinstance(raw_code, int) and not isinstance(raw_code, bool)
            else None
        )
        name = (
            raw_name
            if re.fullmatch(r"SQLITE_[A-Z0-9_]{1,96}", raw_name)
            else ""
        )
        primary_code = code & 0xFF if code is not None else None
        retryable = primary_code in _RETRYABLE_SQLITE_PRIMARY_CODES
        evidence: dict[str, Any] = {
            "failure_class": (
                "sqlite_transient" if retryable else "sqlite_nontransient"
            ),
            "retryable": retryable,
        }
        if code is not None:
            evidence["sqlite_errorcode"] = code
        if name:
            evidence["sqlite_errorname"] = name
        return evidence
    if isinstance(failure, OSError):
        raw_errno = getattr(failure, "errno", None)
        os_errno = (
            int(raw_errno)
            if isinstance(raw_errno, int) and not isinstance(raw_errno, bool)
            else None
        )
        retryable = os_errno in _RETRYABLE_OS_ERRNOS
        evidence = {
            "failure_class": (
                "os_transient" if retryable else "os_nontransient"
            ),
            "retryable": retryable,
        }
        if os_errno is not None:
            evidence["os_errno"] = os_errno
        return evidence
    return {
        "failure_class": "storage_untyped",
        "retryable": False,
    }


_CAPABILITY = threading.local()


def _path_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(stat.S_IFMT(metadata.st_mode)),
    )


def _verify_anchor(
    descriptor: int,
    path: Path,
    expected_identity: tuple[int, int, int],
) -> None:
    try:
        opened = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        raise NativeSQLiteReadError(
            "native_sqlite_artifact_changed_during_open"
        ) from None
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or _path_identity(opened) != expected_identity
        or _path_identity(current) != expected_identity
    ):
        raise NativeSQLiteReadError(
            "native_sqlite_artifact_changed_during_open"
        )


class _AnchoredNativeSQLiteCursor(sqlite3.Cursor):
    """Revalidate the exact native pathname around every cursor operation."""

    def _verify(self) -> None:
        connection = self.connection
        if isinstance(connection, _AnchoredNativeSQLiteConnection):
            connection._verify_native_anchor()  # noqa: SLF001

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


class _AnchoredNativeSQLiteConnection(sqlite3.Connection):
    """SQLite connection retaining a no-follow anchor until close."""

    _native_anchor_fd = -1
    _native_anchor_path: Path | None = None
    _native_anchor_identity: tuple[int, int, int] | None = None

    def _bind_native_anchor(
        self,
        descriptor: int,
        path: Path,
        identity: tuple[int, int, int],
    ) -> None:
        self._native_anchor_fd = descriptor
        self._native_anchor_path = path
        self._native_anchor_identity = identity

    def _verify_native_anchor(self) -> None:
        if (
            self._native_anchor_fd < 0
            or self._native_anchor_path is None
            or self._native_anchor_identity is None
        ):
            return
        _verify_anchor(
            self._native_anchor_fd,
            self._native_anchor_path,
            self._native_anchor_identity,
        )

    def cursor(self, factory=None):  # type: ignore[override]
        self._verify_native_anchor()
        return super().cursor(factory or _AnchoredNativeSQLiteCursor)

    def execute(self, sql, parameters=(), /):  # type: ignore[override]
        self._verify_native_anchor()
        result = super().execute(sql, parameters)
        self._verify_native_anchor()
        return result

    def executemany(self, sql, seq_of_parameters, /):  # type: ignore[override]
        self._verify_native_anchor()
        result = super().executemany(sql, seq_of_parameters)
        self._verify_native_anchor()
        return result

    def executescript(self, sql_script, /):  # type: ignore[override]
        self._verify_native_anchor()
        result = super().executescript(sql_script)
        self._verify_native_anchor()
        return result

    def close(self) -> None:
        failure: NativeSQLiteReadError | None = None
        try:
            self._verify_native_anchor()
        except NativeSQLiteReadError as exc:
            failure = exc
        descriptor = self._native_anchor_fd
        self._native_anchor_fd = -1
        try:
            super().close()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if failure is not None:
            raise failure


def _canonical_file_uri(path: Path, *, immutable: bool) -> str:
    query = "mode=ro"
    if immutable:
        query += "&immutable=1"
    return f"{path.as_uri()}?{query}"


def _target_path(value: object) -> Path | None:
    if not isinstance(value, (str, bytes)):
        return None
    text = value.decode("utf-8", errors="strict") if isinstance(value, bytes) else value
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        return None
    try:
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    if len(query) != len({key for key, _value in query}):
        return None
    values = dict(query)
    if values.get("mode") != "ro" or set(values) - {"mode", "immutable"}:
        return None
    if "immutable" in values and values["immutable"] != "1":
        return None
    try:
        path = Path(unquote(parsed.path))
        if not path.is_absolute():
            return None
        canonical = canonical_native_path(path)
        metadata = canonical.lstat()
        return canonical if stat.S_ISREG(metadata.st_mode) else None
    except (OSError, UnicodeError, ValueError):
        return None


def active_native_sqlite_read_path(value: object) -> Path | None:
    """Return the exact active helper-owned path for one audit target."""

    active = getattr(_CAPABILITY, "path", None)
    expected_uri = getattr(_CAPABILITY, "uri", None)
    if not isinstance(active, Path) or not isinstance(expected_uri, str):
        return None
    if value != expected_uri:
        return None
    parsed = _target_path(value)
    return active if parsed == active else None


@contextmanager
def _connect_capability(path: Path, uri: str) -> Iterator[None]:
    if getattr(_CAPABILITY, "path", None) is not None:
        raise NativeSQLiteReadError("native_sqlite_read_capability_nested")
    _CAPABILITY.path = path
    _CAPABILITY.uri = uri
    try:
        yield
    finally:
        _CAPABILITY.path = None
        _CAPABILITY.uri = None


def connect_native_sqlite_readonly(
    path: Path,
    *,
    immutable: bool = False,
    timeout: float = 5.0,
) -> sqlite3.Connection:
    """Open one exact existing native database with a process-local capability."""

    connection: sqlite3.Connection | None = None
    contract_verified = False
    anchor_descriptor = -1
    anchor_transferred = False
    try:
        canonical = canonical_native_path(Path(path))
        metadata = canonical.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise NativeSQLiteReadError("native_sqlite_artifact_not_regular")
        anchor_descriptor = os.open(
            canonical,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(anchor_descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise NativeSQLiteReadError("native_sqlite_artifact_not_regular")
        anchor_identity = _path_identity(opened)
        _verify_anchor(anchor_descriptor, canonical, anchor_identity)
        uri = _canonical_file_uri(canonical, immutable=immutable)
        with _connect_capability(canonical, uri):
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=timeout,
                factory=_AnchoredNativeSQLiteConnection,
            )
        _verify_anchor(anchor_descriptor, canonical, anchor_identity)
        if isinstance(connection, _AnchoredNativeSQLiteConnection):
            connection._bind_native_anchor(  # noqa: SLF001
                anchor_descriptor,
                canonical,
                anchor_identity,
            )
            anchor_transferred = True
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        row = connection.execute("PRAGMA query_only").fetchone()
        temp_store_row = connection.execute("PRAGMA temp_store").fetchone()
        database_rows = connection.execute("PRAGMA database_list").fetchall()
        main_paths = [
            canonical_native_path(Path(str(item[2])).expanduser())
            for item in database_rows
            if str(item[1]) == "main"
        ]
        if (
            row != (1,)
            or temp_store_row != (2,)
            or main_paths != [canonical]
        ):
            raise NativeSQLiteReadError("native_sqlite_read_contract_unverified")
        contract_verified = True
        return connection
    except NativeSQLiteReadError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise NativeSQLiteReadError.from_storage_failure(exc) from None
    except (UnicodeError, ValueError):
        raise NativeSQLiteReadError() from None
    finally:
        if connection is not None and not contract_verified:
            try:
                connection.close()
            except (NativeSQLiteReadError, OSError, sqlite3.Error):
                pass
        if anchor_descriptor >= 0 and not anchor_transferred:
            os.close(anchor_descriptor)


__all__ = [
    "NativeSQLiteReadError",
    "active_native_sqlite_read_path",
    "connect_native_sqlite_readonly",
    "native_storage_failure_evidence",
]
