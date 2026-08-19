from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.app.blindspot_asset_schema import (
    QUARANTINE_TABLE,
    REVISION_TABLE,
    BlindspotAssetSchemaError,
    inspect_blindspot_asset_schema,
    reconcile_blindspot_asset_schema,
)
from scripts.reconcile_blindspot_asset_schema import main as reconcile_main


def _legacy_database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE blindspots (
                topic TEXT PRIMARY KEY,
                description TEXT,
                confidence REAL DEFAULT 0.5,
                status TEXT DEFAULT 'detected',
                detected_at TEXT,
                reminded_at TEXT,
                last_reminded_at TEXT,
                last_session_id TEXT,
                resolved_at TEXT,
                resolved_by_page TEXT
            )""")
        conn.executemany(
            "INSERT INTO blindspots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    "rustasync",
                    "知识库中缺少关于「rustasync」的记录",
                    0.4,
                    "reminded",
                    "2026-07-01T00:00:00",
                    "2026-07-01T00:01:00",
                    "2026-07-01T00:01:00",
                    "session-1",
                    None,
                    None,
                ),
                (
                    "framing_rigidity",
                    "用户过度依赖单一视角",
                    0.8,
                    "investigating",
                    "2026-07-02T00:00:00",
                    None,
                    None,
                    "session-2",
                    None,
                    None,
                ),
            ),
        )


def test_preview_is_read_only_and_refuses_semantic_promotion(tmp_path):
    db_path = tmp_path / "blindspots.db"
    _legacy_database(db_path)
    before = db_path.read_bytes()
    before_mtime = db_path.stat().st_mtime_ns

    with sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True) as conn:
        report = reconcile_blindspot_asset_schema(conn, apply=False)

    assert report["legacy_row_count"] == 2
    assert report["planned_quarantine_count"] == 2
    assert report["active_promotion_count"] == 0
    assert report["classification_counts"] == {
        "historical_ambiguous_blindspot": 1,
        "historical_unscoped_knowledge_gap": 1,
    }
    assert db_path.read_bytes() == before
    assert db_path.stat().st_mtime_ns == before_mtime
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()


def test_apply_quarantines_every_legacy_row_without_active_assets(tmp_path):
    db_path = tmp_path / "blindspots.db"
    _legacy_database(db_path)

    with sqlite3.connect(db_path) as conn:
        report = reconcile_blindspot_asset_schema(conn, apply=True)
        state = inspect_blindspot_asset_schema(conn)
        quarantine = conn.execute(
            f"SELECT classification, row_json FROM {QUARANTINE_TABLE} ORDER BY classification"
        ).fetchall()
        active_count = conn.execute(f"SELECT COUNT(*) FROM {REVISION_TABLE}").fetchone()[0]
        legacy_count = conn.execute("SELECT COUNT(*) FROM blindspots_legacy_v0").fetchone()[0]

    assert report["conservation_ok"] is True
    assert report["legacy_row_count"] == 2
    assert report["quarantined_row_count"] == 2
    assert report["active_promotion_count"] == 0
    assert report["after"]["ok"] is True
    assert state.ok is True
    assert active_count == 0
    assert legacy_count == 2
    assert len(quarantine) == 2
    assert {json.loads(row_json)["topic"] for _, row_json in quarantine} == {
        "rustasync",
        "framing_rigidity",
    }


def test_revision_history_is_append_only(tmp_path):
    db_path = tmp_path / "blindspots.db"
    from core.app.blindspot_discovery import BlindSpotReminder, BlindspotDiscovery

    discovery = BlindspotDiscovery(db_path=str(db_path))
    discovery._upsert_blindspot(
        BlindSpotReminder(
            topic="rustasync",
            description="知识库中缺少关于 rustasync 的记录",
            confidence=0.7,
            status="detected",
            detected_at="2026-07-23T00:00:00+00:00",
            asset_id="kcg_append_only",
            revision_id="kcg_append_only:r1",
            evidence_refs=("authorized-context-search:sha256:test",),
            expires_at="2026-08-23T00:00:00+00:00",
        )
    )
    with discovery._connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(f"UPDATE {REVISION_TABLE} SET status='resolved'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(f"DELETE FROM {REVISION_TABLE}")


def test_apply_rolls_back_all_schema_and_quarantine_changes_on_failure(tmp_path):
    db_path = tmp_path / "blindspots.db"
    _legacy_database(db_path)

    def failpoint(name: str) -> None:
        if name == "after_quarantine_copy":
            raise RuntimeError("injected migration failure")

    with sqlite3.connect(db_path) as conn:
        with pytest.raises(RuntimeError, match="injected migration failure"):
            reconcile_blindspot_asset_schema(conn, apply=True, failpoint=failpoint)
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        count = conn.execute("SELECT COUNT(*) FROM blindspots").fetchone()[0]

    assert "blindspots" in tables
    assert "blindspots_legacy_v0" not in tables
    assert REVISION_TABLE not in tables
    assert count == 2


def test_cli_apply_requires_backup_and_preserves_preimage(tmp_path, capsys):
    db_path = tmp_path / "blindspots.db"
    backup_dir = tmp_path / "backups"
    _legacy_database(db_path)

    assert reconcile_main(["--db-path", str(db_path), "--apply", "--json"]) == 2
    capsys.readouterr()

    assert (
        reconcile_main(
            [
                "--db-path",
                str(db_path),
                "--apply",
                "--backup-dir",
                str(backup_dir),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    backup = Path(payload["backup"]["path"])
    assert backup.is_file()
    assert payload["backup"]["integrity_check"] == "ok"
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT COUNT(*) FROM blindspots").fetchone()[0] == 2


def test_unknown_legacy_schema_fails_closed(tmp_path):
    db_path = tmp_path / "blindspots.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE blindspots (mystery TEXT)")
        with pytest.raises(BlindspotAssetSchemaError, match="unknown legacy"):
            reconcile_blindspot_asset_schema(conn, apply=False)


def test_schema_inspection_rejects_missing_immutability_trigger(tmp_path):
    db_path = tmp_path / "blindspots.db"
    from core.app.blindspot_discovery import BlindspotDiscovery

    BlindspotDiscovery(db_path=str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"DROP TRIGGER trg_{REVISION_TABLE}_immutable_update")
        state = inspect_blindspot_asset_schema(conn)

    assert state.ok is False
    assert any("immutability trigger" in error for error in state.errors)


def test_schema_inspection_rejects_active_legacy_table_beside_canonical(tmp_path):
    db_path = tmp_path / "blindspots.db"
    from core.app.blindspot_discovery import BlindspotDiscovery

    BlindspotDiscovery(db_path=str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE blindspots (topic TEXT PRIMARY KEY)")
        state = inspect_blindspot_asset_schema(conn)

    assert state.ok is False
    assert any("legacy blindspots table" in error for error in state.errors)
