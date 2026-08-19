"""Independent gate coverage for COG-049."""

import ast
from contextlib import contextmanager
import json
import hashlib
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from scripts.audit_cognitive_calibration_lineage import (
    _run_cli_with_os_write_deny,
    _synthetic_report,
    build_report,
)
from core.event_bus_contract import Event
from core.cognitive.observation_store import ObservationStore
from core.mnemos_bus import EventBus
from core.ops.audit_run import AuditRuntimeConfig
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateStore
from core.wiki_projection_lifecycle import WikiProjectionLedger

_DIAGNOSTIC_ENV_KEYS = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "TZ",
        "TERM",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "PYTHONNOUSERSITE",
    }
)


def _diagnostic_environment(**overrides):
    environment = {key: value for key, value in os.environ.items() if key in _DIAGNOSTIC_ENV_KEYS}
    environment.update(overrides)
    return environment


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def _active_mnemos_runtime():
    """Expose one unambiguous same-user writer identity to the production CLI."""

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(30)",
            "mnemos_daemon.py",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    try:
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_calibration_lineage_audit_synthetic_corpus_is_zero_gap(tmp_path):
    report = build_report(
        tmp_path / "database",
        tmp_path / "wiki",
        test_only_sandbox_readonly=True,
    )

    assert report["ok"] is True
    assert report["failures"] == []
    assert report["static_contract"]["schema_owner_count"] == 1
    assert report["static_contract"]["unverified_binding_callsite_count"] == 0
    assert report["static_contract"]["legacy_public_binder_count"] == 0
    assert report["synthetic"]["derived_source_double_count"] == 0
    assert report["synthetic"]["same_input_spec_revision_equal"] is True
    assert report["synthetic"]["projection_hash_equal"] is True
    assert all(value == 0 for value in report["live"]["metrics"].values())
    assert report["audit_scope"] == "sandbox_readonly_test_fixture"
    assert report["production_evidence"] is False
    assert report["audit_execution"]["mode"] == "sandbox_readonly"
    assert report["audit_execution"]["os_write_guard"] == "hermetic-sandbox-confinement"
    assert report["audit_execution"]["outside_write_count"] == 0
    live_targets = [
        target
        for target in report["audit_execution"]["target_inventory"]
        if str(target["path"]).startswith(str(tmp_path))
    ]
    assert live_targets
    assert {target["classification"] for target in live_targets} == {"uninitialized"}


def test_calibration_lineage_audit_binds_cross_database_evidence_epoch(tmp_path):
    report = build_report(
        tmp_path / "database",
        tmp_path / "wiki",
        test_only_sandbox_readonly=True,
    )

    epoch = report["audit_execution"]["evidence_epoch"]
    assert epoch["schema_version"] == "mnemos.audit_evidence_epoch.v1"
    assert epoch["database_count"] == 2
    assert epoch["writer_quiescence_checks"] == 2
    assert epoch["source_inventory_hash"].startswith("sha256:")


def test_calibration_lineage_audit_types_empty_databases_as_uninitialized(tmp_path):
    database_dir = tmp_path / "database"
    database_dir.mkdir()
    for name in ("observations.db", "producer_consumer_ledger.db"):
        with sqlite3.connect(database_dir / name):
            pass

    report = build_report(
        database_dir,
        tmp_path / "wiki",
        test_only_sandbox_readonly=True,
    )

    assert report["live"]["observation_schema"] == {
        "classification": "uninitialized",
        "ok": True,
    }
    assert report["live"]["cognitive_state_schema"] == {
        "classification": "uninitialized",
        "ok": True,
    }


def test_calibration_lineage_audit_never_reopens_live_state_after_epoch_capture(
    tmp_path,
    monkeypatch,
):
    database_dir = tmp_path / "database"
    database_dir.mkdir()
    state_path = database_dir / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_path)
    original_connect = CognitiveStateStore._connect

    def reject_live_state_reopen(self, *, read_only=False):
        if self.db_path.resolve() == state_path.resolve():
            raise AssertionError("live cognitive state reopened outside evidence epoch")
        return original_connect(self, read_only=read_only)

    monkeypatch.setattr(CognitiveStateStore, "_connect", reject_live_state_reopen)

    report = build_report(
        database_dir,
        tmp_path / "wiki",
        test_only_sandbox_readonly=True,
    )

    assert report["ok"] is True
    assert report["live"]["cognitive_state_schema"]["ok"] is True
    assert report["live"]["current_calibration_record_count"] == 0


def test_calibration_lineage_audit_rejects_partial_cognitive_state_schema(tmp_path):
    database_dir = tmp_path / "database"
    database_dir.mkdir()
    with sqlite3.connect(database_dir / "producer_consumer_ledger.db") as conn:
        conn.execute("CREATE TABLE cognitive_state_revisions(id TEXT PRIMARY KEY)")

    report = build_report(
        database_dir,
        tmp_path / "wiki",
        test_only_sandbox_readonly=True,
    )

    assert report["ok"] is False
    assert "cognitive_state_schema" in report["failures"]
    assert report["live"]["cognitive_state_schema"]["classification"] == "unknown_or_partial"
    assert report["live"]["cognitive_state_schema"]["ok"] is False


def test_calibration_lineage_audit_types_unrelated_state_tables_as_partial(tmp_path):
    database_dir = tmp_path / "database"
    database_dir.mkdir()
    with sqlite3.connect(database_dir / "producer_consumer_ledger.db") as conn:
        conn.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY)")

    report = build_report(
        database_dir,
        tmp_path / "wiki",
        test_only_sandbox_readonly=True,
    )

    assert report["ok"] is False
    assert "cognitive_state_schema" in report["failures"]
    assert report["live"]["cognitive_state_schema"] == {
        "classification": "unknown_or_partial",
        "migration_required": True,
        "errors": ["cognitive state database contains unrelated tables without canonical anchors"],
        "ok": False,
    }


def test_calibration_lineage_audit_types_unrelated_observation_tables_as_partial(
    tmp_path,
):
    database_dir = tmp_path / "database"
    database_dir.mkdir()
    with sqlite3.connect(database_dir / "observations.db") as conn:
        conn.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY)")

    report = build_report(
        database_dir,
        tmp_path / "wiki",
        test_only_sandbox_readonly=True,
    )

    assert report["ok"] is False
    assert "observation_schema" in report["failures"]
    assert report["live"]["observation_schema"] == {
        "classification": "unknown_or_partial",
        "migration_required": True,
        "errors": ["observation database contains unrelated tables without canonical anchors"],
        "ok": False,
    }


def test_synthetic_audit_closes_event_bus_when_projection_fails(monkeypatch):
    closed = []
    original_close = EventBus.close

    def recording_close(self):
        closed.append(self.projection_db_path)
        return original_close(self)

    monkeypatch.setattr(EventBus, "close", recording_close)
    monkeypatch.setattr(
        "core.cognitive.wiki_exporter.WikiExporter.export_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected projection failure")),
    )

    with pytest.raises(RuntimeError, match="injected projection failure"):
        _synthetic_report()

    assert len(closed) == 1


def test_calibration_lineage_static_cli_does_not_fall_back_to_formal_state(tmp_path):
    formal_home = tmp_path / "formal-home"
    formal_database = formal_home / ".mnemos"
    formal_wiki = tmp_path / "formal-wiki"
    live_database = tmp_path / "live-read-database"
    live_wiki = tmp_path / "live-read-wiki"
    environment = _diagnostic_environment(
        HOME=str(formal_home),
        USERPROFILE=str(formal_home),
        MNEMOS_DIR=str(formal_database),
        MNEMOS_DATABASE_DIR=str(formal_database),
        MNEMOS_WIKI_DIR=str(formal_wiki),
    )

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/audit_cognitive_calibration_lineage.py",
            "--database-dir",
            str(live_database),
            "--wiki-dir",
            str(live_wiki),
            "--static-only",
            "--strict",
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["ok"] is True
    assert report["audit_scope"] == "isolated_static_contract"
    assert report["production_evidence"] is False
    assert report["synthetic"]["audit_execution"]["outside_write_count"] == 0
    assert report["synthetic"]["audit_execution"]["target_inventory"]
    assert report["audit_execution"]["mode"] == "isolated"
    assert report["audit_execution"]["outside_write_count"] == 0
    assert report["audit_execution"]["target_inventory"]
    assert not formal_database.exists()
    assert not formal_wiki.exists()


def test_calibration_lineage_static_audit_rejects_formal_state_write(
    tmp_path,
    monkeypatch,
):
    formal_home = tmp_path / "formal-home"
    formal_database = formal_home / ".mnemos"
    leaked = formal_database / "distillation_state.db"
    original_save = ObservationStore.save

    monkeypatch.setenv("HOME", str(formal_home))
    monkeypatch.setenv("USERPROFILE", str(formal_home))
    monkeypatch.setenv("MNEMOS_DIR", str(formal_database))
    monkeypatch.setenv("MNEMOS_DATABASE_DIR", str(formal_database))
    monkeypatch.setenv("MNEMOS_WIKI_DIR", str(tmp_path / "formal-wiki"))

    def leaking_save(self, observation):
        leaked.parent.mkdir(parents=True, exist_ok=True)
        leaked.write_text("unexpected", encoding="utf-8")
        return original_save(self, observation)

    monkeypatch.setattr(ObservationStore, "save", leaking_save)

    with pytest.raises(RuntimeError, match="formal state diff"):
        _synthetic_report()


def test_cross_platform_ci_uses_static_calibration_contract() -> None:
    workflow = (Path(__file__).resolve().parents[3] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )

    assert (
        "python scripts/audit_cognitive_calibration_lineage.py " "--static-only --strict --json"
    ) in workflow


def test_gate_execution_suite_does_not_module_skip_pure_validators() -> None:
    source = (
        Path(__file__).resolve().parents[3] / "tests/unit/ops/test_gate_execution.py"
    ).read_text(encoding="utf-8")

    tree = ast.parse(source)
    module_assignments = {
        target.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    decorated = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Name) and decorator.id == "requires_macos_sandbox"
            for decorator in node.decorator_list
        )
    }
    platform_neutral = {
        "test_non_behavioral_mutation_is_not_credited_as_killed",
        "test_outside_write_is_an_execution_guard_not_a_structural_mutation",
        "test_candidate_hash_drift_fails_before_execution",
        "test_exit_one_relabelled_as_semantic_mutation_is_not_credited",
        "test_pytest_selector_requires_an_existing_exact_node",
        "test_pytest_selector_rejects_explicit_plugins",
    }

    assert "pytestmark" not in module_assignments
    assert "requires_macos_sandbox" in module_assignments
    assert platform_neutral.isdisjoint(decorated)


def test_calibration_lineage_cli_rejects_forged_os_guard_marker(tmp_path):
    environment = _diagnostic_environment(
        HOME=str(tmp_path / "formal-home"),
        USERPROFILE=str(tmp_path / "formal-home"),
        MNEMOS_DIR=str(tmp_path / "formal-home" / ".mnemos"),
        MNEMOS_DATABASE_DIR=str(tmp_path / "formal-home" / ".mnemos"),
        MNEMOS_WIKI_DIR=str(tmp_path / "formal-wiki"),
        MNEMOS_AUDIT_OS_WRITE_DENY="sandbox-exec-v1",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/audit_cognitive_calibration_lineage.py",
            "--database-dir",
            str(tmp_path / "live-read-database"),
            "--wiki-dir",
            str(tmp_path / "live-read-wiki"),
            "--strict",
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode != 0
    assert "sandbox-exec marker is missing its bound sentinel" in completed.stderr


def test_production_calibration_cli_fails_closed_without_a_supported_os_adapter(
    monkeypatch,
) -> None:
    monkeypatch.delenv("MNEMOS_AUDIT_OS_WRITE_DENY", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")

    with pytest.raises(RuntimeError, match="supported OS write-deny adapter"):
        _run_cli_with_os_write_deny(["--strict", "--json"])


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS sandbox contract")
def test_calibration_lineage_cli_fails_closed_with_active_writer_and_preserves_formal_ledgers(
    tmp_path,
):
    formal_home = tmp_path / "formal-home"
    formal_database = formal_home / ".mnemos"
    formal_wiki = tmp_path / "formal-wiki"
    formal_database.mkdir(parents=True)
    formal_wiki.mkdir()
    config = AuditRuntimeConfig(
        mnemos_dir=formal_database,
        database_dir=formal_database,
        data_dir=formal_database,
        wiki_dir=formal_wiki,
    )
    ledger = WikiProjectionLedger(formal_database / "wiki_projection.db")
    bus = EventBus(
        config=config,
        projection_db_path=ledger.db_path,
        run_startup_maintenance=False,
        recover_pending=False,
        enqueue_published_events=False,
    )
    try:
        pending_trace = bus.publish(Event("polled", "test", {"seed": True}))
    finally:
        bus.close()
    before = {path.name: _sha256(path) for path in sorted(formal_database.glob("*.db"))}
    with sqlite3.connect(formal_database / "events.db") as conn:
        before_status = conn.execute(
            "SELECT status, lease_owner FROM events WHERE trace_id=?",
            (pending_trace,),
        ).fetchone()
    environment = _diagnostic_environment(
        HOME=str(formal_home),
        USERPROFILE=str(formal_home),
        MNEMOS_DIR=str(formal_database),
        MNEMOS_DATABASE_DIR=str(formal_database),
        MNEMOS_WIKI_DIR=str(formal_wiki),
    )

    with _active_mnemos_runtime():
        completed = subprocess.run(
            [
                sys.executable,
                "scripts/audit_cognitive_calibration_lineage.py",
                "--database-dir",
                str(tmp_path / "live-read-database"),
                "--wiki-dir",
                str(tmp_path / "live-read-wiki"),
                "--strict",
                "--json",
            ],
            cwd=Path(__file__).resolve().parents[3],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    assert completed.returncode == 1
    assert "multi-database evidence epoch requires inactive runtime writers" in completed.stderr
    after = {path.name: _sha256(path) for path in sorted(formal_database.glob("*.db"))}
    with sqlite3.connect(formal_database / "events.db") as conn:
        after_status = conn.execute(
            "SELECT status, lease_owner FROM events WHERE trace_id=?",
            (pending_trace,),
        ).fetchone()
    assert after == before
    assert after_status == before_status == ("pending", "")


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS sandbox contract")
def test_production_readonly_adapter_reads_initialized_ledgers_and_preserves_bytes(
    tmp_path,
):
    sandbox_exec = shutil.which("sandbox-exec")
    if sandbox_exec is None:
        pytest.skip("sandbox-exec is unavailable")
    formal_database = tmp_path / "formal-home" / ".mnemos"
    formal_database.mkdir(parents=True)
    events = formal_database / "events.db"
    projection = formal_database / "wiki_projection.db"
    trace_id = "pending-production-readonly-fixture"
    with sqlite3.connect(events) as conn:
        conn.execute(
            "CREATE TABLE events(trace_id TEXT PRIMARY KEY, status TEXT, lease_owner TEXT)"
        )
        conn.execute("INSERT INTO events VALUES (?, 'pending', '')", (trace_id,))
    with sqlite3.connect(projection) as conn:
        conn.execute("CREATE TABLE wiki_mutations(mutation_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO wiki_mutations VALUES ('fixture-mutation')")
    before = {path.name: _sha256(path) for path in (events, projection)}

    allowed = tmp_path / "audit-owned"
    temporary = allowed / "tmp"
    pycache = allowed / "pycache"
    temporary.mkdir(parents=True)
    pycache.mkdir()
    sentinel = tmp_path / "write-deny-sentinel"
    sentinel.write_bytes(os.urandom(32))
    sentinel.chmod(0o600)
    sentinel_stat = sentinel.stat()
    sentinel_sha256 = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    profile = "\n".join(
        [
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
            f'(allow file-write* (subpath "{allowed}"))',
        ]
    )
    child = textwrap.dedent("""
        import json
        from pathlib import Path
        import sys
        from core.migrations.model_call_ledger_reconcile import runtime
        from core.ops.audit_run import AuditExecutionEnvironment

        events = Path(sys.argv[1])
        projection = Path(sys.argv[2])
        snapshot_root = Path(sys.argv[3])
        sentinel = Path(sys.argv[4])
        identity = {
            "device": int(sys.argv[5]),
            "inode": int(sys.argv[6]),
            "sha256": sys.argv[7],
        }
        trace_id = sys.argv[8]
        targets = tuple(
            path
            for database in (events, projection)
            for path in (
                database,
                database.with_name(database.name + "-wal"),
                database.with_name(database.name + "-shm"),
            )
        )
        runtime.runtime_writers_are_inactive = lambda _root: True
        audit = AuditExecutionEnvironment.production_readonly(
            targets=targets,
            directory_targets=(events.parent,),
            required_sqlite_databases=(events, projection),
            evidence_snapshot_root=snapshot_root,
            write_deny_probe=sentinel,
            write_deny_identity=identity,
        )
        with audit:
            with audit.open_sqlite_readonly(events) as conn:
                event_row = list(
                    conn.execute(
                        "SELECT status, lease_owner FROM events WHERE trace_id=?",
                        (trace_id,),
                    ).fetchone()
                )
            with audit.open_sqlite_readonly(projection) as conn:
                mutation_count = int(
                    conn.execute("SELECT COUNT(*) FROM wiki_mutations").fetchone()[0]
                )
        report = audit.report()
        print(
            json.dumps(
                {
                    "event_row": event_row,
                    "mutation_count": mutation_count,
                    "mode": report["mode"],
                    "os_write_guard": report["os_write_guard"],
                    "outside_write_count": report["outside_write_count"],
                    "formal_state_diff": report["formal_state_diff"],
                    "writer_quiescence_checks": report["evidence_epoch"][
                        "writer_quiescence_checks"
                    ],
                    "quiescence_source": "deterministic-test-seam",
                    "production_effect": False,
                }
            )
        )
        """)
    environment = _diagnostic_environment(
        HOME=str(tmp_path / "formal-home"),
        USERPROFILE=str(tmp_path / "formal-home"),
        MNEMOS_DIR=str(formal_database),
        MNEMOS_DATABASE_DIR=str(formal_database),
        MNEMOS_WIKI_DIR=str(tmp_path / "formal-wiki"),
        TMPDIR=str(temporary),
        TEMP=str(temporary),
        TMP=str(temporary),
        PYTHONPYCACHEPREFIX=str(pycache),
    )

    completed = subprocess.run(
        [
            sandbox_exec,
            "-p",
            profile,
            sys.executable,
            "-c",
            child,
            str(events),
            str(projection),
            str(allowed / "snapshots"),
            str(sentinel),
            str(sentinel_stat.st_dev),
            str(sentinel_stat.st_ino),
            sentinel_sha256,
            trace_id,
        ],
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report == {
        "event_row": ["pending", ""],
        "mutation_count": 1,
        "mode": "production_readonly",
        "os_write_guard": "sandbox-exec-v1",
        "outside_write_count": 0,
        "formal_state_diff": [],
        "writer_quiescence_checks": 2,
        "quiescence_source": "deterministic-test-seam",
        "production_effect": False,
    }
    after = {path.name: _sha256(path) for path in (events, projection)}
    assert after == before


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS sandbox contract")
def test_calibration_lineage_cli_os_guard_reaches_writer_quiescence_gate(tmp_path):
    sandbox_exec = shutil.which("sandbox-exec")
    if sandbox_exec is None:
        pytest.skip("sandbox-exec is unavailable")
    allowed = tmp_path / "audit-owned"
    temporary = allowed / "tmp"
    pycache = allowed / "pycache"
    temporary.mkdir(parents=True)
    pycache.mkdir()
    sentinel = tmp_path / "write-deny-sentinel"
    sentinel.write_bytes(os.urandom(32))
    sentinel.chmod(0o600)
    sentinel_stat = sentinel.stat()
    sentinel_sha256 = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    formal_home = tmp_path / "formal-home"
    formal_database = formal_home / ".mnemos"
    formal_wiki = tmp_path / "formal-wiki"
    profile = "\n".join(
        [
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
            f'(allow file-write* (subpath "{allowed}"))',
        ]
    )
    environment = _diagnostic_environment(
        HOME=str(formal_home),
        USERPROFILE=str(formal_home),
        MNEMOS_DIR=str(formal_database),
        MNEMOS_DATABASE_DIR=str(formal_database),
        MNEMOS_WIKI_DIR=str(formal_wiki),
        TMPDIR=str(temporary),
        TEMP=str(temporary),
        TMP=str(temporary),
        PYTHONPYCACHEPREFIX=str(pycache),
        MNEMOS_AUDIT_OS_WRITE_DENY="sandbox-exec-v1",
        MNEMOS_AUDIT_WRITE_DENY_SENTINEL=str(sentinel),
        MNEMOS_AUDIT_WRITE_DENY_DEVICE=str(sentinel_stat.st_dev),
        MNEMOS_AUDIT_WRITE_DENY_INODE=str(sentinel_stat.st_ino),
        MNEMOS_AUDIT_WRITE_DENY_SHA256=sentinel_sha256,
    )

    with _active_mnemos_runtime():
        completed = subprocess.run(
            [
                sandbox_exec,
                "-p",
                profile,
                sys.executable,
                "scripts/audit_cognitive_calibration_lineage.py",
                "--database-dir",
                str(tmp_path / "live-read-database"),
                "--wiki-dir",
                str(tmp_path / "live-read-wiki"),
                "--strict",
                "--json",
            ],
            cwd=Path(__file__).resolve().parents[3],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    assert completed.returncode == 1
    assert "multi-database evidence epoch requires inactive runtime writers" in completed.stderr
    assert not formal_database.exists()
    assert not formal_wiki.exists()
