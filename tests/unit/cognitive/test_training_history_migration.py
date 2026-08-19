from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

import core.cognitive.training_history_migration as training_history_migration
from core.cognitive.state_contract import sha256_json
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.training_governance import TrainingGovernanceStore
from core.cognitive.training_governance_audit import (
    audit_training_governance,
    audit_training_governance_static,
)
from core.cognitive.training_history_migration import (
    TRAINING_HISTORY_REASON_CODE,
    build_training_history_inventory,
    inspect_training_history_coverage,
    public_training_inventory_report,
    reconcile_training_history,
    restore_training_history,
)
from core.cognitive.training_migration_barrier import (
    activate_training_migration_barrier,
    deactivate_training_migration_barrier,
)
from core.cognitive.training_governance_static_audit import (
    audit_retired_training_surfaces,
)
from core.migrations.model_call_ledger_reconcile.runtime import (
    is_mnemos_runtime_process,
)
from core.scoring.training_schema import (
    initialize_training_schema,
    inspect_training_schema,
)


REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _isolate_runtime_process_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Migration behavior tests must not depend on workstation runtimes."""

    monkeypatch.setattr(training_history_migration, "_runtime_is_active", lambda: False)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _initialize_fixture(root: Path) -> None:
    target = root / "producer_consumer_ledger.db"
    with sqlite3.connect(target) as conn:
        initialize_cognitive_state_schema(conn)
        prior_payload = {
            "schema_version": "mnemos.historical_unattributed_feedback.v1",
            "source_identity": {
                "database_class": "scoring",
                "table": "ground_truth_signals",
                "primary_key": {"id": 1},
            },
        }
        conn.execute(
            """
            INSERT INTO cognitive_state_migration_quarantine (
                quarantine_id, source_table, source_key, reason_code,
                field_manifest, payload_json, payload_hash, created_at
            ) VALUES (?, ?, ?, ?, '[]', ?, ?, ?)
            """,
            (
                "prior-feedback-ground-truth-1",
                "feedback_history.scoring.ground_truth_signals",
                "feedback-history:test-ground-truth-1",
                "historical_unattributed_feedback",
                json.dumps(prior_payload, sort_keys=True),
                sha256_json(prior_payload),
                "2026-07-19T00:00:00+00:00",
            ),
        )
        conn.commit()

    with sqlite3.connect(root / "mnemos.db") as conn:
        conn.execute(
            """
            CREATE TABLE ground_truth_signals (
                id INTEGER PRIMARY KEY,
                profile_id TEXT,
                session_id TEXT,
                signal_type TEXT,
                signal_value TEXT,
                confidence REAL,
                latency_hours INTEGER,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE scorer_models (
                id INTEGER PRIMARY KEY,
                dimension TEXT,
                model_version TEXT,
                model_type TEXT,
                model_blob BLOB,
                model_hash TEXT,
                train_samples INTEGER,
                is_active INTEGER,
                created_at TEXT,
                meta_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO ground_truth_signals
            VALUES (1, NULL, 's1', 'kg', '1', 1.0, 0, '2026-07-19')
            """
        )
        conn.execute(
            """
            INSERT INTO scorer_models
            VALUES (
                1, 'kg', 'legacy-v1', 'json', X'00', 'sha256:legacy',
                1, 0, '2026-07-19', '{}'
            )
            """
        )
        conn.commit()

    with sqlite3.connect(root / "rule_weight_optimizer.db") as conn:
        conn.execute(
            """
            CREATE TABLE rule_outcomes (
                id INTEGER PRIMARY KEY,
                rule_name TEXT,
                predicted_score REAL,
                actual_label INTEGER,
                created_at TEXT,
                source_event_id TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO rule_outcomes
            VALUES (1, 'legacy-rule', 0.8, 1, '2026-07-19', 'event-1')
            """
        )
        conn.commit()

    with sqlite3.connect(root / "rule_weights.db") as conn:
        conn.execute(
            """
            CREATE TABLE rule_weights (
                rule_name TEXT PRIMARY KEY,
                weight REAL,
                updated_at TEXT
            )
            """
        )
        conn.execute("INSERT INTO rule_weights VALUES ('legacy-rule', 1.2, '2026-07-19')")
        conn.commit()


def test_dry_run_is_read_only_and_content_safe(tmp_path: Path) -> None:
    _initialize_fixture(tmp_path)
    before = {path.name: _file_hash(path) for path in sorted(tmp_path.glob("*.db"))}

    inventory = build_training_history_inventory(tmp_path)
    report = public_training_inventory_report(
        inventory,
        target_db=tmp_path / "producer_consumer_ledger.db",
    )
    after = {path.name: _file_hash(path) for path in sorted(tmp_path.glob("*.db"))}

    assert before == after
    assert inventory["object_count"] == 4
    assert inventory["active_legacy_model_count"] == 0
    assert report["coverage"]["uncovered"] == 4
    assert report["sensitive_bytes_in_report"] == 0
    assert "legacy-rule" not in json.dumps(report)


def test_independent_strict_audit_rejects_completely_uninitialized_state(
    tmp_path: Path,
) -> None:
    report = audit_training_governance(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
    )

    assert report["ok"] is False
    assert report["metrics"]["phase3_training_contract_gap"] >= 1
    assert report["historical_inventory"]["status"] == "not_initialized"
    assert report["denominators"]["historical_objects"] == 0
    assert report["denominators"]["admissions"] == 0


def test_static_only_audit_accepts_repo_without_runtime_databases(
    tmp_path: Path,
) -> None:
    report = audit_training_governance_static(repo_root=REPO_ROOT)

    assert not list(tmp_path.iterdir())
    assert report["audit_mode"] == "static_only"
    assert report["ok"] is True
    assert set(report["metrics"].values()) == {0}


@pytest.mark.parametrize(
    ("name", "cmdline", "expected"),
    (
        ("python3", ("python3", "worker.py"), False),
        ("python3", ("python3", "/opt/mnemos/mnemos_daemon.py"), True),
        ("python3", ("python3", "/opt/mnemos/mnemos_cli.py", "mcp", "serve"), True),
    ),
)
def test_runtime_detection_is_cross_platform_and_command_specific(
    name: str,
    cmdline: tuple[str, ...],
    expected: bool,
) -> None:
    assert is_mnemos_runtime_process(name=name, cmdline=cmdline) is expected


def test_static_audit_does_not_whitelist_an_entire_historical_owner_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "core" / "cognitive" / "training_history_migration.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "def rogue_reader(conn):\n"
        "    return conn.execute('SELECT * FROM scorer_training_queue').fetchall()\n",
        encoding="utf-8",
    )
    (tmp_path / "daemon").mkdir()

    report = audit_retired_training_surfaces(tmp_path)

    assert report["legacy_sql_sites"] == [
        "core/cognitive/training_history_migration.py:2:scorer_training_queue"
    ]


def test_static_audit_resolves_constant_sql_concatenation(tmp_path: Path) -> None:
    path = tmp_path / "core" / "rogue_training_reader.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "def read_legacy(conn):\n"
        "    table = 'scorer_training_queue'\n"
        "    return conn.execute('SELECT * FROM ' + table).fetchall()\n",
        encoding="utf-8",
    )
    (tmp_path / "daemon").mkdir()

    report = audit_retired_training_surfaces(tmp_path)

    assert report["legacy_sql_sites"] == ["core/rogue_training_reader.py:3:scorer_training_queue"]


@pytest.mark.parametrize(
    ("original", "replacement", "expected_gap"),
    (
        (
            "        _reject_retired_training('load_model')\n",
            "        return None\n",
            "core/scoring/adaptive_scorer_v2.py:AdaptiveScorerV2.load_model:"
            "negative_contract_mismatch",
        ),
        (
            "        _reject_retired_training('insert_ground_truth')\n",
            "        return None\n",
            "core/scoring/adaptive_scorer_v2.py:AdaptiveScorerV2.insert_ground_truth:"
            "negative_contract_mismatch",
        ),
        (
            "    raise PermissionError(f'{LEGACY_TRAINING_ERROR}:{operation}')\n",
            "    print('side effect')\n"
            "    raise PermissionError(f'{LEGACY_TRAINING_ERROR}:{operation}')\n",
            "core/scoring/adaptive_scorer_v2.py:_reject_retired_training:"
            "negative_contract_mismatch",
        ),
    ),
)
def test_permanent_fail_closed_boundaries_are_exact_negative_contracts(
    tmp_path: Path,
    original: str,
    replacement: str,
    expected_gap: str,
) -> None:
    path = tmp_path / "core" / "scoring" / "adaptive_scorer_v2.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "LEGACY_TRAINING_ERROR = 'training_admission_receipt_required'\n"
        "def _reject_retired_training(operation):\n"
        "    raise PermissionError(f'{LEGACY_TRAINING_ERROR}:{operation}')\n"
        "class AdaptiveScorerV2:\n"
        "    def load_model(self, dimension, version=None):\n"
        "        del dimension, version\n"
        "        _reject_retired_training('load_model')\n"
        "    @classmethod\n"
        "    def insert_ground_truth(cls, session_id, signal_type, label):\n"
        "        del cls, session_id, signal_type, label\n"
        "        _reject_retired_training('insert_ground_truth')\n",
        encoding="utf-8",
    )
    (tmp_path / "daemon").mkdir()

    baseline = audit_retired_training_surfaces(tmp_path)
    assert baseline["fail_closed_boundary_gaps"] == []

    path.write_text(
        path.read_text(encoding="utf-8").replace(original, replacement),
        encoding="utf-8",
    )
    mutated = audit_retired_training_surfaces(tmp_path)

    assert mutated["fail_closed_boundary_gaps"] == [expected_gap]


def test_apply_replay_restore_and_reapply_are_object_exact(
    tmp_path: Path,
) -> None:
    _initialize_fixture(tmp_path)
    inventory = build_training_history_inventory(tmp_path)

    applied = reconcile_training_history(
        database_dir=tmp_path,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "backup-first",
        repo_root=REPO_ROOT,
    )
    assert applied["effect"] == {"inserted": 4, "existing": 0}
    assert applied["activation_marker"] == {"inserted": 1, "existing": 0}
    assert applied["coverage"]["covered"] == 4
    assert applied["coverage"]["prior_feedback_links"] == 1
    assert applied["coverage"]["activation_marker_valid"] is True
    assert applied["governed_state_counts"] == {
        "training_revisions": 0,
        "training_heads": 0,
        "training_projection_rows": 0,
    }
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        assert inspect_training_schema(conn).ok is True
    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        assert (
            conn.execute(
                """
            SELECT COUNT(*) FROM cognitive_state_migration_quarantine
            WHERE reason_code=?
            """,
                (TRAINING_HISTORY_REASON_CODE,),
            ).fetchone()[0]
            == 4
        )

    replay = reconcile_training_history(
        database_dir=tmp_path,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "backup-replay",
        repo_root=REPO_ROOT,
    )
    assert replay["effect"] == {"inserted": 0, "existing": 4}
    assert replay["activation_marker"] == {"inserted": 0, "existing": 1}

    restored = restore_training_history(
        database_dir=tmp_path,
        restore_manifest=Path(applied["backup_manifest"]),
    )
    assert restored["status"] == "restored"
    restored_inventory = build_training_history_inventory(tmp_path)
    assert restored_inventory["inventory_hash"] == inventory["inventory_hash"]
    coverage = inspect_training_history_coverage(
        tmp_path / "producer_consumer_ledger.db",
        restored_inventory,
    )
    assert coverage["uncovered"] == 4
    assert coverage["activation_marker_valid"] is False
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        assert inspect_training_schema(conn).classification == "absent"

    reapplied = reconcile_training_history(
        database_dir=tmp_path,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "backup-reapply",
        repo_root=REPO_ROOT,
    )
    assert reapplied["effect"] == {"inserted": 4, "existing": 0}
    exact_replay = reconcile_training_history(
        database_dir=tmp_path,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "backup-exact-replay",
        repo_root=REPO_ROOT,
    )
    assert exact_replay["effect"] == {"inserted": 0, "existing": 4}


def test_apply_rejects_inventory_drift_before_mutation(tmp_path: Path) -> None:
    _initialize_fixture(tmp_path)
    inventory = build_training_history_inventory(tmp_path)
    with sqlite3.connect(tmp_path / "rule_weights.db") as conn:
        conn.execute("INSERT INTO rule_weights VALUES ('drift', 1.0, '2026-07-19')")
        conn.commit()

    with pytest.raises(RuntimeError, match="inventory hash drift"):
        reconcile_training_history(
            database_dir=tmp_path,
            expected_inventory_hash=inventory["inventory_hash"],
            expected_object_manifest_hash=inventory["object_manifest_hash"],
            backup_dir=tmp_path / "drift-backup",
            repo_root=REPO_ROOT,
        )
    assert not (tmp_path / "drift-backup").exists()


def test_apply_rejects_active_mnemos_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize_fixture(tmp_path)
    inventory = build_training_history_inventory(tmp_path)
    monkeypatch.setattr(training_history_migration, "_runtime_is_active", lambda: True)

    with pytest.raises(RuntimeError, match="must be inactive"):
        reconcile_training_history(
            database_dir=tmp_path,
            expected_inventory_hash=inventory["inventory_hash"],
            expected_object_manifest_hash=inventory["object_manifest_hash"],
            backup_dir=tmp_path / "active-runtime-backup",
            repo_root=REPO_ROOT,
        )

    assert not (tmp_path / "active-runtime-backup").exists()


def test_apply_failure_restores_all_databases_before_releasing_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize_fixture(tmp_path)
    inventory = build_training_history_inventory(tmp_path)
    before = {
        database_class: training_history_migration._database_logical_hash(path)  # noqa: SLF001
        for database_class, path in training_history_migration._database_map(  # noqa: SLF001
            tmp_path
        ).items()
    }

    def fail_after_schema(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("injected activation failure")

    monkeypatch.setattr(
        training_history_migration,
        "_append_activation_marker",
        fail_after_schema,
    )
    with pytest.raises(RuntimeError, match="injected activation failure"):
        reconcile_training_history(
            database_dir=tmp_path,
            expected_inventory_hash=inventory["inventory_hash"],
            expected_object_manifest_hash=inventory["object_manifest_hash"],
            backup_dir=tmp_path / "rollback-backup",
            repo_root=REPO_ROOT,
        )

    after = {
        database_class: training_history_migration._database_logical_hash(path)  # noqa: SLF001
        for database_class, path in training_history_migration._database_map(  # noqa: SLF001
            tmp_path
        ).items()
    }
    assert after == before
    assert not (tmp_path / ".training_governance_migration_barrier.json").exists()
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        assert inspect_training_schema(conn).classification == "absent"


def test_restore_failure_rolls_back_every_database_before_releasing_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize_fixture(tmp_path)
    inventory = build_training_history_inventory(tmp_path)
    applied = reconcile_training_history(
        database_dir=tmp_path,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "restore-source-backup",
        repo_root=REPO_ROOT,
    )
    post_apply = {
        database_class: training_history_migration._database_logical_hash(path)  # noqa: SLF001
        for database_class, path in training_history_migration._database_map(  # noqa: SLF001
            tmp_path
        ).items()
    }
    original_restore = training_history_migration._restore_database  # noqa: SLF001
    calls = 0

    def fail_second_restore(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected restore failure")
        return original_restore(*args, **kwargs)

    monkeypatch.setattr(
        training_history_migration,
        "_restore_database",
        fail_second_restore,
    )
    with pytest.raises(RuntimeError, match="injected restore failure"):
        restore_training_history(
            database_dir=tmp_path,
            restore_manifest=Path(applied["backup_manifest"]),
        )

    after = {
        database_class: training_history_migration._database_logical_hash(path)  # noqa: SLF001
        for database_class, path in training_history_migration._database_map(  # noqa: SLF001
            tmp_path
        ).items()
    }
    assert calls == 6
    assert after == post_apply
    assert not (tmp_path / ".training_governance_migration_barrier.json").exists()


def test_restore_rollback_failure_retains_barrier_and_recovery_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize_fixture(tmp_path)
    inventory = build_training_history_inventory(tmp_path)
    applied = reconcile_training_history(
        database_dir=tmp_path,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "restore-failclosed-backup",
        repo_root=REPO_ROOT,
    )
    original_restore = training_history_migration._restore_database  # noqa: SLF001
    calls = 0

    def fail_forward_and_rollback(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls in {2, 3}:
            raise RuntimeError("injected rollback failure")
        return original_restore(*args, **kwargs)

    monkeypatch.setattr(
        training_history_migration,
        "_restore_database",
        fail_forward_and_rollback,
    )
    with pytest.raises(RuntimeError, match="barrier remains active"):
        restore_training_history(
            database_dir=tmp_path,
            restore_manifest=Path(applied["backup_manifest"]),
        )

    assert (tmp_path / ".training_governance_migration_barrier.json").is_file()
    recovery_manifests = list(
        (tmp_path / "restore-failclosed-backup").glob(
            "restore-recovery-*/training-history-manifest.*.json"
        )
    )
    assert len(recovery_manifests) == 1


def test_training_store_honors_migration_barrier(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        initialize_cognitive_state_schema(conn)
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        initialize_training_schema(conn)
    store = TrainingGovernanceStore(
        CognitiveStateStore(tmp_path / "producer_consumer_ledger.db"),
        database_dir=tmp_path,
    )
    barrier = activate_training_migration_barrier(
        tmp_path,
        inventory_hash="sha256:" + "1" * 64,
    )
    try:
        with pytest.raises(
            RuntimeError,
            match="training_governance_migration_in_progress",
        ):
            store.reconcile_pending(1)
    finally:
        deactivate_training_migration_barrier(
            tmp_path,
            owner_id=barrier.owner_id,
        )


def test_independent_strict_audit_passes_migrated_cold_state(
    tmp_path: Path,
) -> None:
    _initialize_fixture(tmp_path)
    inventory = build_training_history_inventory(tmp_path)
    reconcile_training_history(
        database_dir=tmp_path,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "audit-backup",
        repo_root=REPO_ROOT,
    )

    report = audit_training_governance(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
    )

    assert report["ok"] is True
    assert set(report["metrics"].values()) == {0}
    assert report["denominators"]["historical_objects"] == 4
    assert report["denominators"]["admissions"] == 0
    assert report["historical_coverage"]["activation_marker_valid"] is True


def test_independent_audit_detects_unreceipted_projection(
    tmp_path: Path,
) -> None:
    _initialize_fixture(tmp_path)
    inventory = build_training_history_inventory(tmp_path)
    reconcile_training_history(
        database_dir=tmp_path,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "audit-gap-backup",
        repo_root=REPO_ROOT,
    )
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        conn.execute(
            """
            INSERT INTO governed_training_samples (
                sample_id, admission_revision_id, admission_payload_hash,
                dimension, metric_id, feature_snapshot_json,
                feature_snapshot_hash, label_numeric, label_value,
                dataset_group_id, dataset_group_hash, dataset_split,
                access_control_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "training-sample-unreceipted",
                "cogrev-" + "1" * 32,
                "sha256:" + "1" * 64,
                "predictive_delivery",
                "predictive_delivery_usefulness",
                "{}",
                "sha256:" + "2" * 64,
                1,
                "useful",
                "group-unreceipted",
                "sha256:" + "3" * 64,
                "train",
                "sha256:" + "4" * 64,
                "2026-07-19T00:00:00+00:00",
            ),
        )
        conn.commit()

    report = audit_training_governance(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
    )

    assert report["ok"] is False
    assert report["metrics"]["training_effect_without_receipt"] == 1
    assert report["metrics"]["phase3_training_contract_gap"] >= 1
