from pathlib import Path
import sqlite3

import pytest

import core.agent_kit.authorization as authorization_module
from core.agent_kit.authorization import AgentAuthorizationStore
from core.ops.durable_io import DurableIOError


def test_authorization_schema_late_abort_restores_preimage_and_closes_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LateSchemaAbort(BaseException):
        pass

    database = tmp_path / "agent_auth.db"
    original_connect = sqlite3.connect
    with original_connect(database) as connection:
        connection.execute(
            "CREATE TABLE preimage_sentinel (value TEXT PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO preimage_sentinel(value) VALUES ('unchanged')"
        )

    opened: list[sqlite3.Connection] = []

    class FailingConnection(sqlite3.Connection):
        create_count = 0

        def execute(self, sql: str, parameters=(), /):  # type: ignore[override]
            result = super().execute(sql, parameters)
            if "CREATE TABLE" in str(sql).upper():
                self.create_count += 1
                if self.create_count == 3:
                    raise LateSchemaAbort("sentinel authorization schema failure")
            return result

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = FailingConnection
        connection = original_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(authorization_module.sqlite3, "connect", connect)

    with pytest.raises(
        LateSchemaAbort,
        match="sentinel authorization schema failure",
    ):
        AgentAuthorizationStore(database)

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")
    with original_connect(database) as connection:
        objects = connection.execute(
            """
            SELECT type, name FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
        rows = connection.execute(
            "SELECT value FROM preimage_sentinel"
        ).fetchall()
    assert objects == [("table", "preimage_sentinel")]
    assert rows == [("unchanged",)]


def test_agent_authorization_store_does_not_authorize_content_by_default(tmp_path: Path):
    store = AgentAuthorizationStore(tmp_path / "agent_auth.db")

    assert store.get_record("codex") is None
    assert store.content_access_authorized("detected") is False
    assert store.content_access_authorized("probe_ok") is False


def test_agent_authorization_store_authorize_and_revoke(tmp_path: Path):
    store = AgentAuthorizationStore(tmp_path / "agent_auth.db")

    authorized = store.set_state(
        "codex",
        "user_authorized",
        directory="/workspace",
        capability="content_analysis",
        purpose="distillation",
    )
    revoked = store.set_state(
        "codex",
        "revoked",
        directory="/workspace",
        capability="content_analysis",
        purpose="distillation",
    )

    assert authorized.state == "user_authorized"
    assert store.content_access_authorized(authorized.state) is True
    assert revoked.state == "revoked"
    assert store.content_access_authorized(revoked.state) is False
    assert store.get_record(
        "codex",
        directory="/workspace",
        capability="content_analysis",
        purpose="distillation",
    ).state == "revoked"


def test_uninspectable_authorization_store_is_not_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "agent_auth.db"
    store = AgentAuthorizationStore(db_path, initialize=False)
    original_lstat = Path.lstat

    def denied(path: Path, *args: object, **kwargs: object):
        if path == db_path:
            raise PermissionError("sentinel")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)

    with pytest.raises(DurableIOError, match="durable_path_inspection_failed"):
        store.get_record("codex")


def test_authorization_read_only_path_never_follows_a_leaf_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "agent_auth.real.db"
    AgentAuthorizationStore(target)
    link = tmp_path / "agent_auth.db"
    link.symlink_to(target)
    store = AgentAuthorizationStore(link, initialize=False)

    with pytest.raises(DurableIOError, match="readonly_sqlite_path_not_regular"):
        store.get_record("codex")
