import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from core.migrations.model_call_ledger_reconcile import (
    ModelCallLedgerReconcileError,
    backup,
    cleanup,
    cli,
    executor,
    inventory,
    planner,
    runtime,
)
from core.ops.hermetic_run import HermeticRunEnvironment
from core.telemetry.model_call_ledger.migration import LedgerReconciliation


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class FakeConfig:
    def __init__(self, root: Path):
        self.data_dir = root
        self.database_dir = root

    def get(self, _key, default=None):
        return default


def _create_legacy_prompt_table(
    path: Path,
    rows: list[tuple],
    *,
    session_id: str = "legacy-test-session",
):
    # ``Connection.__exit__`` commits/rolls back but does not close SQLite's
    # descriptor.  Explicitly close setup handles: a lingering reader changes
    # WAL/journal-mode behaviour and would make a race test assert a property
    # of its fixture rather than the reconciler.
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE prompt_calls (
                id INTEGER PRIMARY KEY,
                task_type TEXT,
                provider TEXT,
                model TEXT,
                prompt_hash TEXT,
                prompt_summary TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                latency_ms INTEGER,
                success INTEGER,
                created_at TEXT,
                session_id TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO prompt_calls(
                task_type, provider, model, prompt_hash, prompt_summary,
                prompt_tokens, completion_tokens, latency_ms, success, created_at, session_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(*row, session_id) for row in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _seed_canonical_legacy_observation(path: Path):
    """Build a test-only pre-migration legacy row without reviving its API."""
    from core.telemetry.prompt_call_log import ModelCallLedger

    ledger = ModelCallLedger(path)
    run_id = ledger.start_run(
        "legacy-seed", subject_scope=("session", "canonical-session")
    )
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            """
            INSERT INTO model_call_entries(
                entry_id, run_id, operation, provider, model, input_digest,
                reserved_input_tokens, reserved_output_tokens, reserved_cost,
                actual_input_tokens, actual_output_tokens, actual_total_tokens,
                actual_cost, refund_cost, latency_ms, provider_usage_id,
                metered_usage_receipt, request_id, price_version, input_price,
                output_price, cache_status, retry_attempt, lifecycle_state,
                request_dispatched, error_code, legacy_fingerprint, created_at, settled_at
            ) VALUES (?, ?, 'legacy', 'legacy', 'unknown', ?, 1, 0, 0,
                      1, 0, 1, NULL, 0, 0, '', '', '', 'legacy', 0, 0,
                      'legacy', 0, 'legacy_observed', 0, '', ?,
                      '2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00')
            """,
            (
                "legacy-model-call-test",
                run_id,
                "a" * 64,
                "b" * 64,
            ),
        )
        conn.execute(
            "INSERT INTO model_call_entry_subjects(entry_id, scope_kind, subject_hash, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                "legacy-model-call-test",
                "session",
                "c" * 64,
                "2026-07-01T00:00:00+00:00",
            ),
        )
    return ledger


def _backup_gated_session(source: Path, backup_dir: Path):
    """Open the non-public migration session through its explicit core seam."""
    proof = LedgerReconciliation.prepare_backup(source)
    backups, receipt = backup.create_sqlite_backups(
        [source],
        backup_dir,
        prepared_canonical_backup=proof,
        return_canonical_backup_receipt=True,
    )
    assert len(backups) == 1
    return LedgerReconciliation.open_after_verified_backup(proof, receipt)


def _registered_test_capability(plan: dict):
    """Mint the non-public registry bridge used by source-level unit tests.

    Public script callers cannot supply a callback: they receive only the
    opaque capability emitted by the registered migration.  These narrow
    source tests exercise the reconciler below that registry boundary.
    """
    from core.migrations.registry import (
        _MODEL_CALL_LEDGER_CAPABILITY_AUTHORITY,
        _mint_model_call_ledger_apply_capability,
    )

    def lifecycle(phase: str, _source_plan: dict) -> dict:
        return {
            "ok": True,
            "status": phase,
            "recovery_manifest": "registered-test-recovery",
        }

    return _mint_model_call_ledger_apply_capability(
        attempt_ledger_id="test-registry-attempt",
        expected_plan_hash=str(plan["plan_fingerprint"]),
        lifecycle=lifecycle,
        _authority=_MODEL_CALL_LEDGER_CAPABILITY_AUTHORITY,
    )


def _apply_reconciliation(reconcile, config, *, apply=True, **kwargs):
    """Apply only the exact dry-run receipt the test just reviewed."""
    assert apply is True
    plan, _ = reconcile.build_reconciliation_plan(config)
    return reconcile.reconcile_model_call_ledger(
        config,
        apply=True,
        expected_plan_hash=plan["plan_fingerprint"],
        migration_capability=_registered_test_capability(plan),
        **kwargs,
    )


def test_model_call_ledger_reconciliation_dry_run_is_private_and_write_free(tmp_path):
    from core.migrations.model_call_ledger_reconcile import build_reconciliation_plan

    secret = "DUMMY_CREDENTIAL_VALUE_FOR_REDACTION_TEST"
    source = tmp_path / "wiki_state.db"
    _create_legacy_prompt_table(
        source,
        [
            (
                "distill",
                "test",
                "model",
                "a" * 64,
                f"prompt body {secret}",
                3,
                2,
                8,
                1,
                "2026-07-01T00:00:00+00:00",
            )
        ],
    )
    before = source.read_bytes()

    plan, pending = build_reconciliation_plan(FakeConfig(tmp_path))

    assert source.read_bytes() == before
    assert not (tmp_path / "model_call_ledger.db").exists()
    assert plan["status"] == "reconciliation_required"
    assert plan["legacy_source_row_count"] == 1
    # A non-empty legacy session_id is not a provenance receipt.  The planner
    # keeps it only inside a one-way fingerprint and requires explicit discard
    # rather than fabricating subject deletion authority.
    assert plan["would_import_count"] == 0
    assert plan["attributable_legacy_call_count"] == 0
    assert plan["unattributable_legacy_call_count"] == 1
    assert plan["requires_explicit_unattributable_discard"] is True
    assert pending == []
    assert secret not in json.dumps(plan, ensure_ascii=False)


@pytest.mark.parametrize(
    ("filename", "suffix"),
    [
        ("wiki_state.db", "-wal"),
        ("model_call_ledger.db", "-journal"),
    ],
)
def test_reconciliation_plan_rejects_orphan_sqlite_sidecars_without_writes(
    tmp_path, filename, suffix
):
    """An absent main DB cannot hide a WAL/journal owner from dry-run evidence."""
    from core.migrations import model_call_ledger_reconcile as reconcile

    config = FakeConfig(tmp_path)
    main = tmp_path / filename
    sidecar = Path(str(main) + suffix)
    sidecar.write_bytes(b"orphan-sidecar-fixture")
    before = sidecar.read_bytes()

    plan, pending = reconcile.build_reconciliation_plan(config)

    assert pending == []
    assert plan["ok"] is False
    assert plan["status"] == "blocked"
    assert plan["error"] == "reconciliation_orphan_sidecar_present"
    report = (
        plan["canonical_retired_storage"]
        if filename == "model_call_ledger.db"
        else next(report for report in plan["sources"] if report["path"] == str(main))
    )
    assert report["exists"] is False
    assert report["error"] == "reconciliation_orphan_sidecar_present"
    assert not main.exists()
    assert sidecar.read_bytes() == before
    assert not (tmp_path / "backups").exists()


def test_direct_apply_rejects_forged_or_unregistered_registry_capability_zero_write(
    tmp_path, monkeypatch, capsys
):
    """The standalone script cannot turn an arbitrary callback into authority."""
    from core.migrations.registry import (
        _ModelCallLedgerApplyCapability,
        _mint_model_call_ledger_apply_capability,
    )
    from core.migrations import model_call_ledger_reconcile as reconcile

    source = tmp_path / "wiki_state.db"
    _create_legacy_prompt_table(
        source,
        [
            (
                "distill", "test", "model", "a" * 64, "fixture body", 1, 1, 1, 1,
                "2026-07-14T00:00:00+00:00",
            )
        ],
    )
    config = FakeConfig(tmp_path)
    plan, _ = reconcile.build_reconciliation_plan(config)
    source_before = source.read_bytes()
    backup_dir = tmp_path / "blocked-backup"

    with pytest.raises(ValueError, match="registered_migration_capability_required"):
        _mint_model_call_ledger_apply_capability(
            attempt_ledger_id="forged-attempt",
            expected_plan_hash=plan["plan_fingerprint"],
            lifecycle=lambda _phase, _source_plan: {"ok": True},
        )
    blocked = reconcile.reconcile_model_call_ledger(
        config,
        apply=True,
        backup_dir=backup_dir,
        expected_plan_hash=plan["plan_fingerprint"],
        discard_unattributable_legacy=True,
        migration_capability=_ModelCallLedgerApplyCapability(),
    )

    assert blocked["status"] == "blocked"
    assert blocked["error"] == "registered_migration_capability_required"
    assert source.read_bytes() == source_before
    assert not backup_dir.exists()
    assert not (tmp_path / "model_call_ledger.db").exists()
    assert not (tmp_path / "migrations.db").exists()

    # The command line invokes the same public boundary, so a direct script
    # apply has no way to supply the opaque registry capability.
    assert (
        cli.main(
            [
                "--apply",
                "--backup-dir",
                str(backup_dir),
                "--expected-plan-hash",
                str(plan["plan_fingerprint"]),
                "--discard-unattributable-legacy",
                "--json",
            ],
            config_factory=lambda **_kwargs: config,
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "registered_migration_capability_required"
    assert source.read_bytes() == source_before
    assert not backup_dir.exists()


def test_reconcile_main_dry_run_does_not_materialize_an_empty_runtime_tree(
    tmp_path, monkeypatch, capsys
):
    """The public diagnostic entry point must be as read-only as its plan."""
    mnemos_dir = tmp_path / "empty-mnemos"
    monkeypatch.setenv("MNEMOS_DIR", str(mnemos_dir))
    for variable in (
        "MNEMOS_DATABASE_DIR",
        "MNEMOS_RUN_DEFAULT_DATABASE_DIR",
        "MNEMOS_RUN_DEFAULT_MNEMOS_DIR",
    ):
        monkeypatch.delenv(variable, raising=False)

    assert cli.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "clean"
    assert payload["ok"] is True
    assert not mnemos_dir.exists()
    assert list(tmp_path.iterdir()) == []


def test_reconcile_script_json_is_standalone_read_only_and_public_safe(tmp_path):
    """The registered wrapper imports itself and never echoes private values."""
    run = HermeticRunEnvironment.create(
        tmp_path / "wrapper-run",
        profile="isolated",
        formal_targets=(),
    )
    environment = dict(run.environment)
    environment.pop("PYTHONPATH", None)
    mnemos_dir = Path(environment["MNEMOS_DIR"])
    source = mnemos_dir / "wiki_state.db"
    private_prompt = (
        "api" + "_key=private-api-value|pass" + "word=private-password-value|"
        "bank" + "_card=private-card-value|email=private-email-value"
    )
    _create_legacy_prompt_table(
        source,
        [
            (
                "distill",
                "test",
                "model",
                "a" * 64,
                private_prompt,
                3,
                2,
                8,
                1,
                "2026-07-14T00:00:00+00:00",
            )
        ],
    )
    source_before = source.read_bytes()
    before_tree = {
        str(path.relative_to(run.root))
        for path in run.root.rglob("*")
        if not str(path.relative_to(run.root)).startswith("pycache/")
    }

    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "reconcile_model_call_ledger.py"),
            "--json",
        ],
        cwd=run.root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "reconciliation_required"
    assert payload["canonical_path"] == "<MNEMOS_DIR>/model_call_ledger.db"
    assert all(
        report["path"].startswith("<MNEMOS_DIR>/")
        for report in payload["sources"]
    )
    assert str(run.root) not in completed.stdout
    assert private_prompt not in completed.stdout
    assert source.read_bytes() == source_before

    caller_input = "private" + "-caller-input-value"
    blocked = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "reconcile_model_call_ledger.py"),
            "--apply",
            "--backup-dir",
            str(run.root / "private-backups"),
            "--expected-plan-hash",
            caller_input,
            "--discard-unattributable-legacy",
            "--json",
        ],
        cwd=run.root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert blocked.returncode == 1, blocked.stderr
    assert json.loads(blocked.stdout)["error"] == "expected_plan_hash_mismatch"
    assert caller_input not in blocked.stdout
    assert str(run.root) not in blocked.stdout
    assert source.read_bytes() == source_before
    assert not (run.root / "private-backups").exists()
    after_tree = {
        str(path.relative_to(run.root))
        for path in run.root.rglob("*")
        if not str(path.relative_to(run.root)).startswith("pycache/")
    }
    assert after_tree == before_tree
    assert run.finalize() == []


def test_reconcile_main_apply_without_backup_is_rejected_before_config_provisioning(
    tmp_path, monkeypatch
):
    mnemos_dir = tmp_path / "empty-mnemos"
    monkeypatch.setenv("MNEMOS_DIR", str(mnemos_dir))
    monkeypatch.delenv("MNEMOS_DATABASE_DIR", raising=False)

    with pytest.raises(SystemExit) as raised:
        cli.main(["--apply"])

    assert raised.value.code == 2
    assert not mnemos_dir.exists()


def test_reconcile_canonical_retired_storage_is_backed_up_scrubbed_and_returns_to_runtime(
    tmp_path, monkeypatch
):
    from core.telemetry.prompt_call_log import ModelCallLedger
    from core.migrations import model_call_ledger_reconcile as reconcile

    config = FakeConfig(tmp_path)
    ledger = ModelCallLedger.for_config(config)
    secret = "CANONICAL_RETIRED_PROMPT_" + ("q" * 12_000)
    _create_legacy_prompt_table(
        ledger.db_path,
        [
            (
                "distill",
                "test",
                "model",
                "9" * 64,
                secret,
                3,
                2,
                8,
                1,
                "2026-07-01T00:00:00+00:00",
            )
        ],
    )
    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.execute("CREATE TABLE prompt_call_stats (name TEXT PRIMARY KEY, value REAL)")
        conn.execute("INSERT INTO prompt_call_stats VALUES ('retired_stats', 1.0)")
        # A normal retired-table index is safe to remove with its known owner;
        # it must not turn canonical cleanup into an invalid unknown index.
        conn.execute("CREATE INDEX idx_retired_prompt_calls_created ON prompt_calls(created_at)")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("UPDATE prompt_calls SET prompt_summary=?", (secret + "-wal",))

    before = ModelCallLedger.inspect(config)
    assert before["legacy_prompt_storage_path_count"] == 1
    assert before["model_call_storage_path_count"] == 2
    assert before["legacy_prompt_call_row_count"] == 1
    assert before["status"] == "degraded"

    plan, pending = reconcile.build_reconciliation_plan(config)
    assert pending == []
    assert plan["canonical_retired_record_count"] == 1
    assert plan["canonical_retired_stats_row_count"] == 1
    assert plan["requires_explicit_unattributable_discard"] is True
    assert plan["requires_explicit_retired_stats_discard"] is True
    assert secret not in json.dumps(plan, ensure_ascii=False)

    blocked = reconcile.reconcile_model_call_ledger(
        config,
        apply=True,
        backup_dir=tmp_path / "backups",
        expected_plan_hash=plan["plan_fingerprint"],
    )
    assert blocked["error"] == "unattributable_legacy_requires_explicit_discard"

    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)
    result = _apply_reconciliation(
        reconcile,
        config,
        backup_dir=tmp_path / "backups",
        discard_unattributable_legacy=True,
    )

    assert result["status"] == "applied"
    assert result["canonical_cleanup"]["dropped_tables"] == [
        "prompt_call_stats",
        "prompt_calls",
    ]
    assert result["sealed_recovery_status"] == "commit"
    assert not list((tmp_path / "backups").glob("model-call-ledger-reconcile-recovery-*.json"))
    with sqlite3.connect(str(ledger.db_path)) as conn:
        retired_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('prompt_calls', 'prompt_call_log', 'prompt_call_stats')"
            )
        }
    assert retired_tables == set()
    for candidate in (
        ledger.db_path,
        Path(str(ledger.db_path) + "-wal"),
        Path(str(ledger.db_path) + "-shm"),
    ):
        if candidate.exists():
            assert secret.encode("utf-8") not in candidate.read_bytes()

    repaired = ModelCallLedger.for_config(config)
    assert ModelCallLedger.inspect(config)["status"] == "ok"
    repaired.start_run("post-canonical-cleanup", subject_scope=("session", "post-cleanup"))
    assert repaired.freeze_subject_scope("session", "post-cleanup")["status"] == "frozen"
    deletion = repaired.delete_subject_scope("session", "post-cleanup")
    assert deletion["status"] == "applied"
    assert deletion["deleted_run_count"] == 1


def test_reconciliation_plan_blocks_canonical_snapshot_error_without_apply(tmp_path, monkeypatch):
    from core.telemetry.prompt_call_log import ModelCallLedger
    from core.migrations import model_call_ledger_reconcile as reconcile

    config = FakeConfig(tmp_path)
    ledger = ModelCallLedger.for_config(config)
    original_snapshot = planner._source_snapshot

    def unreadable_canonical(path):
        report, records = original_snapshot(path)
        if Path(path) == ledger.db_path:
            report = dict(report)
            report["error"] = "OperationalError"
        return report, records

    monkeypatch.setattr(planner, "_source_snapshot", unreadable_canonical)
    plan, _ = reconcile.build_reconciliation_plan(config)

    assert plan["ok"] is False
    assert plan["status"] == "blocked"
    assert plan["error"] == "canonical_retired_storage_unreadable"


@pytest.mark.parametrize(
    ("canonical", "stats_only"),
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_private_cleanup_rejects_pre_journal_wal_mutations_not_in_verified_backup(
    tmp_path, monkeypatch, canonical, stats_only
):
    """Full-table comparison must catch raw/stat changes hidden from the plan.

    The mutation intentionally happens after cleanup's safe pre-inventory but
    before its journal switch checkpoints the new WAL page into the main DB.
    It preserves table count and selected safe metadata, so a stat/fingerprint
    guard alone would have dropped data absent from the recovery backup.
    """
    from core.telemetry.prompt_call_log import ModelCallLedger
    from core.migrations import model_call_ledger_reconcile as reconcile

    config = FakeConfig(tmp_path)
    ledger = ModelCallLedger.for_config(config) if canonical else None
    source = ledger.db_path if ledger is not None else tmp_path / "wiki_state.db"
    # Make the changed prompt occupy distinct SQLite pages as well as differ
    # logically from the backed-up row.  This keeps the WAL race deterministic
    # on platforms with aggressive page-cache reuse.
    marker = "NEW_UNBACKED_RETIRED_RAW_VALUE" * 512
    if not stats_only:
        _create_legacy_prompt_table(
            source,
            [
                (
                    "distill",
                    "test",
                    "model",
                    "8" * 64,
                    "old private prompt",
                    1,
                    1,
                    1,
                    1,
                    "2026-07-01T00:00:00+00:00",
                )
            ],
        )
    setup = sqlite3.connect(str(source))
    try:
        if stats_only:
            setup.execute("CREATE TABLE prompt_call_stats (name TEXT PRIMARY KEY, value REAL)")
            setup.execute("INSERT INTO prompt_call_stats VALUES ('aggregate', 1.0)")
        setup.execute("PRAGMA journal_mode=WAL")
        setup.execute("PRAGMA wal_autocheckpoint=0")
        setup.commit()
    finally:
        setup.close()

    expected, _ = inventory._source_snapshot(source)
    identities: dict[Path, str] = {}
    session = None
    if canonical:
        proof = LedgerReconciliation.prepare_backup(source)
        backups, receipt = backup.create_sqlite_backups(
            [source],
            tmp_path / "backups",
            prepared_canonical_backup=proof,
            return_canonical_backup_receipt=True,
            private_backup_identities=identities,
        )
        session = LedgerReconciliation.open_after_verified_backup(proof, receipt, config=config)
        session.reconcile_privacy_schema()
    else:
        backups = backup.create_sqlite_backups(
            [source], tmp_path / "backups", private_backup_identities=identities
        )
    backup_path = Path(backups[0]["path"]).resolve()
    backup_identity = identities[backup_path]

    original_inventory = cleanup.source_inventory_from_connection
    calls = 0

    def mutate_after_safe_preinventory(path, conn):
        nonlocal calls
        report = original_inventory(path, conn)
        calls += 1
        if calls == 1:
            writer = sqlite3.connect(str(source))
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                if stats_only:
                    writer.execute("UPDATE prompt_call_stats SET value=2.0")
                else:
                    writer.execute("UPDATE prompt_calls SET prompt_summary=?", (marker,))
                writer.commit()
            finally:
                writer.close()
        return report

    monkeypatch.setattr(cleanup, "source_inventory_from_connection", mutate_after_safe_preinventory)
    error = (
        "canonical_retired_source_drift_before_cleanup"
        if canonical
        else "source_drift_before_cleanup"
    )
    try:
        with pytest.raises(reconcile.ModelCallLedgerReconcileError, match=error):
            if canonical:
                cleanup.cleanup_canonical_retired_storage(
                    session,
                    expected_report=expected,
                    verified_backup_path=backup_path,
                    verified_backup_identity=backup_identity,
                )
            else:
                cleanup.cleanup_source_database(
                    source,
                    expected_report=expected,
                    verified_backup_path=backup_path,
                    verified_backup_identity=backup_identity,
                )
    finally:
        if session is not None:
            session.close()

    with sqlite3.connect(str(source)) as conn:
        table = "prompt_call_stats" if stats_only else "prompt_calls"
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() == (1,)
        if stats_only:
            assert conn.execute("SELECT value FROM prompt_call_stats").fetchone() == (2.0,)
        else:
            assert conn.execute("SELECT prompt_summary FROM prompt_calls").fetchone() == (marker,)


def test_cleanup_rejects_verified_backup_replacement_without_dropping_source(tmp_path):
    source = tmp_path / "wiki_state.db"
    _create_legacy_prompt_table(
        source,
        [
            (
                "distill", "test", "model", "7" * 64, "private", 1, 1, 1, 1,
                "2026-07-01T00:00:00+00:00",
            )
        ],
    )
    expected, _ = inventory._source_snapshot(source)
    identities: dict[Path, str] = {}
    backups = backup.create_sqlite_backups(
        [source], tmp_path / "backups", private_backup_identities=identities
    )
    backup_path = Path(backups[0]["path"]).resolve()
    replacement = tmp_path / "replacement.db"
    _create_legacy_prompt_table(
        replacement,
        [
            (
                "distill", "test", "model", "6" * 64, "different", 1, 1, 1, 1,
                "2026-07-01T00:00:00+00:00",
            )
        ],
    )
    os.chmod(replacement, 0o600)
    os.replace(replacement, backup_path)

    with pytest.raises(ModelCallLedgerReconcileError, match="verified_backup_identity_changed"):
        cleanup.cleanup_source_database(
            source,
            expected_report=expected,
            verified_backup_path=backup_path,
            verified_backup_identity=identities[backup_path],
        )
    with sqlite3.connect(str(source)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='prompt_calls'"
        ).fetchone() == (1,)


def test_model_call_ledger_reconciliation_preserves_distinct_matching_legacy_rows(
    tmp_path, monkeypatch
):
    from core.telemetry.prompt_call_log import ModelCallLedger
    from core.migrations import model_call_ledger_reconcile as reconcile

    duplicate = (
        "distill",
        "test",
        "model",
        "b" * 64,
        "private prompt must not migrate",
        3,
        2,
        8,
        1,
        "2026-07-01T00:00:00+00:00",
    )
    unique = (
        "merge",
        "test",
        "model",
        "c" * 64,
        "another private prompt",
        5,
        1,
        9,
        1,
        "2026-07-01T00:01:00+00:00",
    )
    _create_legacy_prompt_table(tmp_path / "wiki_state.db", [duplicate, unique])
    _create_legacy_prompt_table(tmp_path / "prompt_calls.db", [duplicate])
    with sqlite3.connect(str(tmp_path / "prompt_calls.db")) as conn:
        conn.execute("CREATE TABLE prompt_call_stats (name TEXT PRIMARY KEY, value REAL)")

    plan, pending = reconcile.build_reconciliation_plan(FakeConfig(tmp_path))
    assert plan["legacy_source_row_count"] == 3
    assert plan["unique_legacy_call_count"] == 3
    assert plan["duplicate_legacy_row_count"] == 0
    assert plan["would_import_count"] == 0
    assert plan["unattributable_legacy_call_count"] == 3
    assert plan["requires_explicit_unattributable_discard"] is True
    assert pending == []

    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)
    result = _apply_reconciliation(
        reconcile,
        FakeConfig(tmp_path),
        apply=True,
        backup_dir=tmp_path / "backups",
        discard_unattributable_legacy=True,
    )

    assert result["ok"] is True
    assert result["imported_count"] == 0
    assert result["discarded_unattributable_source_count"] == 3
    assert len(result["backup"]) == 2
    assert not (tmp_path / "wiki_state.db").exists()
    assert not (tmp_path / "prompt_calls.db").exists()
    ledger = ModelCallLedger(tmp_path / "model_call_ledger.db")
    rows = ledger.recent(limit=10)
    assert rows == []
    with sqlite3.connect(str(ledger.db_path)) as conn:
        serialized = " ".join(
            str(value) for row in conn.execute("SELECT * FROM model_call_entries") for value in row
        )
    assert "private prompt" not in serialized
    assert result["post_apply"]["model_call_storage_path_count"] == 1
    assert result["post_apply"]["health_ledger_path_mismatch"] == 0
    assert result["post_apply"]["subject_attribution_schema_missing"] == 0


def test_cleanup_securely_removes_retired_prompt_bytes_when_database_is_retained(
    tmp_path, monkeypatch
):
    from core.migrations import model_call_ledger_reconcile as reconcile

    source = tmp_path / "wiki_state.db"
    secret = "RAW_LEGACY_PROMPT_" + ("x" * 12_000)
    _create_legacy_prompt_table(
        source,
        [
            (
                "distill", "test", "model", "d" * 64, secret, 3, 2, 8, 1,
                "2026-07-01T00:00:00+00:00",
            )
        ],
    )
    with sqlite3.connect(str(source)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE unrelated_state (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO unrelated_state VALUES ('keep', 'state')")
    conn.close()

    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)
    result = _apply_reconciliation(
        reconcile,
        FakeConfig(tmp_path),
        apply=True,
        backup_dir=tmp_path / "backups",
        discard_unattributable_legacy=True,
    )

    assert result["ok"] is True
    assert source.exists()
    with sqlite3.connect(str(source)) as conn:
        assert conn.execute("SELECT value FROM unrelated_state WHERE key='keep'").fetchone() == (
            "state",
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='prompt_calls'"
        ).fetchone() == (0,)
    for candidate in (
        source,
        Path(str(source) + "-wal"),
        Path(str(source) + "-shm"),
    ):
        if candidate.exists():
            assert secret.encode("utf-8") not in candidate.read_bytes()


def test_cleanup_fails_closed_when_an_active_wal_reader_prevents_private_scrub(
    tmp_path, monkeypatch
):
    from core.migrations import model_call_ledger_reconcile as reconcile

    source = tmp_path / "wiki_state.db"
    _create_legacy_prompt_table(
        source,
        [
            (
                "distill", "test", "model", "e" * 64, "private body", 3, 2, 8, 1,
                "2026-07-01T00:00:00+00:00",
            )
        ],
    )
    writer = sqlite3.connect(str(source))
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("UPDATE prompt_calls SET prompt_summary='private body retained in wal'")
    writer.commit()
    reader = sqlite3.connect(str(source))
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM prompt_calls").fetchall()
    writer.close()
    try:
        monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)
        result = _apply_reconciliation(
            reconcile,
            FakeConfig(tmp_path),
            apply=True,
            backup_dir=tmp_path / "backups",
            discard_unattributable_legacy=True,
        )
        assert result["ok"] is False
        assert result["error"] == "reconciliation_sqlite_error"
        with sqlite3.connect(str(source)) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='prompt_calls'"
            ).fetchone() == (1,)
    finally:
        reader.close()


def test_reconciliation_rekeys_raw_run_id_without_leaving_sqlite_slack(tmp_path, monkeypatch):
    from core.telemetry.prompt_call_log import ModelCallLedger
    from core.migrations import model_call_ledger_reconcile as reconcile

    config = FakeConfig(tmp_path)

    class PricedConfig(FakeConfig):
        def get(self, key, default=None):
            if key == "llm.provider_prices":
                return {"test": {"model": {"input": 0.1, "output": 0.2}}}
            return super().get(key, default)

    ledger = ModelCallLedger.for_config(PricedConfig(tmp_path))
    raw_run_id = "RAW_RUN_IDENTIFIER_" + ("y" * 12_000)
    canonical_run_id = ledger.start_run(
        raw_run_id, subject_scope=("session", "rekey-test")
    )
    reservation = ledger.reserve(
        run_id=canonical_run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.release()
    # Simulate the pre-rekey Phase-2 database: callers previously stored the
    # raw value in the parent and its FK child table.
    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "UPDATE model_call_run_subjects SET run_id=? WHERE run_id=?",
            (raw_run_id, canonical_run_id),
        )
        conn.execute(
            "UPDATE model_call_entries SET run_id=? WHERE run_id=?",
            (raw_run_id, canonical_run_id),
        )
        conn.execute(
            "UPDATE model_call_runs SET run_id=? WHERE run_id=?",
            (raw_run_id, canonical_run_id),
        )
    conn.close()

    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)
    result = _apply_reconciliation(
        reconcile, config, apply=True, backup_dir=tmp_path / "backups"
    )

    assert result["ok"] is True
    assert result["privacy_reconciliation"]["rekeyed_run_ids"] == 1
    with sqlite3.connect(str(ledger.db_path)) as conn:
        stored_run_id = conn.execute("SELECT run_id FROM model_call_runs").fetchone()[0]
    assert stored_run_id == canonical_run_id
    for candidate in (
        ledger.db_path,
        Path(str(ledger.db_path) + "-wal"),
        Path(str(ledger.db_path) + "-shm"),
    ):
        if candidate.exists():
            assert raw_run_id.encode("utf-8") not in candidate.read_bytes()


def test_reconciliation_scrubs_raw_legacy_tombstone_bytes_and_wal_sidecars(
    tmp_path, monkeypatch
):
    """A tombstone-only legacy DB still releases a caller-controlled run id."""
    from core.telemetry.prompt_call_log import ModelCallLedger
    from core.migrations import model_call_ledger_reconcile as reconcile

    config = FakeConfig(tmp_path)
    ledger = ModelCallLedger.for_config(config)
    raw_prefix = "RAW_LEGACY_TOMBSTONE_IDENTIFIER_"
    raw_run_id = raw_prefix + ("z" * 12_000)
    conn = sqlite3.connect(str(ledger.db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("PRAGMA secure_delete=OFF")
        conn.execute("DROP TABLE model_call_run_spend_tombstones")
        conn.execute(
            """
            CREATE TABLE model_call_run_spend_tombstones (
                run_id TEXT PRIMARY KEY,
                effective_cost REAL NOT NULL,
                deleted_entry_count INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO model_call_run_spend_tombstones VALUES (?, ?, ?, ?)",
            (raw_run_id, 0.1, 1, "2026-07-14T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    candidates = (
        ledger.db_path,
        Path(str(ledger.db_path) + "-wal"),
        Path(str(ledger.db_path) + "-shm"),
    )
    assert any(
        candidate.exists() and raw_prefix.encode("utf-8") in candidate.read_bytes()
        for candidate in candidates
    )

    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)
    result = _apply_reconciliation(
        reconcile, config, apply=True, backup_dir=tmp_path / "backups"
    )

    assert result["ok"] is True
    with sqlite3.connect(str(ledger.db_path)) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(model_call_run_spend_tombstones)")
        }
        stored_digest = conn.execute(
            "SELECT run_id_digest FROM model_call_run_spend_tombstones"
        ).fetchone()[0]
    assert columns == {"run_id_digest", "effective_cost", "deleted_entry_count", "updated_at"}
    assert len(stored_digest) == 64
    for candidate in candidates:
        if candidate.exists():
            assert raw_prefix.encode("utf-8") not in candidate.read_bytes()


def test_reconciliation_normalizes_raw_legacy_metadata_without_sqlite_slack(
    tmp_path, monkeypatch
):
    """Old caller/provider identifiers must not survive reconciliation bytes."""
    from core.migrations import model_call_ledger_reconcile as reconcile

    ledger = _seed_canonical_legacy_observation(tmp_path / "model_call_ledger.db")
    raw_prefix = "RAW_LEGACY_METADATA_IDENTIFIER_"
    raw_value = raw_prefix + ("m" * 12_000)
    conn = sqlite3.connect(str(ledger.db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("PRAGMA secure_delete=OFF")
        conn.execute(
            "UPDATE model_call_entries SET operation=?, provider=?, model=?, cache_status=?, "
            "provider_usage_id=?, request_id=?",
            (raw_value, raw_value, raw_value, raw_value, raw_value, raw_value),
        )
        conn.commit()
    finally:
        conn.close()

    candidates = (
        ledger.db_path,
        Path(str(ledger.db_path) + "-wal"),
        Path(str(ledger.db_path) + "-shm"),
    )
    assert any(
        candidate.exists() and raw_prefix.encode("utf-8") in candidate.read_bytes()
        for candidate in candidates
    )

    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)
    result = _apply_reconciliation(
        reconcile, FakeConfig(tmp_path), apply=True, backup_dir=tmp_path / "backups"
    )

    assert result["ok"] is True
    with sqlite3.connect(str(ledger.db_path)) as conn:
        row = conn.execute(
            "SELECT operation, provider, model, cache_status, provider_usage_id, request_id "
            "FROM model_call_entries"
        ).fetchone()
    assert row is not None
    assert raw_prefix not in " ".join(str(value) for value in row)
    for candidate in candidates:
        if candidate.exists():
            assert raw_prefix.encode("utf-8") not in candidate.read_bytes()


def test_model_call_ledger_backup_target_is_private_before_sqlite_opens(tmp_path, monkeypatch):
    """The raw SQLite destination must already be private at connect time."""
    source = tmp_path / "wiki_state.db"
    _create_legacy_prompt_table(
        source,
        [
            (
                "distill", "test", "model", "d" * 64, "private", 1, 1, 1, 1,
                "2026-07-01T00:00:00+00:00",
            )
        ],
    )
    backup_root = tmp_path / "backups"
    real_connect = backup.sqlite3.connect
    seen_modes: list[tuple[int, int]] = []

    def observe_target_mode(database, *args, **kwargs):
        candidate = Path(str(database))
        if candidate.parent == backup_root and ".pre-model-call-ledger." in candidate.name:
            seen_modes.append(
                (
                    stat.S_IMODE(candidate.stat().st_mode),
                    stat.S_IMODE(candidate.parent.stat().st_mode),
                )
            )
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(backup.sqlite3, "connect", observe_target_mode)

    backups = backup.create_sqlite_backups([source], backup_root)

    assert seen_modes == [(0o600, 0o700)]
    assert stat.S_IMODE(backup_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(Path(backups[0]["path"]).stat().st_mode) == 0o600


def test_model_call_ledger_backup_cleans_target_when_private_mode_setup_fails(tmp_path, monkeypatch):
    from core.migrations import model_call_ledger_reconcile as reconcile

    source = tmp_path / "wiki_state.db"
    _create_legacy_prompt_table(
        source,
        [
            (
                "distill", "test", "model", "f" * 64, "private", 1, 1, 1, 1,
                "2026-07-01T00:00:00+00:00",
            )
        ],
    )

    def deny_fchmod(_descriptor, _mode):
        raise PermissionError("test permission failure")

    monkeypatch.setattr(backup.os, "fchmod", deny_fchmod)
    backup_root = tmp_path / "backups"

    with pytest.raises(reconcile.ModelCallLedgerReconcileError, match="sqlite_backup_io_error"):
        backup.create_sqlite_backups([source], backup_root)

    assert stat.S_IMODE(backup_root.stat().st_mode) == 0o700
    assert list(backup_root.iterdir()) == []


def test_model_call_ledger_plan_rejects_repeated_same_physical_source_record(tmp_path, monkeypatch):
    """Only an exact source-db/table/row identity is allowed to collide."""
    from core.migrations import model_call_ledger_reconcile as reconcile

    source = tmp_path / "wiki_state.db"
    _create_legacy_prompt_table(
        source,
        [
            (
                "distill", "test", "model", "e" * 64, "private", 1, 1, 1, 1,
                "2026-07-01T00:00:00+00:00",
            )
        ],
    )
    report, records = inventory._source_snapshot(source)
    assert len(records) == 1
    original = records[0]
    changed_metadata = reconcile.HistoricalCall(
        source_db=original.source_db,
        source_generation=original.source_generation,
        source_table=original.source_table,
        source_rowid=original.source_rowid,
        operation="different-operation",
        provider=original.provider,
        model=original.model,
        input_digest=original.input_digest,
        input_tokens=original.input_tokens + 1,
        output_tokens=original.output_tokens,
        latency_ms=original.latency_ms,
        success=original.success,
        created_at=original.created_at,
        fingerprint="sha256:" + "f" * 64,
        subject_scope=original.subject_scope,
    )
    snapshots = iter(([original], [changed_metadata], [], []))

    def repeat_same_source_record(_path):
        return dict(report), list(next(snapshots))

    monkeypatch.setattr(planner, "_source_snapshot", repeat_same_source_record)

    with pytest.raises(reconcile.ModelCallLedgerReconcileError, match="duplicate_source_record_identity"):
        reconcile.build_reconciliation_plan(FakeConfig(tmp_path))


def test_model_call_ledger_reconciliation_refuses_apply_while_daemon_active(tmp_path, monkeypatch):
    from core.migrations import model_call_ledger_reconcile as reconcile

    _create_legacy_prompt_table(
        tmp_path / "wiki_state.db",
        [
            (
                "distill", "test", "model", "d" * 64, "private", 1, 1, 1, 1,
                "2026-07-01T00:00:00+00:00",
            )
        ],
    )
    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: False)

    result = _apply_reconciliation(
        reconcile,
        FakeConfig(tmp_path),
        apply=True,
        backup_dir=tmp_path / "backups",
        discard_unattributable_legacy=True,
    )

    assert result["ok"] is False
    assert result["error"] == "daemon_not_inactive"
    assert (tmp_path / "wiki_state.db").exists()
    assert not (tmp_path / "model_call_ledger.db").exists()


def test_model_call_ledger_reconciliation_upgrades_subject_attribution_with_backup(
    tmp_path, monkeypatch
):
    from core.telemetry.prompt_call_log import ModelCallLedger
    from core.migrations import model_call_ledger_reconcile as reconcile

    ledger = ModelCallLedger(tmp_path / "model_call_ledger.db")
    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.execute("DROP TABLE model_call_run_subjects")

    plan, _ = reconcile.build_reconciliation_plan(FakeConfig(tmp_path))

    assert plan["status"] == "reconciliation_required"
    assert plan["canonical_state"] == "privacy_reconciliation_required"
    assert plan["privacy_reconciliation_required"] is True

    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)
    result = _apply_reconciliation(
        reconcile,
        FakeConfig(tmp_path),
        apply=True,
        backup_dir=tmp_path / "backups",
    )

    assert result["ok"] is True
    assert result["status"] == "applied"
    assert result["post_apply"]["subject_attribution_schema_missing"] == 0
    with sqlite3.connect(str(ledger.db_path)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "model_call_run_subjects" in tables


def test_model_call_ledger_reconciliation_preserves_unknown_source_tables(tmp_path, monkeypatch):
    from core.migrations import model_call_ledger_reconcile as reconcile

    _create_legacy_prompt_table(
        tmp_path / "wiki_state.db",
        [
            (
                "distill", "test", "model", "e" * 64, "private", 1, 1, 1, 1,
                "2026-07-01T00:00:00+00:00",
            )
        ],
    )
    with sqlite3.connect(str(tmp_path / "wiki_state.db")) as conn:
        conn.execute("CREATE TABLE unrelated_state (id INTEGER PRIMARY KEY)")
    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)

    result = _apply_reconciliation(
        reconcile,
        FakeConfig(tmp_path),
        apply=True,
        backup_dir=tmp_path / "backups",
        discard_unattributable_legacy=True,
    )

    assert result["ok"] is True
    assert (tmp_path / "wiki_state.db").exists()
    with sqlite3.connect(str(tmp_path / "wiki_state.db")) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"unrelated_state"}


def test_model_call_ledger_reconciliation_blocks_structural_drift_after_backup(
    tmp_path, monkeypatch
):
    """A post-backup owner-table change must never be cleaned without a new backup."""
    from core.migrations import model_call_ledger_reconcile as reconcile

    source = tmp_path / "wiki_state.db"
    _create_legacy_prompt_table(
        source,
        [
            (
                "distill", "test", "model", "f" * 64, "private", 1, 1, 1, 1,
                "2026-07-01T00:00:00+00:00",
            )
        ],
    )
    original_backup = executor.create_sqlite_backups

    def backup_then_add_retired_stats(paths, backup_dir, **kwargs):
        backups = original_backup(paths, backup_dir, **kwargs)
        with sqlite3.connect(str(source)) as conn:
            conn.execute("CREATE TABLE prompt_call_stats (name TEXT PRIMARY KEY, value REAL)")
        return backups

    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)
    monkeypatch.setattr(executor, "create_sqlite_backups", backup_then_add_retired_stats)

    result = _apply_reconciliation(
        reconcile,
        FakeConfig(tmp_path),
        apply=True,
        backup_dir=tmp_path / "backups",
        discard_unattributable_legacy=True,
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["error"] == "source_drift_after_backup"
    assert source.exists()
    assert not (tmp_path / "model_call_ledger.db").exists()


def test_unattributable_source_records_require_explicit_discard_and_never_leak_subject(tmp_path, monkeypatch):
    from core.migrations import model_call_ledger_reconcile as reconcile

    raw_payload = "unattributable-private-source"
    _create_legacy_prompt_table(
        tmp_path / "wiki_state.db",
        [
            (
                "distill", "test", "model", "1" * 64, raw_payload, 1, 1, 1, 1,
                "2026-07-01T00:00:00+00:00",
            )
        ],
        session_id="",
    )
    config = FakeConfig(tmp_path)
    plan, pending = reconcile.build_reconciliation_plan(config)

    assert pending == []
    assert plan["unattributable_legacy_call_count"] == 1
    assert plan["requires_explicit_unattributable_discard"] is True
    assert raw_payload not in json.dumps(plan, ensure_ascii=False)

    blocked = _apply_reconciliation(
        reconcile,
        config,
        apply=True,
        backup_dir=tmp_path / "backups",
    )
    assert blocked["error"] == "unattributable_legacy_requires_explicit_discard"
    assert (tmp_path / "wiki_state.db").exists()
    assert not (tmp_path / "model_call_ledger.db").exists()

    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)
    applied = _apply_reconciliation(
        reconcile,
        config,
        apply=True,
        backup_dir=tmp_path / "backups",
        discard_unattributable_legacy=True,
    )
    assert applied["status"] == "applied"
    assert applied["discarded_unattributable_source_count"] == 1
    assert applied["imported_count"] == 0
    assert not (tmp_path / "wiki_state.db").exists()


def test_unmapped_canonical_legacy_rows_require_discard_not_fingerprint_subject(tmp_path, monkeypatch):
    from core.migrations import model_call_ledger_reconcile as reconcile

    ledger = _seed_canonical_legacy_observation(tmp_path / "model_call_ledger.db")
    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.execute("DELETE FROM model_call_entry_subjects")
        conn.execute("DELETE FROM model_call_run_subjects")

    config = FakeConfig(tmp_path)
    plan, _ = reconcile.build_reconciliation_plan(config)
    assert plan["canonical_state"] == "unattributable_legacy_required"
    assert plan["canonical_privacy_counts"]["canonical_unattributable_legacy_count"] == 1
    assert plan["requires_explicit_unattributable_discard"] is True

    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)
    blocked = _apply_reconciliation(
        reconcile,
        config,
        apply=True,
        backup_dir=tmp_path / "backups",
    )
    assert blocked["status"] == "blocked"
    assert blocked["error"] == "unattributable_legacy_requires_explicit_discard"

    applied = _apply_reconciliation(
        reconcile,
        config,
        apply=True,
        backup_dir=tmp_path / "backups",
        discard_unattributable_legacy=True,
    )
    assert applied["status"] == "applied"
    assert applied["privacy_reconciliation"]["discarded_unattributable_legacy_entries"] == 1
    with sqlite3.connect(str(ledger.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_call_entries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM model_call_runs").fetchone()[0] == 0


def test_reconcile_never_promotes_a_run_root_to_an_entry_level_subject_map(tmp_path, monkeypatch):
    from core.telemetry.prompt_call_log import ModelCallLedger
    from core.migrations import model_call_ledger_reconcile as reconcile

    ledger = _seed_canonical_legacy_observation(tmp_path / "model_call_ledger.db")
    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.execute("DELETE FROM model_call_entry_subjects")

    # No public core migration method is permitted: the registered command
    # must prove daemon inactivity and create the private recovery backup.
    assert not hasattr(ModelCallLedger, "reconcile_privacy_schema")
    monkeypatch.setattr(runtime, "runtime_writers_are_inactive", lambda _directory: True)
    blocked = _apply_reconciliation(
        reconcile,
        FakeConfig(tmp_path),
        apply=True,
        backup_dir=tmp_path / "backups",
    )
    assert blocked["error"] == "unattributable_legacy_requires_explicit_discard"
    with sqlite3.connect(str(ledger.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_call_entry_subjects").fetchone()[0] == 0

    applied = _apply_reconciliation(
        reconcile,
        FakeConfig(tmp_path),
        apply=True,
        backup_dir=tmp_path / "backups",
        discard_unattributable_legacy=True,
    )
    assert applied["privacy_reconciliation"]["discarded_unattributable_legacy_entries"] == 1
    with sqlite3.connect(str(ledger.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_call_entries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM model_call_runs").fetchone()[0] == 0


def test_reconcile_preflight_rejects_unsupported_base_schema_without_partial_upgrade(tmp_path):
    from core.telemetry.prompt_call_log import ModelCallLedger, ModelCallLedgerInvariantError

    ledger = ModelCallLedger(tmp_path / "model_call_ledger.db")
    with sqlite3.connect(str(ledger.db_path)) as conn:
        entry_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='model_call_entries'"
            ).fetchone()[0]
        )
        for table in (
            "model_call_run_spend_tombstones",
            "model_call_daily_spend_tombstones",
            "model_call_frozen_subjects",
            "model_call_entry_subjects",
            "model_call_run_subjects",
        ):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("DROP TABLE model_call_entries")
        unsupported_sql = entry_sql.replace(
            "                    metered_usage_receipt TEXT NOT NULL DEFAULT '',\n", ""
        ).replace(
            "                    request_dispatched INTEGER NOT NULL DEFAULT 0,\n", ""
        )
        assert unsupported_sql != entry_sql
        conn.execute(unsupported_sql)

    session = _backup_gated_session(ledger.db_path, tmp_path / "backups")
    try:
        with pytest.raises(ModelCallLedgerInvariantError, match="request_dispatched"):
            session.reconcile_privacy_schema()
    finally:
        session.close()

    with sqlite3.connect(str(ledger.db_path)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {row[1] for row in conn.execute("PRAGMA table_info(model_call_entries)")}
    assert tables == {"model_call_runs", "model_call_entries"}
    assert "metered_usage_receipt" not in columns
    assert "request_dispatched" not in columns


def test_backup_gated_reconciliation_rewrites_old_raw_run_tombstones(tmp_path):
    from core.telemetry.prompt_call_log import ModelCallLedger, ModelCallLedgerInvariantError

    ledger = ModelCallLedger(tmp_path / "model_call_ledger.db")
    private_run_id = "legacy-person@example.invalid"
    canonical_run_id = ledger.start_run(
        private_run_id, subject_scope=("session", "legacy-tombstone")
    )
    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DROP TABLE model_call_run_spend_tombstones")
        conn.execute(
            """
            CREATE TABLE model_call_run_spend_tombstones (
                run_id TEXT PRIMARY KEY,
                effective_cost REAL NOT NULL,
                deleted_entry_count INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES model_call_runs(run_id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "INSERT INTO model_call_run_spend_tombstones VALUES (?, ?, ?, ?)",
            (canonical_run_id, 0.1, 1, "2026-07-14T00:00:00+00:00"),
        )
        # Simulate the retired cascading behavior: the only evidence for a
        # formerly deleted run is already gone before reconciliation begins.
        conn.execute("DELETE FROM model_call_runs WHERE run_id=?", (canonical_run_id,))

    with pytest.raises(ModelCallLedgerInvariantError, match="registered pre-backup proof"):
        LedgerReconciliation.open_after_verified_backup(object(), None)

    session = _backup_gated_session(ledger.db_path, tmp_path / "backups")
    try:
        with pytest.raises(ModelCallLedgerInvariantError, match="unrecoverable-history disposition"):
            session.reconcile_privacy_schema()
        result = session.reconcile_privacy_schema(
            discard_unrecoverable_run_tombstone_history=True,
        )
    finally:
        session.close()
    assert result["created_schema"] == 0
    with sqlite3.connect(str(ledger.db_path)) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(model_call_run_spend_tombstones)")
        }
        row = conn.execute("SELECT * FROM model_call_run_spend_tombstones").fetchone()
        serialized = "" if row is None else " ".join(str(value) for value in row)
    assert columns == {"run_id_digest", "effective_cost", "deleted_entry_count", "updated_at"}
    assert private_run_id not in serialized
    assert row is None
    repaired = ModelCallLedger(ledger.db_path)
    # The lost identifier cannot be reconstructed, so normal runtime can
    # proceed only with a permanent release-ineligibility disposition exposed.
    repaired.start_run(private_run_id, subject_scope=("session", "new-subject"))
    inspection = ModelCallLedger.inspect(FakeConfig(tmp_path))
    assert inspection["unrecoverable_run_tombstone_history_disposition"] == 1
    assert inspection["status"] == "degraded"
