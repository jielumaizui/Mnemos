"""COG-018 reviewed-hash and sealed-recovery contract tests."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.migrations.model_call_ledger_reconcile import executor, runtime
from core.migrations.registry import MigrationLedger, MigrationRegistry
from core.telemetry.prompt_call_log import ModelCallLedger


class _Config:
    def __init__(self, root: Path):
        self.mnemos_dir = root
        self.data_dir = root
        self.database_dir = root / "db"
        self.database_dir.mkdir(parents=True)
        self.config_path = root / "configs" / "main.json"
        self.config_path.parent.mkdir(parents=True)
        self._data: dict[str, object] = {}
        self.config_path.write_text("{}", encoding="utf-8")

    def get(self, key: str, default=None):
        value: object = self._data
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def to_dict(self):
        return json.loads(json.dumps(self._data))


def _seed_reconcilable_legacy_source(config: _Config, *, secret: str) -> Path:
    # Start with the canonical owner so reconciliation exercises the complete
    # canonical-plus-retired-source backup and restore set.
    ModelCallLedger.for_config(config)
    source = config.database_dir / "wiki_state.db"
    with sqlite3.connect(str(source)) as conn:
        conn.execute(
            """
            CREATE TABLE prompt_calls (
                operation TEXT,
                provider TEXT,
                model TEXT,
                prompt_hash TEXT,
                prompt_summary TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                created_at TEXT,
                session_id TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO prompt_calls VALUES (
                'distill', 'provider', 'model', ?, ?, 2, 1, ?, 'legacy-session'
            )
            """,
            ("a" * 64, secret, "2026-07-14T00:00:00+00:00"),
        )
    return source


def _reviewed_hash(registry: MigrationRegistry, config: _Config) -> str:
    plan = registry.plan(config)
    item = next(
        item
        for item in plan.items
        if item.migration_id == "database.model_call_ledger.v1"
    )
    assert item.execution_plan_hash
    return item.execution_plan_hash


def _fixture_text(label: str) -> str:
    return f"fixture-content-{label}"


def _apply_sealed(config: _Config, monkeypatch):
    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)
    registry = MigrationRegistry()
    expected = _reviewed_hash(registry, config)
    record = registry.apply(
        config,
        "database.model_call_ledger.v1",
        execute_wrapped=True,
        expected_plan_hash=expected,
        discard_unattributable_legacy=True,
    )
    assert record.status == "applied", record.error
    return registry, record


def test_reviewed_hash_missing_or_mismatched_is_zero_write(tmp_path, monkeypatch):
    config = _Config(tmp_path)
    source = _seed_reconcilable_legacy_source(config, secret="PRIVATE_LITERAL_NOT_TO_PERSIST")
    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)
    registry = MigrationRegistry()
    reviewed_hash = _reviewed_hash(registry, config)
    assert source.exists()
    original = source.read_bytes()

    missing = registry.apply(
        config,
        "database.model_call_ledger.v1",
        execute_wrapped=True,
        discard_unattributable_legacy=True,
    )
    mismatch = registry.apply(
        config,
        "database.model_call_ledger.v1",
        execute_wrapped=True,
        expected_plan_hash="sha256:" + "0" * 64,
        discard_unattributable_legacy=True,
    )

    assert missing.status == "blocked"
    assert missing.error == "expected_plan_hash_required"
    assert mismatch.status == "blocked"
    assert mismatch.error == "expected_plan_hash_mismatch"
    assert missing.verification["execution_plan_hash"] == reviewed_hash
    assert mismatch.verification["execution_plan_hash"] == reviewed_hash
    assert source.read_bytes() == original
    assert not (tmp_path / "migrations.db").exists()
    assert not (tmp_path / "backups").exists()


def test_clean_second_apply_is_noop_before_config_ledger_or_backup_provisioning(
    tmp_path, monkeypatch, capsys
):
    from core.cli.commands.migrate import cmd_migrate

    empty_runtime = tmp_path / "empty-runtime"
    monkeypatch.setenv("MNEMOS_DIR", str(empty_runtime))
    args = SimpleNamespace(
        migrate_cmd="apply",
        migration_id="database.model_call_ledger.v1",
        execute_wrapped=True,
        expected_plan_hash=None,
        discard_unattributable_legacy=False,
        discard_unrecoverable_run_tombstone_history=False,
        json=True,
    )

    assert cmd_migrate(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "noop"
    assert payload["verification"]["reconciliation_status"] == "clean"
    assert not empty_runtime.exists()


def test_migrate_cli_masks_private_refs_and_accepts_public_mnemos_dir_reference(
    tmp_path, monkeypatch, capsys
):
    import core.config as config_module
    from core.cli.commands import migrate as migrate_command

    root = tmp_path / "private-path-marker"
    config = _Config(root)
    _seed_reconcilable_legacy_source(config, secret=_fixture_text("cli-path"))
    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)
    monkeypatch.setattr(config_module, "Config", lambda **_kwargs: config)
    registry = MigrationRegistry()
    args = SimpleNamespace(
        migrate_cmd="apply",
        migration_id="database.model_call_ledger.v1",
        execute_wrapped=True,
        expected_plan_hash=_reviewed_hash(registry, config),
        discard_unattributable_legacy=True,
        discard_unrecoverable_run_tombstone_history=False,
        json=True,
    )

    assert migrate_command.cmd_migrate(args) == 0
    applied_payload = json.loads(capsys.readouterr().out)
    assert str(root) not in json.dumps(applied_payload, ensure_ascii=False)
    assert applied_payload["backup_ref"].startswith("<MNEMOS_DIR>/backups/")
    assert applied_payload["rollback_ref"].startswith(
        "<MNEMOS_DIR>/backups/model-call-ledger/"
    )
    stored = MigrationLedger.from_config(config).find_by_id(applied_payload["ledger_id"])
    assert stored is not None
    assert str(root) in str(stored["rollback_ref"])

    rollback_args = SimpleNamespace(
        migrate_cmd="rollback",
        migration_id="database.model_call_ledger.v1",
        recovery_manifest=applied_payload["rollback_ref"],
        apply=False,
        execute_wrapped=False,
        json=True,
    )
    assert migrate_command.cmd_migrate(rollback_args) == 0
    rollback_payload = json.loads(capsys.readouterr().out)
    assert str(root) not in json.dumps(rollback_payload, ensure_ascii=False)
    assert rollback_payload["rollback_ref"].startswith(
        "<MNEMOS_DIR>/backups/model-call-ledger/"
    )

    restored = registry.rollback(
        config,
        "database.model_call_ledger.v1",
        recovery_manifest=rollback_payload["rollback_ref"],
        apply=True,
        execute_wrapped=True,
    )
    assert restored.status == "rolled_back", restored.error


def test_reconcile_os_error_is_typed_in_cli_and_migration_ledger(tmp_path, monkeypatch, capsys):
    """A backend exception message never crosses a COG-018 public boundary."""
    import core.config as config_module
    from core.cli.commands import migrate as migrate_command

    config = _Config(tmp_path)
    _seed_reconcilable_legacy_source(config, secret=_fixture_text("typed-os-error"))
    private_error = "-".join(("credential", "fixture", "opaque"))
    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)

    def raise_private_os_error(*_args, **_kwargs):
        raise OSError(private_error)

    monkeypatch.setattr(executor, "cleanup_source_database", raise_private_os_error)
    monkeypatch.setattr(config_module, "Config", lambda **_kwargs: config)
    registry = MigrationRegistry()
    args = SimpleNamespace(
        migrate_cmd="apply",
        migration_id="database.model_call_ledger.v1",
        execute_wrapped=True,
        expected_plan_hash=_reviewed_hash(registry, config),
        discard_unattributable_legacy=True,
        discard_unrecoverable_run_tombstone_history=False,
        json=True,
    )

    assert migrate_command.cmd_migrate(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "reconciliation_os_error"
    assert private_error not in json.dumps(payload, ensure_ascii=False)
    migration_bytes = (tmp_path / "migrations.db").read_bytes()
    assert private_error.encode("utf-8") not in migration_bytes


def test_sealed_v3_dry_restore_then_actual_restore_and_fresh_second_apply(
    tmp_path, monkeypatch
):
    secret = "RECOVERY_RAW_LITERAL_MUST_NOT_ESCAPE"
    config = _Config(tmp_path)
    source = _seed_reconcilable_legacy_source(config, secret=secret)
    source_journal = Path(str(source) + "-journal")
    source_journal.write_bytes(b"")
    registry, applied = _apply_sealed(config, monkeypatch)
    manifest = Path(applied.rollback_ref)
    manifest_bytes = manifest.read_bytes()
    sealed = json.loads(manifest_bytes)
    migration_ledger = tmp_path / "migrations.db"
    ledger_bytes = migration_ledger.read_bytes()

    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest.parent.stat().st_mode) == 0o700
    assert [entry["target_id"] for entry in sealed["targets"]] == [
        "canonical_ledger",
        "legacy_wiki_state",
    ]
    assert sealed["target_ids"] == ["canonical_ledger", "legacy_wiki_state"]
    assert sealed["targets"][0]["preimage"]["state"] == "present"
    assert sealed["targets"][1]["preimage"]["state"] == "present"
    assert all("postimage" not in entry for entry in sealed["targets"])
    journal = manifest.with_suffix(".progress.jsonl")
    journal_events = [json.loads(line)["event"] for line in journal.read_text().splitlines()]
    assert journal_events == ["apply_prepared", "apply_started", "apply_committed"]
    assert secret.encode("utf-8") not in manifest_bytes
    with sqlite3.connect(str(migration_ledger)) as conn:
        stored = conn.execute(
            "SELECT verification_json FROM migration_ledger WHERE ledger_id=?",
            (applied.ledger_id,),
        ).fetchone()[0]
    assert secret not in stored
    status = registry.status(config, read_only=True)
    status_row = next(
        row
        for row in status["recent_ledger"]
        if row["ledger_id"] == applied.ledger_id
    )
    assert status_row["backup_ref"] == "protected_model_call_ledger_backup"
    assert status_row["rollback_ref"] == "sealed_or_manual_recovery_manifest"
    assert str(manifest) not in json.dumps(status, ensure_ascii=False)
    assert not source.exists()
    assert not source_journal.exists()

    runtime_lock = config.database_dir / ".model-call-ledger-migration.lock"
    runtime_lock.unlink(missing_ok=True)
    assert not runtime_lock.exists()
    planned = registry.rollback(
        config,
        "database.model_call_ledger.v1",
        recovery_manifest=manifest,
    )
    assert planned.status == "planned", planned.error
    assert manifest.read_bytes() == manifest_bytes
    assert migration_ledger.read_bytes() == ledger_bytes
    assert not runtime_lock.exists()

    rolled_back = registry.rollback(
        config,
        "database.model_call_ledger.v1",
        recovery_manifest=manifest,
        apply=True,
        execute_wrapped=True,
    )
    assert rolled_back.status == "rolled_back", rolled_back.error
    assert source.exists()
    with sqlite3.connect(str(source)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM prompt_calls").fetchone()[0] == 1

    recent = MigrationLedger.from_config(config).recent(limit=10)
    assert any(row["ledger_id"] == applied.ledger_id and row["status"] == "applied" for row in recent)
    assert any(row["ledger_id"] == rolled_back.ledger_id and row["status"] == "rolled_back" for row in recent)

    # Restoring the SQLite backup gives the file a fresh physical generation,
    # so a second apply must obtain a *new* reviewed receipt rather than reuse
    # the first hash.  It must still reconcile normally and create a fresh v3
    # recovery bundle.
    fresh_hash = _reviewed_hash(registry, config)
    assert fresh_hash != applied.plan_hash
    second = registry.apply(
        config,
        "database.model_call_ledger.v1",
        execute_wrapped=True,
        expected_plan_hash=fresh_hash,
        discard_unattributable_legacy=True,
    )
    assert second.status == "applied", second.error
    assert second.rollback_ref != applied.rollback_ref


def test_recovery_selects_only_retired_sources_and_leaves_unrelated_sync_log_untouched(
    tmp_path, monkeypatch
):
    """A trigger in a zero-retired source is not a cleanup/recovery owner."""
    config = _Config(tmp_path)
    _seed_reconcilable_legacy_source(config, secret=_fixture_text("dynamic-targets"))
    sync_log = config.database_dir / "sync_log.db"
    sync_conn = sqlite3.connect(str(sync_log))
    try:
        sync_conn.execute("CREATE TABLE sync_events (id INTEGER PRIMARY KEY, body TEXT)")
        sync_conn.execute("CREATE TABLE sync_audit (event_id INTEGER NOT NULL)")
        sync_conn.execute(
            "CREATE TRIGGER sync_events_audit AFTER INSERT ON sync_events "
            "BEGIN INSERT INTO sync_audit(event_id) VALUES (new.id); END"
        )
        sync_conn.execute("INSERT INTO sync_events(body) VALUES ('fixture')")
        sync_conn.commit()
        sync_conn.close()
        sync_conn = None
        # This harmless sidecar belongs to an unrelated no-retired owner.  The
        # migration must neither select it nor rewrite it while inspecting the
        # source inventory.
        Path(str(sync_log) + "-shm").write_bytes(b"fixture-sync-sidecar")
        tracked = [sync_log, *(Path(str(sync_log) + suffix) for suffix in ("-wal", "-shm", "-journal"))]
        before = {str(path): path.read_bytes() for path in tracked if path.exists()}

        registry, applied = _apply_sealed(config, monkeypatch)
        sealed = json.loads(Path(applied.rollback_ref).read_text(encoding="utf-8"))

        assert sealed["target_ids"] == ["canonical_ledger", "legacy_wiki_state"]
        assert all("sync_log" not in str(entry) for entry in sealed["targets"])
        assert all("sync_log" not in path.name for path in Path(applied.backup_ref).iterdir())
        assert {str(path): path.read_bytes() for path in tracked if path.exists()} == before
        item = next(
            value
            for value in registry.plan(config).items
            if value.migration_id == "database.model_call_ledger.v1"
        )
        assert item.affected_paths == (
            "core/migrations/model_call_ledger_reconcile",
            "database:model_call_ledger.db",
        )
        restored = registry.rollback(
            config,
            "database.model_call_ledger.v1",
            recovery_manifest=Path(applied.rollback_ref),
            apply=True,
            execute_wrapped=True,
        )
        assert restored.status == "rolled_back", restored.error
        assert {str(path): path.read_bytes() for path in tracked if path.exists()} == before
    finally:
        if sync_conn is not None:
            sync_conn.close()


def test_recovery_rejects_intermediate_backup_or_runtime_symlink_component(
    tmp_path, monkeypatch
):
    config = _Config(tmp_path)
    _seed_reconcilable_legacy_source(config, secret="SYMLINK_TEST_LITERAL")
    registry, applied = _apply_sealed(config, monkeypatch)
    manifest = Path(applied.rollback_ref)
    canonical = config.database_dir / "model_call_ledger.db"
    before = canonical.read_bytes()

    # The manifest itself remains a regular private file, but its intermediate
    # `model-call-ledger` component becomes a symlink.  Final-only lstat would
    # miss this escape; v3 must fail before opening a target.
    ledger_root = manifest.parent.parent
    relocated = ledger_root.with_name("model-call-ledger-real")
    ledger_root.rename(relocated)
    os.symlink(relocated, ledger_root)
    blocked_backup = registry.rollback(
        config,
        "database.model_call_ledger.v1",
        recovery_manifest=manifest,
    )
    assert blocked_backup.status == "blocked"
    assert canonical.read_bytes() == before

    # Restore the original lexical path for the next independent target-path
    # check, then make only the runtime database directory a symlink.
    ledger_root.unlink()
    relocated.rename(ledger_root)
    real_database = config.database_dir.with_name("db-real")
    config.database_dir.rename(real_database)
    os.symlink(real_database, config.database_dir)
    blocked_runtime = registry.rollback(
        config,
        "database.model_call_ledger.v1",
        recovery_manifest=manifest,
    )
    assert blocked_runtime.status == "blocked"
    assert real_database.joinpath("model_call_ledger.db").read_bytes() == before


def test_restore_postimage_ignores_ephemeral_shm_but_blocks_wal_drift(tmp_path, monkeypatch):
    config = _Config(tmp_path)
    _seed_reconcilable_legacy_source(config, secret=_fixture_text("sidecar-drift"))
    registry, applied = _apply_sealed(config, monkeypatch)
    manifest = Path(applied.rollback_ref)
    canonical = config.database_dir / "model_call_ledger.db"
    shm = Path(str(canonical) + "-shm")
    wal = Path(str(canonical) + "-wal")
    shm.write_bytes(b"ephemeral-shm-fixture")

    shm_only = registry.rollback(
        config,
        "database.model_call_ledger.v1",
        recovery_manifest=manifest,
    )
    assert shm_only.status == "planned", shm_only.error
    wal.write_bytes(b"durable-wal-drift-fixture")
    wal_drift = registry.rollback(
        config,
        "database.model_call_ledger.v1",
        recovery_manifest=manifest,
    )
    assert wal_drift.status == "blocked"
    assert wal_drift.error == "recovery_postimage_drift_detected"


def test_restore_rejects_orphan_legacy_journal_without_writing_runtime(tmp_path, monkeypatch):
    config = _Config(tmp_path)
    source = _seed_reconcilable_legacy_source(config, secret=_fixture_text("orphan-journal"))
    registry, applied = _apply_sealed(config, monkeypatch)
    journal = Path(str(source) + "-journal")
    journal.write_bytes(b"fixture-orphan-journal")
    canonical = config.database_dir / "model_call_ledger.db"
    before = canonical.read_bytes()
    lock = config.database_dir / ".model-call-ledger-migration.lock"
    lock.unlink(missing_ok=True)

    blocked = registry.rollback(
        config,
        "database.model_call_ledger.v1",
        recovery_manifest=Path(applied.rollback_ref),
    )

    assert blocked.status == "blocked"
    assert blocked.error == "recovery_orphan_sidecar_present"
    assert canonical.read_bytes() == before
    assert journal.exists()
    assert not lock.exists()


def test_shared_runtime_lock_blocks_reconcile_apply_and_restore_apply(tmp_path, monkeypatch):
    import core.migrations.model_call_ledger_recovery as recovery

    config = _Config(tmp_path)
    source = _seed_reconcilable_legacy_source(config, secret=_fixture_text("shared-lock"))
    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)
    registry = MigrationRegistry()
    reviewed_hash = _reviewed_hash(registry, config)
    source_before = source.read_bytes()
    held = recovery.acquire_model_call_ledger_migration_lock(config)
    try:
        blocked_apply = registry.apply(
            config,
            "database.model_call_ledger.v1",
            execute_wrapped=True,
            expected_plan_hash=reviewed_hash,
            discard_unattributable_legacy=True,
        )
    finally:
        held.close()

    assert blocked_apply.status == "blocked"
    assert blocked_apply.error == "model_call_ledger_migration_lock_unavailable"
    assert source.read_bytes() == source_before
    assert not list((tmp_path / "backups").glob("**/*.db"))

    _, applied = _apply_sealed(config, monkeypatch)
    manifest = Path(applied.rollback_ref)
    canonical = config.database_dir / "model_call_ledger.db"
    postimage = _runtime_state_bytes(canonical)
    held = recovery.acquire_model_call_ledger_migration_lock(config)
    try:
        blocked_restore = registry.rollback(
            config,
            "database.model_call_ledger.v1",
            recovery_manifest=manifest,
            apply=True,
            execute_wrapped=True,
        )
    finally:
        held.close()

    assert blocked_restore.status == "blocked"
    assert blocked_restore.error == "model_call_ledger_migration_lock_unavailable"
    assert _runtime_state_bytes(canonical) == postimage
    assert not source.exists()


def test_corrupt_recovery_ledger_binding_fails_closed_without_echoing_payload(
    tmp_path, monkeypatch
):
    config = _Config(tmp_path)
    _seed_reconcilable_legacy_source(config, secret=_fixture_text("corrupt-ledger"))
    registry, applied = _apply_sealed(config, monkeypatch)
    manifest = Path(applied.rollback_ref)
    canonical = config.database_dir / "model_call_ledger.db"
    before = canonical.read_bytes()
    corrupt_value = "fixture-malformed-ledger-value"
    with sqlite3.connect(str(tmp_path / "migrations.db")) as conn:
        conn.execute(
            "UPDATE migration_ledger SET verification_json=? WHERE ledger_id=?",
            ("{" + corrupt_value, applied.ledger_id),
        )

    blocked = registry.rollback(
        config,
        "database.model_call_ledger.v1",
        recovery_manifest=manifest,
    )

    assert blocked.status == "blocked"
    assert blocked.error == "migration_ledger_recovery_binding_invalid"
    assert corrupt_value not in json.dumps(blocked.as_dict(), ensure_ascii=False)
    assert canonical.read_bytes() == before
    status = registry.status(config, read_only=True)
    assert corrupt_value not in json.dumps(status, ensure_ascii=False)


def test_legacy_recovery_manifest_is_not_automatically_restorable(tmp_path, monkeypatch):
    config = _Config(tmp_path)
    _seed_reconcilable_legacy_source(config, secret=_fixture_text("legacy-manifest"))
    registry, applied = _apply_sealed(config, monkeypatch)
    legacy = Path(applied.rollback_ref).parent / "model-call-ledger-reconcile-recovery-fixture.json"
    legacy.write_text('{"schema_version":"mnemos.model_call_ledger_recovery.v2"}', encoding="utf-8")
    os.chmod(legacy, 0o600)
    with sqlite3.connect(str(tmp_path / "migrations.db")) as conn:
        conn.execute(
            "UPDATE migration_ledger SET rollback_ref=? WHERE ledger_id=?",
            (str(legacy), applied.ledger_id),
        )
    canonical = config.database_dir / "model_call_ledger.db"
    before = canonical.read_bytes()
    lock = config.database_dir / ".model-call-ledger-migration.lock"
    lock.unlink(missing_ok=True)

    blocked = registry.rollback(
        config,
        "database.model_call_ledger.v1",
        recovery_manifest=legacy,
    )

    assert blocked.status == "blocked"
    assert blocked.error == "legacy_recovery_manifest_not_automatically_restorable"
    assert canonical.read_bytes() == before
    assert not lock.exists()


def _runtime_state_bytes(path: Path) -> dict[str, bytes]:
    return {
        str(candidate): candidate.read_bytes()
        for candidate in (
            path,
            Path(str(path) + "-wal"),
            Path(str(path) + "-shm"),
            Path(str(path) + "-journal"),
        )
        if candidate.exists()
    }


def test_restore_failure_after_replace_compensates_and_allows_deterministic_retry(
    tmp_path, monkeypatch
):
    """An intented target is reversed even if failure occurs after replace."""
    import core.migrations.model_call_ledger_recovery as recovery
    import core.migrations.model_call_ledger_recovery_restore as restore

    config = _Config(tmp_path)
    source = _seed_reconcilable_legacy_source(config, secret=_fixture_text("compensation"))
    registry, applied = _apply_sealed(config, monkeypatch)
    manifest = Path(applied.rollback_ref)
    binding = MigrationLedger.from_config(config).find_recovery_by_rollback_ref(
        "database.model_call_ledger.v1", str(manifest)
    )
    assert binding is not None
    canonical = config.database_dir / "model_call_ledger.db"
    postimage = _runtime_state_bytes(canonical)
    original_remove_sidecars = restore._remove_target_sidecars
    injected = {"raised": False}

    def fail_once_after_replace(target: Path) -> None:
        original_remove_sidecars(target)
        if target == canonical and not injected["raised"]:
            injected["raised"] = True
            raise recovery.ModelCallLedgerRecoveryError("recovery_injected_after_replace")

    monkeypatch.setattr(restore, "_remove_target_sidecars", fail_once_after_replace)
    failed = recovery.restore_model_call_ledger(
        config,
        recovery_manifest=manifest,
        ledger_binding=binding,
        apply=True,
    )

    assert failed["status"] == "blocked"
    assert failed["partial_recovery"] is False
    assert _runtime_state_bytes(canonical) == postimage
    assert not source.exists()
    events = [
        json.loads(line)
        for line in manifest.with_suffix(".progress.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["event"] == "restore_failed"
    assert events[-1]["compensation_ok"] is True

    planned = recovery.plan_model_call_ledger_restore(
        config,
        recovery_manifest=manifest,
        ledger_binding=binding,
    )
    assert planned["status"] == "planned", planned
    assert planned["retry_after_compensation"] is True

    monkeypatch.setattr(restore, "_remove_target_sidecars", original_remove_sidecars)
    retried = registry.rollback(
        config,
        "database.model_call_ledger.v1",
        recovery_manifest=manifest,
        apply=True,
        execute_wrapped=True,
    )
    assert retried.status == "rolled_back", retried.error
    assert source.exists()


def test_reverse_backup_preserves_wal_components_across_injected_sidecar_failure(
    tmp_path, monkeypatch
):
    """WAL is durable compensation data; SHM is normalized coordination state."""
    import core.migrations.model_call_ledger_recovery as recovery
    import core.migrations.model_call_ledger_recovery_evidence as evidence
    import core.migrations.model_call_ledger_recovery_restore as restore

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    target = runtime / "ledger.db"
    connection = sqlite3.connect(str(target))
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE payload (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO payload(value) VALUES ('fixture')")
        connection.commit()
        assert Path(str(target) + "-wal").exists()
        expected = _runtime_state_bytes(target)
        root = tmp_path / "private-reverse"
        root.mkdir(mode=0o700)
        reverse_id = "reverse-fixture"
        reverse_dir = evidence._create_private_directory(root, reverse_id)
        entry = restore._capture_reverse_target(
            "canonical_ledger", target, reverse_dir, reverse_id
        )
    finally:
        connection.close()

    restore._remove_runtime_target(target)
    original_copy = restore._copy_private_backup_to_target
    injected = {"raised": False}

    def fail_after_main_copy(source: Path, destination: Path, expected_sha256: str) -> None:
        original_copy(source, destination, expected_sha256)
        if destination == target and not injected["raised"]:
            injected["raised"] = True
            raise recovery.ModelCallLedgerRecoveryError("recovery_injected_after_main_copy")

    monkeypatch.setattr(restore, "_copy_private_backup_to_target", fail_after_main_copy)
    with pytest.raises(recovery.ModelCallLedgerRecoveryError):
        restore._restore_reverse_target(target, entry, root)
    monkeypatch.setattr(restore, "_copy_private_backup_to_target", original_copy)
    restore._restore_reverse_target(target, entry, root)

    # Main and WAL bytes must be exactly the captured postimage; SQLite may
    # rebuild SHM during the verification read, so it is intentionally outside
    # the durable state hash used by retry planning.
    restored = _runtime_state_bytes(target)
    assert restored[str(target)] == expected[str(target)]
    assert restored[str(Path(str(target) + "-wal"))] == expected[
        str(Path(str(target) + "-wal"))
    ]
    assert evidence._state_matches(entry["target_state"], evidence._target_identity(target))


def _blocked_bundle(recovery, _config, **_kwargs):
    return {
        "schema_version": recovery.RECOVERY_SCHEMA_VERSION,
        "status": "blocked",
        "ok": False,
        "error": "injected_recovery_phase_failure",
    }


@pytest.mark.parametrize(
    ("phase", "source_should_remain"),
    [("prepare", True), ("started", True)],
)
def test_pre_mutation_recovery_phase_failure_blocks_before_ledger_write(
    tmp_path, monkeypatch, phase, source_should_remain
):
    import core.migrations.model_call_ledger_recovery as recovery

    config = _Config(tmp_path)
    source = _seed_reconcilable_legacy_source(config, secret=_fixture_text("phase-check"))
    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)
    monkeypatch.setattr(
        recovery,
        "prepare_sealed_recovery_bundle" if phase == "prepare" else "start_sealed_recovery_apply",
        lambda _config, **kwargs: _blocked_bundle(recovery, _config, **kwargs),
    )
    registry = MigrationRegistry()
    record = registry.apply(
        config,
        "database.model_call_ledger.v1",
        execute_wrapped=True,
        expected_plan_hash=_reviewed_hash(registry, config),
        discard_unattributable_legacy=True,
    )

    assert record.status == ("blocked" if phase == "prepare" else "failed")
    assert record.error == "sealed_recovery_%s_failed" % phase
    assert source.exists() is source_should_remain
    if phase == "prepare":
        assert record.rollback_ref == ""
        assert record.backup_ref == ""
        assert MigrationLedger.from_config(config).find_by_id(record.ledger_id) is None
        assert not list(
            (tmp_path / "backups").glob("model-call-ledger-reconcile-recovery-*.json")
        )
    else:
        assert record.backup_ref
        stored = MigrationLedger.from_config(config).find_by_id(record.ledger_id)
        assert stored is not None and stored["status"] == "failed"
        manifest = Path(record.rollback_ref)
        assert manifest.name.startswith("model-call-ledger-recovery-v3-")
        journal_events = [
            json.loads(line)["event"]
            for line in manifest.with_suffix(".progress.jsonl").read_text().splitlines()
        ]
        assert journal_events == ["apply_prepared", "apply_failed"]
        planned = registry.rollback(
            config,
            "database.model_call_ledger.v1",
            recovery_manifest=manifest,
        )
        assert planned.status == "planned", planned.error
        assert planned.verification["source_ledger_id"] == record.ledger_id


def test_crash_after_mutation_start_keeps_durable_v3_restore_path(tmp_path, monkeypatch):
    """A process loss before final postimage sealing is recoverable, not stranded."""
    import core.migrations.model_call_ledger_recovery as recovery

    config = _Config(tmp_path)
    source = _seed_reconcilable_legacy_source(config, secret=_fixture_text("crash-window"))
    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)

    def simulate_process_loss(*_args, **_kwargs):
        raise SystemExit("injected_process_loss")

    monkeypatch.setattr(recovery, "commit_sealed_recovery_bundle", simulate_process_loss)
    registry = MigrationRegistry()
    with pytest.raises(SystemExit, match="injected_process_loss"):
        registry.apply(
            config,
            "database.model_call_ledger.v1",
            execute_wrapped=True,
            expected_plan_hash=_reviewed_hash(registry, config),
            discard_unattributable_legacy=True,
        )

    attempt = next(
        row
        for row in MigrationLedger.from_config(config).recent(limit=10)
        if row["status"] == "applying"
    )
    manifest = Path(attempt["rollback_ref"])
    journal_events = [
        json.loads(line)["event"]
        for line in manifest.with_suffix(".progress.jsonl").read_text().splitlines()
    ]
    assert journal_events == ["apply_prepared", "apply_started"]
    assert not source.exists()

    planned = registry.rollback(
        config,
        "database.model_call_ledger.v1",
        recovery_manifest=manifest,
    )
    assert planned.status == "planned", planned.error
    assert planned.verification["source_ledger_id"] == attempt["ledger_id"]
    recovered = registry.rollback(
        config,
        "database.model_call_ledger.v1",
        recovery_manifest=manifest,
        apply=True,
        execute_wrapped=True,
    )
    assert recovered.status == "rolled_back", recovered.error
    assert source.exists()
