"""Independent prediction lineage audit and tamper fixtures."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3

import pytest

from core.application.cognitive_state import CognitiveStateApplicationService
from core.cognitive.prediction_history_migration import (
    apply_prediction_history_migration,
    build_prediction_history_inventory,
)
from core.cognitive.prediction_lineage_audit import (
    audit_prediction_outcome_lineage,
)
from core.cognitive.prediction_ledger import PredictionRecordStore
from core.cognitive.state_contract import canonical_json, sha256_json
from core.cognitive.state_store import CognitiveStateStore
from tests.unit.cognitive.test_prediction_history_migration import _legacy_target
from tests.unit.cognitive.test_prediction_ledger import (
    _objective_outcome_request,
    _route,
    _router,
)


def _migrated_runtime(tmp_path: Path) -> tuple[Path, Path]:
    router = _router(tmp_path)
    delivery = tmp_path / "delivery_events.db"
    with sqlite3.connect(delivery) as conn:
        for index in range(5):
            conn.execute(
                "INSERT INTO delivery_events(event_id, created_at, channel, decision) "
                "VALUES (?, ?, 'predictive_push', ?)",
                (
                    f"historical-{index}",
                    f"2026-07-{index + 1:02d}T00:00:00+00:00",
                    "deliver" if index < 3 else "suppress",
                ),
            )
    target = _legacy_target(tmp_path / "producer_consumer_ledger.db")
    inventory = build_prediction_history_inventory(delivery)
    apply_prediction_history_migration(
        delivery_db=delivery,
        target_db=target,
        expected_inventory_hash=inventory.inventory_hash,
        backup_dir=tmp_path / "backup",
        database_dir=tmp_path,
        daemon_check=lambda _: True,
    )
    _route(router)
    return delivery, target


def _measured_runtime(tmp_path: Path) -> tuple[Path, Path]:
    delivery, target = _migrated_runtime(tmp_path)
    state = CognitiveStateStore(target)
    prediction = state.current_revisions(object_type="prediction_record")[0]
    request, principal, observed_at, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
    )
    CognitiveStateApplicationService(state).apply_outcome(
        request,
        principal=principal,
        source_authority_catalog=catalog,
    )
    PredictionRecordStore(state).finalize(prediction.object_id, {}, observed_at)
    return delivery, target


def _corrected_runtime(tmp_path: Path) -> tuple[Path, Path]:
    delivery, target = _measured_runtime(tmp_path)
    state = CognitiveStateStore(target)
    old_outcome = state.current_revisions(
        object_type="outcome_measurement"
    )[0]
    prediction = state.revision(
        str(old_outcome.payload["prediction_ref"]["revision_id"])
    )
    assert prediction is not None
    request, principal, _observed_at, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
        observed_value="not_useful",
        source_suffix="prediction-audit-correction",
        observed_hours=2,
        correction_of_revision_id=old_outcome.revision_id,
    )
    CognitiveStateApplicationService(state).apply_outcome(
        request,
        principal=principal,
        source_authority_catalog=catalog,
    )
    return delivery, target


def _rewrite_current_payload(target: Path, object_type: str, mutate) -> str:
    with sqlite3.connect(target) as conn:
        conn.execute("DROP TRIGGER IF EXISTS cognitive_state_revisions_no_update")
        row = conn.execute(
            "SELECT r.revision_id, r.payload_json FROM cognitive_state_revisions AS r "
            "JOIN cognitive_state_heads AS h ON h.revision_id=r.revision_id "
            "WHERE r.object_type=?",
            (object_type,),
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row[1]))
        mutate(payload)
        payload_hash = sha256_json(payload)
        conn.execute(
            "UPDATE cognitive_state_revisions SET payload_json=?, payload_hash=? "
            "WHERE revision_id=?",
            (canonical_json(payload), payload_hash, str(row[0])),
        )
    return payload_hash


def _rebind_terminal_outcome_hash(target: Path, outcome_hash: str) -> None:
    _rewrite_current_payload(
        target,
        "prediction_record",
        lambda payload: payload["outcome_ref"].update(payload_hash=outcome_hash),
    )


def test_prediction_lineage_audit_passes_history_and_runtime_lineage(
    tmp_path: Path,
) -> None:
    delivery, target = _migrated_runtime(tmp_path)

    report = audit_prediction_outcome_lineage(
        delivery_db=delivery,
        target_db=target,
        repo_root=Path(__file__).resolve().parents[3],
    )

    assert report["ok"] is True
    assert report["blocking_count"] == 0
    assert set(report["metrics"].values()) == {0}
    assert report["denominators"]["historical_predictive_objects"] == 5
    assert report["denominators"]["runtime_predictive_deliveries"] == 1
    assert report["denominators"]["open_not_mature"] == 1


def test_prediction_lineage_audit_rejects_multiple_current_eligible_outcomes(
    tmp_path: Path,
) -> None:
    delivery, target = _measured_runtime(tmp_path)
    with sqlite3.connect(target) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT r.* FROM cognitive_state_revisions AS r "
            "JOIN cognitive_state_heads AS h ON h.revision_id=r.revision_id "
            "WHERE r.object_type='outcome_measurement'"
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row["payload_json"]))
        object_id = "outcome-" + "a" * 32
        revision_id = "cogrev-" + "b" * 32
        payload["outcome_id"] = object_id
        conn.execute(
            "INSERT INTO cognitive_state_revisions("
            "revision_id, object_type, object_id, schema_version, revision_no, "
            "source_event_id, source_revision_id, source_content_hash, scope_type, "
            "scope_id, evidence_refs, evidence_hash, payload_json, payload_hash, "
            "supersedes_revision_id, correction_of_revision_id, admission_state, "
            "redaction_policy, redaction_counts, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)",
            (
                revision_id,
                "outcome_measurement",
                object_id,
                str(row["schema_version"]),
                1,
                str(row["source_event_id"]),
                str(row["source_revision_id"]),
                str(row["source_content_hash"]),
                str(row["scope_type"]),
                str(row["scope_id"]),
                str(row["evidence_refs"]),
                str(row["evidence_hash"]),
                canonical_json(payload),
                sha256_json(payload),
                str(row["admission_state"]),
                str(row["redaction_policy"]),
                str(row["redaction_counts"]),
                str(row["created_at"]),
            ),
        )
        conn.execute(
            "INSERT INTO cognitive_state_heads(object_type, object_id, revision_id, updated_at) "
            "VALUES ('outcome_measurement', ?, ?, ?)",
            (object_id, revision_id, str(row["created_at"])),
        )

    report = audit_prediction_outcome_lineage(
        delivery_db=delivery,
        target_db=target,
        repo_root=Path(__file__).resolve().parents[3],
    )

    assert report["ok"] is False
    assert report["metrics"]["multiple_current_eligible_outcomes"] == 1


def test_prediction_lineage_audit_recomputes_material_receipt_prediction_ref(
    tmp_path: Path,
) -> None:
    delivery, target = _migrated_runtime(tmp_path)
    with sqlite3.connect(target) as conn:
        conn.execute("DROP TRIGGER cognitive_state_effect_receipts_no_update")
        row = conn.execute(
            "SELECT r.command_id, r.evidence_refs "
            "FROM cognitive_state_effect_receipts AS r "
            "JOIN cognitive_state_outbox AS o ON o.command_id=r.command_id "
            "WHERE o.command_type='execute_material_action'"
        ).fetchone()
        assert row is not None
        refs = [
            value
            for value in json.loads(str(row[1]))
            if not str(value).startswith("prediction-revision:")
        ]
        conn.execute(
            "UPDATE cognitive_state_effect_receipts SET evidence_refs=? WHERE command_id=?",
            (canonical_json(refs), str(row[0])),
        )

    report = audit_prediction_outcome_lineage(
        delivery_db=delivery,
        target_db=target,
        repo_root=Path(__file__).resolve().parents[3],
    )

    assert report["ok"] is False
    assert report["metrics"]["prediction_decision_action_binding_mismatch"] >= 1


def test_prediction_lineage_audit_requires_durable_oracle_issuance_receipt(
    tmp_path: Path,
) -> None:
    delivery, target = _measured_runtime(tmp_path)
    with sqlite3.connect(target) as conn:
        conn.execute("DROP TRIGGER cognitive_state_effect_receipts_no_update")
        row = conn.execute(
            "SELECT r.command_id, r.evidence_refs "
            "FROM cognitive_state_effect_receipts AS r "
            "JOIN cognitive_state_outbox AS o ON o.command_id=r.command_id "
            "WHERE o.command_type='project_prediction_outcome'"
        ).fetchone()
        assert row is not None
        refs = [
            value
            for value in json.loads(str(row[1]))
            if not str(value).startswith("objective-oracle-issuance:")
        ]
        conn.execute(
            "UPDATE cognitive_state_effect_receipts SET evidence_refs=? WHERE command_id=?",
            (canonical_json(refs), str(row[0])),
        )

    report = audit_prediction_outcome_lineage(
        delivery_db=delivery,
        target_db=target,
        repo_root=Path(__file__).resolve().parents[3],
    )

    assert report["ok"] is False
    assert report["metrics"]["objective_measurement_issuance_receipt_gap"] == 1


def test_prediction_lineage_audit_recomputes_terminal_projection_receipt(
    tmp_path: Path,
) -> None:
    delivery, target = _measured_runtime(tmp_path)
    with sqlite3.connect(target) as conn:
        conn.execute("DROP TRIGGER cognitive_state_effect_receipts_no_update")
        row = conn.execute(
            "SELECT r.command_id FROM cognitive_state_effect_receipts AS r "
            "JOIN cognitive_state_outbox AS o ON o.command_id=r.command_id "
            "WHERE o.command_type='project_prediction_terminal'"
        ).fetchone()
        assert row is not None
        conn.execute(
            "UPDATE cognitive_state_effect_receipts "
            "SET target_effect_id='forged-terminal', before_hash=?, after_hash=?, "
            "evidence_refs=? WHERE command_id=?",
            (
                "sha256:" + "1" * 64,
                "sha256:" + "2" * 64,
                canonical_json(["forged-terminal-evidence"]),
                str(row[0]),
            ),
        )

    report = audit_prediction_outcome_lineage(
        delivery_db=delivery,
        target_db=target,
        repo_root=Path(__file__).resolve().parents[3],
    )

    assert report["ok"] is False
    assert report["metrics"][
        "prediction_terminal_projection_receipt_mismatch"
    ] == 1


def test_prediction_lineage_audit_recomputes_correction_command_and_receipt(
    tmp_path: Path,
) -> None:
    delivery, target = _corrected_runtime(tmp_path)

    report = audit_prediction_outcome_lineage(
        delivery_db=delivery,
        target_db=target,
        repo_root=Path(__file__).resolve().parents[3],
        raw_db=tmp_path / "raw_events.db",
    )

    assert report["ok"] is True
    assert report["denominators"]["prediction_corrections"] == 1
    assert report["metrics"][
        "prediction_terminal_correction_receipt_mismatch"
    ] == 0


def test_prediction_lineage_audit_rejects_correction_command_hash_tamper(
    tmp_path: Path,
) -> None:
    delivery, target = _corrected_runtime(tmp_path)
    with sqlite3.connect(target) as conn:
        conn.execute("DROP TRIGGER cognitive_state_outbox_no_update")
        conn.execute(
            "UPDATE cognitive_state_outbox SET payload_hash=? "
            "WHERE command_type='correct_prediction_terminal_from_outcome'",
            ("sha256:" + "a" * 64,),
        )

    report = audit_prediction_outcome_lineage(
        delivery_db=delivery,
        target_db=target,
        repo_root=Path(__file__).resolve().parents[3],
        raw_db=tmp_path / "raw_events.db",
    )

    assert report["ok"] is False
    assert report["metrics"][
        "prediction_terminal_correction_receipt_mismatch"
    ] == 1


def test_prediction_lineage_audit_rejects_correction_receipt_tamper(
    tmp_path: Path,
) -> None:
    delivery, target = _corrected_runtime(tmp_path)
    with sqlite3.connect(target) as conn:
        conn.execute("DROP TRIGGER cognitive_state_effect_receipts_no_update")
        conn.execute(
            "UPDATE cognitive_state_effect_receipts SET before_hash=? "
            "WHERE command_id IN ("
            "SELECT command_id FROM cognitive_state_outbox "
            "WHERE command_type='correct_prediction_terminal_from_outcome'"
            ")",
            ("sha256:" + "b" * 64,),
        )

    report = audit_prediction_outcome_lineage(
        delivery_db=delivery,
        target_db=target,
        repo_root=Path(__file__).resolve().parents[3],
        raw_db=tmp_path / "raw_events.db",
    )

    assert report["ok"] is False
    assert report["metrics"][
        "prediction_terminal_correction_receipt_mismatch"
    ] == 1


def test_prediction_lineage_audit_detects_post_activation_unsealed_delivery(
    tmp_path: Path,
) -> None:
    delivery, target = _migrated_runtime(tmp_path)
    with sqlite3.connect(delivery) as conn:
        conn.execute(
            "INSERT INTO delivery_events(event_id, created_at, channel, decision) "
            "VALUES ('unsealed-runtime', '2099-01-01T00:00:00+00:00', "
            "'predictive_push', 'deliver')"
        )

    report = audit_prediction_outcome_lineage(
        delivery_db=delivery,
        target_db=target,
        repo_root=Path(__file__).resolve().parents[3],
    )

    assert report["ok"] is False
    assert report["metrics"][
        "predictive_delivery_without_presealed_prediction"
    ] == 1
    assert report["metrics"]["historical_predictive_object_uncovered"] == 0


def test_prediction_lineage_audit_detects_payload_and_probability_tamper(
    tmp_path: Path,
) -> None:
    delivery, target = _migrated_runtime(tmp_path)
    with sqlite3.connect(target) as conn:
        conn.execute("DROP TRIGGER cognitive_state_revisions_no_update")
        conn.execute(
            "UPDATE cognitive_state_revisions SET payload_json=json_set("
            "payload_json, '$.confidence.is_probability', json('true')) "
            "WHERE object_type='prediction_record'"
        )

    report = audit_prediction_outcome_lineage(
        delivery_db=delivery,
        target_db=target,
        repo_root=Path(__file__).resolve().parents[3],
    )

    assert report["ok"] is False
    assert report["metrics"]["prediction_payload_hash_mismatch"] >= 1
    assert report["metrics"]["score_band_used_as_probability"] == 1


def test_prediction_lineage_audit_detects_historical_source_content_drift(
    tmp_path: Path,
) -> None:
    delivery, target = _migrated_runtime(tmp_path)
    with sqlite3.connect(delivery) as conn:
        conn.execute(
            "UPDATE delivery_events SET reason='tampered' "
            "WHERE event_id='historical-0'"
        )

    report = audit_prediction_outcome_lineage(
        delivery_db=delivery,
        target_db=target,
        repo_root=Path(__file__).resolve().parents[3],
    )

    assert report["ok"] is False
    assert report["metrics"]["historical_predictive_object_uncovered"] == 1


def test_prediction_lineage_audit_recomputes_terminal_error_after_rehash(
    tmp_path: Path,
) -> None:
    delivery, target = _measured_runtime(tmp_path)
    _rewrite_current_payload(
        target,
        "prediction_record",
        lambda payload: payload["error"].update(value=1),
    )

    report = audit_prediction_outcome_lineage(
        delivery_db=delivery,
        target_db=target,
        repo_root=Path(__file__).resolve().parents[3],
    )

    assert report["ok"] is False
    assert report["metrics"]["ineligible_measurement_used_for_error"] >= 1


def test_prediction_lineage_audit_recomputes_unknown_vs_censored(tmp_path: Path) -> None:
    delivery, target = _migrated_runtime(tmp_path)
    state = CognitiveStateStore(target)
    prediction = state.current_revisions(object_type="prediction_record")[0]
    mature_at = datetime.fromisoformat(
        prediction.payload["evaluation_window"]["ends_at"]
    ) + timedelta(seconds=1)
    PredictionRecordStore(state).finalize(prediction.object_id, {}, mature_at)
    _rewrite_current_payload(
        target,
        "prediction_record",
        lambda payload: payload["terminal"].update(state="unknown"),
    )

    report = audit_prediction_outcome_lineage(
        delivery_db=delivery,
        target_db=target,
        repo_root=Path(__file__).resolve().parents[3],
        now=mature_at,
    )

    assert report["ok"] is False
    assert report["metrics"]["terminal_state_derivation_mismatch"] >= 1


def test_prediction_lineage_audit_rejects_reaction_as_objective_outcome(
    tmp_path: Path,
) -> None:
    delivery, target = _measured_runtime(tmp_path)

    def use_reaction(payload):
        payload["raw_evidence"]["refs"] = ["user-reaction:clicked"]
        payload["attribution"]["evidence_refs"] = ["user-reaction:clicked"]

    outcome_hash = _rewrite_current_payload(target, "outcome_measurement", use_reaction)
    _rebind_terminal_outcome_hash(target, outcome_hash)

    report = audit_prediction_outcome_lineage(
        delivery_db=delivery,
        target_db=target,
        repo_root=Path(__file__).resolve().parents[3],
    )

    assert report["ok"] is False
    assert report["metrics"]["reaction_used_as_objective_outcome"] == 1


def test_prediction_lineage_audit_recomputes_authority_catalog_raw_span_binding(
    tmp_path: Path,
) -> None:
    delivery, target = _measured_runtime(tmp_path)

    def forge_catalog(payload):
        authority = payload["source_authority"]
        forged_hash = "sha256:" + "f" * 64
        authority["source_authority_entry"]["content_sha256"] = forged_hash
        authority["source_authority_catalog"]["entries"][0][
            "content_sha256"
        ] = forged_hash
        authority["source_authority_catalog_hash"] = sha256_json(
            authority["source_authority_catalog"]
        )

    outcome_hash = _rewrite_current_payload(target, "outcome_measurement", forge_catalog)
    _rebind_terminal_outcome_hash(target, outcome_hash)

    report = audit_prediction_outcome_lineage(
        delivery_db=delivery,
        target_db=target,
        repo_root=Path(__file__).resolve().parents[3],
    )

    assert report["ok"] is False
    assert report["metrics"]["ineligible_measurement_used_for_error"] >= 1


def test_prediction_lineage_audit_recomputes_code_and_spec_identity(
    tmp_path: Path,
) -> None:
    delivery, target = _migrated_runtime(tmp_path)
    _rewrite_current_payload(
        target,
        "prediction_record",
        lambda payload: payload["confidence"].update(
            code_hash="sha256:" + "0" * 64,
            spec_hash="sha256:" + "1" * 64,
        ),
    )

    report = audit_prediction_outcome_lineage(
        delivery_db=delivery,
        target_db=target,
        repo_root=Path(__file__).resolve().parents[3],
    )

    assert report["ok"] is False
    assert report["metrics"]["prediction_payload_hash_mismatch"] >= 1


@pytest.mark.parametrize("dimension", ("metric", "unit", "subject", "acl", "attribution"))
def test_prediction_lineage_audit_detects_outcome_binding_tamper(
    tmp_path: Path,
    dimension: str,
) -> None:
    delivery, target = _measured_runtime(tmp_path)

    def mutate(payload):
        if dimension == "metric":
            payload["metric"]["metric_id"] = "different_metric"
        elif dimension == "unit":
            payload["metric"]["unit"] = "different_unit"
        elif dimension == "subject":
            payload["subject"]["id"] = "different-subject"
        elif dimension == "acl":
            payload["access_control"]["scope"]["scope_id"] = "different-scope"
        else:
            payload["attribution"]["competing_causes"] = [
                {
                    "cause": "unmodeled intervention",
                    "evidence_refs": list(payload["attribution"]["evidence_refs"]),
                }
            ]

    outcome_hash = _rewrite_current_payload(target, "outcome_measurement", mutate)
    _rebind_terminal_outcome_hash(target, outcome_hash)

    report = audit_prediction_outcome_lineage(
        delivery_db=delivery,
        target_db=target,
        repo_root=Path(__file__).resolve().parents[3],
    )

    assert report["ok"] is False
    assert report["metrics"]["ineligible_measurement_used_for_error"] >= 1 or report[
        "metrics"
    ]["prediction_payload_hash_mismatch"] >= 1


def test_prediction_lineage_audit_fails_closed_on_schema_corruption(
    tmp_path: Path,
) -> None:
    delivery, target = _migrated_runtime(tmp_path)
    with sqlite3.connect(target) as conn:
        conn.execute("DROP TRIGGER cognitive_state_revisions_no_update")

    report = audit_prediction_outcome_lineage(
        delivery_db=delivery,
        target_db=target,
        repo_root=Path(__file__).resolve().parents[3],
    )

    assert report["ok"] is False
    assert report["additional_metrics"]["schema_or_activation_error"] == 1
