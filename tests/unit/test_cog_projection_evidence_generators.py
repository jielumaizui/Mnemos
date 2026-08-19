from __future__ import annotations

from argparse import Namespace
import base64
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import generate_cog009_gap_evidence as cog009
from scripts import generate_cog026_projection_plan_evidence as cog026
from scripts import generate_phase0_governance_contracts as governance
from scripts import generate_phase1_baseline_execution_evidence as phase1_evidence
from scripts import refresh_phase1_deep_audit_governance as phase1_refresh


def test_cog009_generator_rejects_snapshot_replacement_during_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "checkpoint.db"
    db_path.write_bytes(b"snapshot-a")
    expected_hash = hashlib.sha256(b"snapshot-a").hexdigest()

    def replace_snapshot(**_kwargs):
        db_path.write_bytes(b"snapshot-b")
        return {
            "schema_version": "test",
            "missing_event_ids": 1,
            "unexpected_event_ids": 0,
            "structural_error_count": 0,
            "gap_generation": {
                "evidence_epoch_stable": True,
                "gap_hash": "a" * 64,
            },
        }

    monkeypatch.setattr(cog009, "audit_raw_projection_fidelity", replace_snapshot)
    with pytest.raises(ValueError, match="snapshot changed"):
        cog009.build_artifact(
            raw_dir=tmp_path / "raw",
            db_path=db_path,
            canonical_db_identity=tmp_path / "canonical.db",
            expected_snapshot_sha256=expected_hash,
            expected_missing=1,
            expected_unexpected=0,
            expected_structural_errors=0,
            expected_reference_mismatches=0,
            expected_metric_aggregate_mismatches=0,
            expected_gap_hash="a" * 64,
            expected_evidence_decoded_sha256="b" * 64,
        )

    db_path.write_bytes(b"snapshot-a")
    exact_evidence = {
        "missing_revision_evidence": [
            {"revision_id": "rawrev-" + "1" * 40, "revision_number": 0}
        ],
        "projection_reference_mismatch_evidence": [
            {
                "revision_id": "rawrev-" + "2" * 40,
                "expected_projection_reference_hash": "a" * 64,
                "observed_projection_reference_hash": "b" * 64,
            }
        ],
        "projection_metric_aggregate_mismatch_evidence": [
            {
                "relative_path": "codex/day/chunk.md",
                "expected_projection_metric_aggregate_hash": "c" * 64,
                "observed_projection_metric_aggregate_hash": "d" * 64,
            }
        ],
        "structural_error_evidence": [
            {"error_hash": "e" * 64, "error_class": "projection_structure"}
        ],
        "unexpected_revision_evidence": [
            {"revision_id": "rawrev-" + "3" * 40, "lineage_state": "unknown"}
        ],
    }
    gap_hash = "4" * 64
    report = {
        "schema_version": "mnemos.raw_projection_fidelity.v-test",
        "expected_event_ids": 2,
        "observed_event_ids": 2,
        "missing_event_ids": 1,
        "unexpected_event_ids": 1,
        "logical_event_id_mismatch_count": 0,
        "projection_metadata_mismatch_count": 0,
        "projection_reference_mismatch_count": 1,
        "projection_metric_aggregate_mismatch_count": 1,
        "field_hash_mismatch_count": 0,
        "truncated_marker_files": 0,
        "structural_error_count": 1,
        "error_count": 4,
        "gap_generation": {
            "evidence_epoch_stable": True,
            "gap_hash": gap_hash,
            "paired_superseded_revision_count": 0,
            "unpaired_superseded_revision_count": 0,
        },
        **exact_evidence,
    }
    expected_decoded = cog009._canonical_json_bytes(exact_evidence)  # noqa: SLF001
    monkeypatch.setattr(
        cog009,
        "audit_raw_projection_fidelity",
        lambda **_kwargs: report,
    )

    artifact = cog009.build_artifact(
        raw_dir=tmp_path / "raw",
        db_path=db_path,
        canonical_db_identity=tmp_path / "canonical.db",
        expected_snapshot_sha256=expected_hash,
        expected_missing=1,
        expected_unexpected=1,
        expected_structural_errors=1,
        expected_reference_mismatches=1,
        expected_metric_aggregate_mismatches=1,
        expected_gap_hash=gap_hash,
        expected_evidence_decoded_sha256=hashlib.sha256(
            expected_decoded
        ).hexdigest(),
    )

    decoded = gzip.decompress(
        base64.b64decode(artifact["evidence_archive"]["payload_base64"])
    )
    assert decoded == expected_decoded
    assert json.loads(decoded) == exact_evidence


def test_cog009_generator_never_accepts_a_leaf_symlink_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "checkpoint.real.db"
    target.write_bytes(b"snapshot")
    link = tmp_path / "checkpoint.db"
    link.symlink_to(target)
    called = {"audit": False}

    def unexpected_audit(**_kwargs):
        called["audit"] = True
        return {}

    monkeypatch.setattr(cog009, "audit_raw_projection_fidelity", unexpected_audit)

    with pytest.raises(ValueError, match="snapshot is unsafe"):
        cog009.build_artifact(
            raw_dir=tmp_path / "raw",
            db_path=link,
            canonical_db_identity=tmp_path / "canonical.db",
            expected_snapshot_sha256=hashlib.sha256(b"snapshot").hexdigest(),
            expected_missing=0,
            expected_unexpected=0,
            expected_structural_errors=0,
            expected_reference_mismatches=0,
            expected_metric_aggregate_mismatches=0,
            expected_gap_hash="a" * 64,
            expected_evidence_decoded_sha256="b" * 64,
        )

    assert called["audit"] is False


def test_cog026_generator_rejects_snapshot_replacement_during_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "checkpoint.db"
    db_path.write_bytes(b"snapshot-a")
    expected_hash = hashlib.sha256(b"snapshot-a").hexdigest()

    class Source:
        def assert_epoch_current(self) -> None:
            db_path.write_bytes(b"snapshot-b")

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        cog026,
        "plan_projection",
        lambda _args: (Source(), [], {"projection_plan": {}}),
    )
    args = Namespace(
        db_path=db_path,
        expected_snapshot_sha256=expected_hash,
    )
    with pytest.raises(ValueError, match="snapshot changed"):
        cog026.build_artifact(args)


def test_cog026_generator_never_accepts_a_leaf_symlink_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "checkpoint.real.db"
    target.write_bytes(b"snapshot")
    link = tmp_path / "checkpoint.db"
    link.symlink_to(target)
    called = {"plan": False}

    def unexpected_plan(_args):
        called["plan"] = True
        raise AssertionError("plan must not run")

    monkeypatch.setattr(cog026, "plan_projection", unexpected_plan)

    with pytest.raises(ValueError, match="snapshot is unsafe"):
        cog026.build_artifact(
            Namespace(
                db_path=link,
                expected_snapshot_sha256=hashlib.sha256(b"snapshot").hexdigest(),
            )
        )

    assert called["plan"] is False


def test_cog026_plan_archive_replaces_local_paths_with_verified_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_paths = {
        "canonical_db": "/private/tmp/machine-local/raw_events.db",
        "raw_dir": "/private/tmp/machine-local/raw",
        "backup_dir": "/private/tmp/machine-local/backups/raw-projection",
    }
    redacted, path_hashes = cog026.redact_plan_path_identities(
        {
            **local_paths,
            "plan_hash": "a" * 64,
            "changed_paths": ["codex/day/chunk.md"],
        }
    )

    encoded = json.dumps(redacted, sort_keys=True)
    assert "/private/tmp/machine-local" not in encoded
    assert redacted["canonical_db"] == "${MNEMOS_CANONICAL_RAW_DB}"
    assert redacted["raw_dir"] == "${MNEMOS_RAW_VAULT}"
    assert redacted["backup_dir"] == "${MNEMOS_RAW_PROJECTION_BACKUP_DIR}"
    assert path_hashes == {
        field: hashlib.sha256(value.encode("utf-8")).hexdigest()
        for field, value in local_paths.items()
    }

    db_path = tmp_path / "checkpoint.db"
    db_path.write_bytes(b"snapshot-a")
    snapshot_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
    plan = {
        **local_paths,
        "schema_version": "mnemos.raw_projection_plan.v2",
        "projection_contract": "lossless-visible-v1",
        "plan_hash": "1" * 64,
        "generation_hash": "2" * 64,
        "source_epoch_hash": "3" * 64,
        "source_revision_set_hash": "4" * 64,
        "desired_index_generation_hash": "5" * 64,
        "changed_paths": ["codex/day/chunk.md"],
        "stale_paths": ["codex/day/stale.md"],
        "index_changed_paths": ["codex/day/chunk.md"],
        "index_deleted_paths": ["codex/day/stale.md"],
        "write_set_empty": False,
        "exact_nested_contract": {
            "desired_file_hashes": {"codex/day/chunk.md": "6" * 64},
            "index_orphan_row_counts": {"raw_fts": 2, "raw_tags": 3},
        },
    }
    expected_redacted, _expected_path_hashes = (
        cog026.redact_plan_path_identities(plan)
    )
    expected_archive = cog026._canonical_json_bytes(expected_redacted)  # noqa: SLF001

    class Source:
        def assert_epoch_current(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        cog026,
        "plan_projection",
        lambda _args: (
            Source(),
            [],
            {
                "candidate_turns": 2,
                "projected_files": 1,
                "projection_plan": plan,
            },
        ),
    )
    args = Namespace(
        db_path=db_path,
        expected_snapshot_sha256=snapshot_hash,
        expected_candidate_turns=2,
        expected_projected_files=1,
        expected_changed_paths=1,
        expected_stale_paths=1,
        expected_index_changed_paths=1,
        expected_index_deleted_paths=1,
        expected_plan_hash=plan["plan_hash"],
        expected_generation_hash=plan["generation_hash"],
        expected_desired_index_generation_hash=plan[
            "desired_index_generation_hash"
        ],
        expected_plan_archive_decoded_sha256=hashlib.sha256(
            expected_archive
        ).hexdigest(),
    )

    artifact = cog026.build_artifact(args)

    decoded = gzip.decompress(
        base64.b64decode(artifact["plan_archive"]["payload_base64"])
    )
    assert decoded == expected_archive
    assert json.loads(decoded) == expected_redacted
    assert artifact["exact_plan_payload_sha256"] == hashlib.sha256(
        cog026._canonical_json_bytes(plan)  # noqa: SLF001
    ).hexdigest()


def _hermetic_execution(
    *,
    failed: bool = False,
    execution_id: str = "ROOT-COG-001-candidate",
) -> dict:
    passed = [] if failed else ["tests/test_fake.py::test_contract"]
    failures = ["tests/test_fake.py::test_contract"] if failed else []
    return {
        "execution_id": execution_id,
        "exit_code": 1 if failed else 0,
        "outcomes": {
            "passed": passed,
            "failed": failures,
            "error": [],
            "skipped": [],
            "xfail": [],
            "xpass": [],
        },
        "executed_node_count": 1,
        "hermetic_run": {
            "profile": "isolated",
            "environment_hash": "a" * 64,
            "outside_write_count": 0,
            "formal_state_diff": [],
            "credentials_inherited": False,
            "manifest_verified": True,
            "manifest_integrity_digest": "b" * 64,
        },
    }


def _write_phase1_execution_fixture(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict, dict, Path]:
    oracle = root / "tests" / "test_fake.py"
    candidate = root / "core" / "fake.py"
    evidence = root / "docs" / "acceptance" / "phase1_historical_defect_execution_evidence.json"
    oracle.parent.mkdir(parents=True)
    candidate.parent.mkdir(parents=True)
    evidence.parent.mkdir(parents=True)
    oracle.write_text("def test_contract():\\n    assert True\\n", encoding="utf-8")
    candidate.write_text("STATE = 'GOOD'\\n", encoding="utf-8")
    replacement = {
        "operator_id": "break_fake_state",
        "path": "core/fake.py",
        "old": "GOOD",
        "new": "BAD",
    }
    spec = {
        "requirement_id": "ROOT-COG-001",
        "node_ids": ("tests/test_fake.py::test_contract",),
        "candidate_paths": ("core/fake.py",),
        "mutation_candidate_paths": ("core/fake.py",),
        "mutation_source_replacement": replacement,
        "mutation_source_replacements": (replacement,),
        "mutation_operator_ids": ("break_fake_state",),
        "fault_model_ids": ("break_fake_state",),
        "risk_scenario_ids": ("reject_bad_state",),
        "risk_scenario_evidence_role": "non_credit_descriptive_risk_register",
        "mutation_oracle_node_ids": ("tests/test_fake.py::test_contract",),
        "mutation_oracle_node_ids_by_operator": {
            "break_fake_state": ("tests/test_fake.py::test_contract",),
        },
    }
    monkeypatch.setattr(governance, "ROOT", root)
    monkeypatch.setattr(
        governance,
        "PHASE1_ROOT_REQUIREMENT_SPECS",
        (spec,),
    )
    monkeypatch.setattr(
        governance,
        "PHASE1_BASELINE_COMMITS",
        {"COG-001": "c" * 40},
    )
    snapshot = {"path_count": 2, "sha256": "d" * 64}
    monkeypatch.setattr(
        governance,
        "_phase1_execution_snapshot",
        lambda: dict(snapshot),
    )
    changes = governance._expected_phase1_mutation_changes(
        spec,
        "c" * 40,
        "break_fake_state",
    )
    mutation = {
        "operator_id": "break_fake_state",
        "strategy": "exact_source_replacement",
        "baseline_commit": None,
        "changed_artifacts": changes,
    }
    run = {
        "root_id": "COG-001",
        "selected_nodes": ["tests/test_fake.py::test_contract"],
        "oracle_materialization": [governance._phase1_path_identity("tests/test_fake.py")],
        "candidate_snapshot": dict(snapshot),
        "fault_model_ids": ["break_fake_state"],
        "risk_scenario_ids": ["reject_bad_state"],
        "risk_scenario_evidence_role": "non_credit_descriptive_risk_register",
        "mutation_oracle_node_ids": ["tests/test_fake.py::test_contract"],
        "mutation_oracle_node_ids_by_operator": {
            "break_fake_state": ["tests/test_fake.py::test_contract"],
        },
        "mutation_operator_ids": ["break_fake_state"],
        "candidate_execution": _hermetic_execution(),
        "mutation_executions": {
            "break_fake_state": {
                "mutation": mutation,
                "mutation_hash": governance._hash(mutation),
                "oracle_binding": {
                    "declared_killing_node_ids": ["tests/test_fake.py::test_contract"],
                    "observed_failed_node_ids": ["tests/test_fake.py::test_contract"],
                },
                "execution": _hermetic_execution(
                    failed=True,
                    execution_id="ROOT-COG-001-mutation-break_fake_state",
                ),
                "status": "killed",
            }
        },
        "kill_summary": {
            "executed_operator_ids": ["break_fake_state"],
            "killed_operator_ids": ["break_fake_state"],
            "survived_operator_ids": [],
            "kill_rate_percent": 100,
        },
    }
    payload = {
        "schema_version": "mnemos.phase1_historical_defect_execution_evidence.v4",
        "execution_boundary": {},
        "candidate_snapshot": {
            **snapshot,
            "scope": "phase1_code_tests_and_exact_candidate_artifacts",
        },
        "runs": {"ROOT-COG-001": run},
    }
    payload["denominator_summary"] = governance.phase1_execution_denominator_summary(
        payload["runs"]
    )
    payload["evidence_hash"] = governance._hash(payload)
    evidence.write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    item = {
        "requirement_id": "ROOT-COG-001",
        "root_id": "COG-001",
        "node_ids": ["tests/test_fake.py::test_contract"],
        "mutation_operator_ids": ["break_fake_state"],
        "fault_model_ids": ["break_fake_state"],
        "risk_scenario_ids": ["reject_bad_state"],
        "risk_scenario_evidence_role": "non_credit_descriptive_risk_register",
        "mutation_oracle_node_ids": ["tests/test_fake.py::test_contract"],
        "mutation_oracle_node_ids_by_operator": {
            "break_fake_state": ["tests/test_fake.py::test_contract"],
        },
        "baseline_artifact": {
            "execution_artifact": {
                "path": ("docs/acceptance/" "phase1_historical_defect_execution_evidence.json"),
                "entry_key": "ROOT-COG-001",
                "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
            }
        },
    }
    assert governance._valid_phase1_execution_artifact(item)
    return item, payload, evidence


def _rewrite_execution_payload(
    item: dict,
    payload: dict,
    evidence: Path,
) -> None:
    payload["evidence_hash"] = governance._hash(
        {key: value for key, value in payload.items() if key != "evidence_hash"}
    )
    evidence.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    item["baseline_artifact"]["execution_artifact"]["sha256"] = hashlib.sha256(
        evidence.read_bytes()
    ).hexdigest()


def test_phase1_execution_validator_rejects_undeclared_mutation_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, payload, evidence = _write_phase1_execution_fixture(tmp_path, monkeypatch)
    changed = payload["runs"]["ROOT-COG-001"]["mutation_executions"]["break_fake_state"][
        "mutation"
    ]["changed_artifacts"]
    changed.append(
        {
            "path": "core/undeclared.py",
            "operation": "delete",
            "candidate_sha256": "e" * 64,
            "historical_sha256": None,
        }
    )
    mutation = payload["runs"]["ROOT-COG-001"]["mutation_executions"]["break_fake_state"][
        "mutation"
    ]
    payload["runs"]["ROOT-COG-001"]["mutation_executions"]["break_fake_state"]["mutation_hash"] = (
        governance._hash(mutation)
    )
    _rewrite_execution_payload(item, payload, evidence)
    assert not governance._valid_phase1_execution_artifact(item)


def test_phase1_execution_validator_rejects_stale_oracle_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, payload, evidence = _write_phase1_execution_fixture(tmp_path, monkeypatch)
    (tmp_path / "tests" / "test_fake.py").write_text(
        "def test_contract():\\n    assert 1 == 1\\n",
        encoding="utf-8",
    )
    _rewrite_execution_payload(item, payload, evidence)
    assert not governance._valid_phase1_execution_artifact(item)


def test_phase1_execution_validator_rejects_candidate_drift_with_refreshed_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, payload, evidence = _write_phase1_execution_fixture(tmp_path, monkeypatch)
    (tmp_path / "core" / "fake.py").write_text(
        "STATE = 'GOOD'\\nEXTRA = True\\n",
        encoding="utf-8",
    )
    _rewrite_execution_payload(item, payload, evidence)
    assert not governance._valid_phase1_execution_artifact(item)


def test_phase1_oracle_validator_accepts_unittest_assertion_methods() -> None:
    assert governance._pytest_node_has_assertion(
        "tests/unit/test_amphora.py::TestAmphoraQueue::"
        "test_existing_revision_schema_without_terminal_anchor_fails_closed"
    )
    assert governance._pytest_node_has_assertion(
        "tests/unit/test_sync_backfill.py::"
        "test_backfill_uses_canonical_session_for_existing_lookup"
    )


def test_phase1_execution_validator_rejects_unexecuted_kill_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, payload, evidence = _write_phase1_execution_fixture(tmp_path, monkeypatch)
    result = payload["runs"]["ROOT-COG-001"]["mutation_executions"]["break_fake_state"]
    result["execution"] = _hermetic_execution()
    _rewrite_execution_payload(item, payload, evidence)
    assert not governance._valid_phase1_execution_artifact(item)


def test_phase1_mutation_requires_its_declared_killing_oracle_to_fail() -> None:
    execution = _hermetic_execution(failed=True)

    with pytest.raises(RuntimeError, match="was not cleanly killed"):
        phase1_evidence._require_mutation_killed(
            "ROOT-COG-001",
            execution,
            ("tests/test_fake.py::test_different_contract",),
        )


def test_phase1_mutation_batch_reports_every_survivor() -> None:
    failures = [
        "ROOT-COG-045-mutation-first was not cleanly killed",
        "ROOT-COG-008-mutation-second was not cleanly killed",
    ]

    with pytest.raises(
        RuntimeError,
        match=r"found 2 survivor\(s\)",
    ) as raised:
        phase1_evidence._require_all_mutations_killed(failures)

    assert all(failure in str(raised.value) for failure in failures)


def test_phase1_execution_validator_rejects_raw_pytest_output_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, payload, evidence = _write_phase1_execution_fixture(tmp_path, monkeypatch)
    payload["runs"]["ROOT-COG-001"]["candidate_execution"]["output_sha256"] = "f" * 64
    _rewrite_execution_payload(item, payload, evidence)
    assert not governance._valid_phase1_execution_artifact(item)


def test_phase1_execution_validator_rejects_inflated_denominator_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, payload, evidence = _write_phase1_execution_fixture(
        tmp_path,
        monkeypatch,
    )
    payload["denominator_summary"]["mutation_execution_count"] += 1
    _rewrite_execution_payload(item, payload, evidence)

    assert not governance._valid_phase1_execution_artifact(item)


def test_phase1_refresh_rejects_existing_generation_id_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(phase1_refresh, "RECORD_ID", "current-generation")
    existing = {
        "record_type": "append_only",
        "verification": {"evidence_hash": "old"},
        "governance_revalidation": {"derived": "old"},
        "artifacts": {"derived": "old"},
    }
    exact_proposed = {
        "record_type": "append_only",
        "verification": {"evidence_hash": "old"},
        "governance_revalidation": {},
        "artifacts": {},
    }
    assert phase1_refresh._existing_generation_is_same(
        {"current-generation": existing},
        exact_proposed,
    )
    frozen = json.dumps(existing, sort_keys=True)
    drifted = {
        **exact_proposed,
        "verification": {"evidence_hash": "new"},
    }
    with pytest.raises(
        RuntimeError,
        match="requires_new_record_id",
    ):
        phase1_refresh._existing_generation_is_same(
            {"current-generation": existing},
            drifted,
        )
    assert json.dumps(existing, sort_keys=True) == frozen


def test_phase1_refresh_cli_help_never_attempts_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresh_calls: list[str] = []
    monkeypatch.setattr(
        phase1_refresh,
        "refresh",
        lambda: refresh_calls.append("refresh"),
    )

    with pytest.raises(SystemExit) as exit_info:
        phase1_refresh.main(["--help"])

    assert exit_info.value.code == 0
    assert refresh_calls == []


def test_current_phase1_generation_requires_exact_execution_evidence_hash(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "phase1-evidence.json"
    evidence.write_text(
        json.dumps({"evidence_hash": "current-evidence"}),
        encoding="utf-8",
    )
    latest = {
        "verification": {
            "phase1_execution_evidence_hash": "current-evidence",
        }
    }

    assert governance._phase1_execution_evidence_binding_is_current(  # noqa: SLF001
        latest,
        evidence_path=evidence,
    )
    latest["verification"]["phase1_execution_evidence_hash"] = "stale-evidence"
    assert not governance._phase1_execution_evidence_binding_is_current(  # noqa: SLF001
        latest,
        evidence_path=evidence,
    )


def test_phase1_refresh_targets_the_current_append_only_generation() -> None:
    ledger = json.loads(phase1_refresh.governance.PHASE1_LEDGER_PATH.read_text(encoding="utf-8"))
    evidence = json.loads(
        (
            phase1_refresh.ROOT
            / "docs"
            / "acceptance"
            / "phase1_historical_defect_execution_evidence.json"
        ).read_text(encoding="utf-8")
    )
    assert phase1_refresh.RECORD_ID != phase1_refresh.PREDECESSOR_ID
    assert phase1_refresh.PREDECESSOR_ID in ledger
    assert phase1_refresh.governance.PHASE1_REVALIDATION_SEQUENCE[-2] == (
        "COG-045",
        phase1_refresh.PREDECESSOR_ID,
    )
    assert phase1_refresh.governance.PHASE1_REVALIDATION_SEQUENCE[-1] == (
        "COG-045",
        phase1_refresh.RECORD_ID,
    )
    assert (
        phase1_refresh.RECORD_ID in phase1_refresh.governance.PHASE1_REVALIDATION_BOUNDARY_OVERRIDES
    )
    current = ledger.get(phase1_refresh.RECORD_ID)
    if current is not None:
        assert current["sequence_predecessor"] == phase1_refresh.PREDECESSOR_ID
        assert (
            current["verification"]["phase1_execution_evidence_hash"] == evidence["evidence_hash"]
        )


def test_phase1_refresh_record_covers_exact_current_phase1_requirement_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_ids = [
        str(spec["requirement_id"]) for spec in governance.PHASE1_ROOT_REQUIREMENT_SPECS
    ]
    evidence_runs = {
        requirement_id: {
            "selected_nodes": [],
            "candidate_execution": {"outcomes": {"passed": []}},
            "mutation_executions": {},
        }
        for requirement_id in expected_ids
    }
    evidence = {
        "schema_version": "mnemos.phase1_historical_defect_execution_evidence.v4",
        "evidence_hash": "e" * 64,
        "runs": evidence_runs,
        "denominator_summary": (governance.phase1_execution_denominator_summary(evidence_runs)),
    }
    monkeypatch.setattr(
        phase1_refresh,
        "_load",
        lambda _path: evidence,
    )
    monkeypatch.setattr(
        phase1_refresh,
        "_json_command_evidence",
        lambda _argv: {
            "argv": [],
            "exit_code": 0,
            "output_sha256": "a" * 64,
            "schema_version": "test",
            "ok": True,
            "current_count": 0,
            "release_eligible": False,
        },
    )

    record = phase1_refresh._record()  # noqa: SLF001

    assert record["root_id"] == "COG-045"
    assert record["state"] == ("CODE_CONTRACT_REVALIDATED_LIVE_RAW_REBUILD_PENDING")
    assert record["requirement_revalidation"]["requirement_ids"] == expected_ids
    assert record["requirement_revalidation"]["registered_count"] == len(expected_ids)
    assert record["requirement_revalidation"] == (
        governance.phase1_requirement_revalidation_summary()
    )
    assert record["requirement_revalidation"]["population_policy_mode"] == ("per_requirement_exact")
    assert record["requirement_revalidation"]["population_policy_by_requirement"][
        "ROOT-COG-045"
    ] == ("all_exact_nodes_required_no_skip_no_xfail_on_darwin")
    assert record["requirement_revalidation"]["execution_platforms_by_requirement"][
        "ROOT-COG-045"
    ] == ["darwin"]
    assert set(record["verification"]["phase1_candidate_executions"]) == set(expected_ids)
    assert set(record["verification"]["phase1_mutation_executions"]) == set(expected_ids)
    assert record["verification"]["phase1_execution_denominator"] == (
        evidence["denominator_summary"]
    )
    assert record["closure_boundary"] == (
        governance.PHASE1_REVALIDATION_BOUNDARY_OVERRIDES[phase1_refresh.RECORD_ID]
    )
    assert record["closure_boundary"]["live_snapshot_raw_rebuilt"] is False
    assert record["closure_boundary"]["release_eligible"] is False


def test_phase1_governance_revalidation_covers_every_generated_contract_family() -> None:
    expected = governance.build_assets()

    summary = phase1_refresh._governance_revalidation(expected)

    assert set(summary) == {
        "root_dag",
        "root_change_budgets",
        "finding_overlay",
        "schema_inventory",
        "root_closure_projection",
        "requirement_test",
        "audit_artifacts",
        "migrations",
        "release_certificates",
    }
    assert summary["migrations"]["count"] == json.loads(
        expected[
            governance.ACCEPTANCE / "cognitive_migration_manifest.json"
        ]
    )["migration_count"]
    assert summary["release_certificates"]["required"] > 0


@pytest.mark.parametrize("strict_errors", ([], ["stale-current-assets"]))
def test_phase1_refresh_same_core_noop_validates_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    strict_errors: list[str],
) -> None:
    monkeypatch.setattr(
        phase1_refresh,
        "GOVERNANCE_REFRESH_LOCK_PATH",
        tmp_path / "phase1-governance.lock",
    )
    monkeypatch.setattr(phase1_refresh, "RECORD_ID", "current-generation")
    proposed = {
        "record_type": "append_only",
        "verification": {"evidence_hash": "same"},
        "governance_revalidation": {},
        "artifacts": {},
    }
    existing = {
        **proposed,
        "governance_revalidation": {"derived": "current"},
        "artifacts": {"derived": "current"},
    }
    monkeypatch.setattr(phase1_refresh, "_record", lambda: proposed)
    monkeypatch.setattr(
        phase1_refresh,
        "_load",
        lambda _path: {"current-generation": existing},
    )
    validation_calls: list[str] = []
    monkeypatch.setattr(
        phase1_refresh.governance,
        "validate_assets",
        lambda *, desktop_mode: (validation_calls.append(desktop_mode) or strict_errors),
    )
    writes: list[str] = []
    monkeypatch.setattr(
        phase1_refresh,
        "_write",
        lambda *_args, **_kwargs: writes.append("json"),
    )
    monkeypatch.setattr(
        phase1_refresh,
        "atomic_write_text",
        lambda *_args, **_kwargs: writes.append("text"),
    )

    if strict_errors:
        with pytest.raises(
            RuntimeError,
            match="noop_requires_current_assets",
        ):
            phase1_refresh.refresh()
    else:
        phase1_refresh.refresh()
    assert validation_calls == ["required"]
    assert writes == []


def test_phase1_refresh_failure_restores_preimages_and_retry_can_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path = tmp_path / "phase1-ledger.json"
    independent_path = tmp_path / "independent.json"
    ledger_path.write_text('{"historical":{"state":"frozen"}}\n', encoding="utf-8")
    independent_path.write_text('{"denominator":"frozen"}\n', encoding="utf-8")
    before_ledger = ledger_path.read_bytes()
    before_independent = independent_path.read_bytes()
    proposed = {
        "record_type": "append_only",
        "verification": {"evidence_hash": "new"},
        "governance_revalidation": {},
        "artifacts": {},
    }
    monkeypatch.setattr(
        phase1_refresh,
        "GOVERNANCE_REFRESH_LOCK_PATH",
        tmp_path / "phase1-governance.lock",
    )
    monkeypatch.setattr(phase1_refresh, "RECORD_ID", "new-generation")
    monkeypatch.setattr(
        phase1_refresh.governance,
        "PHASE1_LEDGER_PATH",
        ledger_path,
    )
    monkeypatch.setattr(
        phase1_refresh.governance,
        "INDEPENDENT_DENOMINATOR_PATH",
        independent_path,
    )
    monkeypatch.setattr(phase1_refresh, "_record", lambda: proposed)
    monkeypatch.setattr(
        phase1_refresh,
        "_refresh_independent",
        lambda _independent, _ledger: None,
    )
    monkeypatch.setattr(
        phase1_refresh.governance,
        "build_assets",
        lambda: (_ for _ in ()).throw(RuntimeError("injected_build_failure")),
    )
    with pytest.raises(RuntimeError, match="injected_build_failure"):
        phase1_refresh.refresh()
    assert ledger_path.read_bytes() == before_ledger
    assert independent_path.read_bytes() == before_independent

    monkeypatch.setattr(
        phase1_refresh.governance,
        "build_assets",
        lambda: {},
    )
    monkeypatch.setattr(
        phase1_refresh,
        "_governance_revalidation",
        lambda _expected: {},
    )
    monkeypatch.setattr(
        phase1_refresh.governance,
        "phase1_current_generation_artifact_paths",
        lambda: (),
    )
    monkeypatch.setattr(
        phase1_refresh.governance,
        "validate_assets",
        lambda *, desktop_mode: [],
    )
    phase1_refresh.refresh()
    persisted = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert persisted["historical"] == {"state": "frozen"}
    assert persisted["new-generation"] == proposed


def test_phase1_refresh_projects_current_external_governing_generation_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external_contract = [
        {
            "asset_id": "desktop:current",
            "required_current_root_generations": ["COG-045"],
        }
    ]
    monkeypatch.setattr(
        phase1_refresh,
        "_load",
        lambda _path: {"external_governing_assets": external_contract},
    )
    independent: dict = {}
    ledger = {
        phase1_refresh.PREDECESSOR_ID: {
            "state": "frozen",
        }
    }

    phase1_refresh._refresh_independent(independent, ledger)

    assert independent["external_governing_assets"] == external_contract


def test_phase1_refresh_excludes_concurrent_writer_before_any_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "phase1-governance.lock"
    successful_ledger = tmp_path / "successful-ledger.json"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import json, sys, time\n"
                "from pathlib import Path\n"
                "from core.ops.exclusive_file_lock import exclusive_file_lock\n"
                "lock_path = Path(sys.argv[1])\n"
                "ledger_path = Path(sys.argv[2])\n"
                "with exclusive_file_lock(\n"
                "    lock_path,\n"
                "    unavailable_message='child_lock_unavailable',\n"
                "):\n"
                "    print('locked', flush=True)\n"
                "    time.sleep(0.5)\n"
                "    ledger_path.write_text(\n"
                "        json.dumps({'governance_revalidation': {'complete': True}}),\n"
                "        encoding='utf-8',\n"
                "    )\n"
            ),
            str(lock_path),
            str(successful_ledger),
        ],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "locked"
    monkeypatch.setattr(
        phase1_refresh,
        "GOVERNANCE_REFRESH_LOCK_PATH",
        lock_path,
    )
    unlocked_calls: list[str] = []
    monkeypatch.setattr(
        phase1_refresh,
        "_refresh_locked",
        lambda: unlocked_calls.append("entered"),
    )

    with pytest.raises(
        RuntimeError,
        match="phase1_governance_refresh_already_running",
    ):
        phase1_refresh.refresh()
    stdout, stderr = child.communicate(timeout=10)

    assert child.returncode == 0, stdout + stderr
    assert unlocked_calls == []
    assert json.loads(successful_ledger.read_text(encoding="utf-8")) == {
        "governance_revalidation": {"complete": True}
    }
    phase1_refresh.refresh()
    assert unlocked_calls == ["entered"]


def test_phase1_refresh_lock_uses_git_common_dir_for_linked_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    linked = tmp_path / "linked"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "phase1@example.test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Phase One"],
        cwd=repository,
        check=True,
    )
    (repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repository, check=True)
    subprocess.run(
        ["git", "worktree", "add", "-qb", "linked", str(linked)],
        cwd=repository,
        check=True,
    )
    assert (linked / ".git").is_file()
    monkeypatch.setattr(phase1_refresh, "ROOT", linked)

    from core.ops.git_repository_lock import git_common_lock_path

    lock_path = git_common_lock_path(
        linked,
        "mnemos_phase1_governance_refresh.lock",
    )

    common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repository,
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout.strip()
    expected_common = Path(common)
    if not expected_common.is_absolute():
        expected_common = repository / expected_common
    assert lock_path.parent == expected_common.resolve()
    assert lock_path.name == "mnemos_phase1_governance_refresh.lock"


def test_governance_generator_write_cannot_bypass_refresh_writer_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "governance.lock"
    target = tmp_path / "generated.json"
    target.write_text("preimage\n", encoding="utf-8")
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys, time\n"
                "from pathlib import Path\n"
                "from scripts import refresh_phase1_deep_audit_governance as refresh\n"
                "refresh.GOVERNANCE_REFRESH_LOCK_PATH = Path(sys.argv[1])\n"
                "target = Path(sys.argv[2])\n"
                "def publish():\n"
                "    print('locked', flush=True)\n"
                "    time.sleep(0.5)\n"
                "    target.write_text('refresh-winner\\n', encoding='utf-8')\n"
                "refresh._refresh_locked = publish\n"
                "refresh.refresh()\n"
            ),
            str(lock_path),
            str(target),
        ],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "locked"
    monkeypatch.setattr(
        governance,
        "GOVERNANCE_REFRESH_LOCK_PATH",
        lock_path,
    )
    monkeypatch.setattr(
        governance,
        "build_assets",
        lambda: {target: "generator-loser\n"},
    )
    monkeypatch.setattr(
        governance,
        "validate_assets",
        lambda *, desktop_mode: [],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["generate_phase0_governance_contracts.py", "--write", "--json"],
    )

    assert governance.main() == 1
    stdout, stderr = child.communicate(timeout=10)

    assert child.returncode == 0, stdout + stderr
    assert target.read_text(encoding="utf-8") == "refresh-winner\n"


def test_governance_generator_validation_failure_restores_all_preimages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = tmp_path / "existing.json"
    created = tmp_path / "created.json"
    existing.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(
        governance,
        "build_assets",
        lambda: {
            existing: "new\n",
            created: "created\n",
        },
    )
    monkeypatch.setattr(
        governance,
        "validate_assets",
        lambda *, desktop_mode: ["injected_validation_failure"],
    )

    errors = governance._write_assets_transactionally(
        desktop_mode="required",
    )

    assert errors == ["injected_validation_failure"]
    assert existing.read_text(encoding="utf-8") == "old\n"
    assert not created.exists()


def test_governance_generator_write_failure_restores_prior_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text("old-first\n", encoding="utf-8")
    second.write_text("old-second\n", encoding="utf-8")
    monkeypatch.setattr(
        governance,
        "build_assets",
        lambda: {
            first: "new-first\n",
            second: "new-second\n",
        },
    )

    def _flaky_atomic_write(path, content, *, encoding):
        if path == second and content == "new-second\n":
            raise OSError("injected_second_write_failure")
        path.write_text(content, encoding=encoding)

    monkeypatch.setattr(
        governance,
        "atomic_write_text",
        _flaky_atomic_write,
    )

    with pytest.raises(OSError, match="injected_second_write_failure"):
        governance._write_assets_transactionally(desktop_mode="required")

    assert first.read_text(encoding="utf-8") == "old-first\n"
    assert second.read_text(encoding="utf-8") == "old-second\n"


def test_phase1_materialization_includes_staged_only_candidate_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "phase1@example.test"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Phase One"],
        cwd=tmp_path,
        check=True,
    )
    source = tmp_path / "core" / "owner.py"
    source.parent.mkdir()
    source.write_text("VALUE = 'baseline'\\n", encoding="utf-8")
    subprocess.run(["git", "add", "core/owner.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    source.write_text("VALUE = 'staged'\\n", encoding="utf-8")
    subprocess.run(["git", "add", "core/owner.py"], cwd=tmp_path, check=True)

    monkeypatch.setattr(phase1_evidence, "ROOT", tmp_path)
    source_paths = phase1_evidence._current_source_paths()
    destination = tmp_path / "materialized"
    destination.mkdir()
    phase1_evidence._materialize_current_tree(destination, source_paths)

    assert (destination / "core" / "owner.py").read_bytes() == source.read_bytes()
