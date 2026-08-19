import hashlib
import os
from pathlib import Path
import sqlite3

import pytest

from core.ops.audit_run import AuditExecutionEnvironment, verify_os_write_denied


def test_isolated_projection_dependencies_stay_inside_owned_root(tmp_path):
    formal = tmp_path / "formal"
    run = AuditExecutionEnvironment.isolated(
        tmp_path / "run",
        formal_targets=(formal,),
        formal_directory_targets=(formal,),
    )

    lifecycle = run.create_projection_lifecycle()

    assert lifecycle.ledger.db_path == run.database_dir / "wiki_projection.db"
    assert lifecycle.event_bus.projection_db_path == lifecycle.ledger.db_path
    assert lifecycle.event_bus._db_path == run.database_dir / "events.db"
    run.close()
    assert run.report()["outside_write_count"] == 0
    assert not formal.exists()


def test_sandbox_readonly_missing_target_stays_uninitialized(tmp_path):
    missing = tmp_path / "not-initialized"

    with AuditExecutionEnvironment.sandbox_readonly(
        directory_targets=(missing,),
    ) as audit:
        assert audit.target_inventory()[0]["classification"] == "uninitialized"

    assert not missing.exists()


def test_sandbox_readonly_fails_closed_on_mutation(tmp_path):
    formal = tmp_path / "formal"
    formal.mkdir()
    audit = AuditExecutionEnvironment.sandbox_readonly(
        directory_targets=(formal,),
    )
    (formal / "unexpected.db-wal").write_text("mutation", encoding="utf-8")

    with pytest.raises(RuntimeError, match="formal state diff"):
        audit.close()


def test_directory_signature_detects_same_size_content_rewrite(tmp_path):
    formal = tmp_path / "formal"
    formal.mkdir()
    page = formal / "page.md"
    page.write_text("AAAA", encoding="utf-8")
    original = page.stat()
    audit = AuditExecutionEnvironment.sandbox_readonly(
        directory_targets=(formal,),
    )

    page.write_text("BBBB", encoding="utf-8")
    page.touch()
    os.utime(page, ns=(original.st_atime_ns, original.st_mtime_ns))

    with pytest.raises(RuntimeError, match="formal state diff"):
        audit.close()


def test_production_readonly_requires_os_guard_outside_hermetic_test(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("MNEMOS_AUDIT_OS_WRITE_DENY", raising=False)
    monkeypatch.delenv("MNEMOS_RUN_PROFILE", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with pytest.raises(RuntimeError, match="requires a verified OS write-deny probe"):
        AuditExecutionEnvironment.production_readonly(
            directory_targets=(tmp_path,),
        )


def test_production_readonly_has_no_test_only_bypass(
    tmp_path,
):
    with pytest.raises(TypeError, match="test_only_allow_unenforced"):
        AuditExecutionEnvironment.production_readonly(
            directory_targets=(tmp_path,),
            test_only_allow_unenforced=True,
        )


def test_forged_pytest_marker_cannot_enable_sandbox_readonly(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "forged")
    monkeypatch.delenv("MNEMOS_TEST_RUN", raising=False)
    monkeypatch.delenv("MNEMOS_RUN_PROFILE", raising=False)
    monkeypatch.delenv("MNEMOS_RUN_ROOT", raising=False)

    with pytest.raises(RuntimeError, match="valid hermetic test environment"):
        AuditExecutionEnvironment.sandbox_readonly(
            directory_targets=(tmp_path,),
        )


def test_forged_hermetic_manifest_cannot_enable_sandbox_readonly(
    tmp_path,
    monkeypatch,
):
    run_root = tmp_path / "forged-run"
    manifest = run_root / "artifacts" / "environment-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "forged")
    monkeypatch.setenv("MNEMOS_TEST_RUN", "1")
    monkeypatch.setenv("MNEMOS_RUN_PROFILE", "isolated")
    monkeypatch.setenv("MNEMOS_RUN_ROOT", str(run_root))
    monkeypatch.setenv("MNEMOS_RUN_ENVIRONMENT_MANIFEST", str(manifest))
    monkeypatch.setenv("MNEMOS_RUN_ENVIRONMENT_HASH", "forged")

    with pytest.raises(RuntimeError, match="valid hermetic test environment"):
        AuditExecutionEnvironment.sandbox_readonly(
            directory_targets=(tmp_path,),
        )


def test_valid_hermetic_manifest_cannot_bypass_production_os_guard(
    tmp_path,
    monkeypatch,
):
    from core.ops.hermetic_run import HermeticRunEnvironment

    run = HermeticRunEnvironment.create(
        tmp_path / "run",
        profile="isolated",
        base_environment={"PATH": os.environ.get("PATH", "")},
    )
    for key, value in run.environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "forged")
    monkeypatch.setenv("MNEMOS_TEST_RUN", "1")

    with pytest.raises(RuntimeError, match="verified OS write-deny probe"):
        AuditExecutionEnvironment.production_readonly(
            directory_targets=(run.root / "formal",),
        )


def test_sandbox_readonly_rejects_target_outside_hermetic_root():
    run_root = Path(os.environ["MNEMOS_RUN_ROOT"]).resolve()

    with pytest.raises(RuntimeError, match="escapes hermetic run root"):
        AuditExecutionEnvironment.sandbox_readonly(
            directory_targets=(run_root.parent / "formal-production-path",),
        )


def test_self_declared_hermetic_root_cannot_rebind_sandbox(
    tmp_path,
    monkeypatch,
):
    from core.ops.hermetic_run import HermeticRunEnvironment

    self_declared = HermeticRunEnvironment.create(
        tmp_path / "self-declared-run",
        profile="isolated",
        base_environment={"PATH": os.environ.get("PATH", "")},
    )
    for key, value in self_declared.environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "forged")
    monkeypatch.setenv("MNEMOS_TEST_RUN", "1")

    with pytest.raises(RuntimeError, match="valid hermetic test environment"):
        AuditExecutionEnvironment.sandbox_readonly(
            directory_targets=(self_declared.root / "formal",),
        )


def test_forged_guard_environment_does_not_prove_os_enforcement(
    tmp_path,
    monkeypatch,
):
    probe = tmp_path / "probe.py"
    probe.write_text("pass\n", encoding="utf-8")
    probe_stat = probe.stat()
    monkeypatch.setenv("MNEMOS_AUDIT_OS_WRITE_DENY", "sandbox-exec-v1")
    monkeypatch.delenv("MNEMOS_RUN_PROFILE", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with pytest.raises(RuntimeError, match="OS write guard is not enforced"):
        AuditExecutionEnvironment.production_readonly(
            targets=(probe,),
            write_deny_probe=probe,
            write_deny_identity={
                "device": probe_stat.st_dev,
                "inode": probe_stat.st_ino,
                "sha256": hashlib.sha256(probe.read_bytes()).hexdigest(),
            },
        )


def test_readonly_file_permissions_do_not_impersonate_os_sandbox(tmp_path):
    probe = tmp_path / "readonly-probe"
    probe.write_text("frozen", encoding="utf-8")
    probe.chmod(0o444)
    probe_stat = probe.stat()

    with pytest.raises(RuntimeError, match="writable mode mismatch"):
        verify_os_write_denied(
            probe,
            expected_device=probe_stat.st_dev,
            expected_inode=probe_stat.st_ino,
            expected_sha256=hashlib.sha256(probe.read_bytes()).hexdigest(),
        )


def test_os_write_deny_probe_fails_typed_when_owner_identity_is_unavailable(
    tmp_path,
    monkeypatch,
):
    probe = tmp_path / "probe"
    probe.write_text("frozen", encoding="utf-8")
    probe_stat = probe.stat()
    monkeypatch.delattr(os, "geteuid", raising=False)

    with pytest.raises(RuntimeError, match="owner identity is unavailable"):
        verify_os_write_denied(
            probe,
            expected_device=probe_stat.st_dev,
            expected_inode=probe_stat.st_ino,
            expected_sha256=hashlib.sha256(probe.read_bytes()).hexdigest(),
        )


def test_production_sqlite_reader_is_mode_ro(tmp_path):
    database = tmp_path / "formal.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sample(value TEXT)")
        conn.execute("INSERT INTO sample VALUES ('frozen')")
    audit = AuditExecutionEnvironment.sandbox_readonly(
        targets=(database,),
    )

    with audit.open_sqlite_readonly(database) as conn:
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == "frozen"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO sample VALUES ('mutation')")

    audit.close()


def test_multi_database_epoch_requires_complete_db_wal_shm_targets(tmp_path):
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    for database in (first, second):
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE sample(value TEXT)")

    with pytest.raises(ValueError, match="DB/WAL/SHM target group"):
        AuditExecutionEnvironment.sandbox_readonly(
            targets=(first, second),
            directory_targets=(tmp_path,),
            required_sqlite_databases=(first, second),
            evidence_snapshot_root=tmp_path / "epoch",
            writer_inactive=lambda _root: True,
        )


def test_multi_database_epoch_reads_hash_bound_immutable_snapshots(tmp_path):
    formal = tmp_path / "formal"
    formal.mkdir()
    first = formal / "first.db"
    second = formal / "second.db"
    for database, value in ((first, "first"), (second, "second")):
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE sample(value TEXT)")
            conn.execute("INSERT INTO sample VALUES (?)", (value,))
    targets = tuple(
        path
        for database in (first, second)
        for path in (
            database,
            database.with_name(database.name + "-wal"),
            database.with_name(database.name + "-shm"),
        )
    )

    audit = AuditExecutionEnvironment.sandbox_readonly(
        targets=targets,
        directory_targets=(formal,),
        required_sqlite_databases=(first, second),
        evidence_snapshot_root=tmp_path / "audit-owned" / "epoch",
        writer_inactive=lambda _root: True,
    )

    with audit.open_sqlite_readonly(first) as conn:
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == "first"
    with audit.open_sqlite_readonly(second) as conn:
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == "second"
    epoch = audit.report()["evidence_epoch"]
    assert epoch["schema_version"] == "mnemos.audit_evidence_epoch.v1"
    assert epoch["database_count"] == 2
    assert epoch["writer_quiescence_checks"] == 2
    assert epoch["source_inventory_hash"].startswith("sha256:")
    assert {item["integrity_check"] for item in epoch["database_snapshots"]} == {"ok"}
    audit.close()


def test_multi_database_epoch_rejects_active_runtime_writer(tmp_path):
    formal = tmp_path / "formal"
    formal.mkdir()
    databases = (formal / "first.db", formal / "second.db")
    for database in databases:
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE sample(value TEXT)")
    targets = tuple(
        path
        for database in databases
        for path in (
            database,
            database.with_name(database.name + "-wal"),
            database.with_name(database.name + "-shm"),
        )
    )

    with pytest.raises(RuntimeError, match="inactive runtime writers"):
        AuditExecutionEnvironment.sandbox_readonly(
            targets=targets,
            directory_targets=(formal,),
            required_sqlite_databases=databases,
            evidence_snapshot_root=tmp_path / "audit-owned" / "epoch",
            writer_inactive=lambda _root: False,
        )


def test_multi_database_epoch_rejects_writer_start_during_capture(tmp_path):
    formal = tmp_path / "formal"
    formal.mkdir()
    databases = (formal / "first.db", formal / "second.db")
    for database in databases:
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE sample(value TEXT)")
    targets = tuple(
        path
        for database in databases
        for path in (
            database,
            database.with_name(database.name + "-wal"),
            database.with_name(database.name + "-shm"),
        )
    )
    writer_states = iter((True, False))
    snapshot_root = tmp_path / "audit-owned" / "epoch"

    with pytest.raises(RuntimeError, match="became active during evidence epoch"):
        AuditExecutionEnvironment.sandbox_readonly(
            targets=targets,
            directory_targets=(formal,),
            required_sqlite_databases=databases,
            evidence_snapshot_root=snapshot_root,
            writer_inactive=lambda _root: next(writer_states),
        )
    assert not snapshot_root.exists() or not any(snapshot_root.iterdir())


def test_evidence_epoch_rejects_snapshot_content_or_inode_substitution(tmp_path):
    formal = tmp_path / "formal"
    formal.mkdir()
    databases = (formal / "first.db", formal / "second.db")
    for database in databases:
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE sample(value TEXT)")
    targets = tuple(
        path
        for database in databases
        for path in (
            database,
            database.with_name(database.name + "-wal"),
            database.with_name(database.name + "-shm"),
        )
    )
    audit = AuditExecutionEnvironment.sandbox_readonly(
        targets=targets,
        directory_targets=(formal,),
        required_sqlite_databases=databases,
        evidence_snapshot_root=tmp_path / "audit-owned" / "epoch",
        writer_inactive=lambda _root: True,
    )
    snapshot = audit.evidence_epoch.snapshot_for(databases[0])
    assert snapshot is not None
    original = snapshot.read_bytes()
    snapshot.write_bytes(bytes([original[0] ^ 0xFF]) + original[1:])
    with pytest.raises(RuntimeError, match="snapshot hash changed"):
        audit.open_sqlite_readonly(databases[0])
    snapshot.write_bytes(original)

    replacement = tmp_path / "replacement.db"
    replacement.write_bytes(original)
    snapshot.unlink()
    snapshot.symlink_to(replacement)

    with pytest.raises(RuntimeError, match="snapshot identity changed"):
        audit.open_sqlite_readonly(databases[0])


def test_close_still_reports_formal_diff_when_dependency_close_fails(tmp_path):
    class FailingBus:
        def close(self):
            raise RuntimeError("injected close failure")

    formal = tmp_path / "formal"
    formal.mkdir()
    audit = AuditExecutionEnvironment.sandbox_readonly(
        directory_targets=(formal,),
    )
    audit._event_buses.append(FailingBus())
    (formal / "unexpected.db").write_text("mutation", encoding="utf-8")

    with pytest.raises(RuntimeError) as raised:
        audit.close()

    assert "EventBus close failed: injected close failure" in str(raised.value)
    assert "formal state diff:" in str(raised.value)
