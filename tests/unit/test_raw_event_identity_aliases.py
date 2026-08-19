"""Legacy-to-native Raw identity alias reconciliation tests."""

from __future__ import annotations

import sqlite3
import shutil
from pathlib import Path

import pytest

from core.sync_framework.raw_event_identity_aliases import (
    RawEventIdentityAliasError,
    apply_reconciliation,
    inspect_reconciliation,
)
from core.ops.durable_io import DurableIOError
from core.sync_framework.raw_event_store import RawEventStore
from scripts import project_raw_vault
from scripts import reconcile_raw_event_identity_aliases as reconcile_command


class _Config:
    def __init__(self, database_dir):
        self.database_dir = database_dir

    def get(self, _key, default=None):
        return default


def _store(tmp_path):
    return RawEventStore(db_path=tmp_path / "raw_events.db", config=_Config(tmp_path))


def test_identity_alias_inspection_does_not_certify_unavailable_as_uninitialized(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "raw_events.db"
    original_lstat = Path.lstat

    def denied(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("sentinel")
        return original_lstat(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)

    with pytest.raises(
        RawEventIdentityAliasError,
        match="database unavailable",
    ):
        inspect_reconciliation(path)
    with pytest.raises(
        RawEventIdentityAliasError,
        match="database unavailable",
    ):
        apply_reconciliation(path)


def test_identity_alias_inspection_rejects_database_replacement_during_open(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "raw_events.db"
    store = _store(tmp_path)
    store.close()
    replacement_template = tmp_path / "foreign.db"
    with sqlite3.connect(replacement_template) as connection:
        connection.execute("CREATE TABLE foreign_state(value TEXT)")
        connection.execute("INSERT INTO foreign_state VALUES ('preserve')")
    detached = tmp_path / "raw_events.detached.db"
    real_connect = sqlite3.connect
    injected = {"done": False}

    def replace_before_connect(database, *args, **kwargs):
        rendered = str(database)
        if (
            ("raw_events.db" in rendered)
            and not injected["done"]
            and ("mode=ro" in rendered or rendered == str(path))
        ):
            injected["done"] = True
            path.replace(detached)
            shutil.copyfile(replacement_template, path)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", replace_before_connect)

    with pytest.raises(DurableIOError, match="readonly_sqlite_identity_changed"):
        inspect_reconciliation(path)

    assert injected["done"] is True
    with real_connect(path) as connection:
        assert connection.execute("SELECT value FROM foreign_state").fetchone() == ("preserve",)


def test_reconciliation_aliases_one_legacy_turn_to_one_native_identity(tmp_path):
    store = _store(tmp_path)
    try:
        legacy_revision = store.upsert_turn(
            source_agent="codex",
            session_id="legacy-session",
            turn_number=0,
            user_content="legacy user",
            assistant_content="legacy assistant",
            metadata={},
        )
        native_revision = store.upsert_turn(
            source_agent="codex",
            session_id="legacy-session",
            turn_number=0,
            user_content="native user",
            assistant_content="native assistant",
            metadata={"native_event_id": "native-event-1"},
        )
        legacy_event_id = store.get_turn(legacy_revision)["logical_event_id"]
        native_event_id = store.get_turn(native_revision)["logical_event_id"]
    finally:
        store.close()

    before = inspect_reconciliation(tmp_path / "raw_events.db")
    assert before["ok"] is False
    assert before["candidate_count"] == 1
    assert before["blocking_count"] == 0

    applied = apply_reconciliation(tmp_path / "raw_events.db")

    assert applied["ok"] is True
    assert applied["alias_count"] == 1
    assert applied["applied_count"] == 1
    with sqlite3.connect(tmp_path / "raw_events.db") as conn:
        row = conn.execute(
            """
            SELECT alias_event_id, canonical_event_id
            FROM raw_event_identity_aliases
            """
        ).fetchone()
        assert row == (legacy_event_id, native_event_id)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    store = _store(tmp_path)
    try:
        headers = store.list_current_headers(session_id="legacy-session")
        assert [item["revision_id"] for item in headers] == [native_revision]
        refs = project_raw_vault._fetch_refs(store)  # noqa: SLF001
        assert [item.event_id for item in refs] == [native_revision]
        assert store.get_turn(legacy_revision)["user_content"] == "legacy user"
        revisions = store.list_revisions(
            source_agent="codex",
            session_id="legacy-session",
            turn_number=0,
            native_event_id="native-event-1",
        )
        assert {item["revision_id"] for item in revisions} == {
            legacy_revision,
            native_revision,
        }
        replay = store.upsert_turn(
            source_agent="codex",
            session_id="legacy-session",
            turn_number=0,
            user_content="legacy replay",
            assistant_content="legacy replay",
            metadata={},
        )
        assert store.get_turn(replay)["logical_event_id"] == native_event_id

        conn = store._pool.get_conn()  # noqa: SLF001
        conn.execute(
            "UPDATE raw_metrics SET retention_state='eligible_delete' "
            "WHERE event_id IN (?, ?)",
            (legacy_event_id, native_event_id),
        )
        conn.commit()
        assert store.purge_eligible_delete()["purged"] == 0
    finally:
        store.close()


def test_reconciliation_rejects_ambiguous_legacy_to_many_native_mapping(tmp_path):
    store = _store(tmp_path)
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="ambiguous-session",
            turn_number=0,
            user_content="legacy",
            assistant_content="legacy",
            metadata={},
        )
        for native_event_id in ("native-event-1", "native-event-2"):
            store.upsert_turn(
                source_agent="codex",
                session_id="ambiguous-session",
                turn_number=0,
                user_content=native_event_id,
                assistant_content=native_event_id,
                metadata={"native_event_id": native_event_id},
            )
    finally:
        store.close()

    before = inspect_reconciliation(tmp_path / "raw_events.db")
    assert before["candidate_count"] == 0
    assert before["blocking_count"] == 1

    with pytest.raises(RawEventIdentityAliasError):
        apply_reconciliation(tmp_path / "raw_events.db")


def test_reconciliation_command_requires_backup_and_converges(tmp_path, capsys):
    store = _store(tmp_path)
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="command-session",
            turn_number=0,
            user_content="legacy",
            assistant_content="legacy",
            metadata={},
        )
        store.upsert_turn(
            source_agent="codex",
            session_id="command-session",
            turn_number=0,
            user_content="native",
            assistant_content="native",
            metadata={"native_event_id": "native-command"},
        )
    finally:
        store.close()

    db_path = tmp_path / "raw_events.db"
    assert reconcile_command.main(["--db", str(db_path), "--apply", "--json"]) == 1
    rejected = capsys.readouterr().out
    assert "--apply requires --backup-dir" in rejected

    backup_dir = tmp_path / "backups"
    assert (
        reconcile_command.main(
            [
                "--db",
                str(db_path),
                "--apply",
                "--backup-dir",
                str(backup_dir),
                "--json",
            ]
        )
        == 0
    )
    assert '"ok": true' in capsys.readouterr().out
    assert len(list(backup_dir.glob("*.pre_raw_identity_alias.sqlite"))) == 1
