#!/usr/bin/env python3
"""Independently audit the aggregate Phase 3 cognition-to-model chain."""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.access_control import (  # noqa: E402
    validate_cognitive_access_envelope,
)
from core.cognitive.decision_snapshot_access import (  # noqa: E402
    DECISION_SNAPSHOT_OUTPUT_PURPOSE,
    DECISION_SNAPSHOT_SOURCE_PURPOSES,
)
from core.cognitive.delivery_router import resolve_delivery_db_path  # noqa: E402
from core.cognitive.feedback_attribution_audit import (  # noqa: E402
    audit_feedback_attribution,
)
from core.cognitive.prediction_lineage_audit import (  # noqa: E402
    audit_prediction_outcome_lineage,
)
from core.cognitive.state_contract import LocalConsumerCommand  # noqa: E402
from core.cognitive.state_schema import inspect_cognitive_state_schema  # noqa: E402
from core.cognitive.state_store import CognitiveStateStore  # noqa: E402
from core.cognitive.training_contract import (  # noqa: E402
    TRAINING_ADMISSION_COMMAND,
    TRAINING_ADMISSION_CONSUMER,
    validate_training_admission_intake_payload,
)
from core.cognitive.training_governance_audit import (  # noqa: E402
    audit_training_governance,
)
from core.scoring.training_schema import inspect_training_schema  # noqa: E402
from scripts.audit_belief_revision_lineage import (  # noqa: E402
    audit_belief_revision_lineage,
)
from scripts.audit_decision_trace_effects import (  # noqa: E402
    audit_decision_trace_effects,
)


AUDIT_SCHEMA_VERSION = "mnemos.phase3_cognitive_chain_audit.v1"
ZERO_BUDGET_METRICS = (
    "decision_belief_candidate_gap",
    "decision_source_purpose_contract_gap",
    "eligible_feedback_without_admission_intake",
    "terminal_training_target_without_admission",
    "mature_training_intake_pending_without_reason",
    "immature_or_open_prediction_admitted",
    "admission_upstream_revision_gap",
    "corrected_sample_still_active",
    "correction_dependent_run_or_model_not_stale",
    "stale_model_head_active",
    "training_intake_without_terminal_receipt",
    "aggregate_chain_test_denominator_gap",
)
_TERMINAL_RECEIPT_STATUSES = frozenset(
    {
        "committed",
        "failed_terminal",
        "intentional_skip",
        "rejected",
        "revoked",
        "dead_letter",
    }
)
_REQUIRED_CHAIN_TESTS: Mapping[str, tuple[str, ...]] = {
    "tests/unit/cognitive/test_decision_trace.py": (
        "test_record_decision_consumes_same_scope_belief_through_typed_purpose",
    ),
    "tests/unit/cognitive/test_training_governance_store.py": (
        "test_durable_admission_intake_survives_feedback_closure_and_restart",
        "test_admission_waits_for_maturity_and_current_measured_prediction",
        "test_mature_outcome_stays_deferred_while_prediction_head_is_open",
        "test_admission_intake_crash_replays_only_missing_effects",
        "test_public_correction_crash_replays_each_missing_effect_once",
        "test_corrected_outcome_excludes_old_sample_stales_model_and_requires_rebuild",
        "test_build_ready_run_revalidates_complete_admission_upstream_before_write",
    ),
    "tests/integration/test_scorer_v2_training_loop.py": (
        "test_objective_outcome_trains_and_activates_exact_governed_model",
    ),
    "tests/unit/test_data_ownership_object_provenance_adapters.py": (
        "test_data_ownership_delete_verifies_only_after_three_object_owners_apply",
    ),
}


def audit_phase3_cognitive_chain_static(*, repo_root: Path) -> dict[str, Any]:
    """Verify fixed ownership, test denominator, and all required gate surfaces."""

    repository = Path(repo_root).resolve()
    metrics = {name: 0 for name in ZERO_BUDGET_METRICS}
    purpose_gap = int(
        DECISION_SNAPSHOT_SOURCE_PURPOSES.get("belief_revision") != "belief_read"
    ) + int(DECISION_SNAPSHOT_OUTPUT_PURPOSE != "cognitive_state_read")
    metrics["decision_source_purpose_contract_gap"] = purpose_gap
    missing_tests = _missing_test_symbols(repository)
    gate_gaps, registered_gates = _gate_registration_gaps(repository)
    owner_paths = (
        repository / "core/cognitive/training_intake_derivation.py",
        repository / "core/cognitive/phase3_training_intake_reconciliation.py",
        repository / "scripts/reconcile_phase3_training_admission_intakes.py",
        repository / "scripts/audit_phase3_cognitive_chain.py",
    )
    owner_gaps = sum(not path.is_file() for path in owner_paths)
    metrics["aggregate_chain_test_denominator_gap"] = (
        len(missing_tests) + gate_gaps + owner_gaps
    )
    return _report(
        metrics,
        audit_mode="static_only",
        denominators={
            "required_chain_tests": sum(
                len(symbols) for symbols in _REQUIRED_CHAIN_TESTS.values()
            ),
            "present_chain_tests": (
                sum(len(symbols) for symbols in _REQUIRED_CHAIN_TESTS.values())
                - len(missing_tests)
            ),
            "required_gate_surfaces": 4,
            "registered_gate_surfaces": registered_gates,
            "required_owner_paths": len(owner_paths),
        },
        findings=(
            [
                {
                    "metric": "aggregate_chain_test_denominator_gap",
                    "code": "missing_chain_test",
                    "evidence": value,
                }
                for value in missing_tests
            ]
        ),
    )


def audit_phase3_chain_state(
    *,
    database_dir: Path,
    repo_root: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Recompute cross-domain gaps directly from canonical state and projection DBs."""

    root = Path(database_dir).expanduser()
    repository = Path(repo_root).resolve()
    timestamp = now or datetime.now(timezone.utc)
    static = audit_phase3_cognitive_chain_static(repo_root=repository)
    metrics = {name: int(static["metrics"][name]) for name in ZERO_BUDGET_METRICS}
    denominators = {
        **static["denominators"],
        "belief_candidates": 0,
        "decision_traces": 0,
        "objective_attributions": 0,
        "eligible_training_targets": 0,
        "admission_intakes": 0,
        "terminal_admission_intakes": 0,
        "mature_terminal_targets": 0,
        "admitted_samples": 0,
        "corrected_admitted_samples": 0,
        "dependent_runs": 0,
        "active_model_heads": 0,
    }
    findings = list(static["findings"])
    state_path = root / "producer_consumer_ledger.db"
    if not state_path.is_file():
        metrics["aggregate_chain_test_denominator_gap"] += 1
        findings.append(
            {
                "metric": "aggregate_chain_test_denominator_gap",
                "code": "canonical_state_not_initialized",
            }
        )
        return _report(
            metrics,
            audit_mode="runtime",
            denominators=denominators,
            findings=findings,
        )

    state = CognitiveStateStore(state_path)
    with _connect(state_path) as conn:
        inspection = inspect_cognitive_state_schema(conn)
        if not inspection.ok:
            metrics["aggregate_chain_test_denominator_gap"] += 1
            findings.append(
                {
                    "metric": "aggregate_chain_test_denominator_gap",
                    "code": "canonical_state_schema_invalid",
                }
            )
            return _report(
                metrics,
                audit_mode="runtime",
                denominators=denominators,
                findings=findings,
            )
        revisions = _revision_index(conn)
        current_ids = _current_revision_ids(conn)
        commands = _command_index(conn)
        receipts = _receipt_index(conn)

    current = {
        revision_id: revision
        for revision_id, revision in revisions.items()
        if revision_id in current_ids
    }
    _audit_belief_to_decision(
        current,
        metrics=metrics,
        denominators=denominators,
    )
    target_context = _audit_feedback_to_training(
        state,
        current=current,
        revisions=revisions,
        commands=commands,
        receipts=receipts,
        now=timestamp,
        metrics=metrics,
        denominators=denominators,
    )
    corrected_admissions = _audit_admissions(
        state,
        current=current,
        revisions=revisions,
        commands=commands,
        receipts=receipts,
        now=timestamp,
        metrics=metrics,
        denominators=denominators,
    )
    _audit_runs_and_models(
        root,
        current=current,
        corrected_admissions=corrected_admissions,
        metrics=metrics,
        denominators=denominators,
    )
    denominators["eligible_training_targets"] = len(target_context)
    return _report(
        metrics,
        audit_mode="runtime",
        denominators=denominators,
        findings=findings,
    )


def audit_phase3_cognitive_chain(
    *,
    database_dir: Path,
    repo_root: Path,
    delivery_db: Path | None = None,
) -> dict[str, Any]:
    """Compose individual audits and the independent cross-domain challenger."""

    root = Path(database_dir).expanduser()
    repository = Path(repo_root).resolve()
    report = audit_phase3_chain_state(
        database_dir=root,
        repo_root=repository,
    )
    metrics = dict(report["metrics"])
    denominators = dict(report["denominators"])
    findings = list(report["findings"])
    domain_reports = _domain_audits(
        database_dir=root,
        repo_root=repository,
        delivery_db=delivery_db,
    )
    denominators["required_domain_audits"] = len(domain_reports)
    denominators["passed_domain_audits"] = sum(
        bool(item["ok"]) for item in domain_reports.values()
    )
    failed_domains = [name for name, item in domain_reports.items() if not item["ok"]]
    if failed_domains:
        metrics["aggregate_chain_test_denominator_gap"] += len(failed_domains)
        findings.extend(
            {
                "metric": "aggregate_chain_test_denominator_gap",
                "code": "required_domain_audit_failed",
                "evidence": name,
            }
            for name in failed_domains
        )
    combined = _report(
        metrics,
        audit_mode="full",
        denominators=denominators,
        findings=findings,
    )
    combined["domain_audits"] = domain_reports
    return combined


def _audit_belief_to_decision(
    current: Mapping[str, Mapping[str, Any]],
    *,
    metrics: dict[str, int],
    denominators: dict[str, int],
) -> None:
    beliefs = tuple(
        item for item in current.values() if item["object_type"] == "belief_revision"
    )
    decisions = tuple(
        item for item in current.values() if item["object_type"] == "decision_trace"
    )
    denominators["decision_traces"] = len(decisions)
    for belief in beliefs:
        try:
            access = validate_cognitive_access_envelope(
                belief["payload"]["access_control"],
                expected_scope_type=belief["scope_type"],
                expected_scope_id=belief["scope_id"],
            )
        except (KeyError, TypeError, ValueError):
            metrics["decision_source_purpose_contract_gap"] += 1
            continue
        if "belief_read" not in access["purposes"]:
            metrics["decision_source_purpose_contract_gap"] += 1
    for decision in decisions:
        referenced = set(str(value) for value in decision["payload"]["belief_revision_refs"])
        for belief in beliefs:
            if not _belief_is_decision_candidate(belief, decision):
                continue
            denominators["belief_candidates"] += 1
            if belief["revision_id"] not in referenced:
                metrics["decision_belief_candidate_gap"] += 1


def _belief_is_decision_candidate(
    belief: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> bool:
    if (
        belief["scope_type"] != decision["scope_type"]
        or belief["scope_id"] != decision["scope_id"]
        or _parse_time(belief["created_at"]) > _parse_time(decision["created_at"])
    ):
        return False
    try:
        belief_access = validate_cognitive_access_envelope(
            belief["payload"]["access_control"]
        )
        decision_access = validate_cognitive_access_envelope(
            decision["payload"]["access_control"]
        )
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        "belief_read" in belief_access["purposes"]
        and belief_access["owner"] == decision_access["owner"]
        and belief_access["scope"] == decision_access["scope"]
        and belief_access["visibility"] == decision_access["visibility"]
        and belief_access["consent"]["status"]
        == decision_access["consent"]["status"]
    )


def _audit_feedback_to_training(
    state: CognitiveStateStore,
    *,
    current: Mapping[str, Mapping[str, Any]],
    revisions: Mapping[str, Mapping[str, Any]],
    commands: Mapping[str, Mapping[str, Any]],
    receipts: Mapping[str, Mapping[str, Any]],
    now: datetime,
    metrics: dict[str, int],
    denominators: dict[str, int],
) -> dict[str, dict[str, Any]]:
    by_revision: dict[str, list[Mapping[str, Any]]] = {}
    for command in commands.values():
        by_revision.setdefault(str(command["revision_id"]), []).append(command)
    eligible: dict[str, dict[str, Any]] = {}
    for attribution in current.values():
        payload = attribution["payload"]
        if (
            attribution["object_type"] != "feedback_attribution_record"
            or payload.get("evidence_class") != "objective_outcome"
            or payload.get("disposition") != "objective_only"
        ):
            continue
        denominators["objective_attributions"] += 1
        source_commands = by_revision.get(str(attribution["revision_id"]), [])
        targets = [
            command
            for command in source_commands
            if command["consumer_id"] == "training_evidence"
            and command["command_type"] == "evaluate_feedback_target"
        ]
        intakes = [
            command
            for command in source_commands
            if command["consumer_id"] == TRAINING_ADMISSION_CONSUMER
            and command["command_type"] == TRAINING_ADMISSION_COMMAND
        ]
        denominators["admission_intakes"] += len(intakes)
        for intake in intakes:
            try:
                validate_training_admission_intake_payload(intake["payload"])
            except (KeyError, TypeError, ValueError):
                metrics["eligible_feedback_without_admission_intake"] += 1
                continue
            receipt = receipts.get(str(intake["command_id"]))
            if receipt is None or receipt["status"] not in _TERMINAL_RECEIPT_STATUSES:
                metrics["training_intake_without_terminal_receipt"] += 1
            else:
                denominators["terminal_admission_intakes"] += 1
        if len(targets) != 1:
            if targets:
                metrics["eligible_feedback_without_admission_intake"] += len(targets)
            continue
        target = targets[0]
        try:
            state.validate_feedback_effect_receipt(str(target["command_id"]))
        except (KeyError, RuntimeError, TypeError, ValueError):
            continue
        target_receipt = receipts.get(str(target["command_id"]))
        if target_receipt is None or target_receipt["status"] != "committed":
            continue
        matching_intakes = [
            intake
            for intake in intakes
            if intake["payload"].get("training_target_ref")
            == {
                "command_id": target["command_id"],
                "payload_hash": target["payload_hash"],
            }
        ]
        if len(matching_intakes) != 1:
            metrics["eligible_feedback_without_admission_intake"] += 1
        outcome = _target_outcome(target, revisions, current)
        mature_terminal = _outcome_is_mature_terminal(
            outcome,
            current=current,
            now=now,
        )
        if mature_terminal:
            denominators["mature_terminal_targets"] += 1
            terminal_intakes = [
                intake
                for intake in matching_intakes
                if receipts.get(str(intake["command_id"]), {}).get("status")
                in _TERMINAL_RECEIPT_STATUSES
            ]
            if not terminal_intakes:
                metrics["mature_training_intake_pending_without_reason"] += 1
        eligible[str(target["command_id"])] = {
            "target": target,
            "outcome": outcome,
            "mature_terminal": mature_terminal,
            "intakes": matching_intakes,
        }

    admissions_by_target = _current_admissions_by_target(current)
    for command_id, context in eligible.items():
        if context["mature_terminal"] and not admissions_by_target.get(command_id):
            metrics["terminal_training_target_without_admission"] += 1
    return eligible


def _audit_admissions(
    state: CognitiveStateStore,
    *,
    current: Mapping[str, Mapping[str, Any]],
    revisions: Mapping[str, Mapping[str, Any]],
    commands: Mapping[str, Mapping[str, Any]],
    receipts: Mapping[str, Mapping[str, Any]],
    now: datetime,
    metrics: dict[str, int],
    denominators: dict[str, int],
) -> set[str]:
    corrected: set[str] = set()
    for admission in current.values():
        if (
            admission["object_type"] != "training_admission_record"
            or admission["payload"].get("lifecycle_state") != "admitted"
        ):
            continue
        denominators["admitted_samples"] += 1
        payload = admission["payload"]
        outcome_ref = payload.get("outcome_ref") or {}
        outcome = revisions.get(str(outcome_ref.get("revision_id") or ""))
        current_outcome = _current_for_object(
            current,
            object_type="outcome_measurement",
            object_id=str(outcome_ref.get("object_id") or ""),
        )
        if outcome is None or current_outcome is None or (
            current_outcome["revision_id"] != outcome_ref.get("revision_id")
        ):
            metrics["corrected_sample_still_active"] += 1
            denominators["corrected_admitted_samples"] += 1
            corrected.add(str(admission["revision_id"]))
        if not _admission_is_mature_terminal(
            admission,
            outcome=outcome,
            current=current,
            now=now,
        ):
            metrics["immature_or_open_prediction_admitted"] += 1
        if not _admission_upstream_is_current(
            state,
            admission,
            current=current,
            revisions=revisions,
            commands=commands,
            receipts=receipts,
        ):
            metrics["admission_upstream_revision_gap"] += 1
    return corrected


def _audit_runs_and_models(
    root: Path,
    *,
    current: Mapping[str, Mapping[str, Any]],
    corrected_admissions: set[str],
    metrics: dict[str, int],
    denominators: dict[str, int],
) -> None:
    runs = {
        str(item["revision_id"]): item
        for item in current.values()
        if item["object_type"] == "training_run_record"
    }
    affected_runs: set[str] = set()
    for revision_id, run in runs.items():
        refs = {
            str(item["revision_id"])
            for item in run["payload"].get("admission_refs", ())
        }
        if refs & corrected_admissions:
            affected_runs.add(revision_id)
            denominators["dependent_runs"] += 1
            if run["payload"].get("state") != "stale":
                metrics["correction_dependent_run_or_model_not_stale"] += 1
    scoring_path = root / "mnemos.db"
    if not scoring_path.is_file():
        if runs:
            metrics["stale_model_head_active"] += 1
        return
    with _connect(scoring_path) as conn:
        inspection = inspect_training_schema(conn)
        if inspection.classification != "canonical" or not inspection.ok:
            if runs:
                metrics["stale_model_head_active"] += 1
            return
        heads = conn.execute(
            "SELECT dimension, model_id, run_revision_id "
            "FROM governed_scorer_model_heads ORDER BY dimension"
        ).fetchall()
    denominators["active_model_heads"] = len(heads)
    for row in heads:
        run_id = str(row["run_revision_id"])
        head_run = runs.get(run_id)
        if head_run is None or head_run["payload"].get("state") != "applied":
            metrics["stale_model_head_active"] += 1
        if run_id in affected_runs:
            metrics["correction_dependent_run_or_model_not_stale"] += 1


def _target_outcome(
    target: Mapping[str, Any],
    revisions: Mapping[str, Mapping[str, Any]],
    current: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    ref = target["payload"].get("objective_outcome_ref")
    if not isinstance(ref, Mapping) or ref.get("state") != "available":
        return None
    outcome = revisions.get(str(ref.get("revision_id") or ""))
    if (
        outcome is None
        or outcome["object_type"] != "outcome_measurement"
        or outcome["object_id"] != ref.get("outcome_id")
        or outcome["payload_hash"] != ref.get("payload_hash")
    ):
        return None
    current_outcome = _current_for_object(
        current,
        object_type="outcome_measurement",
        object_id=str(outcome["object_id"]),
    )
    return outcome if current_outcome == outcome else None


def _outcome_is_mature_terminal(
    outcome: Mapping[str, Any] | None,
    *,
    current: Mapping[str, Mapping[str, Any]],
    now: datetime,
) -> bool:
    if outcome is None:
        return False
    try:
        if _parse_time(outcome["payload"]["maturity"]["matured_at"]) > now:
            return False
        prediction_ref = outcome["payload"]["prediction_ref"]
        prediction = _current_for_object(
            current,
            object_type="prediction_record",
            object_id=str(prediction_ref["prediction_id"]),
        )
        terminal = prediction["payload"]["terminal"] if prediction else None
        outcome_ref = prediction["payload"]["outcome_ref"] if prediction else None
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        isinstance(terminal, Mapping)
        and terminal.get("state")
        in {"measured", "unknown", "censored", "confounded"}
        and isinstance(outcome_ref, Mapping)
        and outcome_ref.get("revision_id") == outcome["revision_id"]
        and outcome_ref.get("payload_hash") == outcome["payload_hash"]
    )


def _admission_is_mature_terminal(
    admission: Mapping[str, Any],
    *,
    outcome: Mapping[str, Any] | None,
    current: Mapping[str, Mapping[str, Any]],
    now: datetime,
) -> bool:
    if not _outcome_is_mature_terminal(outcome, current=current, now=now):
        return False
    try:
        temporal = admission["payload"]["temporal_proof"]
        terminal_ref = admission["payload"]["prediction_terminal_ref"]
        prediction = current.get(str(terminal_ref["revision_id"]))
        if prediction is None:
            return False
        effective_at = _parse_time(temporal["admission_effective_at"])
        matured_at = _parse_time(temporal["outcome_matured_at"])
        evaluated_at = _parse_time(
            prediction["payload"]["terminal"]["evaluated_at"]
        )
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        prediction["object_type"] == "prediction_record"
        and terminal_ref["payload_hash"] == prediction["payload_hash"]
        and terminal_ref["terminal_state"]
        == prediction["payload"]["terminal"]["state"]
        and effective_at >= matured_at
        and effective_at >= evaluated_at
    )


def _admission_upstream_is_current(
    state: CognitiveStateStore,
    admission: Mapping[str, Any],
    *,
    current: Mapping[str, Mapping[str, Any]],
    revisions: Mapping[str, Mapping[str, Any]],
    commands: Mapping[str, Mapping[str, Any]],
    receipts: Mapping[str, Mapping[str, Any]],
) -> bool:
    try:
        payload = admission["payload"]
        evidence_ref = payload["training_evidence_ref"]
        target = commands[str(evidence_ref["command_id"])]
        if (
            target["consumer_id"] != "training_evidence"
            or target["command_type"] != "evaluate_feedback_target"
            or target["payload_hash"] != evidence_ref["command_payload_hash"]
        ):
            return False
        state.validate_feedback_effect_receipt(str(target["command_id"]))
        attribution = current.get(str(target["revision_id"]))
        if (
            attribution is None
            or attribution["object_type"] != "feedback_attribution_record"
            or attribution["payload_hash"]
            != evidence_ref["attribution_payload_hash"]
        ):
            return False
        intakes = [
            command
            for command in commands.values()
            if command["revision_id"] == attribution["revision_id"]
            and command["consumer_id"] == TRAINING_ADMISSION_CONSUMER
            and command["command_type"] == TRAINING_ADMISSION_COMMAND
            and command["payload"].get("training_target_ref")
            == {
                "command_id": target["command_id"],
                "payload_hash": target["payload_hash"],
            }
        ]
        if len(intakes) != 1:
            return False
        validate_training_admission_intake_payload(intakes[0]["payload"])
        intake_receipt = receipts.get(str(intakes[0]["command_id"]))
        if intake_receipt is None or intake_receipt["status"] != "committed":
            return False
        state.validate_training_admission_intake_receipt(
            str(intakes[0]["command_id"])
        )
        for ref_name, object_type in (
            ("outcome_ref", "outcome_measurement"),
            ("prediction_terminal_ref", "prediction_record"),
            ("decision_ref", "decision_trace"),
        ):
            ref = payload[ref_name]
            revision = revisions.get(str(ref["revision_id"]))
            if (
                revision is None
                or revision["object_type"] != object_type
                or revision["payload_hash"] != ref["payload_hash"]
            ):
                return False
            if ref_name != "decision_ref" and revision["revision_id"] not in current:
                return False
        return True
    except (KeyError, RuntimeError, TypeError, ValueError):
        return False


def _current_admissions_by_target(
    current: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = {}
    for revision in current.values():
        if revision["object_type"] != "training_admission_record":
            continue
        command_id = str(
            revision["payload"].get("training_evidence_ref", {}).get("command_id")
            or ""
        )
        if command_id:
            result.setdefault(command_id, []).append(revision)
    return result


def _current_for_object(
    current: Mapping[str, Mapping[str, Any]],
    *,
    object_type: str,
    object_id: str,
) -> Mapping[str, Any] | None:
    matches = [
        item
        for item in current.values()
        if item["object_type"] == object_type and item["object_id"] == object_id
    ]
    return matches[0] if len(matches) == 1 else None


def _domain_audits(
    *,
    database_dir: Path,
    repo_root: Path,
    delivery_db: Path | None,
) -> dict[str, dict[str, Any]]:
    state_db = database_dir / "producer_consumer_ledger.db"
    graph_db = database_dir / "cognitive_graph.db"
    resolved_delivery = Path(delivery_db or database_dir / "delivery_events.db")
    audits: dict[str, dict[str, Any]] = {}
    runners = {
        "COG-035": lambda: audit_belief_revision_lineage(
            live_state_db=state_db,
            live_graph_db=graph_db,
            strict=True,
        ),
        "COG-036": lambda: audit_decision_trace_effects(
            state_db=state_db,
            strict=True,
            root=repo_root,
            database_dir=database_dir,
        ),
        "COG-037": lambda: audit_prediction_outcome_lineage(
            delivery_db=resolved_delivery,
            target_db=state_db,
            repo_root=repo_root,
        ),
        "COG-038": lambda: audit_feedback_attribution(
            database_dir=database_dir,
            repo_root=repo_root,
        ),
        "COG-048": lambda: audit_training_governance(
            database_dir=database_dir,
            repo_root=repo_root,
        ),
    }
    for name, runner in runners.items():
        try:
            report = runner()
            audits[name] = {
                "schema_version": str(report.get("schema_version") or ""),
                "ok": bool(report.get("ok")),
                "status": str(report.get("status") or ("pass" if report.get("ok") else "fail")),
            }
        except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error) as exc:
            audits[name] = {
                "schema_version": "",
                "ok": False,
                "status": "error",
                "error": type(exc).__name__,
            }
    return audits


def _revision_index(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM cognitive_state_revisions ORDER BY revision_id"
    ).fetchall()
    return {
        str(row["revision_id"]): {
            "revision_id": str(row["revision_id"]),
            "object_type": str(row["object_type"]),
            "object_id": str(row["object_id"]),
            "payload": json.loads(str(row["payload_json"])),
            "payload_hash": str(row["payload_hash"]),
            "scope_type": str(row["scope_type"]),
            "scope_id": str(row["scope_id"]),
            "supersedes_revision_id": str(row["supersedes_revision_id"] or ""),
            "correction_of_revision_id": str(
                row["correction_of_revision_id"] or ""
            ),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    }


def _current_revision_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT h.revision_id
        FROM cognitive_state_heads AS h
        WHERE NOT EXISTS (
            SELECT 1 FROM cognitive_state_outbox AS tombstone
            WHERE tombstone.command_type='tombstone_cognitive_state'
              AND EXISTS (
                  SELECT 1 FROM json_each(
                      tombstone.payload_json, '$.target_revision_ids'
                  ) AS target
                  WHERE target.value=h.revision_id
              )
        )
        """
    ).fetchall()
    return {str(row[0]) for row in rows}


def _command_index(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM cognitive_state_outbox ORDER BY command_id"
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = json.loads(str(row["payload_json"]))
        command = LocalConsumerCommand.create(
            revision_id=str(row["revision_id"]),
            consumer_id=str(row["consumer_id"]),
            command_type=str(row["command_type"]),
            payload=payload,
            created_at=str(row["created_at"]),
        )
        result[str(row["command_id"])] = {
            "command_id": str(row["command_id"]),
            "revision_id": str(row["revision_id"]),
            "event_id": str(row["event_id"]),
            "consumer_id": str(row["consumer_id"]),
            "command_type": str(row["command_type"]),
            "payload": payload,
            "payload_hash": str(row["payload_hash"]),
            "created_at": str(row["created_at"]),
            "identity_valid": (
                command.command_id == row["command_id"]
                and command.payload_hash == row["payload_hash"]
            ),
        }
    return result


def _receipt_index(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM cognitive_state_effect_receipts ORDER BY command_id"
    ).fetchall()
    return {
        str(row["command_id"]): {
            "receipt_id": str(row["receipt_id"]),
            "command_id": str(row["command_id"]),
            "revision_id": str(row["revision_id"]),
            "consumer_id": str(row["consumer_id"]),
            "status": str(row["status"]),
            "target_effect_id": str(row["target_effect_id"]),
            "before_hash": str(row["before_hash"]),
            "after_hash": str(row["after_hash"]),
        }
        for row in rows
    }


def _missing_test_symbols(repo_root: Path) -> list[str]:
    missing: list[str] = []
    for relative, required in _REQUIRED_CHAIN_TESTS.items():
        path = repo_root / relative
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError):
            missing.extend(f"{relative}:{symbol}" for symbol in required)
            continue
        present = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        missing.extend(
            f"{relative}:{symbol}" for symbol in required if symbol not in present
        )
    return missing


def _gate_registration_gaps(repo_root: Path) -> tuple[int, int]:
    checks = (
        (
            repo_root / "scripts/run_local_gates.py",
            ("phase3 cognitive chain", "scripts/audit_phase3_cognitive_chain.py"),
        ),
        (
            repo_root / ".pre-commit-config.yaml",
            (
                "phase3-cognitive-chain",
                "scripts/audit_phase3_cognitive_chain.py --static-only --strict --json",
            ),
        ),
        (
            repo_root / ".github/workflows/ci.yml",
            (
                "scripts/audit_phase3_cognitive_chain.py --static-only --strict --json",
            ),
        ),
        (
            repo_root / "scripts/run_full_score_gates.py",
            ("contracts.phase3_cognitive_chain", "audit_phase3_cognitive_chain.py"),
        ),
    )
    gaps = 0
    registered = 0
    for path, needles in checks:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            gaps += 1
            continue
        if all(needle in content for needle in needles):
            registered += 1
        else:
            gaps += 1
    return gaps, registered


def _report(
    metrics: Mapping[str, int],
    *,
    audit_mode: str,
    denominators: Mapping[str, int],
    findings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    normalized = {name: int(metrics[name]) for name in ZERO_BUDGET_METRICS}
    all_findings = list(findings)
    represented = {str(item.get("metric") or "") for item in all_findings}
    all_findings.extend(
        {
            "metric": name,
            "code": name,
            "count": count,
        }
        for name, count in normalized.items()
        if count and name not in represented
    )
    ok = all(value == 0 for value in normalized.values())
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "audit_mode": audit_mode,
        "ok": ok,
        "status": "pass" if ok else "fail",
        "metrics": normalized,
        "denominators": dict(denominators),
        "findings": all_findings,
        "sensitive_bytes_in_report": 0,
    }


def _parse_time(value: Any) -> datetime:
    timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("phase3 audit timestamp must be timezone-aware")
    return timestamp.astimezone(timezone.utc)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{Path(path).resolve(strict=True)}?mode=ro",
        uri=True,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-dir", type=Path)
    parser.add_argument("--delivery-db", type=Path)
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.static_only:
        report = audit_phase3_cognitive_chain_static(repo_root=ROOT)
    else:
        from core.config import get_config

        config = get_config()
        database_dir = Path(args.database_dir or config.database_dir).expanduser()
        delivery_db = resolve_delivery_db_path(
            config=config,
            database_dir=database_dir,
            explicit=args.delivery_db,
        )
        report = audit_phase3_cognitive_chain(
            database_dir=database_dir,
            repo_root=ROOT,
            delivery_db=delivery_db,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if args.strict and not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
