"""Independent strict audit for the canonical COG-038 feedback owner."""

from __future__ import annotations

import base64
import binascii
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from core.cognitive.feedback_attribution_principal_audit import (
    audit_attribution_principals,
)
from core.cognitive.feedback_attribution_reaction_audit import (
    independent_reaction_payload_valid,
)
from core.cognitive.feedback_attribution_history_audit import (
    audit_history_source_refs,
    audit_history_specs,
)
from core.cognitive.feedback_attribution_materiality_audit import (
    independent_weak_materiality_valid,
)
from core.cognitive.feedback_attribution_static_audit import (
    audit_feedback_static,
)
from core.cognitive.state_schema import inspect_cognitive_state_schema
from core.db_utils import render_sql
from core.cognitive.feedback_attribution_audit_support import (
    _connect,
    _finding,
    _json,
    _revision,
    _table_exists,
)


AUDIT_SCHEMA_VERSION = "mnemos.feedback_attribution_audit.v1"
FEEDBACK_TARGETS = (
    "belief_correction_proposal",
    "delivery_state",
    "persona_proposal",
    "policy_proposal",
    "reflection_evidence",
    "training_evidence",
    "trust_proposal",
)
TARGET_DB_FILE_BY_ID = {
    "belief_correction_proposal": "cognitive_graph.db",
    "delivery_state": "delivery_events.db",
    "persona_proposal": "user_signals.db",
    "policy_proposal": "policy_patches.db",
    "reflection_evidence": "reflections.db",
    "training_evidence": "mnemos.db",
    "trust_proposal": "trust_decisions.db",
}
TARGET_DOMAIN_TABLES = {
    target: (
        target.replace("_proposal", "").replace("_evidence", "")
        + "_feedback_proposals",
        target.replace("_proposal", "").replace("_evidence", "")
        + "_feedback_actions",
        target.replace("_proposal", "").replace("_evidence", "")
        + "_feedback_receipts",
    )
    for target in FEEDBACK_TARGETS
}
# These names deliberately duplicate the target-owner registry so the audit
# does not accept a writer-side registry drift as its own ground truth.
TARGET_DOMAIN_TABLES.update(
    {
        "belief_correction_proposal": (
            "belief_feedback_proposals",
            "belief_feedback_proposal_actions",
            "belief_feedback_proposal_receipts",
        ),
        "delivery_state": (
            "delivery_feedback_proposals",
            "delivery_feedback_proposal_actions",
            "delivery_feedback_proposal_receipts",
        ),
        "persona_proposal": (
            "persona_feedback_proposals",
            "persona_feedback_proposal_actions",
            "persona_feedback_proposal_receipts",
        ),
        "policy_proposal": (
            "policy_feedback_proposals",
            "policy_feedback_proposal_actions",
            "policy_feedback_proposal_receipts",
        ),
        "reflection_evidence": (
            "reflection_feedback_proposals",
            "reflection_feedback_proposal_actions",
            "reflection_feedback_proposal_receipts",
        ),
        "training_evidence": (
            "training_feedback_proposals",
            "training_feedback_proposal_actions",
            "training_feedback_proposal_receipts",
        ),
        "trust_proposal": (
            "trust_feedback_proposals",
            "trust_feedback_proposal_actions",
            "trust_feedback_proposal_receipts",
        ),
    }
)
TARGET_DOMAIN_OWNERS = {
    "belief_correction_proposal": "cognitive_graph_store",
    "delivery_state": "delivery_router",
    "persona_proposal": "persona_signal_store",
    "policy_proposal": "policy_patch_store",
    "reflection_evidence": "reflection_store",
    "training_evidence": "adaptive_scorer",
    "trust_proposal": "trusted_push",
}
ZERO_METRICS = (
    "feedback_without_subject",
    "unknown_action_default_positive",
    "reaction_used_as_objective_ground_truth",
    "duplicate_training_effect",
    "replayed_negative_trust_effect",
    "auto_update_from_weak_single_signal",
    "committed_effect_without_attribution",
    "reaction_target_effect_duplicate",
    "correction_without_latest_supersedes",
    "correction_effect_without_neutralization_receipt",
    "replacement_effect_before_compensation_complete",
    "target_receipt_reciprocity_gap",
    "feedback_owner_bypass",
    "legacy_feedback_object_uncovered",
    "historical_quarantine_promoted_active",
    "active_feedback_without_current_attribution",
    "feedback_schema_registry_mismatch",
    "feedback_migration_barrier_bypass",
    "feedback_command_without_terminal_receipt",
    "feedback_receipt_without_command",
    "pending_superseded_feedback_command",
    "current_target_terminal_gap",
    "formal_user_seam_bypass",
    "legacy_feedback_active_reader",
    "attribution_principal_binding_gap",
    "feedback_terminal_disposition_gap",
)


@dataclass(frozen=True)
class _AuditHistoryObject:
    domain: str
    database_class: str
    table: str
    primary_key: tuple[tuple[str, Any], ...]
    schema_fingerprint: str
    field_manifest: tuple[str, ...]
    source_refs: tuple[str, ...]
    row_hash: str
    projection_links: tuple[str, ...] = ()

    @property
    def source_key(self) -> str:
        identity = {
            "schema_version": "mnemos.feedback_history_inventory.v1",
            "domain": self.domain,
            "database_class": self.database_class,
            "table": self.table,
            "primary_key": dict(self.primary_key),
            "schema_fingerprint": self.schema_fingerprint,
        }
        return "feedback-history:" + _independent_sha256_json(identity).split(
            ":", 1
        )[1][:40]

    def quarantine_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "mnemos.historical_unattributed_feedback.v1",
            "source_identity": {
                "domain": self.domain,
                "database_class": self.database_class,
                "table": self.table,
                "primary_key": dict(self.primary_key),
                "primary_key_hash": _independent_sha256_json(
                    dict(self.primary_key)
                ),
                "schema_fingerprint": self.schema_fingerprint,
            },
            "row_hash": self.row_hash,
            "field_manifest": list(self.field_manifest),
            "source_refs": list(self.source_refs),
            "projection_links": list(self.projection_links),
            "semantic_state": "historical_unattributed_feedback",
            "active_promotion": False,
            "reaction_created": False,
            "attribution_created": False,
            "objective_outcome_created": False,
            "target_command_created": False,
            "training_admitted": False,
        }


def audit_feedback_attribution(
    *,
    database_dir: Path,
    repo_root: Path,
) -> dict[str, Any]:
    """Independently recompute canonical feedback and historical coverage."""

    root = Path(database_dir).expanduser()
    ledger = root / "producer_consumer_ledger.db"
    metrics = {name: 0 for name in ZERO_METRICS}
    denominators = {
        "reaction_revision_count": 0,
        "active_reaction_count": 0,
        "attribution_revision_count": 0,
        "active_attribution_count": 0,
        "feedback_command_count": 0,
        "feedback_effect_receipt_count": 0,
        "pending_feedback_command_count": 0,
        "command_without_receipt_count": 0,
        "receipt_without_command_count": 0,
        "current_target_expected_count": 0,
        "current_target_terminal_count": 0,
        "complete_registry_target_command_count": 0,
        "terminal_disposition_count": 0,
        "correction_command_count": 0,
        "neutralized_effect_count": 0,
        "active_objective_outcome_count": 0,
        "historical_feedback_object_count": 0,
        "historical_quarantine_object_count": 0,
        "static_python_file_count": 0,
        "attribution_principal_binding_expected_count": 0,
        "attribution_principal_binding_verified_count": 0,
        "formal_user_entrypoint_expected_count": 0,
        "formal_user_entrypoint_covered_count": 0,
    }
    findings: list[dict[str, Any]] = []
    initialized = ledger.is_file()
    if initialized:
        try:
            _audit_ledger(
                ledger,
                database_dir=root,
                metrics=metrics,
                denominators=denominators,
                findings=findings,
            )
        except (sqlite3.Error, ValueError, RuntimeError, TypeError) as exc:
            metrics["feedback_schema_registry_mismatch"] += 1
            findings.append(
                _finding("feedback_schema_registry_mismatch", str(exc)[:300])
            )
    try:
        inventory = _independent_history_inventory(root)
        coverage = _independent_history_coverage(ledger, inventory)
        denominators["historical_feedback_object_count"] = len(inventory)
        denominators["historical_quarantine_object_count"] = int(
            coverage["covered"]
        ) + int(coverage["unexpected"])
        metrics["legacy_feedback_object_uncovered"] = int(coverage["uncovered"])
        metrics["historical_quarantine_promoted_active"] = int(
            coverage["active_promotion"]
        )
        if coverage["unexpected"]:
            metrics["legacy_feedback_object_uncovered"] += int(coverage["unexpected"])
    except (OSError, sqlite3.Error, ValueError, RuntimeError, TypeError) as exc:
        metrics["legacy_feedback_object_uncovered"] += 1
        findings.append(_finding("legacy_feedback_object_uncovered", str(exc)[:300]))

    static = audit_feedback_static(Path(repo_root))
    metrics["feedback_owner_bypass"] = len(static["owner_bypasses"])
    metrics["feedback_migration_barrier_bypass"] = len(static["barrier_bypasses"])
    metrics["formal_user_seam_bypass"] = len(static["formal_user_seam_bypasses"])
    metrics["legacy_feedback_active_reader"] = len(static["legacy_active_readers"])
    denominators["static_python_file_count"] = static["python_file_count"]
    denominators["formal_user_entrypoint_expected_count"] = static[
        "formal_user_entrypoint_expected_count"
    ]
    denominators["formal_user_entrypoint_covered_count"] = static[
        "formal_user_entrypoint_covered_count"
    ]
    for metric, entries in (
        ("feedback_owner_bypass", static["owner_bypasses"]),
        ("feedback_migration_barrier_bypass", static["barrier_bypasses"]),
        ("formal_user_seam_bypass", static["formal_user_seam_bypasses"]),
        ("legacy_feedback_active_reader", static["legacy_active_readers"]),
    ):
        findings.extend(_finding(metric, item) for item in entries)

    for name, value in metrics.items():
        if value and not any(item["code"] == name for item in findings):
            findings.append(_finding(name, f"count={value}"))
    ok = all(value == 0 for value in metrics.values())
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "ok": ok,
        "status": ("pass" if initialized else "not_initialized") if ok else "fail",
        "metrics": metrics,
        "denominators": denominators,
        "findings": findings,
        "sensitive_bytes_in_report": 0,
    }


def _audit_ledger(
    ledger: Path,
    *,
    database_dir: Path,
    metrics: dict[str, int],
    denominators: dict[str, int],
    findings: list[dict[str, Any]],
) -> None:
    with _connect(ledger) as conn:
        schema = inspect_cognitive_state_schema(conn)
        if schema.classification != "canonical":
            metrics["feedback_schema_registry_mismatch"] += 1
            findings.append(
                _finding(
                    "feedback_schema_registry_mismatch",
                    schema.classification,
                )
            )
            return
        rows = conn.execute(
            """
            SELECT r.*, CASE WHEN h.revision_id IS NULL THEN 0 ELSE 1 END AS is_head
            FROM cognitive_state_revisions AS r
            LEFT JOIN cognitive_state_heads AS h ON h.revision_id=r.revision_id
            WHERE r.object_type IN (
                'user_reaction_event','feedback_attribution_record','outcome_measurement'
            )
            ORDER BY r.object_type, r.object_id, r.revision_no
            """
        ).fetchall()
        revisions = [_revision(row) for row in rows]
        causal_rows = conn.execute(
            "SELECT r.*, 0 AS is_head FROM cognitive_state_revisions AS r "
            "WHERE r.object_type IN ('decision_trace','prediction_record') "
            "ORDER BY r.object_type, r.object_id, r.revision_no"
        ).fetchall()
        causal_revisions = [_revision(row) for row in causal_rows]
        causal_revisions_by_id = {
            str(item["revision_id"]): item for item in causal_revisions
        }
        reaction_rows = [item for item in revisions if item["object_type"] == "user_reaction_event"]
        attribution_rows = [
            item for item in revisions if item["object_type"] == "feedback_attribution_record"
        ]
        outcomes = [
            item for item in revisions if item["object_type"] == "outcome_measurement"
        ]
        outcome_revision_ids = {item["revision_id"] for item in outcomes}
        denominators["reaction_revision_count"] = len(reaction_rows)
        denominators["active_reaction_count"] = sum(item["is_head"] for item in reaction_rows)
        denominators["attribution_revision_count"] = len(attribution_rows)
        denominators["active_attribution_count"] = sum(
            item["is_head"] for item in attribution_rows
        )
        denominators["active_objective_outcome_count"] = sum(
            item["is_head"] for item in outcomes
        )
        _audit_reactions(reaction_rows, causal_revisions_by_id, metrics)
        _audit_attributions(
            attribution_rows,
            reaction_rows,
            outcome_revision_ids,
            metrics,
        )
        audit_attribution_principals(
            attribution_rows,
            reaction_rows,
            outcomes,
            metrics,
            denominators,
        )
        for row in (*reaction_rows, *attribution_rows, *outcomes):
            metrics["feedback_schema_registry_mismatch"] += _revision_hash_gap(row)
        _audit_current_coverage(reaction_rows, attribution_rows, metrics)
        commands = [dict(row) for row in conn.execute("SELECT * FROM cognitive_state_outbox")]
        receipts = [
            dict(row)
            for row in conn.execute(
                """
                SELECT e.*, c.outcome AS consumption_outcome,
                       c.metadata AS consumption_metadata
                FROM cognitive_state_effect_receipts AS e
                LEFT JOIN cognitive_data_consumptions AS c
                  ON c.consumption_id=e.consumption_id
                """
            )
        ]
        feedback_revision_ids = {item["revision_id"] for item in attribution_rows}
        commands = [
            item
            for item in commands
            if item["revision_id"] in feedback_revision_ids
            and item["consumer_id"] in FEEDBACK_TARGETS
        ]
        receipts = [
            item
            for item in receipts
            if item["revision_id"] in feedback_revision_ids
            and item["consumer_id"] in FEEDBACK_TARGETS
        ]
        denominators["feedback_command_count"] = len(commands)
        denominators["feedback_effect_receipt_count"] = len(receipts)
        _audit_effects(
            database_dir=database_dir,
            attribution_rows=attribution_rows,
            commands=commands,
            receipts=receipts,
            metrics=metrics,
            denominators=denominators,
        )


def _audit_reactions(
    rows: list[dict[str, Any]],
    causal_revisions_by_id: Mapping[str, Mapping[str, Any]],
    metrics: dict[str, int],
) -> None:
    by_object: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_object[row["object_id"]].append(row)
        payload = row["payload"]
        subject = payload.get("subject_ref")
        if not isinstance(subject, Mapping) or not subject.get("type") or not subject.get("id"):
            metrics["feedback_without_subject"] += 1
        if not independent_reaction_payload_valid(
            payload,
            feedback_targets=FEEDBACK_TARGETS,
            canonical_revisions_by_id=causal_revisions_by_id,
        ):
            metrics["unknown_action_default_positive"] += 1
    for chain in by_object.values():
        for index, row in enumerate(chain):
            payload = row["payload"]
            supersedes = str(payload.get("supersedes_event_id") or "")
            correction_of = str(payload.get("correction_of_event_id") or "")
            if not supersedes and not correction_of:
                continue
            prior = chain[index - 1] if index else None
            if (
                prior is None
                or supersedes != prior["source_event_id"]
                or row["supersedes_revision_id"] != prior["revision_id"]
            ):
                metrics["correction_without_latest_supersedes"] += 1
                continue
            if correction_of and (
                correction_of != prior["source_event_id"]
                or row["correction_of_revision_id"] != prior["revision_id"]
            ):
                metrics["correction_without_latest_supersedes"] += 1
            if not correction_of and row["correction_of_revision_id"]:
                metrics["correction_without_latest_supersedes"] += 1


def _audit_attributions(
    rows: list[dict[str, Any]],
    reaction_rows: list[dict[str, Any]],
    outcome_revision_ids: set[str],
    metrics: dict[str, int],
) -> None:
    reactions_by_revision = {
        str(item["revision_id"]): item for item in reaction_rows
    }
    for row in rows:
        payload = row["payload"]
        if not _independent_attribution_payload_valid(payload):
            if payload.get("evidence_class") == "weak_behavior":
                metrics["auto_update_from_weak_single_signal"] += 1
            else:
                metrics["feedback_schema_registry_mismatch"] += 1
        if payload.get("evidence_class") == "weak_behavior" and payload.get(
            "disposition"
        ) == "proposal_eligible" and not independent_weak_materiality_valid(
            payload,
            reactions_by_revision,
        ):
            metrics["auto_update_from_weak_single_signal"] += 1
        for ref in payload.get("outcome_refs") or ():
            if str(ref.get("revision_id") or "") not in outcome_revision_ids:
                metrics["reaction_used_as_objective_ground_truth"] += 1


def _audit_current_coverage(
    reactions: list[dict[str, Any]],
    attributions: list[dict[str, Any]],
    metrics: dict[str, int],
) -> None:
    referenced = {
        str(ref.get("revision_id") or "")
        for item in attributions
        if item["is_head"]
        for ref in item["payload"].get("reaction_refs") or ()
    }
    metrics["active_feedback_without_current_attribution"] += sum(
        1
        for item in reactions
        if item["is_head"] and item["revision_id"] not in referenced
    )


def _audit_effects(
    *,
    database_dir: Path,
    attribution_rows: list[dict[str, Any]],
    commands: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    metrics: dict[str, int],
    denominators: dict[str, int],
) -> None:
    attribution_ids = {item["revision_id"] for item in attribution_rows}
    command_by_id = {str(item["command_id"]): item for item in commands}
    receipt_by_command = {
        str(item["command_id"]): item for item in receipts
    }
    command_ids = set(command_by_id)
    receipt_command_ids = set(receipt_by_command)
    missing_receipts = command_ids - receipt_command_ids
    orphan_receipts = receipt_command_ids - command_ids
    denominators["pending_feedback_command_count"] = len(missing_receipts)
    denominators["command_without_receipt_count"] = len(missing_receipts)
    denominators["receipt_without_command_count"] = len(orphan_receipts)
    metrics["feedback_command_without_terminal_receipt"] += len(missing_receipts)
    metrics["feedback_receipt_without_command"] += len(orphan_receipts)
    current_revision_ids = {
        str(item["revision_id"]) for item in attribution_rows if item["is_head"]
    }
    metrics["pending_superseded_feedback_command"] += sum(
        1
        for command_id in missing_receipts
        if str(command_by_id[command_id]["revision_id"]) not in current_revision_ids
    )
    expected_current = {
        (str(row["revision_id"]), str(target["target_id"]))
        for row in attribution_rows
        if row["is_head"]
        for target in row["payload"].get("target_dispositions") or ()
        if target.get("command_ref", {}).get("command_type")
        in {"evaluate_feedback_target", "neutralize_feedback_effect"}
    }
    attribution_by_revision = {
        str(item["revision_id"]): item for item in attribution_rows
    }
    semantically_terminal_receipts = [
        item
        for item in receipts
        if _terminal_disposition_gap(
            command_by_id.get(str(item["command_id"])),
            item,
            attribution_by_revision.get(str(item["revision_id"])),
        )
        == 0
    ]
    terminal_current = {
        (str(item["revision_id"]), str(item["consumer_id"]))
        for item in semantically_terminal_receipts
        if str(item["revision_id"]) in current_revision_ids
    }
    denominators["current_target_expected_count"] = len(expected_current)
    denominators["current_target_terminal_count"] = len(
        expected_current & terminal_current
    )
    metrics["current_target_terminal_gap"] += len(
        expected_current - terminal_current
    )
    denominators["complete_registry_target_command_count"] = sum(
        str(item.get("consumer_id") or "") in FEEDBACK_TARGETS
        and str(item.get("command_type") or "")
        in {"evaluate_feedback_target", "neutralize_feedback_effect"}
        for item in commands
    )
    denominators["terminal_disposition_count"] = len(
        semantically_terminal_receipts
    )
    denominators["correction_command_count"] = sum(
        str(item.get("command_type") or "") == "neutralize_feedback_effect"
        for item in commands
    )
    denominators["neutralized_effect_count"] = sum(
        str(item.get("consumption_outcome") or "")
        in {"suppressed", "revoked", "compensated"}
        for item in semantically_terminal_receipts
    )
    for command in commands:
        metrics["feedback_schema_registry_mismatch"] += _command_hash_gap(command)
    for receipt in receipts:
        metrics["feedback_terminal_disposition_gap"] += _terminal_disposition_gap(
            command_by_id.get(str(receipt["command_id"])),
            receipt,
            attribution_by_revision.get(str(receipt["revision_id"])),
        )
        if not receipt.get("consumption_id") or receipt.get("consumption_outcome") is None:
            metrics["target_receipt_reciprocity_gap"] += 1
        metrics["target_receipt_reciprocity_gap"] += _effect_receipt_hash_gap(
            receipt
        )
    duplicate = Counter(
        (str(item["revision_id"]), str(item["consumer_id"]))
        for item in receipts
        if item["status"] in {"committed", "revoked"}
    )
    metrics["reaction_target_effect_duplicate"] += sum(
        count - 1 for count in duplicate.values() if count > 1
    )
    metrics["duplicate_training_effect"] += sum(
        count - 1
        for (revision_id, target), count in duplicate.items()
        if target == "training_evidence" and revision_id and count > 1
    )
    metrics["replayed_negative_trust_effect"] += sum(
        count - 1
        for (_revision_id, target), count in duplicate.items()
        if target == "trust_proposal" and count > 1
    )
    for receipt in receipts:
        if receipt["revision_id"] not in attribution_ids or receipt["command_id"] not in command_by_id:
            metrics["committed_effect_without_attribution"] += 1
        if receipt["status"] in {"committed", "revoked"}:
            metrics["target_receipt_reciprocity_gap"] += _target_receipt_gap(
                database_dir,
                receipt,
            )
    by_revision = {item["revision_id"]: item for item in attribution_rows}
    receipts_by_revision: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for receipt in receipts:
        receipts_by_revision[str(receipt["revision_id"])].append(receipt)
    for row in attribution_rows:
        disposition = str(row["payload"].get("disposition") or "")
        if disposition == "correction_pending":
            required = {
                str(item["target_id"])
                for item in row["payload"].get("target_dispositions") or ()
                if item.get("command_ref", {}).get("command_type")
                == "neutralize_feedback_effect"
            }
            neutralized = {
                str(item["consumer_id"])
                for item in receipts_by_revision.get(row["revision_id"], ())
                if item.get("status") in {"committed", "revoked"}
                and item.get("consumption_outcome")
                in {"suppressed", "revoked", "compensated"}
            }
            missing = required - neutralized
            if missing:
                metrics["correction_effect_without_neutralization_receipt"] += 1
            replacement_receipts = {
                str(item["consumer_id"])
                for item in receipts_by_revision.get(row["revision_id"], ())
                if str(
                    command_by_id.get(str(item["command_id"]), {}).get(
                        "command_type"
                    )
                    or ""
                )
                == "evaluate_feedback_target"
                and item.get("status") in {"committed", "revoked"}
            }
            if replacement_receipts and missing:
                metrics["replacement_effect_before_compensation_complete"] += 1
            continue
        if not row["correction_of_revision_id"] or disposition not in {
            "proposal_eligible",
            "record_only",
        }:
            continue
        prior = by_revision.get(row["correction_of_revision_id"])
        if prior is None:
            metrics["correction_effect_without_neutralization_receipt"] += 1
            continue
        neutralized = {
            str(item["consumer_id"])
            for item in receipts_by_revision.get(prior["revision_id"], ())
            if item.get("status") in {"committed", "revoked"}
            and item.get("consumption_outcome")
            in {"suppressed", "revoked", "compensated"}
        }
        required = {
            str(item["target_id"])
            for item in prior["payload"].get("target_dispositions") or ()
            if item.get("command_ref", {}).get("command_type")
            == "neutralize_feedback_effect"
        }
        if required - neutralized:
            metrics["correction_effect_without_neutralization_receipt"] += 1
        replacement_rows = receipts_by_revision.get(row["revision_id"], ())
        if replacement_rows and required - neutralized:
            metrics["replacement_effect_before_compensation_complete"] += 1


def _target_receipt_gap(database_dir: Path, receipt: Mapping[str, Any]) -> int:
    target = str(receipt["consumer_id"])
    if target not in FEEDBACK_TARGETS:
        return 1
    refs = _json(receipt.get("evidence_refs"), default=[])
    prefix = f"domain-feedback-receipt:{target}:"
    target_refs = [str(value)[len(prefix) :] for value in refs if str(value).startswith(prefix)]
    if len(target_refs) != 1:
        return 1
    db_path = database_dir / TARGET_DB_FILE_BY_ID[target]
    if not db_path.is_file():
        return 1
    try:
        with _connect(db_path) as conn:
            proposal_table, action_table, receipt_table = TARGET_DOMAIN_TABLES[target]
            if not all(
                _table_exists(conn, table)
                for table in (proposal_table, action_table, receipt_table)
            ):
                return 1
            row = conn.execute(
                render_sql(
                    "SELECT * FROM {table} WHERE receipt_id=?",
                    identifiers={"table": receipt_table},
                ),
                (target_refs[0],),
            ).fetchone()
            if row is None:
                return 1
            state_kind = str(row["state_kind"])
            if state_kind not in {"proposal", "action"}:
                return 1
            state_table = proposal_table if state_kind == "proposal" else action_table
            id_column = "proposal_id" if state_kind == "proposal" else "action_id"
            state = conn.execute(
                render_sql(
                    "SELECT * FROM {table} WHERE {id_column}=?",
                    identifiers={"table": state_table, "id_column": id_column},
                ),
                (str(row["state_id"]),),
            ).fetchone()
    except sqlite3.Error:
        return 1
    if state is None:
        return 1
    state_payload = _json(state["payload_json"], default=None)
    if not isinstance(state_payload, Mapping):
        return 1
    state_hash = _independent_sha256_json(state_payload)
    receipt_identity = {
        "target_id": target,
        "owner_id": TARGET_DOMAIN_OWNERS[target],
        "command_key": str(row["command_key"]),
        "state_kind": state_kind,
        "state_id": str(row["state_id"]),
        "after_hash": str(row["after_hash"]),
        "material_command_id": str(row["material_command_id"]),
    }
    suffix = _independent_sha256_json(receipt_identity).split(":", 1)[1][:32]
    expected_receipt_id = "domain-feedback-receipt-" + suffix
    expected_effect_id = (
        str(row["target_effect_id"])
        if state_kind == "proposal"
        else "domain-feedback-effect-" + suffix
    )
    if state_kind == "proposal":
        expected_before_hash = _independent_sha256_json(
            {
                "target_id": target,
                "owner_id": TARGET_DOMAIN_OWNERS[target],
                "proposal_id": str(row["state_id"]),
                "state": "absent",
            }
        )
    else:
        expected_before_hash = str(state_payload.get("prior_after_hash") or "")
    structural_gap = int(
        str(row["schema_version"]) != "mnemos.domain_feedback_proposal_receipt.v1"
        or str(row["receipt_id"]) != expected_receipt_id
        or str(row["target_id"]) != target
        or str(row["owner_id"]) != TARGET_DOMAIN_OWNERS[target]
        or str(state_payload.get("attribution_revision_id") or "")
        != str(receipt["revision_id"])
        or str(row["target_effect_id"]) != expected_effect_id
        or str(row["target_effect_id"]) != str(receipt["target_effect_id"])
        or str(row["before_hash"]) != expected_before_hash
        or str(row["before_hash"]) != str(receipt["before_hash"])
        or str(row["after_hash"]) != str(receipt["after_hash"])
        or str(row["after_hash"]) != state_hash
        or str(row["state_payload_hash"]) != state_hash
        or str(state["payload_hash"]) != state_hash
    )
    if structural_gap:
        return 1
    return _material_target_receipt_gap(
        database_dir=database_dir,
        row=row,
        state_kind=state_kind,
        state_payload=state_payload,
        canonical_evidence_refs=refs,
    )


def _material_target_receipt_gap(
    *,
    database_dir: Path,
    row: Mapping[str, Any],
    state_kind: str,
    state_payload: Mapping[str, Any],
    canonical_evidence_refs: Any,
) -> int:
    """Recompute the proposal material permit without trusting writer helpers."""

    decision_refs = _independent_entity_refs(row["decision_trace_refs_json"])
    action_refs = _independent_entity_refs(row["action_refs_json"])
    if decision_refs is None or action_refs is None:
        return 1
    evidence_decisions = _independent_evidence_entity_refs(
        canonical_evidence_refs,
        kind="decision_trace",
    )
    evidence_actions = _independent_evidence_entity_refs(
        canonical_evidence_refs,
        kind="action",
    )
    if evidence_decisions is None or evidence_actions is None:
        return 1
    if state_kind == "action":
        return int(
            bool(str(row["material_command_id"]))
            or bool(decision_refs)
            or bool(action_refs)
            or bool(evidence_decisions)
            or bool(evidence_actions)
        )
    if (
        len(decision_refs) != 1
        or len(action_refs) != 1
        or decision_refs != evidence_decisions
        or action_refs != evidence_actions
    ):
        return 1
    trusted = state_payload.get("trusted_gate")
    if not isinstance(trusted, Mapping) or set(trusted) != {
        "decision",
        "risk_level",
        "reasons",
        "missing_info",
        "candidate_payload_hash",
    }:
        return 1
    base_proposal = dict(state_payload)
    base_proposal.pop("trusted_gate", None)
    if (
        trusted.get("decision")
        not in {"allow_pending_user_decision", "needs_manual_review"}
        or not str(trusted.get("risk_level") or "")
        or not isinstance(trusted.get("reasons"), list)
        or not isinstance(trusted.get("missing_info"), list)
        or trusted.get("candidate_payload_hash")
        != _independent_sha256_json(base_proposal).removeprefix("sha256:")
    ):
        return 1
    material_command_id = str(row["material_command_id"])
    state_db = database_dir / "producer_consumer_ledger.db"
    if not material_command_id or not state_db.is_file():
        return 1
    decision_ref = decision_refs[0]
    action_ref = action_refs[0]
    try:
        with _connect(state_db) as conn:
            command = conn.execute(
                "SELECT command_type, payload_json FROM cognitive_state_outbox "
                "WHERE command_id=?",
                (material_command_id,),
            ).fetchone()
            decision = conn.execute(
                "SELECT object_id, object_type, payload_hash, payload_json "
                "FROM cognitive_state_revisions WHERE revision_id=?",
                (decision_ref["revision_id"],),
            ).fetchone()
            terminal = conn.execute(
                "SELECT status, target_effect_id, before_hash, after_hash "
                "FROM cognitive_state_effect_receipts WHERE command_id=?",
                (material_command_id,),
            ).fetchone()
    except sqlite3.Error:
        return 1
    if command is None or decision is None or terminal is None:
        return 1
    command_payload = _json(command["payload_json"], default=None)
    decision_payload = _json(decision["payload_json"], default=None)
    if not isinstance(command_payload, Mapping) or not isinstance(
        decision_payload,
        Mapping,
    ):
        return 1
    action_specs = [
        dict(item)
        for item in decision_payload.get("action_specs") or ()
        if isinstance(item, Mapping) and item.get("action_id") == action_ref["id"]
    ]
    return int(
        str(command["command_type"]) != "execute_material_action"
        or command_payload.get("decision_revision_id")
        != decision_ref["revision_id"]
        or command_payload.get("action_id") != action_ref["id"]
        or command_payload.get("effect_id") != str(row["target_effect_id"])
        or str(decision["object_type"]) != "decision_trace"
        or str(decision["object_id"]) != decision_ref["id"]
        or str(decision["payload_hash"]) != decision_ref["content_hash"]
        or action_ref["revision_id"] != decision_ref["revision_id"]
        or len(action_specs) != 1
        or _independent_sha256_json(action_specs[0]) != action_ref["content_hash"]
        or str(terminal["status"]) != "committed"
        or str(terminal["target_effect_id"]) != str(row["target_effect_id"])
        or str(terminal["before_hash"]) != str(row["before_hash"])
        or str(terminal["after_hash"]) != str(row["after_hash"])
    )


def _independent_entity_refs(value: Any) -> list[dict[str, str]] | None:
    payload = _json(value, default=None)
    if not isinstance(payload, list):
        return None
    refs: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, Mapping) or set(item) != {
            "id",
            "revision_id",
            "content_hash",
        }:
            return None
        ref = {key: str(item[key]) for key in ("id", "revision_id", "content_hash")}
        if (
            not ref["id"]
            or not ref["revision_id"]
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", ref["content_hash"])
        ):
            return None
        refs.append(ref)
    return refs


def _independent_evidence_entity_refs(
    values: Any,
    *,
    kind: str,
) -> list[dict[str, str]] | None:
    if not isinstance(values, list):
        return None
    prefix = f"feedback-{kind}-ref:"
    decoded: list[dict[str, str]] = []
    for value in values:
        normalized = str(value)
        if not normalized.startswith(prefix):
            continue
        encoded = normalized[len(prefix) :]
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            payload = json.loads(raw.decode("utf-8"))
        except (
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            binascii.Error,
        ):
            return None
        refs = _independent_entity_refs(
            json.dumps([payload], ensure_ascii=False, sort_keys=True)
        )
        if refs is None or len(refs) != 1:
            return None
        decoded.extend(refs)
    return decoded


def _revision_hash_gap(row: Mapping[str, Any]) -> int:
    """Recompute one cognitive revision identity without using its constructor."""

    payload = row.get("payload")
    evidence_refs = _json(row.get("evidence_refs"), default=None)
    if not isinstance(payload, Mapping) or not isinstance(evidence_refs, list):
        return 1
    payload_hash = _independent_sha256_json(payload)
    evidence_hash = _independent_sha256_json(evidence_refs)
    identity = {
        "object_type": row.get("object_type"),
        "object_id": row.get("object_id"),
        "schema_version": row.get("schema_version"),
        "source_event_id": row.get("source_event_id"),
        "source_revision_id": row.get("source_revision_id"),
        "source_content_hash": row.get("source_content_hash"),
        "scope_type": row.get("scope_type"),
        "scope_id": row.get("scope_id"),
        "evidence_hash": evidence_hash,
        "payload_hash": payload_hash,
        "supersedes_revision_id": str(row.get("supersedes_revision_id") or ""),
        "correction_of_revision_id": str(
            row.get("correction_of_revision_id") or ""
        ),
    }
    expected_revision = (
        "cogrev-" + _independent_sha256_json(identity).split(":", 1)[1][:32]
    )
    return int(
        str(row.get("payload_hash")) != payload_hash
        or str(row.get("evidence_hash")) != evidence_hash
        or str(row.get("revision_id")) != expected_revision
    )


def _command_hash_gap(command: Mapping[str, Any]) -> int:
    """Recompute one target command payload hash and deterministic identity."""

    payload = _json(command.get("payload_json"), default=None)
    if not isinstance(payload, Mapping):
        return 1
    payload_hash = _independent_sha256_json(payload)
    identity = {
        "revision_id": command.get("revision_id"),
        "consumer_id": command.get("consumer_id"),
        "command_type": command.get("command_type"),
        "payload_hash": payload_hash,
    }
    expected_id = "cogcmd-" + _independent_sha256_json(identity).split(":", 1)[1][:32]
    return int(
        str(command.get("payload_hash")) != payload_hash
        or str(command.get("command_id")) != expected_id
    )


def _effect_receipt_hash_gap(receipt: Mapping[str, Any]) -> int:
    """Recompute one canonical effect-receipt identity from stored fields."""

    evidence_refs = _json(receipt.get("evidence_refs"), default=None)
    if not isinstance(evidence_refs, list):
        return 1
    metadata = _json(receipt.get("consumption_metadata"), default={})
    if not isinstance(metadata, Mapping):
        return 1
    identity = {
        "command_id": receipt.get("command_id"),
        "status": receipt.get("status"),
        "target_effect_id": receipt.get("target_effect_id"),
        "before_hash": str(receipt.get("before_hash") or ""),
        "after_hash": str(receipt.get("after_hash") or ""),
        "evidence_refs": evidence_refs,
        "terminal_reason_code": str(metadata.get("terminal_reason_code") or ""),
        "retry_exhausted": bool(metadata.get("retry_exhausted")),
    }
    expected_id = (
        "cogeffect-" + _independent_sha256_json(identity).split(":", 1)[1][:32]
    )
    return int(str(receipt.get("receipt_id")) != expected_id)


def _terminal_disposition_gap(
    command: Mapping[str, Any] | None,
    receipt: Mapping[str, Any],
    attribution: Mapping[str, Any] | None,
) -> int:
    """Reject terminal states that the immutable registry row cannot derive."""

    if command is None or attribution is None:
        return 1
    payload = _json(command.get("payload_json"), default=None)
    attribution_payload = attribution.get("payload")
    if not isinstance(payload, Mapping) or not isinstance(
        attribution_payload, Mapping
    ):
        return 1
    target_id = str(command.get("consumer_id") or "")
    status = str(receipt.get("status") or "")
    if status == "failed_terminal":
        evidence_refs = _json(receipt.get("evidence_refs"), default=[])
        metadata = _json(receipt.get("consumption_metadata"), default={})
        if not isinstance(evidence_refs, list) or not isinstance(metadata, Mapping):
            return 1
        failure_refs = [
            str(ref)
            for ref in evidence_refs
            if str(ref).startswith("feedback-permanent-failure:sha256:")
        ]
        terminal_reason = str(metadata.get("terminal_reason_code") or "")
        reason = _independent_feedback_command_failure_reason(
            command,
            payload,
            attribution,
        )
        if len(failure_refs) != 1 or not reason:
            return 1
        command_id = str(command.get("command_id") or "")
        revision_id = str(command.get("revision_id") or "")
        proof_identity = {
            "schema_version": "mnemos.feedback_command_failure.v1",
            "command_id": command_id,
            "command_type": str(command.get("command_type") or ""),
            "consumer_id": target_id,
            "revision_id": revision_id,
            "payload_hash": str(command.get("payload_hash") or ""),
            "attribution_payload_hash": str(
                attribution.get("payload_hash") or ""
            ),
            "reason_code": reason,
        }
        expected_proof = _independent_sha256_json(proof_identity)
        unchanged_hash = _independent_sha256_json(
            {
                "command_id": command_id,
                "target_id": target_id,
                "state": "unchanged_after_permanent_failure",
                "failure_hash": expected_proof,
            }
        )
        expected_effect = (
            f"feedback-failed:{target_id}:"
            + _independent_sha256_json(
                {"command_id": command_id}
            ).split(":", 1)[1][:32]
        )
        return int(
            failure_refs[0]
            != "feedback-permanent-failure:" + expected_proof
            or terminal_reason != "feedback_target_" + reason
            or str(receipt.get("revision_id") or "") != revision_id
            or str(receipt.get("consumer_id") or "") != target_id
            or str(receipt.get("target_effect_id") or "") != expected_effect
            or str(receipt.get("before_hash") or "") != unchanged_hash
            or str(receipt.get("after_hash") or "") != unchanged_hash
            or str(receipt.get("consumption_outcome") or "")
            != "failed_terminal"
        )
    target_rows = [
        item
        for item in attribution_payload.get("target_dispositions") or ()
        if isinstance(item, Mapping)
        and str(item.get("target_id") or "") == target_id
    ]
    if len(target_rows) != 1:
        return 1
    target = target_rows[0]
    command_type = str(command.get("command_type") or "")
    if (
        payload.get("target_id") != target_id
        or payload.get("command_key")
        != target.get("command_ref", {}).get("command_key")
        or target.get("command_ref", {}).get("command_type") != command_type
    ):
        return 1
    if command_type == "evaluate_feedback_target" and (
        payload.get("eligible") is not target.get("eligible")
        or payload.get("exclusion_reason") != target.get("exclusion_reason")
    ):
        return 1
    if command_type == "neutralize_feedback_effect" and target.get(
        "eligible"
    ) is not True:
        return 1
    if status == "intentional_skip":
        evidence_refs = _json(receipt.get("evidence_refs"), default=[])
        metadata = _json(receipt.get("consumption_metadata"), default={})
        return int(
            target.get("eligible") is not False
            or command_type != "evaluate_feedback_target"
            or str(receipt.get("before_hash") or "")
            != str(receipt.get("after_hash") or "")
            or str(receipt.get("consumption_outcome") or "")
            != str(target.get("exclusion_reason") or "")
            or not isinstance(metadata, Mapping)
            or metadata.get("terminal_reason_code")
            != "feedback_target_ineligible"
            or not isinstance(evidence_refs, list)
            or not any(
                str(ref).startswith("feedback-target-registry:sha256:")
                for ref in evidence_refs
            )
        )
    if status == "rejected":
        evidence_refs = _json(receipt.get("evidence_refs"), default=[])
        metadata = _json(receipt.get("consumption_metadata"), default={})
        command_id = str(command.get("command_id") or "")
        revision_id = str(command.get("revision_id") or "")
        unchanged_hash = str(command.get("payload_hash") or "")
        correction_refs = (
            []
            if not isinstance(evidence_refs, list)
            else [
                str(ref)
                for ref in evidence_refs
                if str(ref).startswith("feedback-correction:cogrev-")
            ]
        )
        return int(
            command_type != "evaluate_feedback_target"
            or len(correction_refs) != 1
            or not isinstance(metadata, Mapping)
            or metadata.get("terminal_reason_code")
            != "feedback_correction_superseded_before_effect"
            or str(receipt.get("target_effect_id") or "")
            != "feedback-command-superseded:" + command_id
            or str(receipt.get("before_hash") or "")
            != unchanged_hash
            or str(receipt.get("after_hash") or "")
            != unchanged_hash
            or str(receipt.get("consumption_outcome") or "")
            != "superseded_before_effect"
            or not isinstance(evidence_refs, list)
            or f"feedback-command:{command_id}" not in evidence_refs
            or f"feedback-attribution:{revision_id}"
            not in evidence_refs
            or f"no-effect-oracle:{command_id}:{unchanged_hash}"
            not in evidence_refs
        )
    if status in {"committed", "revoked"}:
        return int(target.get("eligible") is not True)
    return 1


def _independent_feedback_command_failure_reason(
    command: Mapping[str, Any],
    payload: Mapping[str, Any],
    attribution: Mapping[str, Any],
) -> str:
    """Recompute only immutable structural failures, never runtime errors."""

    command_type = str(command.get("command_type") or "")
    target_id = str(command.get("consumer_id") or "")
    revision_id = str(command.get("revision_id") or "")
    if command_type not in {
        "evaluate_feedback_target",
        "neutralize_feedback_effect",
    }:
        return "unsupported_command_type"
    if target_id not in FEEDBACK_TARGETS:
        return "unregistered_target"
    if payload.get("target_id") != target_id:
        return "target_binding_mismatch"
    if payload.get("attribution_revision_id") != revision_id:
        return "attribution_binding_mismatch"
    if command_type == "neutralize_feedback_effect":
        if (
            payload.get("schema_version")
            != "mnemos.feedback_neutralization_command.v1"
            or payload.get("neutralization_kind")
            not in {"suppress", "revoke", "compensate"}
        ):
            return "neutralization_contract_mismatch"
        return ""
    if tuple(payload.get("required_target_ids") or ()) != FEEDBACK_TARGETS:
        return "target_registry_mismatch"
    attribution_payload = attribution.get("payload")
    if not isinstance(attribution_payload, Mapping):
        return "attribution_missing"
    if attribution_payload.get("input_set_hash") != payload.get("input_set_hash"):
        return "input_set_hash_mismatch"
    rows = [
        item
        for item in attribution_payload.get("target_dispositions") or ()
        if isinstance(item, Mapping) and item.get("target_id") == target_id
    ]
    if len(rows) != 1:
        return "target_disposition_missing"
    row = rows[0]
    if (
        row.get("eligible") != payload.get("eligible")
        or row.get("exclusion_reason") != payload.get("exclusion_reason")
        or row.get("command_ref", {}).get("command_key")
        != payload.get("command_key")
    ):
        return "target_disposition_binding_mismatch"
    return ""


def _independent_sha256_json(value: Any) -> str:
    """Hash canonical JSON locally so the audit does not trust writer helpers."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _independent_attribution_payload_valid(payload: Mapping[str, Any]) -> bool:
    registry = payload.get("target_registry")
    targets = payload.get("target_dispositions")
    if (
        payload.get("schema_version")
        != "mnemos.feedback_attribution_record.v1"
        or payload.get("disposition")
        not in {
            "record_only",
            "proposal_eligible",
            "objective_only",
            "correction_pending",
            "compensation_pending",
            "superseded",
            "rejected",
        }
        or payload.get("post_neutralization_disposition")
        not in {"record_only", "proposal_eligible", "objective_only"}
        or not isinstance(registry, Mapping)
        or tuple(registry.get("targets") or ()) != FEEDBACK_TARGETS
        or not str(registry.get("registry_hash") or "").startswith("sha256:")
        or not isinstance(targets, list)
        or tuple(sorted(str(item.get("target_id") or "") for item in targets))
        != FEEDBACK_TARGETS
        or payload.get("input_set_hash")
        != _independent_sha256_json(
            {
                "reaction_refs": payload.get("reaction_refs"),
                "outcome_refs": payload.get("outcome_refs"),
                "independence_keys": payload.get("independence_keys"),
                "method": payload.get("method"),
                "target_registry": payload.get("target_registry"),
            }
        )
    ):
        return False
    return all(
        isinstance(item, Mapping)
        and isinstance(item.get("eligible"), bool)
        and isinstance(item.get("command_ref"), Mapping)
        and item["command_ref"].get("command_type")
        in {
            "evaluate_feedback_target",
            "neutralize_feedback_effect",
        }
        and str(item["command_ref"].get("command_key") or "")
        for item in targets
    )


def _independent_history_inventory(
    database_dir: Path,
) -> tuple[_AuditHistoryObject, ...]:
    """Enumerate every historical object without importing migration code."""

    sources = (
        ("delivery_events", "delivery_feedback", database_dir / "delivery_events.db"),
        ("feedback_signals", "delivery_feedback", database_dir / "feedback_signals.db"),
        ("scoring", "scoring_search", database_dir / "mnemos.db"),
        ("reflections", "reflection_optimizer", database_dir / "reflections.db"),
        (
            "rule_weight_optimizer",
            "reflection_optimizer",
            database_dir / "rule_weight_optimizer.db",
        ),
    )
    objects: list[_AuditHistoryObject] = []
    for database_class, domain, path in sources:
        if not path.is_file():
            continue
        with _connect(path) as conn:
            for table, predicate in audit_history_specs(database_class, conn):
                if not _table_exists(conn, table):
                    continue
                columns = tuple(
                    tuple(row)
                    for row in conn.execute(f'PRAGMA table_info("{table}")')
                )
                primary = tuple(
                    str(name)
                    for _, name, _type, _notnull, _default, pk in columns
                    if int(pk) > 0
                )
                if not primary:
                    raise ValueError(
                        f"feedback history source lacks primary key: {database_class}.{table}"
                    )
                schema_row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                schema_fingerprint = _independent_sha256_json(
                    {
                        "table": table,
                        "columns": [list(item) for item in columns],
                        "ddl_hash": _independent_sha256_json(
                            {"sql": str(schema_row[0] if schema_row else "")}
                        ),
                    }
                )
                query = f'SELECT * FROM "{table}"'  # nosec B608 - fixed registry
                if predicate:
                    query += " WHERE " + predicate
                query += " ORDER BY " + ", ".join(
                    f'"{name}"' for name in primary
                )
                for row in conn.execute(query).fetchall():
                    normalized = {
                        str(name): _audit_sql_value(row[str(name)])
                        for _, name, *_rest in columns
                    }
                    objects.append(
                        _AuditHistoryObject(
                            domain=domain,
                            database_class=database_class,
                            table=table,
                            primary_key=tuple(
                                (name, normalized[name]) for name in primary
                            ),
                            schema_fingerprint=schema_fingerprint,
                            field_manifest=tuple(
                                str(name) for _, name, *_rest in columns
                            ),
                            source_refs=audit_history_source_refs(normalized),
                            row_hash=_independent_sha256_json(normalized),
                        )
                    )
    ref_index: dict[str, set[str]] = defaultdict(set)
    for item in objects:
        for ref in item.source_refs:
            ref_index[ref].add(item.source_key)
    linked = tuple(
        replace(
            item,
            projection_links=tuple(
                sorted(
                    {
                        key
                        for ref in item.source_refs
                        for key in ref_index[ref]
                        if key != item.source_key
                    }
                )
            ),
        )
        for item in objects
    )
    return tuple(sorted(linked, key=lambda item: item.source_key))


def _audit_sql_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"blob_base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (str, int, float)) or value is None:
        return value
    return str(value)


def _independent_history_coverage(
    ledger: Path,
    inventory: tuple[_AuditHistoryObject, ...],
) -> dict[str, int]:
    if not ledger.is_file():
        return {
            "covered": 0,
            "uncovered": len(inventory),
            "unexpected": 0,
            "active_promotion": 0,
        }
    expected = {item.source_key: item for item in inventory}
    with _connect(ledger) as conn:
        if not _table_exists(conn, "cognitive_state_migration_quarantine"):
            return {
                "covered": 0,
                "uncovered": len(inventory),
                "unexpected": 0,
                "active_promotion": 0,
            }
        rows = conn.execute(
            """
            SELECT source_key, payload_json, payload_hash
            FROM cognitive_state_migration_quarantine
            WHERE reason_code='historical_unattributed_feedback'
            """
        ).fetchall()
        actual = {str(row["source_key"]): row for row in rows}
        active_promotion = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM cognitive_state_revisions
                WHERE admission_state='active'
                  AND object_type IN (
                      'user_reaction_event','feedback_attribution_record'
                  )
                  AND source_event_id LIKE 'feedback-history:%'
                """
            ).fetchone()[0]
        )
    covered = 0
    for source_key, item in expected.items():
        payload = item.quarantine_payload()
        row = actual.get(source_key)
        if (
            row is not None
            and str(row["payload_hash"]) == _independent_sha256_json(payload)
            and _json(row["payload_json"], default=None) == payload
        ):
            covered += 1
    return {
        "covered": covered,
        "uncovered": len(expected) - covered,
        "unexpected": len(set(actual) - set(expected)),
        "active_promotion": active_promotion,
    }
