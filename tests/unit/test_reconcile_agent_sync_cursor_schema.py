"""Tests for backup-first AgentSource cursor v1-v4 to v5 migration."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts import reconcile_agent_sync_cursor_schema as cursor_reconciler
from daemon.agent_sync_cursor import (
    AgentSyncCursorError,
    AgentSyncCursorStore,
    CURSOR_SCHEMA_VERSION,
    FINGERPRINTLESS_CURSOR_SCHEMA_VERSION,
    LEGACY_CURSOR_SCHEMA_VERSION,
    PREVIOUS_CURSOR_SCHEMA_VERSION,
    SNAPSHOTLESS_CURSOR_SCHEMA_VERSION,
)
from scripts.reconcile_agent_sync_cursor_schema import reconcile_cursor_schema
from scripts.reconcile_agent_sync_cursor_schema import main as reconcile_main
from scripts.reconcile_agent_sync_cursor_schema import (
    _execute_unresolved_cursor_schema_for_test as execute_cursor_migration_for_test,
)


def _legacy_cursor(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE cursor_schema (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE session_raw_cursors (
                source_name TEXT NOT NULL, canonical_session_id TEXT NOT NULL,
                next_turn_number INTEGER NOT NULL, last_raw_commit_at TEXT NOT NULL,
                PRIMARY KEY (source_name, canonical_session_id)
            );
            CREATE TABLE source_reconciliation_cursors (
                source_name TEXT PRIMARY KEY, after_canonical_session_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE source_denominator_state (
                source_name TEXT PRIMARY KEY, roster_hash TEXT NOT NULL, session_count INTEGER NOT NULL,
                observed_session_count INTEGER NOT NULL, observed_turn_count INTEGER NOT NULL,
                complete INTEGER NOT NULL, completed_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE source_denominator_sessions (
                source_name TEXT NOT NULL, canonical_session_id TEXT NOT NULL, roster_hash TEXT NOT NULL,
                turn_count INTEGER NOT NULL, observed_at TEXT NOT NULL,
                PRIMARY KEY (source_name, canonical_session_id)
            );
            """)
        conn.execute(
            "INSERT INTO cursor_schema VALUES ('schema_version', ?)",
            (LEGACY_CURSOR_SCHEMA_VERSION,),
        )


def _v2_cursor(path: Path) -> None:
    _legacy_cursor(path)
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE source_capture_generations (
                source_name TEXT PRIMARY KEY,
                generation_id TEXT NOT NULL UNIQUE,
                roster_hash TEXT NOT NULL,
                started_at TEXT NOT NULL
            );
            CREATE TABLE source_capture_expected_turns (
                source_name TEXT NOT NULL,
                generation_id TEXT NOT NULL,
                canonical_session_id TEXT NOT NULL,
                turn_number INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (source_name, generation_id, canonical_session_id, turn_number)
            );
            CREATE INDEX idx_capture_expected_turns_source_generation
                ON source_capture_expected_turns(
                    source_name, generation_id, canonical_session_id
                );
            CREATE TABLE source_capture_raw_receipts (
                source_name TEXT NOT NULL,
                generation_id TEXT NOT NULL,
                canonical_session_id TEXT NOT NULL,
                turn_number INTEGER NOT NULL,
                raw_revision_id TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (
                    source_name, generation_id, canonical_session_id, turn_number
                )
            );
            CREATE INDEX idx_capture_raw_receipts_source_generation
                ON source_capture_raw_receipts(
                    source_name, generation_id, canonical_session_id
                );
            """)
        conn.execute(
            "UPDATE cursor_schema SET value=? WHERE key='schema_version'",
            (SNAPSHOTLESS_CURSOR_SCHEMA_VERSION,),
        )


def _v3_cursor(path: Path) -> None:
    _v2_cursor(path)
    with sqlite3.connect(path) as conn:
        conn.execute("""
            ALTER TABLE source_capture_generations
            ADD COLUMN native_source_snapshot_hash TEXT NOT NULL DEFAULT ''
            """)
        conn.execute("""
            ALTER TABLE source_capture_generations
            ADD COLUMN snapshot_binding_eligible INTEGER NOT NULL DEFAULT 0
            """)
        conn.execute(
            "UPDATE cursor_schema SET value=? WHERE key='schema_version'",
            (PREVIOUS_CURSOR_SCHEMA_VERSION,),
        )


def _v4_cursor(path: Path) -> None:
    _v3_cursor(path)
    with sqlite3.connect(path) as conn:
        conn.execute("""
            ALTER TABLE source_denominator_sessions
            ADD COLUMN disposition TEXT NOT NULL DEFAULT 'legacy_unverified'
            """)
        conn.execute("""
            ALTER TABLE source_denominator_sessions
            ADD COLUMN disposition_reason TEXT NOT NULL DEFAULT ''
            """)
        conn.execute("""
            ALTER TABLE source_denominator_sessions
            ADD COLUMN artifact_evidence_hash TEXT NOT NULL DEFAULT ''
            """)
        conn.execute(
            "UPDATE cursor_schema SET value=? WHERE key='schema_version'",
            (FINGERPRINTLESS_CURSOR_SCHEMA_VERSION,),
        )


def test_cursor_schema_preview_is_read_only_and_apply_is_backed_up(tmp_path: Path):
    cursor = tmp_path / "agent_sync_cursors.db"
    _legacy_cursor(cursor)
    before = cursor.read_bytes()

    preview = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=None,
        apply=False,
        daemon_inactive=False,
    )
    assert preview["ok"] is True
    assert preview["plan_version"] == "mnemos.agent_sync_cursor_migration_plan.v3"
    assert preview["plan_hash"].startswith("sha256:")
    assert preview["writer_lock_state"] == "active_or_unverified"
    assert cursor.read_bytes() == before

    locked_preview = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=tmp_path / "backups",
        apply=False,
        daemon_inactive=True,
    )
    assert locked_preview["writer_lock_state"] == "inactive"
    assert locked_preview["plan_hash"] != preview["plan_hash"]
    assert "--confirm-read-native-history" in locked_preview["rebuild_command"]
    other_backup_preview = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=tmp_path / "other-backups",
        apply=False,
        daemon_inactive=True,
    )
    assert other_backup_preview["plan_hash"] != locked_preview["plan_hash"]

    from scripts.reconcile_agent_sync_cursor_schema import CursorSchemaReconciliationError

    with pytest.raises(
        CursorSchemaReconciliationError,
        match="expected_plan_hash_required",
    ):
        execute_cursor_migration_for_test(
            cursor_path=cursor,
            backup_dir=tmp_path / "backups",
            apply=True,
            daemon_inactive=True,
        )

    applied = execute_cursor_migration_for_test(
        cursor_path=cursor,
        backup_dir=tmp_path / "backups",
        apply=True,
        daemon_inactive=True,
        expected_plan_hash=locked_preview["plan_hash"],
    )
    assert applied["ok"] is True
    assert applied["reviewed_plan_hash"] == locked_preview["plan_hash"]
    assert applied["after_schema_version"] == CURSOR_SCHEMA_VERSION
    assert applied["backup"]["integrity"] == "ok"
    assert (tmp_path / "backups" / applied["backup"]["filename"]).is_file()


def test_cursor_schema_apply_refuses_when_daemon_is_active(tmp_path: Path):
    cursor = tmp_path / "agent_sync_cursors.db"
    _legacy_cursor(cursor)
    preview = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=tmp_path / "backups",
        apply=False,
        daemon_inactive=False,
    )

    from scripts.reconcile_agent_sync_cursor_schema import CursorSchemaReconciliationError

    try:
        execute_cursor_migration_for_test(
            cursor_path=cursor,
            backup_dir=tmp_path / "backups",
            apply=True,
            daemon_inactive=False,
            expected_plan_hash=preview["plan_hash"],
        )
    except CursorSchemaReconciliationError as exc:
        assert str(exc) == "daemon_not_inactive"
    else:  # pragma: no cover - explicit fail-closed assertion
        raise AssertionError("active daemon must block cursor schema migration")


def test_cursor_v4_migration_adds_turn_fingerprints_but_requires_rebuild(
    tmp_path: Path,
) -> None:
    cursor = tmp_path / "agent_sync_cursors.db"
    _v4_cursor(cursor)
    roster_hash = AgentSyncCursorStore._roster_hash(["session"])
    with sqlite3.connect(cursor) as connection:
        connection.execute(
            """
            INSERT INTO source_denominator_state VALUES (
                'codex', ?, 1, 1, 1, 1, 'done', 'now'
            )
            """,
            (roster_hash,),
        )
        connection.execute(
            """
            INSERT INTO source_denominator_sessions (
                source_name, canonical_session_id, roster_hash, turn_count,
                observed_at, disposition, disposition_reason,
                artifact_evidence_hash
            ) VALUES (
                'codex', 'session', ?, 1, 'now', 'parsed',
                'native_turns_parsed', ?
            )
            """,
            (roster_hash, "sha256:" + ("a" * 64)),
        )
        connection.execute(
            """
            INSERT INTO source_capture_generations (
                source_name, generation_id, roster_hash, started_at,
                native_source_snapshot_hash, snapshot_binding_eligible
            ) VALUES ('codex', 'generation', ?, 'now', 'b', 1)
            """,
            (roster_hash,),
        )
        connection.execute("""
            INSERT INTO source_capture_expected_turns VALUES (
                'codex', 'generation', 'session', 0, 'now'
            )
            """)
        connection.execute("""
            INSERT INTO source_capture_raw_receipts VALUES (
                'codex', 'generation', 'session', 0, 'revision', 'now'
            )
            """)

    preview = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=tmp_path / "backups",
        apply=False,
        daemon_inactive=True,
    )
    applied = execute_cursor_migration_for_test(
        cursor_path=cursor,
        backup_dir=tmp_path / "backups",
        apply=True,
        daemon_inactive=True,
        expected_plan_hash=preview["plan_hash"],
    )

    assert applied["after_schema_version"] == CURSOR_SCHEMA_VERSION
    with sqlite3.connect(cursor) as connection:
        assert {
            row[1] for row in connection.execute("PRAGMA table_info(source_capture_expected_turns)")
        } >= {"turn_fingerprint"}
        assert {
            row[1] for row in connection.execute("PRAGMA table_info(source_capture_raw_receipts)")
        } >= {"turn_fingerprint"}
        assert connection.execute("""
            SELECT native_source_snapshot_hash, snapshot_binding_eligible
            FROM source_capture_generations WHERE source_name='codex'
            """).fetchone() == ("", 0)
        assert connection.execute(
            "SELECT turn_fingerprint FROM source_capture_expected_turns"
        ).fetchone() == ("",)


def test_cursor_cli_json_reports_missing_reviewed_hash(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    exit_code = reconcile_main(["--apply", "--backup-dir", str(tmp_path / "backups"), "--json"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "expected_plan_hash_required"


def test_public_cursor_apply_supports_same_plan_verified_noop(
    tmp_path: Path,
):
    cursor = tmp_path / "agent_sync_cursors.db"
    _legacy_cursor(cursor)
    preview = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=tmp_path / "backups",
        apply=False,
        daemon_inactive=True,
    )

    first = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=tmp_path / "backups",
        apply=True,
        daemon_inactive=True,
        expected_plan_hash=preview["plan_hash"],
    )
    second = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=tmp_path / "backups",
        apply=True,
        daemon_inactive=True,
        expected_plan_hash=preview["plan_hash"],
    )
    post = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=tmp_path / "backups",
        apply=False,
        daemon_inactive=True,
    )

    assert first["first_apply"]["comparator_ok"] is True
    assert first["restore_drill_ok"] is True
    assert first["second_apply_changed"] is False
    assert second["mode"] == "same_plan_second_apply"
    assert second["physical_delta"] == 0
    assert second["semantic_delta"] == 0
    assert post["required_gap"] == 0
    assert post["apply_required"] is False


def test_public_cursor_apply_restores_source_when_migration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cursor = tmp_path / "agent_sync_cursors.db"
    _legacy_cursor(cursor)
    preview = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=tmp_path / "backups",
        apply=False,
        daemon_inactive=True,
    )
    before = cursor_reconciler._sqlite_snapshot_sha256(cursor)  # noqa: SLF001

    def mutate_then_fail(connection: sqlite3.Connection) -> None:
        connection.execute("UPDATE cursor_schema SET value='broken' WHERE key='schema_version'")
        connection.commit()
        raise AgentSyncCursorError("injected migration failure")

    monkeypatch.setattr(
        cursor_reconciler,
        "migrate_historical_cursor_schema",
        mutate_then_fail,
    )

    with pytest.raises(
        cursor_reconciler.CursorSchemaReconciliationError,
        match="cursor_schema_migration_failed",
    ):
        reconcile_cursor_schema(
            cursor_path=cursor,
            backup_dir=tmp_path / "backups",
            apply=True,
            daemon_inactive=True,
            expected_plan_hash=preview["plan_hash"],
        )

    assert cursor_reconciler._sqlite_snapshot_sha256(cursor) == before  # noqa: SLF001


def test_public_cursor_apply_rolls_back_when_post_restore_drill_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cursor = tmp_path / "agent_sync_cursors.db"
    _legacy_cursor(cursor)
    preview = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=tmp_path / "backups",
        apply=False,
        daemon_inactive=True,
    )
    before = cursor_reconciler._sqlite_snapshot_sha256(cursor)  # noqa: SLF001
    real_drill = cursor_reconciler._restore_drill  # noqa: SLF001
    calls = 0

    def fail_only_post_apply(backup_path: Path, expected_hash: str) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            return False
        return real_drill(backup_path, expected_hash)

    monkeypatch.setattr(
        cursor_reconciler,
        "_restore_drill",
        fail_only_post_apply,
    )

    with pytest.raises(
        cursor_reconciler.CursorSchemaReconciliationError,
        match="backup_restore_drill_failed",
    ):
        reconcile_cursor_schema(
            cursor_path=cursor,
            backup_dir=tmp_path / "backups",
            apply=True,
            daemon_inactive=True,
            expected_plan_hash=preview["plan_hash"],
        )

    assert cursor_reconciler._sqlite_snapshot_sha256(cursor) == before  # noqa: SLF001


def test_public_cursor_apply_rolls_back_on_post_apply_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cursor = tmp_path / "agent_sync_cursors.db"
    _legacy_cursor(cursor)
    backup_dir = tmp_path / "backups"
    preview = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=backup_dir,
        apply=False,
        daemon_inactive=True,
    )
    before = cursor_reconciler._sqlite_snapshot_sha256(cursor)  # noqa: SLF001

    def interrupt_post_apply(_cursor_path: Path):
        raise KeyboardInterrupt

    monkeypatch.setattr(cursor_reconciler, "_post_schema_dry_run", interrupt_post_apply)

    with pytest.raises(
        cursor_reconciler.CursorSchemaReconciliationError,
        match="cursor_schema_migration_interrupted",
    ):
        reconcile_cursor_schema(
            cursor_path=cursor,
            backup_dir=backup_dir,
            apply=True,
            daemon_inactive=True,
            expected_plan_hash=preview["plan_hash"],
        )

    assert cursor_reconciler._sqlite_snapshot_sha256(cursor) == before  # noqa: SLF001
    assert not list(backup_dir.glob("agent-sync-cursor-migration.*.json"))


def test_cursor_v2_comparator_covers_existing_capture_tables(tmp_path: Path):
    cursor = tmp_path / "agent_sync_cursors.db"
    _v2_cursor(cursor)
    with sqlite3.connect(cursor) as connection:
        connection.execute("""
            INSERT INTO source_capture_generations (
                source_name, generation_id, roster_hash, started_at
            ) VALUES ('codex', 'generation', 'roster', 'timestamp')
            """)
        connection.execute("""
            INSERT INTO source_capture_expected_turns (
                source_name, generation_id, canonical_session_id,
                turn_number, observed_at
            ) VALUES ('codex', 'generation', 'session', 0, 'timestamp')
            """)
        connection.execute("""
            INSERT INTO source_capture_raw_receipts (
                source_name, generation_id, canonical_session_id,
                turn_number, raw_revision_id, recorded_at
            ) VALUES (
                'codex', 'generation', 'session', 0, 'revision', 'timestamp'
            )
            """)
    backup_dir = tmp_path / "backups"
    preview = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=backup_dir,
        apply=False,
        daemon_inactive=True,
    )

    applied = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=backup_dir,
        apply=True,
        daemon_inactive=True,
        expected_plan_hash=preview["plan_hash"],
    )
    receipt = json.loads((backup_dir / applied["receipt_filename"]).read_text())

    compared = receipt["first_apply_comparator"]["before"]
    assert set(compared) == {
        "session_raw_cursors",
        "source_reconciliation_cursors",
        "source_denominator_state",
        "source_denominator_sessions",
        "source_capture_generations",
        "source_capture_expected_turns",
        "source_capture_raw_receipts",
    }
    assert receipt["first_apply_comparator"]["before"] == receipt["first_apply_comparator"]["after"]


def test_same_plan_cursor_receipt_rejects_tampered_restore_evidence(
    tmp_path: Path,
):
    cursor = tmp_path / "agent_sync_cursors.db"
    _legacy_cursor(cursor)
    backup_dir = tmp_path / "backups"
    preview = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=backup_dir,
        apply=False,
        daemon_inactive=True,
    )
    applied = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=backup_dir,
        apply=True,
        daemon_inactive=True,
        expected_plan_hash=preview["plan_hash"],
    )
    receipt_path = backup_dir / applied["receipt_filename"]
    receipt = json.loads(receipt_path.read_text())
    receipt["restore_drill_ok"] = False
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(
        cursor_reconciler.CursorSchemaReconciliationError,
        match="migration_receipt_binding_mismatch",
    ):
        reconcile_cursor_schema(
            cursor_path=cursor,
            backup_dir=backup_dir,
            apply=True,
            daemon_inactive=True,
            expected_plan_hash=preview["plan_hash"],
        )


def test_same_plan_cursor_receipt_rejects_forged_before_comparator(
    tmp_path: Path,
):
    cursor = tmp_path / "agent_sync_cursors.db"
    _legacy_cursor(cursor)
    backup_dir = tmp_path / "backups"
    preview = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=backup_dir,
        apply=False,
        daemon_inactive=True,
    )
    applied = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=backup_dir,
        apply=True,
        daemon_inactive=True,
        expected_plan_hash=preview["plan_hash"],
    )
    receipt_path = backup_dir / applied["receipt_filename"]
    receipt = json.loads(receipt_path.read_text())
    forged = dict(receipt["first_apply_comparator"]["before"])
    first_table = next(iter(forged))
    forged[first_table] = {**forged[first_table], "content_hash": "sha256:" + "0" * 64}
    receipt["first_apply_comparator"]["before"] = forged
    receipt["first_apply_comparator"]["after"] = forged
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(
        cursor_reconciler.CursorSchemaReconciliationError,
        match="migration_receipt_evidence_invalid",
    ):
        reconcile_cursor_schema(
            cursor_path=cursor,
            backup_dir=backup_dir,
            apply=True,
            daemon_inactive=True,
            expected_plan_hash=preview["plan_hash"],
        )


def test_cursor_schema_apply_holds_offline_lock_and_rechecks_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cursor = tmp_path / "agent_sync_cursors.db"
    _legacy_cursor(cursor)
    preview = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=tmp_path / "backups",
        apply=False,
        daemon_inactive=True,
    )
    lock_events: list[str] = []

    @contextmanager
    def tracked_lock(database_dir: Path, *, daemon_check):
        assert database_dir == tmp_path
        assert daemon_check(database_dir) is True
        lock_events.append("entered")
        yield
        lock_events.append("exited")

    monkeypatch.setattr(
        "scripts.reconcile_agent_sync_cursor_schema.offline_migration_lock",
        tracked_lock,
    )

    applied = execute_cursor_migration_for_test(
        cursor_path=cursor,
        backup_dir=tmp_path / "backups",
        apply=True,
        daemon_inactive=True,
        expected_plan_hash=preview["plan_hash"],
    )

    assert applied["ok"] is True
    assert lock_events == ["entered", "exited"]


def test_cursor_schema_apply_rejects_changed_source_after_review(tmp_path: Path):
    cursor = tmp_path / "agent_sync_cursors.db"
    _legacy_cursor(cursor)
    preview = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=tmp_path / "backups",
        apply=False,
        daemon_inactive=True,
    )
    with sqlite3.connect(cursor) as connection:
        connection.execute("""
            INSERT INTO source_reconciliation_cursors (
                source_name, after_canonical_session_id, updated_at
            ) VALUES ('codex', 'session-1', '2026-07-25T00:00:00+00:00')
            """)

    from scripts.reconcile_agent_sync_cursor_schema import CursorSchemaReconciliationError

    with pytest.raises(
        CursorSchemaReconciliationError,
        match="expected_plan_hash_mismatch",
    ):
        execute_cursor_migration_for_test(
            cursor_path=cursor,
            backup_dir=tmp_path / "backups",
            apply=True,
            daemon_inactive=True,
            expected_plan_hash=preview["plan_hash"],
        )
    assert not (tmp_path / "backups").exists()


def test_cursor_v2_migration_adds_empty_snapshot_binding_without_forgery(
    tmp_path: Path,
):
    cursor = tmp_path / "agent_sync_cursors.db"
    _v2_cursor(cursor)
    with sqlite3.connect(cursor) as connection:
        connection.execute("""
            INSERT INTO source_capture_generations (
                source_name, generation_id, roster_hash, started_at
            ) VALUES ('gemini', 'generation-1', 'roster-1', '2026-07-25T00:00:00+00:00')
            """)
        connection.execute("""
            INSERT INTO source_denominator_state (
                source_name, roster_hash, session_count, observed_session_count,
                observed_turn_count, complete, completed_at, updated_at
            ) VALUES (
                'gemini', 'roster-1', 1, 1, 1, 1,
                '2026-07-25T00:00:00+00:00', '2026-07-25T00:00:00+00:00'
            )
            """)

    applied = execute_cursor_migration_for_test(
        cursor_path=cursor,
        backup_dir=tmp_path / "backups",
        apply=True,
        daemon_inactive=True,
        expected_plan_hash=reconcile_cursor_schema(
            cursor_path=cursor,
            backup_dir=tmp_path / "backups",
            apply=False,
            daemon_inactive=True,
        )["plan_hash"],
    )

    assert applied["ok"] is True
    with sqlite3.connect(cursor) as connection:
        assert connection.execute("""
            SELECT native_source_snapshot_hash, snapshot_binding_eligible
            FROM source_capture_generations WHERE source_name='gemini'
            """).fetchone() == ("", 0)

    with pytest.raises(
        AgentSyncCursorError,
        match="source capture roster hash is invalid",
    ):
        migrated_store = AgentSyncCursorStore(tmp_path)
        migrated_store.bind_native_source_snapshot(
            "gemini",
            "a" * 64,
            expected_capture_state=migrated_store.source_capture_fingerprint_state("gemini"),
        )

    store = AgentSyncCursorStore(tmp_path)
    store.reset_source_reconciliation("gemini")
    store.begin_source_denominator("gemini", ["session-1"])
    store.record_denominator_session(
        "gemini",
        "session-1",
        turn_count=1,
        turn_numbers=[0],
        turn_fingerprints={0: "b" * 64},
        artifact_evidence_hash="sha256:" + ("a" * 64),
    )
    store.record_raw_capture_receipts(
        "gemini",
        "session-1",
        [(0, "revision", "b" * 64)],
    )
    store.bind_native_source_snapshot(
        "gemini",
        "a" * 64,
        expected_capture_state=store.source_capture_fingerprint_state("gemini"),
    )
    with sqlite3.connect(cursor) as connection:
        assert connection.execute("""
            SELECT native_source_snapshot_hash, snapshot_binding_eligible
            FROM source_capture_generations WHERE source_name='gemini'
            """).fetchone() == ("a" * 64, 1)


def test_cursor_v3_migration_marks_every_historical_session_unverified(
    tmp_path: Path,
) -> None:
    cursor = tmp_path / "agent_sync_cursors.db"
    _v3_cursor(cursor)
    roster_hash = AgentSyncCursorStore._roster_hash(["session"])
    with sqlite3.connect(cursor) as connection:
        connection.execute(
            """
            INSERT INTO source_denominator_state (
                source_name, roster_hash, session_count, observed_session_count,
                observed_turn_count, complete, completed_at, updated_at
            ) VALUES ('codex', ?, 1, 1, 2, 1, 'done', 'now')
            """,
            (roster_hash,),
        )
        connection.execute(
            """
            INSERT INTO source_denominator_sessions (
                source_name, canonical_session_id, roster_hash, turn_count,
                observed_at
            ) VALUES ('codex', 'session', ?, 2, 'now')
            """,
            (roster_hash,),
        )
        connection.execute(
            """
            INSERT INTO source_capture_generations (
                source_name, generation_id, roster_hash,
                native_source_snapshot_hash, snapshot_binding_eligible,
                started_at
            ) VALUES (
                'codex', 'generation', ?, ?, 1, 'now'
            )
            """,
            (roster_hash, "a" * 64),
        )

    backup_dir = tmp_path / "backups"
    preview = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=backup_dir,
        apply=False,
        daemon_inactive=True,
    )
    applied = reconcile_cursor_schema(
        cursor_path=cursor,
        backup_dir=backup_dir,
        apply=True,
        daemon_inactive=True,
        expected_plan_hash=preview["plan_hash"],
    )

    assert applied["ok"] is True
    with sqlite3.connect(cursor) as connection:
        assert connection.execute("""
            SELECT disposition, disposition_reason, artifact_evidence_hash
            FROM source_denominator_sessions
            WHERE source_name='codex' AND canonical_session_id='session'
            """).fetchone() == ("legacy_unverified", "", "")
        assert connection.execute("""
            SELECT snapshot_binding_eligible
            FROM source_capture_generations WHERE source_name='codex'
            """).fetchone() == (0,)
    with pytest.raises(
        AgentSyncCursorError,
        match="explicit reset required",
    ):
        AgentSyncCursorStore(tmp_path).begin_source_denominator(
            "codex",
            ["session"],
        )
