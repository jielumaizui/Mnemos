"""Contract tests for the single native SQLite read capability owner."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from core.sync_framework import native_sqlite


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
        connection.execute("INSERT INTO records VALUES ('visible')")


def test_native_sqlite_helper_is_query_only_and_does_not_create_targets(
    tmp_path: Path,
) -> None:
    database = tmp_path / "native.db"
    _database(database)

    connection = native_sqlite.connect_native_sqlite_readonly(database)
    try:
        assert connection.execute("SELECT value FROM records").fetchone() == (
            "visible",
        )
        assert connection.execute("PRAGMA query_only").fetchone() == (1,)
        assert connection.execute("PRAGMA temp_store").fetchone() == (2,)
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO records VALUES ('forbidden')")
    finally:
        connection.close()

    missing = tmp_path / "missing.db"
    with pytest.raises(
        native_sqlite.NativeSQLiteReadError,
        match="native_sqlite_read_failed",
    ):
        native_sqlite.connect_native_sqlite_readonly(missing)
    assert not missing.exists()


def test_native_sqlite_helper_rejects_leaf_symlink_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "native.db"
    _database(database)
    link = tmp_path / "native-link.db"
    link.symlink_to(database)

    with pytest.raises(
        native_sqlite.NativeSQLiteReadError,
        match="native_sqlite_artifact_not_regular",
    ):
        native_sqlite.connect_native_sqlite_readonly(link)


def test_native_sqlite_helper_rejects_path_replacement_during_connect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "native.db"
    replacement = tmp_path / "replacement.db"
    _database(database)
    _database(replacement)
    original_connect = native_sqlite.sqlite3.connect

    def replace_then_connect(*args, **kwargs):
        database.unlink()
        replacement.replace(database)
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(native_sqlite.sqlite3, "connect", replace_then_connect)

    with pytest.raises(
        native_sqlite.NativeSQLiteReadError,
        match="native_sqlite_artifact_changed_during_open",
    ):
        native_sqlite.connect_native_sqlite_readonly(database)


def test_native_sqlite_capability_exists_only_for_the_exact_connect_event(
    tmp_path: Path,
) -> None:
    database = tmp_path / "native.db"
    _database(database)
    observations: list[Path | None] = []

    def audit(event: str, args: tuple[object, ...]) -> None:
        if event == "sqlite3.connect" and args:
            observations.append(
                native_sqlite.active_native_sqlite_read_path(args[0])
            )

    sys.addaudithook(audit)
    connection = native_sqlite.connect_native_sqlite_readonly(database)
    connection.close()

    assert observations[-1] == database.resolve()
    forged = f"{database.resolve().as_uri()}?mode=ro"
    assert native_sqlite.active_native_sqlite_read_path(forged) is None


@pytest.mark.parametrize(
    "query",
    [
        "mode=ro&mode=ro",
        "mode=rw",
        "mode=ro&cache=shared",
        "mode=ro&immutable=0",
    ],
)
def test_native_sqlite_capability_rejects_malformed_or_expanded_uris(
    tmp_path: Path,
    query: str,
) -> None:
    database = tmp_path / "native.db"
    _database(database)
    uri = f"{database.resolve().as_uri()}?{query}"

    with native_sqlite._connect_capability(database.resolve(), uri):  # noqa: SLF001
        assert native_sqlite.active_native_sqlite_read_path(uri) is None


def test_native_sqlite_capability_rejects_nested_ownership(
    tmp_path: Path,
) -> None:
    database = tmp_path / "native.db"
    _database(database)
    uri = f"{database.resolve().as_uri()}?mode=ro"

    with native_sqlite._connect_capability(database.resolve(), uri):  # noqa: SLF001
        with pytest.raises(
            native_sqlite.NativeSQLiteReadError,
            match="native_sqlite_read_capability_nested",
        ):
            with native_sqlite._connect_capability(  # noqa: SLF001
                database.resolve(),
                uri,
            ):
                pass


@pytest.mark.parametrize(
    ("error_name", "error_code", "retryable"),
    [
        ("SQLITE_BUSY", sqlite3.SQLITE_BUSY, True),
        ("SQLITE_LOCKED", sqlite3.SQLITE_LOCKED, True),
        ("SQLITE_NOMEM", sqlite3.SQLITE_NOMEM, True),
        (
            "SQLITE_IOERR_GETTEMPPATH",
            sqlite3.SQLITE_IOERR_GETTEMPPATH,
            False,
        ),
        ("SQLITE_IOERR_DATA", sqlite3.SQLITE_IOERR_DATA, False),
        ("SQLITE_CORRUPT", sqlite3.SQLITE_CORRUPT, False),
    ],
)
def test_native_storage_failure_evidence_is_typed_content_free_and_exact(
    error_name: str,
    error_code: int,
    retryable: bool,
) -> None:
    error = sqlite3.OperationalError(
        "sensitive native path and SQLite payload must never escape"
    )
    error.sqlite_errorcode = error_code
    error.sqlite_errorname = error_name

    evidence = native_sqlite.native_storage_failure_evidence(error)

    assert evidence == {
        "failure_class": (
            "sqlite_transient" if retryable else "sqlite_nontransient"
        ),
        "retryable": retryable,
        "sqlite_errorcode": error_code,
        "sqlite_errorname": error_name,
    }
    assert "sensitive" not in repr(evidence)


def test_native_sqlite_read_error_preserves_only_safe_storage_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "native.db"
    _database(database)
    failure = sqlite3.OperationalError("private database path must not escape")
    failure.sqlite_errorcode = sqlite3.SQLITE_BUSY
    failure.sqlite_errorname = "SQLITE_BUSY"

    def fail_connect(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(native_sqlite.sqlite3, "connect", fail_connect)

    with pytest.raises(native_sqlite.NativeSQLiteReadError) as raised:
        native_sqlite.connect_native_sqlite_readonly(database)

    assert raised.value.retryable is True
    assert raised.value.details == {
        "failure_class": "sqlite_transient",
        "retryable": True,
        "sqlite_errorcode": sqlite3.SQLITE_BUSY,
        "sqlite_errorname": "SQLITE_BUSY",
    }
    assert "private database" not in repr(raised.value.details)


def test_native_sqlite_helper_closes_connection_when_contract_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "native.db"
    _database(database)
    failure = sqlite3.OperationalError("private setup failure must not escape")
    failure.sqlite_errorcode = sqlite3.SQLITE_BUSY
    failure.sqlite_errorname = "SQLITE_BUSY"

    class FailingConnection:
        closed = False

        def execute(self, _statement: str):
            raise failure

        def close(self) -> None:
            self.closed = True

    connection = FailingConnection()
    monkeypatch.setattr(
        native_sqlite.sqlite3,
        "connect",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(native_sqlite.NativeSQLiteReadError):
        native_sqlite.connect_native_sqlite_readonly(database)

    assert connection.closed is True
