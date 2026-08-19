from __future__ import annotations

import json
import stat
from pathlib import Path
import sqlite3

import pytest

import core.cognitive.feedback_history_migration as migration
from core.cognitive.feedback_history_migration import (
    build_feedback_history_inventory,
    inspect_feedback_history_coverage,
    public_inventory_report,
    reconcile_feedback_history,
    restore_feedback_history,
)
from core.cognitive.feedback_migration_barrier import (
    read_feedback_migration_barrier,
)
from core.cognitive.state_schema import initialize_cognitive_state_schema


def _execute(path: Path, ddl: str, rows: tuple[tuple[str, tuple], ...] = ()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(ddl)
        for sql, params in rows:
            conn.execute(sql, params)


def _fixture_databases(root: Path) -> None:
    _execute(
        root / "delivery_events.db",
        """
        CREATE TABLE cognitive_outcomes (
            outcome_id TEXT PRIMARY KEY,
            delivery_event_id TEXT NOT NULL
        );
        """,
        (("INSERT INTO cognitive_outcomes VALUES (?, ?)", ("out-1", "delivery-1")),),
    )
    _execute(
        root / "feedback_signals.db",
        """
        CREATE TABLE feedback_signals (
            signal_id TEXT PRIMARY KEY,
            source_event_id TEXT NOT NULL,
            target_ref TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        );
        """,
        (
            (
                "INSERT INTO feedback_signals VALUES (?, ?, ?, ?)",
                ("signal-1", "feedback-1", "delivery-1", '{"subject":"secret-a"}'),
            ),
            (
                "INSERT INTO feedback_signals VALUES (?, ?, ?, ?)",
                ("signal-2", "feedback-1", "delivery-1", '{"subject":"secret-a"}'),
            ),
        ),
    )
    _execute(
        root / "mnemos.db",
        """
        CREATE TABLE search_sessions (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            query TEXT NOT NULL,
            result_paths TEXT NOT NULL,
            outcome_status TEXT NOT NULL
        );
        """,
        (
            (
                "INSERT INTO search_sessions VALUES (?, ?, ?, ?, ?)",
                (1, "search-1", "private-query", "[]", "click"),
            ),
        ),
    )
    _execute(
        root / "reflections.db",
        """
        CREATE TABLE layer5_experiences (
            id INTEGER PRIMARY KEY,
            type TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            evidence TEXT NOT NULL
        );
        """,
        (
            (
                "INSERT INTO layer5_experiences VALUES (?, ?, ?, ?)",
                (1, "outcome_feedback", "feedback-1", '["private-reflection"]'),
            ),
        ),
    )
    _execute(
        root / "rule_weight_optimizer.db",
        """
        CREATE TABLE rule_outcomes (
            id INTEGER PRIMARY KEY,
            rule_name TEXT NOT NULL,
            source_event_id TEXT NOT NULL
        );
        """,
        (
            (
                "INSERT INTO rule_outcomes VALUES (?, ?, ?)",
                (1, "push_feedback:profile", "feedback-1"),
            ),
        ),
    )
    initialize_cognitive_state_schema(root / "producer_consumer_ledger.db")


def test_inventory_preserves_object_identity_and_hides_semantic_bytes(tmp_path: Path):
    _fixture_databases(tmp_path)

    inventory = build_feedback_history_inventory(tmp_path)
    report = public_inventory_report(
        inventory,
        target_db=tmp_path / "producer_consumer_ledger.db",
    )
    encoded = json.dumps(report, ensure_ascii=False)

    assert inventory["object_count"] == 6
    assert inventory["counts_by_domain"] == {
        "delivery_feedback": 3,
        "reflection_optimizer": 2,
        "scoring_search": 1,
    }
    signal_objects = [
        item
        for item in inventory["objects"]
        if item.database_class == "feedback_signals"
    ]
    assert len({item.source_key for item in signal_objects}) == 2
    assert "private-query" not in encoded
    assert "private-reflection" not in encoded
    assert "secret-a" not in encoded
    assert report["sensitive_bytes_in_report"] == 0


def test_inventory_is_read_only_and_rejects_schema_drift(tmp_path: Path):
    _fixture_databases(tmp_path)
    before = migration._file_hash(tmp_path / "feedback_signals.db")

    build_feedback_history_inventory(tmp_path)

    assert migration._file_hash(tmp_path / "feedback_signals.db") == before
    with sqlite3.connect(tmp_path / "feedback_signals.db") as conn:
        conn.execute("ALTER TABLE feedback_signals ADD COLUMN surprise TEXT")
    with pytest.raises(RuntimeError, match="unknown feedback source schema"):
        build_feedback_history_inventory(tmp_path)


def test_inventory_rejects_malformed_json_and_orphan_receipt(tmp_path: Path):
    _fixture_databases(tmp_path)
    with sqlite3.connect(tmp_path / "reflections.db") as conn:
        conn.execute("UPDATE layer5_experiences SET evidence='not-json'")
    with pytest.raises(RuntimeError, match="malformed feedback source JSON"):
        build_feedback_history_inventory(tmp_path)

    _fixture_databases(tmp_path / "orphan")
    _execute(
        tmp_path / "orphan" / "delivery_events.db",
        """
        CREATE TABLE outcome_feedback_events (
            feedback_event_id TEXT PRIMARY KEY
        );
        CREATE TABLE outcome_projection_receipts (
            feedback_event_id TEXT NOT NULL,
            projection TEXT NOT NULL,
            PRIMARY KEY(feedback_event_id, projection)
        );
        """,
        (
            (
                "INSERT INTO outcome_projection_receipts VALUES (?, ?)",
                ("feedback-orphan", "profile"),
            ),
        ),
    )
    with pytest.raises(RuntimeError, match="orphan feedback projection receipt"):
        build_feedback_history_inventory(tmp_path / "orphan")


def test_apply_quarantines_only_replays_and_restores_exact_preimage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "db"
    _fixture_databases(root)
    inventory = build_feedback_history_inventory(root)
    target = root / "producer_consumer_ledger.db"
    preimage_hash = migration._database_logical_hash(target)
    monkeypatch.setattr(migration, "_daemon_is_active", lambda: False)

    applied = reconcile_feedback_history(
        database_dir=root,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "backups-1",
    )

    assert applied["effect"] == {"inserted": 6, "existing": 0}
    assert applied["active_head_delta"] == 0
    assert applied["active_revision_delta"] == 0
    assert applied["coverage"]["covered"] == 6
    assert not (root / ".feedback_migration_barrier.json").exists()
    with sqlite3.connect(f"file:{target}?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_heads").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0] == 0

    replayed = reconcile_feedback_history(
        database_dir=root,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "backups-2",
    )
    assert replayed["effect"] == {"inserted": 0, "existing": 6}

    restored = restore_feedback_history(
        database_dir=root,
        restore_manifest=Path(applied["backup_manifest"]),
    )
    assert restored["status"] == "restored"
    assert migration._database_logical_hash(target) == preimage_hash
    assert inspect_feedback_history_coverage(target, inventory)["uncovered"] == 6


def test_restore_fails_closed_when_any_sealed_source_backup_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "db"
    _fixture_databases(root)
    inventory = build_feedback_history_inventory(root)
    monkeypatch.setattr(migration, "_daemon_is_active", lambda: False)
    applied = reconcile_feedback_history(
        database_dir=root,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "backups",
    )
    manifest_path = Path(applied["backup_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    delivery_backup = next(
        item
        for item in manifest["backups"]
        if item["database_class"] == "delivery_events"
    )
    Path(delivery_backup["backup_path"]).unlink()

    with pytest.raises(ValueError, match="backup is missing: delivery_events"):
        restore_feedback_history(
            database_dir=root,
            restore_manifest=manifest_path,
        )

    assert not (root / ".feedback_migration_barrier.json").exists()


def test_restore_rejects_rehashed_manifest_with_omitted_database_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "db"
    _fixture_databases(root)
    inventory = build_feedback_history_inventory(root)
    monkeypatch.setattr(migration, "_daemon_is_active", lambda: False)
    applied = reconcile_feedback_history(
        database_dir=root,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "backups",
    )
    manifest_path = Path(applied["backup_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["backups"] = [
        item
        for item in manifest["backups"]
        if item["database_class"] != "delivery_events"
    ]
    manifest["database_classes"] = [
        value for value in manifest["database_classes"] if value != "delivery_events"
    ]
    core = dict(manifest)
    core.pop("manifest_hash")
    manifest["manifest_hash"] = migration.sha256_json(core)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="class set is incomplete"):
        restore_feedback_history(
            database_dir=root,
            restore_manifest=manifest_path,
        )


def test_restore_validates_all_source_backups_under_migration_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "db"
    _fixture_databases(root)
    inventory = build_feedback_history_inventory(root)
    monkeypatch.setattr(migration, "_daemon_is_active", lambda: False)
    applied = reconcile_feedback_history(
        database_dir=root,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "backups",
    )
    original_validate = migration._validate_feedback_restore_manifest

    def validate_under_barrier(database_dir, manifest):
        assert read_feedback_migration_barrier(database_dir) is not None
        return original_validate(database_dir, manifest)

    monkeypatch.setattr(
        migration,
        "_validate_feedback_restore_manifest",
        validate_under_barrier,
    )

    restored = restore_feedback_history(
        database_dir=root,
        restore_manifest=Path(applied["backup_manifest"]),
    )

    assert restored["status"] == "restored"
    assert not (root / ".feedback_migration_barrier.json").exists()


def test_backup_manifest_seals_absent_source_database_classes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "db"
    _fixture_databases(root)
    (root / "feedback_signals.db").unlink()
    inventory = build_feedback_history_inventory(root)
    monkeypatch.setattr(migration, "_daemon_is_active", lambda: False)

    applied = reconcile_feedback_history(
        database_dir=root,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "backups",
    )

    manifest = json.loads(
        Path(applied["backup_manifest"]).read_text(encoding="utf-8")
    )
    absent = next(
        item
        for item in manifest["backups"]
        if item["database_class"] == "feedback_signals"
    )
    assert absent == {
        "database_class": "feedback_signals",
        "state": "absent",
        "source_path": str((root / "feedback_signals.db").resolve()),
        "backup_path": "",
        "file_hash": "",
        "integrity": "not_applicable",
        "source_logical_hash": "",
        "backup_logical_hash": "",
    }
    restored = restore_feedback_history(
        database_dir=root,
        restore_manifest=Path(applied["backup_manifest"]),
    )
    assert restored["validated_backup_count"] == 6


def test_apply_rejects_inventory_drift_before_barrier_or_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _fixture_databases(tmp_path)
    inventory = build_feedback_history_inventory(tmp_path)
    monkeypatch.setattr(migration, "_daemon_is_active", lambda: False)

    with pytest.raises(RuntimeError, match="inventory hash drift"):
        reconcile_feedback_history(
            database_dir=tmp_path,
            expected_inventory_hash="sha256:" + "0" * 64,
            expected_object_manifest_hash=inventory["object_manifest_hash"],
            backup_dir=tmp_path / "backups",
        )

    assert not (tmp_path / ".feedback_migration_barrier.json").exists()
    assert not (tmp_path / "backups").exists()


def test_apply_holds_every_source_write_lock_before_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _fixture_databases(tmp_path)
    inventory = build_feedback_history_inventory(tmp_path)
    monkeypatch.setattr(migration, "_daemon_is_active", lambda: False)
    original_backup = migration._backup_feedback_databases
    observed: dict[str, bool] = {}

    def assert_locked_then_backup(*args, **kwargs):
        for source in migration.feedback_source_databases(tmp_path):
            if not source.path.is_file():
                continue
            competing = sqlite3.connect(source.path, timeout=0)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    competing.execute("BEGIN IMMEDIATE")
            finally:
                competing.close()
            observed[source.database_class] = True
        target = tmp_path / "producer_consumer_ledger.db"
        competing_target = sqlite3.connect(target, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing_target.execute("BEGIN IMMEDIATE")
        finally:
            competing_target.close()
        observed["cognitive_state_target"] = True
        return original_backup(*args, **kwargs)

    monkeypatch.setattr(migration, "_backup_feedback_databases", assert_locked_then_backup)

    applied = reconcile_feedback_history(
        database_dir=tmp_path,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "backups",
    )

    expected = {
        source.database_class
        for source in migration.feedback_source_databases(tmp_path)
        if source.path.is_file()
    }
    expected.add("cognitive_state_target")
    assert set(observed) == expected
    assert applied["effect"] == {"inserted": 6, "existing": 0}


def test_apply_rolls_back_target_when_in_transaction_coverage_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _fixture_databases(tmp_path)
    inventory = build_feedback_history_inventory(tmp_path)
    target = tmp_path / "producer_consumer_ledger.db"
    target_before = migration._database_logical_hash(target)
    monkeypatch.setattr(migration, "_daemon_is_active", lambda: False)
    monkeypatch.setattr(
        migration,
        "_inspect_feedback_history_coverage_in_connection",
        lambda _conn, objects: {
            "covered": 0,
            "uncovered": len(objects),
            "unexpected": 0,
            "active_promotion": 0,
        },
    )

    with pytest.raises(RuntimeError, match="coverage verification failed"):
        reconcile_feedback_history(
            database_dir=tmp_path,
            expected_inventory_hash=inventory["inventory_hash"],
            expected_object_manifest_hash=inventory["object_manifest_hash"],
            backup_dir=tmp_path / "backups",
        )

    assert migration._database_logical_hash(target) == target_before
    assert not (tmp_path / ".feedback_migration_barrier.json").exists()


def test_backup_directory_and_artifacts_are_owner_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _fixture_databases(tmp_path)
    inventory = build_feedback_history_inventory(tmp_path)
    monkeypatch.setattr(migration, "_daemon_is_active", lambda: False)
    backup_dir = tmp_path / "backups"

    applied = reconcile_feedback_history(
        database_dir=tmp_path,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=backup_dir,
    )

    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
    manifest = Path(applied["backup_manifest"])
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert {
        stat.S_IMODE(Path(item["backup_path"]).stat().st_mode)
        for item in payload["backups"]
    } == {0o600}


def test_private_backup_files_are_owner_only_from_inode_creation(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "private-backup.db"
    manifest_path = tmp_path / "private-manifest.json"

    migration._secure_create_empty_file(sqlite_path)
    migration._write_private_text_file(manifest_path, "{}\n")

    assert stat.S_IMODE(sqlite_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600


def test_restore_holds_exclusive_target_lock_through_staged_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "db"
    _fixture_databases(root)
    inventory = build_feedback_history_inventory(root)
    target = root / "producer_consumer_ledger.db"
    monkeypatch.setattr(migration, "_daemon_is_active", lambda: False)
    applied = reconcile_feedback_history(
        database_dir=root,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "backups",
    )
    original_integrity = migration._connection_integrity
    observed = {"locked": False}

    def assert_target_locked(conn: sqlite3.Connection) -> str:
        staged_path = Path(
            str(conn.execute("PRAGMA database_list").fetchone()[2])
        )
        if ".feedback-restore-" in staged_path.name:
            competitor = sqlite3.connect(target, timeout=0)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    competitor.execute("BEGIN IMMEDIATE")
            finally:
                competitor.close()
            observed["locked"] = True
        return original_integrity(conn)

    monkeypatch.setattr(migration, "_connection_integrity", assert_target_locked)

    restored = restore_feedback_history(
        database_dir=root,
        restore_manifest=Path(applied["backup_manifest"]),
    )

    assert restored["status"] == "restored"
    assert observed == {"locked": True}


def test_restore_locks_target_before_staging_can_accept_a_competing_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "db"
    _fixture_databases(root)
    inventory = build_feedback_history_inventory(root)
    target = root / "producer_consumer_ledger.db"
    monkeypatch.setattr(migration, "_daemon_is_active", lambda: False)
    applied = reconcile_feedback_history(
        database_dir=root,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "backups",
    )
    original_backup = migration._sqlite_backup
    observed = {"staging_locked": False}

    def assert_locked_before_staging(source: Path, destination: Path) -> None:
        if ".feedback-restore-" in destination.name:
            competitor = sqlite3.connect(target, timeout=0)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    competitor.execute("BEGIN IMMEDIATE")
            finally:
                competitor.close()
            observed["staging_locked"] = True
        original_backup(source, destination)

    monkeypatch.setattr(migration, "_sqlite_backup", assert_locked_before_staging)

    restored = restore_feedback_history(
        database_dir=root,
        restore_manifest=Path(applied["backup_manifest"]),
    )

    assert restored["status"] == "restored"
    assert observed == {"staging_locked": True}


def test_restore_verification_failure_keeps_current_target_preimage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "db"
    _fixture_databases(root)
    inventory = build_feedback_history_inventory(root)
    target = root / "producer_consumer_ledger.db"
    monkeypatch.setattr(migration, "_daemon_is_active", lambda: False)
    applied = reconcile_feedback_history(
        database_dir=root,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "backups",
    )
    applied_hash = migration._database_logical_hash(target)
    original_logical_hash = migration._connection_logical_hash

    def fail_staged_verification(conn: sqlite3.Connection) -> str:
        staged_path = Path(
            str(conn.execute("PRAGMA database_list").fetchone()[2])
        )
        if ".feedback-restore-" in staged_path.name:
            raise OSError("injected staged restore verification failure")
        return original_logical_hash(conn)

    monkeypatch.setattr(
        migration,
        "_connection_logical_hash",
        fail_staged_verification,
    )

    with pytest.raises(OSError, match="staged restore verification failure"):
        restore_feedback_history(
            database_dir=root,
            restore_manifest=Path(applied["backup_manifest"]),
        )

    assert migration._database_logical_hash(target) == applied_hash
    assert not tuple(target.parent.glob(".*.feedback-restore-*.tmp"))


def test_source_integrity_failure_precedes_backup_and_target_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _fixture_databases(tmp_path)
    inventory = build_feedback_history_inventory(tmp_path)
    target = tmp_path / "producer_consumer_ledger.db"
    target_before = migration._database_logical_hash(target)
    monkeypatch.setattr(migration, "_daemon_is_active", lambda: False)
    monkeypatch.setattr(migration, "_connection_integrity", lambda _conn: "corrupt")

    with pytest.raises(RuntimeError, match="feedback source integrity failed"):
        reconcile_feedback_history(
            database_dir=tmp_path,
            expected_inventory_hash=inventory["inventory_hash"],
            expected_object_manifest_hash=inventory["object_manifest_hash"],
            backup_dir=tmp_path / "backups",
        )

    assert migration._database_logical_hash(target) == target_before
    assert not (tmp_path / "backups").exists()
    assert not (tmp_path / ".feedback_migration_barrier.json").exists()
