import sqlite3
from datetime import datetime, timezone


def _legacy_db(path):
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE distill_chunk_results (
                session_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                fragment_json TEXT NOT NULL DEFAULT '[]',
                chunk_info_json TEXT NOT NULL DEFAULT '{}',
                structured_output_json TEXT,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, chunk_index)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO distill_chunk_results
            VALUES ('session-a', 0, 'legacy-hash', 'completed', '[]', '{}', NULL, '', ?, ?)
            """,
            (now, now),
        )


def test_checkpoint_reconciliation_dry_run_is_read_only(tmp_path):
    from scripts.reconcile_distill_execution_checkpoints import inspect_checkpoint_db

    db_path = tmp_path / "chunks.db"
    _legacy_db(db_path)
    before = db_path.read_bytes()

    report = inspect_checkpoint_db(db_path)

    assert report["schema_state"] == "legacy_v1"
    assert report["rows"] == 1
    assert report["affected_sessions"] == 1
    assert report["legacy_rows"] == 1
    assert db_path.read_bytes() == before


def test_checkpoint_reconciliation_backs_up_then_migrates_without_row_loss(tmp_path):
    from scripts.reconcile_distill_execution_checkpoints import (
        inspect_checkpoint_db,
        migrate_checkpoint_db,
    )

    db_path = tmp_path / "chunks.db"
    backup_dir = tmp_path / "backups"
    _legacy_db(db_path)

    result = migrate_checkpoint_db(db_path, backup_dir)
    report = inspect_checkpoint_db(db_path)

    backup_path = result["backup_path"]
    assert result["migrated"] is True
    assert backup_path.exists()
    assert inspect_checkpoint_db(backup_path)["schema_state"] == "legacy_v1"
    assert report["schema_state"] == "execution_spec_v2"
    assert report["rows"] == 1
    assert report["legacy_rows"] == 1
    assert report["integrity"] == "ok"
    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute(
            "SELECT execution_spec_json FROM distill_chunk_results"
        ).fetchone()[0] == "{}"
