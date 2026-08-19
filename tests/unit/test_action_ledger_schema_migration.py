from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.ops.action_ledger_schema import (
    ActionLedgerSchemaError,
    initialize_action_ledger_schema,
    inspect_action_ledger_schema,
    reconcile_action_ledger_schema,
)
from core.system_contracts import ActionLedger
from scripts.reconcile_action_ledger import main as reconcile_main


LEGACY_DDL = """
CREATE TABLE action_ledger (
    action_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    actor TEXT NOT NULL,
    action_type TEXT NOT NULL,
    target TEXT NOT NULL,
    before_ref TEXT NOT NULL DEFAULT '',
    after_ref TEXT NOT NULL DEFAULT '',
    evidence_refs_json TEXT NOT NULL,
    quality_decision_id TEXT NOT NULL DEFAULT '',
    verification_json TEXT NOT NULL DEFAULT '{}',
    rollback_ref TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_action_ledger_type_status ON action_ledger(action_type, status);
"""


def _legacy(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_DDL)
        conn.execute(
            "INSERT INTO action_ledger VALUES "
            "('act-1', 'mnemos.action_ledger.v1', 'test', 'quality_gate', "
            "'target', '', '', '[\"evidence\"]', '', '{}', '', 'verified', "
            "'2026-07-16T00:00:00+00:00')"
        )


def test_legacy_constructor_fails_closed_until_explicit_migration(tmp_path: Path) -> None:
    db = tmp_path / "action_ledger.db"
    _legacy(db)

    with pytest.raises(ActionLedgerSchemaError, match="migration required"):
        ActionLedger(db)

    with sqlite3.connect(db) as conn:
        report = reconcile_action_ledger_schema(conn, apply=True)
    assert report["applied"] is True
    assert ActionLedger(db).recent()[0]["action_id"] == "act-1"


def test_constructor_does_not_initialize_missing_action_ledger(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "action_ledger.db"

    ledger = ActionLedger(db)

    assert db.exists() is False
    assert db.parent.exists() is False
    with pytest.raises(FileNotFoundError, match="not initialized"):
        ledger.recent()


def test_action_migration_rolls_back_and_cli_backs_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "action_ledger.db"
    monkeypatch.setattr(
        "scripts.reconcile_action_ledger.runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    _legacy(db)
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT * FROM action_ledger").fetchall()

        def failpoint(stage: str) -> None:
            if stage == "after_copy":
                raise sqlite3.OperationalError("injected action migration failure")

        with pytest.raises(sqlite3.OperationalError):
            reconcile_action_ledger_schema(conn, apply=True, failpoint=failpoint)
        assert conn.execute("SELECT * FROM action_ledger").fetchall() == before

    assert reconcile_main(["--db-path", str(db), "--apply", "--json"]) == 2
    capsys.readouterr()
    backup_dir = tmp_path / "backups"
    assert (
        reconcile_main(
            [
                "--db-path",
                str(db),
                "--apply",
                "--backup-dir",
                str(backup_dir),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert Path(payload["backup"]["path"]).is_file()
    with sqlite3.connect(db) as conn:
        assert inspect_action_ledger_schema(conn).ok is True


def test_canonical_shape_with_registry_or_trigger_drift_is_reconciled(
    tmp_path: Path,
) -> None:
    db = tmp_path / "action_ledger.db"
    initialize_action_ledger_schema(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER action_ledger_no_update")
        conn.execute("DELETE FROM mnemos_schema_registry WHERE component='action_ledger'")
        assert inspect_action_ledger_schema(conn).classification == "legacy_mutable_v0"

        report = reconcile_action_ledger_schema(conn, apply=True)

        assert report["applied"] is True
        assert inspect_action_ledger_schema(conn).ok is True


def test_unknown_action_schema_registry_structure_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "action_ledger.db"
    _legacy(db)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE mnemos_schema_registry (component TEXT PRIMARY KEY)")
        state = inspect_action_ledger_schema(conn)

        assert state.classification == "unknown"
        with pytest.raises(ActionLedgerSchemaError, match="unknown"):
            reconcile_action_ledger_schema(conn, apply=True)
