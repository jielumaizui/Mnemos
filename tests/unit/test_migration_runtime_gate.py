from __future__ import annotations

import importlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from daemon import instance_control

from core.migrations.model_call_ledger_reconcile import runtime
from core.sync_framework.capture_schema import CaptureQueueSchema
from scripts import reconcile_capture_queue_schema as capture_schema_reconcile
from scripts import reconcile_raw_event_identity_schema as raw_identity_reconcile


class _Process:
    def __init__(self, info):
        self.info = info

    def status(self):
        return self.info.get("status", "running")


def test_apply_runtime_gate_rejects_mcp_when_daemon_pid_is_absent(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        instance_control,
        "status",
        lambda *_args, **_kwargs: SimpleNamespace(
            exit_code=0,
            daemon_pid=None,
            messages=("Mnemos daemon 未运行",),
        ),
    )
    monkeypatch.setattr(
        runtime,
        "mnemos_runtime_is_active",
        lambda: True,
        raising=False,
    )

    assert runtime.runtime_writers_are_inactive(tmp_path) is False


def test_mcp_serve_command_is_a_mnemos_runtime_process() -> None:
    assert (
        runtime.is_mnemos_runtime_process(
            name="Python",
            cmdline=(
                "/opt/mnemos/.venv/bin/python",
                "/opt/mnemos/mnemos_cli.py",
                "mcp",
                "serve",
            ),
        )
        is True
    )


def test_phase3_audit_is_not_a_mnemos_runtime_process() -> None:
    assert (
        runtime.is_mnemos_runtime_process(
            name="Python",
            cmdline=(
                "/opt/mnemos/.venv/bin/python",
                "/opt/mnemos/scripts/audit_phase3_cognitive_chain.py",
                "--strict",
                "--json",
            ),
        )
        is False
    )


def test_runtime_enumerator_detects_a_same_user_mcp_process(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime.psutil,
        "Process",
        lambda _pid: SimpleNamespace(username=lambda: "zhuwei"),
    )
    monkeypatch.setattr(
        runtime.psutil,
        "process_iter",
        lambda _attrs, **_kwargs: iter(
            (
                _Process(
                    {
                        "pid": 31339,
                        "username": "zhuwei",
                        "name": "Python",
                        "cmdline": (
                            "/opt/mnemos/.venv/bin/python",
                            "/opt/mnemos/mnemos_cli.py",
                            "mcp",
                            "serve",
                        ),
                    }
                ),
            )
        ),
    )

    assert runtime.mnemos_runtime_is_active() is True


def test_runtime_enumerator_fails_closed_when_process_scan_is_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runtime.psutil,
        "Process",
        lambda _pid: SimpleNamespace(username=lambda: "zhuwei"),
    )

    def _denied(_attrs, **_kwargs):
        raise runtime.psutil.AccessDenied(pid=31339)

    monkeypatch.setattr(runtime.psutil, "process_iter", _denied)

    assert runtime.mnemos_runtime_is_active() is True


def test_runtime_enumerator_fails_closed_when_process_details_are_incomplete(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runtime.psutil,
        "Process",
        lambda _pid: SimpleNamespace(username=lambda: "zhuwei"),
    )
    monkeypatch.setattr(
        runtime.psutil,
        "process_iter",
        lambda _attrs, **_kwargs: iter(
            (
                _Process(
                    {
                        "pid": 31339,
                        "username": "zhuwei",
                        "name": "Python",
                        "cmdline": (),
                        "inspection_incomplete": True,
                    }
                ),
            )
        ),
    )

    assert runtime.mnemos_runtime_is_active() is True


def test_runtime_enumerator_fails_closed_when_psutil_cmdline_is_none(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runtime.psutil,
        "Process",
        lambda _pid: SimpleNamespace(username=lambda: "zhuwei"),
    )
    monkeypatch.setattr(
        runtime.psutil,
        "process_iter",
        lambda _attrs, **_kwargs: iter(
            (
                _Process(
                    {
                        "pid": 31339,
                        "username": "zhuwei",
                        "name": "Python",
                        "cmdline": None,
                    }
                ),
            )
        ),
    )

    assert runtime.mnemos_runtime_is_active() is True


def test_runtime_enumerator_ignores_unrelated_system_process_without_cmdline(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runtime.psutil,
        "Process",
        lambda _pid: SimpleNamespace(username=lambda: "zhuwei"),
    )
    monkeypatch.setattr(
        runtime.psutil,
        "process_iter",
        lambda _attrs, **_kwargs: iter(
            (
                _Process(
                    {
                        "pid": 31339,
                        "username": "zhuwei",
                        "name": "dscacheutil",
                        "cmdline": None,
                    }
                ),
            )
        ),
    )

    assert runtime.mnemos_runtime_is_active() is False


def test_runtime_enumerator_fails_closed_when_name_and_cmdline_are_unavailable(
    monkeypatch,
) -> None:
    """A process with no classifiable identity cannot be proven unrelated."""

    monkeypatch.setattr(
        runtime.psutil,
        "Process",
        lambda _pid: SimpleNamespace(username=lambda: "zhuwei"),
    )
    monkeypatch.setattr(
        runtime.psutil,
        "process_iter",
        lambda _attrs, **_kwargs: iter(
            (
                _Process(
                    {
                        "pid": 31339,
                        "username": "zhuwei",
                        "name": None,
                        "cmdline": None,
                    }
                ),
            )
        ),
    )

    assert runtime.mnemos_runtime_is_active() is True


@pytest.mark.parametrize(
    ("name", "cmdline"),
    (
        (None, ("/usr/bin/python", "worker.py")),
        (" ", (" ",)),
    ),
)
def test_runtime_enumerator_fails_closed_when_same_user_name_is_unclassifiable(
    monkeypatch,
    name,
    cmdline,
) -> None:
    monkeypatch.setattr(
        runtime.psutil,
        "Process",
        lambda _pid: SimpleNamespace(username=lambda: "zhuwei"),
    )
    monkeypatch.setattr(
        runtime.psutil,
        "process_iter",
        lambda _attrs, **_kwargs: iter(
            (
                _Process(
                    {
                        "pid": 31339,
                        "username": "zhuwei",
                        "name": name,
                        "cmdline": cmdline,
                    }
                ),
            )
        ),
    )

    assert runtime.mnemos_runtime_is_active() is True


def test_runtime_enumerator_fails_closed_on_attr_level_access_denied(
    monkeypatch,
) -> None:
    """psutil reports per-attribute inspection failures through ad_value."""

    monkeypatch.setattr(
        runtime.psutil,
        "Process",
        lambda _pid: SimpleNamespace(username=lambda: "zhuwei"),
    )

    def _iter_with_denied_cmdline(_attrs, *, ad_value):
        return iter(
            (
                _Process(
                    {
                        "pid": 31339,
                        "username": "zhuwei",
                        "name": "dscacheutil",
                        "cmdline": ad_value,
                    }
                ),
            )
        )

    monkeypatch.setattr(
        runtime.psutil,
        "process_iter",
        _iter_with_denied_cmdline,
    )

    assert runtime.mnemos_runtime_is_active() is True


def test_runtime_enumerator_ignores_same_user_zombie_with_denied_cmdline(
    monkeypatch,
) -> None:
    """A zombie cannot be a live writer even when cmdline inspection fails."""

    monkeypatch.setattr(
        runtime.psutil,
        "Process",
        lambda _pid: SimpleNamespace(username=lambda: "zhuwei"),
    )

    def _iter_with_zombie_cmdline(_attrs, *, ad_value):
        return iter(
            (
                _Process(
                    {
                        "pid": 31339,
                        "username": "zhuwei",
                        "name": "dscacheutil",
                        "cmdline": ad_value,
                        "status": runtime.psutil.STATUS_ZOMBIE,
                    }
                ),
            )
        )

    monkeypatch.setattr(
        runtime.psutil,
        "process_iter",
        _iter_with_zombie_cmdline,
    )

    assert runtime.mnemos_runtime_is_active() is False


def test_runtime_enumerator_ignores_uninspectable_known_other_user_process(
    monkeypatch,
) -> None:
    """Other-user process details are irrelevant once ownership is known."""

    monkeypatch.setattr(
        runtime.psutil,
        "Process",
        lambda _pid: SimpleNamespace(username=lambda: "zhuwei"),
    )

    def _iter_with_other_user_denied_cmdline(_attrs, *, ad_value):
        return iter(
            (
                _Process(
                    {
                        "pid": 31339,
                        "username": "root",
                        "name": ad_value,
                        "cmdline": ad_value,
                    }
                ),
            )
        )

    monkeypatch.setattr(
        runtime.psutil,
        "process_iter",
        _iter_with_other_user_denied_cmdline,
    )

    assert runtime.mnemos_runtime_is_active() is False


def test_runtime_enumerator_ignores_empty_argument_in_non_mnemos_command(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runtime.psutil,
        "Process",
        lambda _pid: SimpleNamespace(username=lambda: "zhuwei"),
    )
    monkeypatch.setattr(
        runtime.psutil,
        "process_iter",
        lambda _attrs, **_kwargs: iter(
            (
                _Process(
                    {
                        "pid": 31339,
                        "username": "zhuwei",
                        "name": "Python",
                        "cmdline": ("Kimi Code", ""),
                    }
                ),
            )
        ),
    )

    assert runtime.mnemos_runtime_is_active() is False


def test_runtime_enumerator_without_psutil_fails_closed_on_darwin(
    monkeypatch,
) -> None:
    real_import = importlib.import_module

    def import_without_psutil(name, package=None):
        if name == "psutil":
            raise ModuleNotFoundError("psutil intentionally unavailable", name=name)
        return real_import(name, package)

    try:
        with monkeypatch.context() as isolated:
            isolated.setattr(importlib, "import_module", import_without_psutil)
            isolated.setattr(sys, "platform", "darwin")
            reloaded = importlib.reload(runtime)

            assert reloaded.psutil is None
            assert reloaded.mnemos_runtime_is_active() is True
    finally:
        importlib.reload(runtime)


def _legacy_capture_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE capture_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT UNIQUE,
                source_agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_id TEXT,
                turn_number INTEGER,
                payload_json TEXT,
                content_hash TEXT,
                status TEXT,
                retry_count INTEGER,
                created_at TEXT,
                processed_at TEXT,
                error TEXT,
                working_dir TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO capture_events (
                dedupe_key, source_agent, session_id, content_hash, status
            ) VALUES ('legacy', 'codex', 'session', 'hash', 'done')
            """
        )


def test_capture_schema_dry_run_binds_an_exact_zero_write_plan(tmp_path) -> None:
    db_path = tmp_path / "capture_queue.db"
    backup_dir = tmp_path / "backups"

    result = capture_schema_reconcile.reconcile(
        db_path=db_path,
        apply=False,
        backup_dir=backup_dir,
        expected_plan_hash="",
        runtime_writers_are_inactive=lambda _database_dir: True,
    )

    assert result["before"]["status"] == "uninitialized"
    assert result["apply_eligible"] is True
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", result["plan_hash"])
    assert result["ok"] is True
    assert not db_path.exists()
    assert not backup_dir.exists()


def test_capture_schema_apply_requires_the_reviewed_plan_hash(tmp_path) -> None:
    db_path = tmp_path / "capture_queue.db"
    backup_dir = tmp_path / "backups"

    with pytest.raises(ValueError, match="expected_plan_hash_required"):
        capture_schema_reconcile.reconcile(
            db_path=db_path,
            apply=True,
            backup_dir=backup_dir,
            expected_plan_hash="",
            runtime_writers_are_inactive=lambda _database_dir: True,
        )

    assert not db_path.exists()
    assert not backup_dir.exists()


def test_capture_schema_apply_rejects_an_active_writer_without_mutation(
    tmp_path,
) -> None:
    db_path = tmp_path / "capture_queue.db"
    backup_dir = tmp_path / "backups"

    with pytest.raises(RuntimeError, match="capture_schema_writers_not_inactive"):
        capture_schema_reconcile.reconcile(
            db_path=db_path,
            apply=True,
            backup_dir=backup_dir,
            expected_plan_hash=f"sha256:{'0' * 64}",
            runtime_writers_are_inactive=lambda _database_dir: False,
        )

    assert not db_path.exists()
    assert not backup_dir.exists()


def test_capture_schema_apply_preserves_rows_and_writes_a_verified_backup(
    tmp_path,
) -> None:
    db_path = tmp_path / "capture_queue.db"
    backup_dir = tmp_path / "backups"
    _legacy_capture_database(db_path)
    planned = capture_schema_reconcile.reconcile(
        db_path=db_path,
        apply=False,
        backup_dir=backup_dir,
        expected_plan_hash="",
        runtime_writers_are_inactive=lambda _database_dir: True,
    )

    applied = capture_schema_reconcile.reconcile(
        db_path=db_path,
        apply=True,
        backup_dir=backup_dir,
        expected_plan_hash=planned["plan_hash"],
        runtime_writers_are_inactive=lambda _database_dir: True,
    )

    assert applied["ok"] is True
    assert applied["reviewed_plan_hash"] == planned["plan_hash"]
    assert CaptureQueueSchema.inspect(db_path)["status"] == "current"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT dedupe_key FROM capture_events WHERE dedupe_key='legacy'"
        ).fetchone() == ("legacy",)
    backup_path = Path(applied["backup"]["path"])
    assert backup_path.parent == backup_dir
    assert backup_path.is_file()
    with sqlite3.connect(f"{backup_path.as_uri()}?mode=ro&immutable=1", uri=True) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute(
            "SELECT dedupe_key FROM capture_events WHERE dedupe_key='legacy'"
        ).fetchone() == ("legacy",)


def test_capture_schema_apply_rejects_state_drift_before_backup(tmp_path) -> None:
    db_path = tmp_path / "capture_queue.db"
    backup_dir = tmp_path / "backups"
    _legacy_capture_database(db_path)
    planned = capture_schema_reconcile.reconcile(
        db_path=db_path,
        apply=False,
        backup_dir=backup_dir,
        expected_plan_hash="",
        runtime_writers_are_inactive=lambda _database_dir: True,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE concurrent_drift(value TEXT)")

    with pytest.raises(ValueError, match="expected_plan_hash_mismatch"):
        capture_schema_reconcile.reconcile(
            db_path=db_path,
            apply=True,
            backup_dir=backup_dir,
            expected_plan_hash=planned["plan_hash"],
            runtime_writers_are_inactive=lambda _database_dir: True,
        )

    assert not backup_dir.exists()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='concurrent_drift'"
        ).fetchone() == ("concurrent_drift",)


def test_capture_schema_apply_restores_the_reviewed_database_on_failure(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "capture_queue.db"
    backup_dir = tmp_path / "backups"
    _legacy_capture_database(db_path)
    planned = capture_schema_reconcile.reconcile(
        db_path=db_path,
        apply=False,
        backup_dir=backup_dir,
        expected_plan_hash="",
        runtime_writers_are_inactive=lambda _database_dir: True,
    )

    def mutate_then_fail(cls, path):  # noqa: ARG001
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE partial_apply(value TEXT)")
        raise RuntimeError("simulated_capture_schema_failure")

    monkeypatch.setattr(
        CaptureQueueSchema,
        "initialize",
        classmethod(mutate_then_fail),
    )

    with pytest.raises(RuntimeError, match="simulated_capture_schema_failure"):
        capture_schema_reconcile.reconcile(
            db_path=db_path,
            apply=True,
            backup_dir=backup_dir,
            expected_plan_hash=planned["plan_hash"],
            runtime_writers_are_inactive=lambda _database_dir: True,
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT dedupe_key FROM capture_events WHERE dedupe_key='legacy'"
        ).fetchone() == ("legacy",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='partial_apply'"
        ).fetchone() is None


def test_capture_schema_failed_first_bootstrap_removes_partial_database(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "capture_queue.db"
    backup_dir = tmp_path / "backups"
    planned = capture_schema_reconcile.reconcile(
        db_path=db_path,
        apply=False,
        backup_dir=backup_dir,
        expected_plan_hash="",
        runtime_writers_are_inactive=lambda _database_dir: True,
    )

    def create_then_fail(cls, path):  # noqa: ARG001
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE partial_bootstrap(value TEXT)")
        raise RuntimeError("simulated_capture_bootstrap_failure")

    monkeypatch.setattr(
        CaptureQueueSchema,
        "initialize",
        classmethod(create_then_fail),
    )

    with pytest.raises(RuntimeError, match="simulated_capture_bootstrap_failure"):
        capture_schema_reconcile.reconcile(
            db_path=db_path,
            apply=True,
            backup_dir=backup_dir,
            expected_plan_hash=planned["plan_hash"],
            runtime_writers_are_inactive=lambda _database_dir: True,
        )

    assert not db_path.exists()
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()


def test_capture_schema_failure_never_restores_over_a_replacement_database(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "capture_queue.db"
    backup_dir = tmp_path / "backups"
    detached = tmp_path / "reviewed.detached.db"
    _legacy_capture_database(db_path)
    planned = capture_schema_reconcile.reconcile(
        db_path=db_path,
        apply=False,
        backup_dir=backup_dir,
        expected_plan_hash="",
        runtime_writers_are_inactive=lambda _database_dir: True,
    )

    def replace_then_fail(cls, path):  # noqa: ARG001
        path.replace(detached)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE foreign_generation(value TEXT)")
            connection.execute("INSERT INTO foreign_generation VALUES ('preserve')")
        raise RuntimeError("simulated_replacement_failure")

    monkeypatch.setattr(
        CaptureQueueSchema,
        "initialize",
        classmethod(replace_then_fail),
    )

    with pytest.raises(
        RuntimeError,
        match="offline_schema_restore_target_changed",
    ):
        capture_schema_reconcile.reconcile(
            db_path=db_path,
            apply=True,
            backup_dir=backup_dir,
            expected_plan_hash=planned["plan_hash"],
            runtime_writers_are_inactive=lambda _database_dir: True,
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT value FROM foreign_generation"
        ).fetchone() == ("preserve",)
    with sqlite3.connect(detached) as connection:
        assert connection.execute(
            "SELECT dedupe_key FROM capture_events WHERE dedupe_key='legacy'"
        ).fetchone() == ("legacy",)
    assert len(list(backup_dir.glob("capture-queue.pre-schema-v2.*.sqlite"))) == 1


def test_capture_schema_failed_bootstrap_preserves_a_replacement_database(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "capture_queue.db"
    backup_dir = tmp_path / "backups"
    detached = tmp_path / "owned-bootstrap.detached.db"
    planned = capture_schema_reconcile.reconcile(
        db_path=db_path,
        apply=False,
        backup_dir=backup_dir,
        expected_plan_hash="",
        runtime_writers_are_inactive=lambda _database_dir: True,
    )

    def replace_then_fail(cls, path):  # noqa: ARG001
        path.replace(detached)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE foreign_generation(value TEXT)")
            connection.execute("INSERT INTO foreign_generation VALUES ('preserve')")
        raise RuntimeError("simulated_bootstrap_replacement_failure")

    monkeypatch.setattr(
        CaptureQueueSchema,
        "initialize",
        classmethod(replace_then_fail),
    )

    with pytest.raises(
        RuntimeError,
        match="offline_schema_restore_target_changed",
    ):
        capture_schema_reconcile.reconcile(
            db_path=db_path,
            apply=True,
            backup_dir=backup_dir,
            expected_plan_hash=planned["plan_hash"],
            runtime_writers_are_inactive=lambda _database_dir: True,
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT value FROM foreign_generation"
        ).fetchone() == ("preserve",)
    assert detached.is_file()
    assert not backup_dir.exists()


def test_legacy_raw_identity_apply_entrypoint_is_read_only_and_retired(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    db_path = tmp_path / "raw_events.db"
    backup_dir = tmp_path / "backups"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES ('preserve')")
    before = db_path.read_bytes()

    def forbidden_apply(_db_path):
        raise AssertionError("legacy identity apply must never run")

    monkeypatch.setattr(
        raw_identity_reconcile,
        "apply",
        forbidden_apply,
        raising=False,
    )
    exit_code = raw_identity_reconcile.main(
        [
            "--db",
            str(db_path),
            "--apply",
            "--backup-dir",
            str(backup_dir),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["error"] == "raw_identity_apply_owned_by_agent_source_raw_recovery"
    assert payload["replacement_owner"] == "scripts/reconcile_agent_source_raw_capture.py"
    assert db_path.read_bytes() == before
    assert not backup_dir.exists()
