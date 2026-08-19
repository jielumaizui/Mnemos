"""Cross-owner physical contract for Phase 1 SQLite copies and restores."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.kia import amphora_provenance_support as provenance_support
from core.sync_framework import native_artifact_inventory as native_inventory
from core.ops.durable_io import (
    DurableIOError,
    normalize_private_sqlite_copy,
    private_sqlite_sidecars,
    validate_private_sqlite_copy,
)
from scripts import agent_source_raw_migration_certification as raw_certification
from scripts import agent_source_raw_recovery_support as raw_support
from scripts import reconcile_agent_source_raw_capture as raw_reconciler
from scripts import reconcile_agent_sync_cursor_schema as cursor_reconciler
from scripts import reconcile_amphora_source_spans as amphora_reconciler
from scripts import reconcile_cognitive_state_store as cognitive_reconciler
from scripts import reconcile_distill_runtime_receipts as distill_reconciler
from scripts import reconcile_sync_event_handoffs as handoff_reconciler


def test_phase1_sqlite_backup_owner_denominator_is_governance_derived() -> None:
    from scripts.generate_phase1_baseline_execution_evidence import (
        _phase1_specs,
    )

    production_prefixes = ("core/", "daemon/", "integrations/", "scripts/")
    candidate_paths = {
        str(path)
        for spec in _phase1_specs()
        for key in ("candidate_paths", "mutation_candidate_paths")
        for path in spec.get(key, ())
        if str(path).startswith(production_prefixes)
    }
    discovered = {
        path
        for path in candidate_paths
        if (Path(path).is_file() and ".backup(" in Path(path).read_text(encoding="utf-8"))
    }
    expected = {
        "core/sync_framework/native_artifact_inventory.py",
        "core/kia/amphora_provenance_support.py",
        "core/ops/offline_schema_plan.py",
        "scripts/agent_source_raw_migration_certification.py",
        "scripts/agent_source_raw_recovery_support.py",
        "scripts/reconcile_agent_source_raw_capture.py",
        "scripts/reconcile_agent_sync_cursor_schema.py",
        "scripts/reconcile_amphora_source_spans.py",
        "scripts/reconcile_cognitive_state_store.py",
        "scripts/reconcile_distill_runtime_receipts.py",
        "scripts/reconcile_sync_event_handoffs.py",
    }

    assert discovered == expected
    for path in discovered:
        source = Path(path).read_text(encoding="utf-8")
        assert "normalize_private_sqlite_copy(" in source
        assert source.count("owned_sqlite_connection_pair(") == source.count(".backup(")


def _create_wal_database(path: Path, value: str = "before") -> None:
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()


def _logical_value(path: Path) -> str:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        return str(connection.execute("SELECT value FROM sentinel").fetchone()[0])
    finally:
        connection.close()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_standalone_and_read_stable(path: Path) -> None:
    validate_private_sqlite_copy(path)
    assert not any(sidecar.exists() for sidecar in private_sqlite_sidecars(path))
    before = (
        _file_sha256(path),
        path.stat().st_size,
        path.stat().st_mtime_ns,
    )
    with sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro",
        uri=True,
    ) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    after = (
        _file_sha256(path),
        path.stat().st_size,
        path.stat().st_mtime_ns,
    )
    assert after == before
    assert not any(sidecar.exists() for sidecar in private_sqlite_sidecars(path))


def _assert_no_restore_stages(directory: Path) -> None:
    assert not [item.name for item in directory.iterdir() if ".restore" in item.name]


def _provenance_backup(
    source: Path,
    backup_root: Path,
) -> Path:
    messages_dir = source.parent / "distill_messages"
    messages_dir.mkdir(mode=0o700, exist_ok=True)
    messages_path = messages_dir / "legacy-task.json"
    messages_bytes = b'[{"role":"user","content":"legacy"}]'
    messages_path.write_bytes(messages_bytes)
    with sqlite3.connect(source) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("""
            CREATE TABLE distillation_tasks (
                task_id TEXT PRIMARY KEY,
                messages_path TEXT NOT NULL
            )
            """)
        connection.execute(
            "INSERT INTO distillation_tasks VALUES (?, ?)",
            ("legacy-task", str(messages_path)),
        )
        row = dict(
            connection.execute(
                "SELECT * FROM distillation_tasks WHERE task_id='legacy-task'"
            ).fetchone()
        )
    context = provenance_support.AmphoraProvenanceContext(
        db_path=lambda: source,
        normalize_messages=lambda value: value,
        messages_revision=lambda _value: "unused",
        conn_seconds=30,
        legacy_provenance_reason="unused",
        migration_schema="mnemos.test.provenance.v1",
    )
    reviewed = {
        "primary_key": "legacy-task",
        "object_hash": "sha256:" + ("a" * 64),
        "row": row,
        "messages_asset": {
            "path": str(messages_path),
            "exists": True,
            "size": len(messages_bytes),
            "sha256": "sha256:" + hashlib.sha256(messages_bytes).hexdigest(),
        },
    }
    manifest_path, _manifest_hash = (
        provenance_support._backup_historical_provenance_object(  # noqa: SLF001
            context=context,
            backup_dir=backup_root,
            inventory={"inventory_hash": "sha256:" + ("b" * 64)},
            reviewed_object=reviewed,
        )
    )
    return manifest_path.parent / "distill_queue.db"


def _set_current_delete_mode(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE sentinel SET value='current'")
        connection.commit()
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)


def test_shared_normalizer_seals_wal_copy_without_read_side_effects(
    tmp_path: Path,
) -> None:
    database = tmp_path / "copy.db"
    _create_wal_database(database)

    normalize_private_sqlite_copy(database)

    assert _logical_value(database) == "before"
    _assert_standalone_and_read_stable(database)


def test_all_phase1_backup_owners_publish_standalone_sqlite_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    _create_wal_database(source)

    raw_path, _raw_record = raw_support._backup_sqlite(  # noqa: SLF001
        source,
        tmp_path / "raw-backups",
        "raw",
    )
    assert raw_path is not None

    cursor_record = cursor_reconciler._backup_sqlite(  # noqa: SLF001
        source,
        tmp_path / "cursor-backups",
    )
    cursor_path = tmp_path / "cursor-backups" / str(cursor_record["filename"])

    cognitive_record = cognitive_reconciler._backup_database(  # noqa: SLF001
        source,
        tmp_path / "cognitive-backups",
    )
    cognitive_path = Path(cognitive_record["path"])

    distill_record = distill_reconciler._backup_database(  # noqa: SLF001
        source,
        tmp_path / "distill-backups",
        label="queue",
    )
    distill_path = Path(distill_record["path"])

    handoff_record = handoff_reconciler._backup_databases(  # noqa: SLF001
        [source],
        tmp_path / "handoff-backups",
    )[0]
    handoff_path = Path(handoff_record["path"])

    amphora_path = tmp_path / "amphora-backup.db"
    amphora_reconciler._backup_database(source, amphora_path)  # noqa: SLF001

    provenance_source = tmp_path / "provenance-source.db"
    _create_wal_database(provenance_source)
    provenance_path = _provenance_backup(
        provenance_source,
        tmp_path / "provenance-backups",
    )

    for backup in (
        raw_path,
        cursor_path,
        cognitive_path,
        distill_path,
        handoff_path,
        amphora_path,
        provenance_path,
    ):
        assert _logical_value(backup) == "before"
        _assert_standalone_and_read_stable(backup)
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_all_phase1_restore_owners_use_atomic_standalone_stages(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    _create_wal_database(source)

    raw_backup, _record = raw_support._backup_sqlite(  # noqa: SLF001
        source,
        tmp_path / "raw-backups",
        "raw",
    )
    assert raw_backup is not None
    raw_target = tmp_path / "raw-target.db"
    _create_wal_database(raw_target, "wrong")
    raw_support._restore_sqlite_backup(raw_backup, raw_target)  # noqa: SLF001

    cursor_record = cursor_reconciler._backup_sqlite(  # noqa: SLF001
        source,
        tmp_path / "cursor-backups",
    )
    cursor_backup = tmp_path / "cursor-backups" / str(cursor_record["filename"])
    cursor_target = tmp_path / "cursor-target.db"
    _create_wal_database(cursor_target, "wrong")
    cursor_reconciler._restore_sqlite_backup(  # noqa: SLF001
        backup_path=cursor_backup,
        cursor_path=cursor_target,
        expected_snapshot_hash=(
            "sha256:"
            + cursor_reconciler._sqlite_snapshot_sha256(  # noqa: SLF001
                cursor_backup,
                immutable=True,
            )
        ),
    )

    cognitive_record = cognitive_reconciler._backup_database(  # noqa: SLF001
        source,
        tmp_path / "cognitive-backups",
    )
    cognitive_target = tmp_path / "cognitive-target.db"
    _create_wal_database(cognitive_target, "wrong")
    cognitive_reconciler._restore_backup(  # noqa: SLF001
        cognitive_record,
        cognitive_target,
    )

    distill_record = distill_reconciler._backup_database(  # noqa: SLF001
        source,
        tmp_path / "distill-backups",
        label="queue",
    )
    distill_target = tmp_path / "distill-target.db"
    _create_wal_database(distill_target, "wrong")
    distill_reconciler._restore_database_from_backup(  # noqa: SLF001
        distill_record,
        distill_target,
    )

    handoff_record = handoff_reconciler._backup_databases(  # noqa: SLF001
        [source],
        tmp_path / "handoff-backups",
    )[0]
    handoff_target = Path(handoff_record["source"])
    with sqlite3.connect(handoff_target) as connection:
        connection.execute("UPDATE sentinel SET value='wrong'")
    handoff_reconciler._restore_databases([handoff_record])  # noqa: SLF001

    for target in (
        raw_target,
        cursor_target,
        cognitive_target,
        distill_target,
        handoff_target,
    ):
        assert _logical_value(target) == "before"
        _assert_standalone_and_read_stable(target)
        _assert_no_restore_stages(target.parent)


def test_every_atomic_restore_failure_preserves_live_main_and_exact_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_target = tmp_path / "raw-live.db"
    _create_wal_database(raw_target)
    raw_backup, _raw_record = raw_support._backup_sqlite(  # noqa: SLF001
        raw_target,
        tmp_path / "raw-backups",
        "raw",
    )
    assert raw_backup is not None
    _set_current_delete_mode(raw_target)

    cursor_target = tmp_path / "cursor-live.db"
    _create_wal_database(cursor_target)
    cursor_record = cursor_reconciler._backup_sqlite(  # noqa: SLF001
        cursor_target,
        tmp_path / "cursor-backups",
    )
    cursor_backup = tmp_path / "cursor-backups" / str(cursor_record["filename"])
    cursor_hash = "sha256:" + cursor_reconciler._sqlite_snapshot_sha256(  # noqa: SLF001
        cursor_backup,
        immutable=True,
    )
    _set_current_delete_mode(cursor_target)

    cognitive_target = tmp_path / "cognitive-live.db"
    _create_wal_database(cognitive_target)
    cognitive_record = cognitive_reconciler._backup_database(  # noqa: SLF001
        cognitive_target,
        tmp_path / "cognitive-backups",
    )
    _set_current_delete_mode(cognitive_target)

    distill_target = tmp_path / "distill-live.db"
    _create_wal_database(distill_target)
    distill_record = distill_reconciler._backup_database(  # noqa: SLF001
        distill_target,
        tmp_path / "distill-backups",
        label="queue",
    )
    _set_current_delete_mode(distill_target)

    handoff_target = tmp_path / "handoff-live.db"
    _create_wal_database(handoff_target)
    handoff_record = handoff_reconciler._backup_databases(  # noqa: SLF001
        [handoff_target],
        tmp_path / "handoff-backups",
    )[0]
    _set_current_delete_mode(handoff_target)

    cases = (
        (
            raw_support,
            raw_target,
            lambda: raw_support._restore_sqlite_backup(  # noqa: SLF001
                raw_backup,
                raw_target,
            ),
            raw_support.AgentSourceRawReconciliationError,
            "rollback_sqlite_failed",
        ),
        (
            cursor_reconciler,
            cursor_target,
            lambda: cursor_reconciler._restore_sqlite_backup(  # noqa: SLF001
                backup_path=cursor_backup,
                cursor_path=cursor_target,
                expected_snapshot_hash=cursor_hash,
            ),
            cursor_reconciler.CursorSchemaReconciliationError,
            "rollback_failed",
        ),
        (
            cognitive_reconciler,
            cognitive_target,
            lambda: cognitive_reconciler._restore_backup(  # noqa: SLF001
                cognitive_record,
                cognitive_target,
            ),
            OSError,
            "injected atomic replace failure",
        ),
        (
            distill_reconciler,
            distill_target,
            lambda: distill_reconciler._restore_database_from_backup(  # noqa: SLF001
                distill_record,
                distill_target,
            ),
            OSError,
            "injected atomic replace failure",
        ),
        (
            handoff_reconciler,
            handoff_target,
            lambda: handoff_reconciler._restore_databases([handoff_record]),  # noqa: SLF001
            OSError,
            "injected atomic replace failure",
        ),
    )

    for owner, target, restore, expected_error, match in cases:
        live_main = target.read_bytes()
        sidecars = private_sqlite_sidecars(target)
        for index, sidecar in enumerate(sidecars):
            sidecar.write_bytes(f"sidecar-{index}".encode("ascii"))
        sidecar_preimages = {sidecar: sidecar.read_bytes() for sidecar in sidecars}

        def fail_replace(_source: Path, _target: Path) -> None:
            raise OSError("injected atomic replace failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(owner.os, "replace", fail_replace)
            with pytest.raises(expected_error, match=match):
                restore()

        assert target.read_bytes() == live_main
        assert {sidecar: sidecar.read_bytes() for sidecar in sidecars} == sidecar_preimages
        _assert_no_restore_stages(target.parent)
        for sidecar in sidecars:
            sidecar.unlink()

        original_open = os.open
        collision_paths: list[Path] = []

        def collide_with_restore_stage(
            path: Path,
            flags: int,
            mode: int = 0o777,
        ) -> int:
            candidate = Path(path)
            if flags & os.O_EXCL and ".restore" in candidate.name and not collision_paths:
                descriptor = original_open(
                    candidate,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                os.write(descriptor, b"foreign-restore-collision")
                os.close(descriptor)
                collision_paths.append(candidate)
            return original_open(candidate, flags, mode)

        collision_error = (
            raw_support.AgentSourceRawReconciliationError
            if owner is raw_support
            else (
                cursor_reconciler.CursorSchemaReconciliationError
                if owner is cursor_reconciler
                else FileExistsError
            )
        )
        with monkeypatch.context() as scoped:
            scoped.setattr(owner.os, "open", collide_with_restore_stage)
            if owner is cursor_reconciler:
                scoped.setattr(
                    cursor_reconciler,
                    "_restore_drill",
                    lambda *_args, **_kwargs: True,
                )
            with pytest.raises(collision_error):
                restore()

        assert len(collision_paths) == 1
        collision = collision_paths[0]
        assert collision.read_bytes() == b"foreign-restore-collision"
        assert target.read_bytes() == live_main
        collision.unlink()
        _assert_no_restore_stages(target.parent)


def test_backup_path_collision_is_never_deleted_or_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    _create_wal_database(source)
    coverage = tmp_path / "coverage.json"
    coverage.write_text('{"ok":true}\n', encoding="utf-8")
    cases = (
        (
            raw_support,
            lambda root: raw_support._backup_sqlite(  # noqa: SLF001
                source,
                root,
                "raw",
            ),
            raw_support.AgentSourceRawReconciliationError,
        ),
        (
            raw_support,
            lambda root: raw_support._backup_coverage(  # noqa: SLF001
                coverage,
                root,
            ),
            raw_support.AgentSourceRawReconciliationError,
        ),
        (
            cursor_reconciler,
            lambda root: cursor_reconciler._backup_sqlite(  # noqa: SLF001
                source,
                root,
            ),
            cursor_reconciler.CursorSchemaReconciliationError,
        ),
        (
            distill_reconciler,
            lambda root: distill_reconciler._backup_database(  # noqa: SLF001
                source,
                root,
                label="queue",
            ),
            FileExistsError,
        ),
        (
            cognitive_reconciler,
            lambda root: cognitive_reconciler._backup_database(  # noqa: SLF001
                source,
                root,
            ),
            FileExistsError,
        ),
        (
            handoff_reconciler,
            lambda root: handoff_reconciler._backup_databases(  # noqa: SLF001
                [source],
                root,
            ),
            FileExistsError,
        ),
        (
            amphora_reconciler,
            lambda root: amphora_reconciler._backup_database(  # noqa: SLF001
                source,
                root / "amphora.db",
            ),
            FileExistsError,
        ),
    )
    original_open = os.open

    for index, (owner, backup, expected_error) in enumerate(cases):
        backup_root = tmp_path / f"collision-backup-{index}"
        backup_root.mkdir(mode=0o700)
        collision_paths: list[Path] = []

        def collide_with_backup_target(
            path: Path,
            flags: int,
            mode: int = 0o777,
        ) -> int:
            candidate = Path(path)
            if flags & os.O_EXCL and not collision_paths:
                descriptor = original_open(
                    candidate,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                os.write(descriptor, b"foreign-backup-collision")
                os.close(descriptor)
                collision_paths.append(candidate)
            return original_open(candidate, flags, mode)

        with monkeypatch.context() as scoped:
            scoped.setattr(owner.os, "open", collide_with_backup_target)
            with pytest.raises(expected_error):
                backup(backup_root)

        assert len(collision_paths) == 1
        collision = collision_paths[0]
        assert collision.read_bytes() == b"foreign-backup-collision"
        assert list(backup_root.iterdir()) == [collision]


def test_provenance_backup_collision_is_never_deleted_or_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "provenance-source.db"
    _create_wal_database(source)
    backup_root = tmp_path / "provenance-backups"
    original_open = os.open
    collision_paths: list[Path] = []

    def collide_with_provenance_target(
        path: Path,
        flags: int,
        mode: int = 0o777,
        *args: object,
        **kwargs: object,
    ) -> int:
        candidate = Path(path)
        if flags & os.O_EXCL and candidate.name == "distill_queue.db" and not collision_paths:
            descriptor = original_open(
                candidate,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            os.write(descriptor, b"foreign-provenance-backup")
            os.close(descriptor)
            collision_paths.append(candidate)
        return original_open(candidate, flags, mode, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(provenance_support.os, "open", collide_with_provenance_target)
        with pytest.raises(FileExistsError):
            _provenance_backup(source, backup_root)

    assert len(collision_paths) == 1
    assert collision_paths[0].read_bytes() == b"foreign-provenance-backup"


def test_provenance_failed_backup_cleanup_preserves_foreign_leaf_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "provenance-cleanup-source.db"
    _create_wal_database(source)
    backup_root = tmp_path / "provenance-cleanup-backups"
    real_normalize = provenance_support.normalize_private_sqlite_copy
    foreign_path = backup_root / "legacy-task" / "foreign-during-cleanup"

    def inject_foreign_then_fail(path: Path) -> None:
        real_normalize(path)
        foreign_path.write_bytes(b"foreign")
        raise RuntimeError("injected backup failure")

    monkeypatch.setattr(
        provenance_support,
        "normalize_private_sqlite_copy",
        inject_foreign_then_fail,
    )
    with pytest.raises(RuntimeError, match="injected backup failure"):
        _provenance_backup(source, backup_root)

    assert foreign_path.read_bytes() == b"foreign"


def test_provenance_backup_rejects_replaced_manifest_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "provenance-manifest-source.db"
    _create_wal_database(source)
    backup_root = tmp_path / "provenance-manifest-backups"
    real_fsync_directory = provenance_support.fsync_directory
    replacements: list[Path] = []

    def replace_manifest_after_sync(path: Path) -> None:
        real_fsync_directory(path)
        manifest = path / "backup_manifest.json"
        if manifest.exists() and not replacements:
            manifest.unlink()
            manifest.write_bytes(b"foreign-manifest")
            replacements.append(manifest)

    monkeypatch.setattr(
        provenance_support,
        "fsync_directory",
        replace_manifest_after_sync,
    )
    with pytest.raises(
        DurableIOError,
        match="durable_target_preimage_changed",
    ):
        _provenance_backup(source, backup_root)

    assert len(replacements) == 1
    assert replacements[0].read_bytes() == b"foreign-manifest"


def test_restore_drill_collision_preserves_foreign_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    _create_wal_database(source)
    cursor_record = cursor_reconciler._backup_sqlite(  # noqa: SLF001
        source,
        tmp_path / "cursor-backups",
    )
    cursor_backup = tmp_path / "cursor-backups" / str(cursor_record["filename"])
    cursor_hash = "sha256:" + cursor_reconciler._sqlite_snapshot_sha256(  # noqa: SLF001
        cursor_backup,
        immutable=True,
    )
    raw_backup, raw_record = raw_support._backup_sqlite(  # noqa: SLF001
        source,
        tmp_path / "raw-backups",
        "raw",
    )
    cursor_raw_backup, cursor_raw_record = raw_support._backup_sqlite(  # noqa: SLF001
        source,
        tmp_path / "raw-backups",
        "cursor",
    )
    coverage = tmp_path / "coverage.json"
    coverage.write_text('{"ok":true}\n', encoding="utf-8")
    coverage_backup, coverage_record = raw_support._backup_coverage(  # noqa: SLF001
        coverage,
        tmp_path / "raw-backups",
    )
    assert raw_backup is not None
    assert cursor_raw_backup is not None
    assert coverage_backup is not None
    before = {
        "raw": raw_support._file_scope(source, sqlite_file=True),  # noqa: SLF001
        "cursor": raw_support._file_scope(source, sqlite_file=True),  # noqa: SLF001
        "coverage": raw_support._file_scope(coverage),  # noqa: SLF001
    }
    for owner, create_name, drill in (
        (
            cursor_reconciler,
            "_create_private_sqlite_target",
            lambda: cursor_reconciler._restore_drill(  # noqa: SLF001
                cursor_backup,
                cursor_hash,
            ),
        ),
        (
            raw_certification,
            "_create_private_target",
            lambda: raw_certification._restore_drill_ok(  # noqa: SLF001
                before=before,
                backups={
                    "raw": raw_record,
                    "cursor": cursor_raw_record,
                    "coverage": coverage_record,
                },
                backup_dir=tmp_path / "raw-backups",
            ),
        ),
    ):
        collision_paths: list[Path] = []
        original_create = getattr(owner, create_name)

        def collide_with_restore_drill(path: Path) -> None:
            candidate = Path(path)
            if ".restore-drill" in candidate.name and not collision_paths:
                candidate.write_bytes(b"foreign-restore-drill")
                collision_paths.append(candidate)
            original_create(candidate)

        with monkeypatch.context() as scoped:
            scoped.setattr(owner, create_name, collide_with_restore_drill)
            assert drill() is False

        assert len(collision_paths) == 1
        collision = collision_paths[0]
        assert collision.read_bytes() == b"foreign-restore-drill"
        collision.unlink()


def test_challenger_snapshot_collision_preserves_foreign_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "raw.db"
    _create_wal_database(source)
    worker_root = tmp_path / "challenger"
    worker_root.mkdir(mode=0o700)
    collision = worker_root / "raw-read-snapshot.sqlite"
    collision.write_bytes(b"foreign-challenger-snapshot")

    with pytest.raises(
        raw_support.AgentSourceRawReconciliationError,
        match="native_challenger_raw_snapshot_failed",
    ):
        raw_reconciler._create_private_challenger_raw_snapshot(  # noqa: SLF001
            source,
            worker_root,
        )

    assert collision.read_bytes() == b"foreign-challenger-snapshot"


def test_control_record_temp_collision_preserves_foreign_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_uuid = SimpleNamespace(hex="fixed")
    cases: list[tuple[object, Path, object, type[BaseException]]] = []

    cognitive_target = tmp_path / "cognitive" / "receipt.json"
    cases.append(
        (
            cognitive_reconciler,
            cognitive_target,
            lambda: cognitive_reconciler._atomic_write_json(  # noqa: SLF001
                cognitive_target,
                {"status": "prepared"},
            ),
            FileExistsError,
        )
    )
    distill_target = tmp_path / "distill" / "receipt.json"
    cases.append(
        (
            distill_reconciler,
            distill_target,
            lambda: distill_reconciler._atomic_write_json(  # noqa: SLF001
                distill_target,
                {"status": "prepared"},
            ),
            FileExistsError,
        )
    )
    cursor_target = tmp_path / "cursor" / "receipt.json"
    cases.append(
        (
            cursor_reconciler,
            cursor_target,
            lambda: cursor_reconciler._write_receipt(  # noqa: SLF001
                cursor_target,
                {"status": "prepared"},
            ),
            FileExistsError,
        )
    )
    raw_receipt_target = tmp_path / "raw-receipt" / "receipt.json"
    cases.append(
        (
            raw_support,
            raw_receipt_target,
            lambda: raw_support._write_receipt(  # noqa: SLF001
                raw_receipt_target,
                {"status": "prepared"},
            ),
            FileExistsError,
        )
    )
    raw_new_target = tmp_path / "raw-new" / "receipt.json"
    cases.append(
        (
            raw_support,
            raw_new_target,
            lambda: raw_support._write_new_receipt(  # noqa: SLF001
                raw_new_target,
                {"status": "prepared"},
            ),
            raw_support.AgentSourceRawReconciliationError,
        )
    )

    for owner, target, write, expected_error in cases:
        target.parent.mkdir(mode=0o700, parents=True)
        temporary = target.with_name(f".{target.name}.fixed.tmp")
        temporary.write_bytes(b"foreign-control-temp")
        with monkeypatch.context() as scoped:
            scoped.setattr(owner.uuid, "uuid4", lambda: fixed_uuid)
            with pytest.raises(expected_error):
                write()
        assert temporary.read_bytes() == b"foreign-control-temp"
        assert not target.exists()

    receipt_dir = tmp_path / "raw-history"
    receipt_dir.mkdir(mode=0o700)
    source_receipt = receipt_dir / "source-receipt.json"
    source_receipt.write_text(
        '{"status":"recovered_rollback"}',
        encoding="utf-8",
    )
    source_receipt.chmod(0o600)
    receipt_bytes = source_receipt.read_bytes()
    digest = hashlib.sha256(receipt_bytes).hexdigest()
    archive = receipt_dir / (f"agent-source-raw-migration-history.abc.{digest}.json")
    archive_temporary = archive.with_name(f".{archive.name}.fixed.tmp")
    archive_temporary.write_bytes(b"foreign-history-temp")
    with monkeypatch.context() as scoped:
        scoped.setattr(raw_certification.uuid, "uuid4", lambda: fixed_uuid)
        with pytest.raises(
            raw_support.AgentSourceRawReconciliationError,
            match="migration_receipt_history_write_failed",
        ):
            raw_certification._archive_terminal_migration_receipt(  # noqa: SLF001
                receipt_path=source_receipt,
                backup_dir=receipt_dir,
                plan_hash="sha256:abc",
            )
    assert archive_temporary.read_bytes() == b"foreign-history-temp"
    assert not archive.exists()

    owner_root = tmp_path / "native-owner"
    owner_root.mkdir(mode=0o700)
    owner_temporary = owner_root / f".owner.{os.getpid()}.123.tmp"
    owner_temporary.write_bytes(b"foreign-owner-temp")
    with monkeypatch.context() as scoped:
        scoped.setattr(
            native_inventory.time,
            "time_ns",
            lambda: 123,
        )
        with pytest.raises(
            native_inventory.NativeArtifactInventoryError,
            match="native_snapshot_registry_unavailable",
        ):
            native_inventory._write_snapshot_owner_marker(  # noqa: SLF001
                owner_root,
                (os.getpid(),),
            )
    assert owner_temporary.read_bytes() == b"foreign-owner-temp"
    assert not (owner_root / ".owner.json").exists()


def test_challenger_restore_drills_and_same_plan_copies_leave_no_sidecars(
    tmp_path: Path,
) -> None:
    source = tmp_path / "raw.db"
    _create_wal_database(source)

    worker_root = tmp_path / "challenger"
    worker_root.mkdir()
    challenger = raw_reconciler._create_private_challenger_raw_snapshot(  # noqa: SLF001
        source,
        worker_root,
    )
    _assert_standalone_and_read_stable(challenger)

    verification = tmp_path / "verification.db"
    raw_certification._copy_verification_file(  # noqa: SLF001
        source,
        verification,
        sqlite_file=True,
    )
    _assert_standalone_and_read_stable(verification)

    raw_backup, raw_record = raw_support._backup_sqlite(  # noqa: SLF001
        source,
        tmp_path / "raw-backups",
        "raw",
    )
    cursor_backup, cursor_record = raw_support._backup_sqlite(  # noqa: SLF001
        source,
        tmp_path / "raw-backups",
        "cursor",
    )
    coverage = tmp_path / "coverage.json"
    coverage.write_text('{"ok":true}\n', encoding="utf-8")
    coverage_backup, coverage_record = raw_support._backup_coverage(  # noqa: SLF001
        coverage,
        tmp_path / "raw-backups",
    )
    assert raw_backup is not None
    assert cursor_backup is not None
    assert coverage_backup is not None
    before = {
        "raw": raw_support._file_scope(source, sqlite_file=True),  # noqa: SLF001
        "cursor": raw_support._file_scope(source, sqlite_file=True),  # noqa: SLF001
        "coverage": raw_support._file_scope(coverage),  # noqa: SLF001
    }
    assert raw_certification._restore_drill_ok(  # noqa: SLF001
        before=before,
        backups={
            "raw": raw_record,
            "cursor": cursor_record,
            "coverage": coverage_record,
        },
        backup_dir=tmp_path / "raw-backups",
    )
    assert not [
        item.name for item in (tmp_path / "raw-backups").iterdir() if ".restore-drill" in item.name
    ]
