"""Read-only lineage and freshness metrics for cognitive readiness.

The readiness score must be reproducible from concrete producer identifiers.  This
module deliberately keeps the cross-database joins in one place so callers cannot
substitute unrelated global row counts for per-driver coverage.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from core.sync_framework.raw_event_store import (
    CanonicalRawReadError,
    canonical_observation_text,
    iter_current_raw_turns_readonly,
)
from core.sync_framework.raw_provenance_store import (
    INTENTIONAL_NO_OBSERVATION_REASONS,
    INTENTIONAL_NO_OBSERVATION_STATUS,
)
from core.ops.readiness_query_budget import connect_readonly_sqlite


LINEAGE_SAMPLE_LIMIT = 20
LEARNING_SIGNAL_SCHEMA_VERSION = "mnemos.learning_signal.v2"


def build_learning_signal_metrics(
    database_dir: Path,
    metrics: dict[str, Any],
    *,
    raw_events_db: Path,
    observations_db: Path,
    policy_db: Path,
    consolidation_db: Path,
    freshness_window_seconds: int,
    now: datetime,
) -> dict[str, Any]:
    """Build count, lineage, cold-start, and freshness evidence as one contract."""
    observations = _table_count_metric(observations_db, "observations")
    reflections = _table_count_metric(database_dir / "reflections.db", "reflection_records")
    policy_patches = _table_count_metric(policy_db, "policy_patches")
    policy_feedback = _policy_patch_feedback_metric(policy_db)
    consolidation_runs = _consolidation_runs_metric(consolidation_db)
    consolidation_coverage = _consolidation_coverage_metric(consolidation_db)
    delivery_lineage = metrics["delivery_outcome_lineage"]
    observation_lineage = observation_lineage_metric(
        raw_events_db,
        observations_db,
        freshness_window_seconds=freshness_window_seconds,
        now=now,
    )
    policy_lineage = policy_driver_lineage_metric(
        database_dir,
        policy_db,
        freshness_window_seconds=freshness_window_seconds,
        now=now,
    )
    consolidation_lineage = consolidation_lineage_metric(
        consolidation_db,
        freshness_window_seconds=freshness_window_seconds,
        now=now,
    )

    raw_signal_count = _int(metrics["raw_turns"].get("total"))
    search_signal_count = _int(metrics["search_sessions"].get("total"))
    search_feedback_count = _int(metrics["search_sessions"].get("behavior_outcomes"))
    dialog_closed_count = _int(metrics["dialog_reminders"].get("closed"))
    recap_closed_count = _closed_status_count(metrics["recap_tasks"].get("status_counts", {}))
    delivery_feedback_count = _int(delivery_lineage.get("covered"))
    cognitive_outcome_count = _int(metrics["cognitive_outcomes"].get("total"))
    reflection_count = _int(reflections.get("total"))
    observation_count = _int(observations.get("total"))
    policy_patch_count = _int(policy_patches.get("total"))
    policy_feedback_count = _int(policy_feedback.get("total"))
    policy_no_patch_count = _int(policy_feedback.get("no_patch"))
    consolidation_run_count = _int(consolidation_runs.get("total"))
    consolidation_applied_count = _int(consolidation_runs.get("applied"))
    method_candidate_count = _int(consolidation_runs.get("raw_candidate_total"))
    feedback_signal_count = (
        search_feedback_count
        + dialog_closed_count
        + recap_closed_count
        + delivery_feedback_count
    )
    policy_driver_count = _int(policy_lineage.get("denominator"))

    observation_output_gap = 1 if raw_signal_count > 0 and observation_count == 0 else 0
    policy_patch_gap = int(
        policy_driver_count > 0 and policy_patch_count == 0 and policy_no_patch_count == 0
    )
    consolidation_run_gap = int(raw_signal_count > 0 and consolidation_applied_count == 0)
    lineage_coverage = {
        "delivery_to_effect": _public_lineage_metric(delivery_lineage),
        "raw_to_observation": _public_lineage_metric(observation_lineage),
        "driver_to_policy_effect": _public_lineage_metric(policy_lineage),
        "consolidation_candidate_to_applied": _public_lineage_metric(consolidation_lineage),
    }

    required_tables = {
        "search_sessions": metrics["search_sessions"],
        "dialog_reminders": metrics["dialog_reminders"],
        "recap_tasks": metrics["recap_tasks"],
        "knowledge_graph": metrics["knowledge_graph"],
        "cognitive_graph": metrics["cognitive_graph"],
        "evidence_graph": metrics["evidence_graph"],
        "delivery_events": metrics["delivery_events"],
        "cognitive_outcomes": metrics["cognitive_outcomes"],
        "observations": observations,
        "reflections": reflections,
        "policy_patches": policy_patches,
        "policy_patch_feedback": policy_feedback,
        "consolidation_runs": consolidation_runs,
        "consolidation_coverage": consolidation_coverage,
    }
    required_tables_missing = [
        name
        for name, metric in required_tables.items()
        if not metric.get("exists") or metric.get("schema_valid") is False
    ]
    required_lineage_invalid = [
        name
        for name, metric in {
            "delivery_to_effect": delivery_lineage,
            "raw_to_observation": observation_lineage,
            "driver_to_policy_effect": policy_lineage,
            "consolidation_candidate_to_applied": consolidation_lineage,
        }.items()
        if metric.get("cold_start_state") == "blocked"
    ]
    required_evidence_empty = [
        name
        for name, metric in {
            "raw_turns": metrics["raw_turns"],
            "page_metrics": metrics["page_metrics"],
            **required_tables,
        }.items()
        if metric.get("exists") and "total" in metric and _int(metric.get("total")) == 0
    ]
    stale_lineages = [
        name
        for name, item in lineage_coverage.items()
        if _int(item["denominator"]) > 0 and item["freshness_state"] != "fresh"
    ]
    unobserved_lineages = [
        name for name, item in lineage_coverage.items() if _int(item["denominator"]) == 0
    ]
    unavailable_count = len(set(required_tables_missing + required_lineage_invalid))
    if unavailable_count:
        cold_start_state = "blocked"
    elif required_evidence_empty and not any(
        _int(item.get("denominator")) > 0 for item in lineage_coverage.values()
    ):
        cold_start_state = "unobserved"
    elif required_evidence_empty:
        cold_start_state = "partial"
    else:
        cold_start_state = "observed"

    return {
        "schema_version": LEARNING_SIGNAL_SCHEMA_VERSION,
        "raw_signal_count": raw_signal_count,
        "search_signal_count": search_signal_count,
        "feedback_signal_count": feedback_signal_count,
        "search_feedback_count": search_feedback_count,
        "dialog_closed_count": dialog_closed_count,
        "recap_closed_count": recap_closed_count,
        "delivery_feedback_count": delivery_feedback_count,
        "cognitive_outcome_count": cognitive_outcome_count,
        "reflection_count": reflection_count,
        "observation_count": observation_count,
        "policy_patch_count": policy_patch_count,
        "policy_patch_feedback_count": policy_feedback_count,
        "policy_patch_no_patch_count": policy_no_patch_count,
        "consolidation_run_count": consolidation_run_count,
        "consolidation_applied_count": consolidation_applied_count,
        "consolidation_coverage_count": _int(consolidation_coverage.get("total")),
        "method_candidate_count": method_candidate_count,
        "wiki_page_count": _int(metrics["wiki_pages"].get("total")),
        "policy_driver_count": policy_driver_count,
        "evolution_asset_count": (
            observation_count + reflection_count + policy_patch_count + consolidation_run_count
        ),
        "observation_output_gap": observation_output_gap,
        "policy_patch_gap": policy_patch_gap,
        "consolidation_run_gap": consolidation_run_gap,
        "delivery_feedback_lineage_gap": _int(delivery_lineage.get("uncovered")),
        "observation_lineage_gap": _int(observation_lineage.get("uncovered")),
        "policy_driver_lineage_gap": _int(policy_lineage.get("uncovered")),
        "consolidation_coverage_gap": _int(consolidation_lineage.get("uncovered")),
        "required_tables_missing": required_tables_missing,
        "required_lineage_invalid": required_lineage_invalid,
        "required_tables_missing_count": unavailable_count,
        "required_evidence_empty": required_evidence_empty,
        "required_evidence_empty_count": len(required_evidence_empty),
        "stale_lineages": stale_lineages,
        "stale_lineage_count": len(stale_lineages),
        "unobserved_lineages": unobserved_lineages,
        "unobserved_lineage_count": len(unobserved_lineages),
        "freshness_window_seconds": freshness_window_seconds,
        "cold_start_state": cold_start_state,
        "lineage_coverage": lineage_coverage,
        "observation_status": _observation_status(raw_signal_count, observation_count),
        "policy_patch_status": _policy_patch_status(
            policy_driver_count, policy_patch_count, policy_no_patch_count
        ),
        "consolidation_status": _consolidation_status(
            raw_signal_count, consolidation_runs, consolidation_run_count
        ),
        "tables": {
            "observations": observations,
            "reflections": reflections,
            "policy_patches": policy_patches,
            "policy_patch_feedback": policy_feedback,
            "consolidation_runs": consolidation_runs,
            "consolidation_coverage": consolidation_coverage,
        },
    }


def delivery_outcome_metric(
    db_path: Path,
    *,
    freshness_window_seconds: int,
    now: datetime,
) -> dict[str, Any]:
    """Measure delivery -> explicit feedback/effect coverage by exact IDs."""
    delivery = _table_state(db_path, "delivery_events")
    presentation = _table_state(db_path, "delivery_presentation_receipts")
    if presentation["exists"]:
        return _presentation_delivery_outcome_metric(
            db_path,
            delivery=delivery,
            presentation=presentation,
            freshness_window_seconds=freshness_window_seconds,
            now=now,
        )
    outcomes = _table_state(db_path, "cognitive_outcomes")
    result = _coverage(0, set(), freshness_at="", window=freshness_window_seconds, now=now)
    result.update(
        {
            "path": str(db_path),
            "exists": bool(delivery["exists"] and outcomes["exists"]),
            "delivery_table": delivery,
            "outcome_table": outcomes,
            "explicit_feedback_count": 0,
            "linked_outcome_count": 0,
            "unlinked_outcome_count": int(outcomes.get("total", 0) or 0),
        }
    )
    if not result["exists"]:
        result["cold_start_state"] = "blocked"
        return result

    required_delivery = {
        "event_id",
        "feedback",
        "feedback_at",
        "outcome_id",
        "created_at",
        "decision",
        "delivered_level",
    }
    required_outcome = {"outcome_id", "delivery_event_id", "created_at"}
    if not required_delivery <= set(delivery.get("columns", [])) or not required_outcome <= set(
        outcomes.get("columns", [])
    ):
        result["schema_valid"] = False
        result["cold_start_state"] = "blocked"
        return result

    with _connect_ro(db_path) as conn:
        delivery_columns = set(delivery.get("columns", []))
        eligible_where = (
            "WHERE decision = 'deliver' AND delivered_level != 'silent'"
            if {"decision", "delivered_level"} <= delivery_columns
            else ""
        )
        eligible_feedback = (
            "AND decision = 'deliver' AND delivered_level != 'silent'"
            if eligible_where
            else ""
        )
        delivery_ids = {
            str(row[0])
            for row in conn.execute(
                f"SELECT event_id FROM delivery_events {eligible_where}"  # nosec B608
            ).fetchall()
            if row[0]
        }
        feedback_rows = conn.execute(
            f"""
            SELECT event_id,
                   COALESCE(NULLIF(TRIM(feedback_at), ''), created_at)
            FROM delivery_events
            WHERE feedback IS NOT NULL AND TRIM(feedback) != ''
              {eligible_feedback}
            """
            if "feedback_at" in set(delivery.get("columns", []))
            else f"""
            SELECT event_id, created_at
            FROM delivery_events
            WHERE feedback IS NOT NULL AND TRIM(feedback) != ''
              {eligible_feedback}
            """
        ).fetchall()
        linked_rows = conn.execute(
            """
            SELECT d.event_id, o.created_at
            FROM delivery_events d
            JOIN cognitive_outcomes o
              ON o.outcome_id = d.outcome_id
             AND o.delivery_event_id = d.event_id
            WHERE TRIM(d.outcome_id) != ''
              AND TRIM(o.delivery_event_id) != ''
            """
        ).fetchall()
    feedback_ids = {str(row[0]) for row in feedback_rows if row[0]}
    eligible_linked_rows = [row for row in linked_rows if str(row[0] or "") in delivery_ids]
    linked_ids = {str(row[0]) for row in eligible_linked_rows if row[0]}
    covered_ids = feedback_ids | linked_ids
    freshness_at = _latest_timestamp(
        [row[1] for row in feedback_rows + eligible_linked_rows]
    )
    result.update(
        _coverage(
            len(delivery_ids),
            covered_ids,
            freshness_at=freshness_at,
            window=freshness_window_seconds,
            now=now,
        )
    )
    linked_count = len(linked_ids)
    result.update(
        {
            "schema_valid": True,
            "explicit_feedback_count": len(feedback_ids),
            "linked_outcome_count": linked_count,
            "unlinked_outcome_count": max(0, int(outcomes.get("total", 0)) - linked_count),
        }
    )
    return result


def _presentation_delivery_outcome_metric(
    db_path: Path,
    *,
    delivery: dict[str, Any],
    presentation: dict[str, Any],
    freshness_window_seconds: int,
    now: datetime,
) -> dict[str, Any]:
    """Audit the current route -> presentation -> terminal-effect contract.

    Current delivery records deliberately carry no mutable ``feedback`` or
    ``outcome_id`` columns.  Coverage therefore starts at an immutable host
    receipt, then resolves terminal reaction/outcome revisions from the
    canonical state ledger or a typed reminder timeout.
    """

    state_db = db_path.parent / "producer_consumer_ledger.db"
    reminders_db = db_path.parent / "dialog_reminder.db"
    state = _table_state(state_db, "cognitive_state_revisions")
    heads = _table_state(state_db, "cognitive_state_heads")
    reminders = _table_state(reminders_db, "dialog_reminders")
    result = _coverage(0, set(), freshness_at="", window=freshness_window_seconds, now=now)
    result.update(
        {
            "path": str(db_path),
            "exists": bool(delivery["exists"] and presentation["exists"] and state["exists"] and heads["exists"]),
            "delivery_table": delivery,
            "presentation_table": presentation,
            "outcome_table": state,
            "state_heads_table": heads,
            "reminder_table": reminders,
            "schema_valid": False,
            "explicit_feedback_count": 0,
            "linked_outcome_count": 0,
            "typed_timeout_count": 0,
            "empty_delivery_event_id": 0,
            "routed_without_presentation_ack": 0,
            "outcome_for_unshown_event": 0,
            "cross_scope_outcome_link": 0,
        }
    )
    required_delivery = {"event_id", "created_at", "decision", "delivered_level", "metadata_json"}
    required_presentation = {
        "event_id",
        "recorded_at",
        "host_agent",
        "rendered_content_hash",
        "delivery_event_hash",
        "receipt_hash",
    }
    if (
        not result["exists"]
        or not required_delivery <= set(delivery["columns"])
        or not required_presentation <= set(presentation["columns"])
    ):
        result["cold_start_state"] = "blocked"
        return result

    with _connect_ro(db_path) as conn:
        delivery_rows = conn.execute(
            """SELECT event_id, created_at, metadata_json
               FROM delivery_events
               WHERE decision='deliver' AND delivered_level != 'silent'"""
        ).fetchall()
        receipt_rows = conn.execute(
            """SELECT event_id, recorded_at, host_agent, rendered_content_hash,
                      delivery_event_hash, receipt_hash
               FROM delivery_presentation_receipts"""
        ).fetchall()
    routed_ids = {str(row["event_id"]) for row in delivery_rows if row["event_id"]}
    receipts = {str(row["event_id"]): row for row in receipt_rows if row["event_id"]}
    acked_ids = set(receipts).intersection(routed_ids)

    delivery_metadata: dict[str, dict[str, Any]] = {}
    for row in delivery_rows:
        event_id = str(row["event_id"] or "")
        if not event_id:
            continue
        try:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        delivery_metadata[event_id] = metadata if isinstance(metadata, dict) else {}

    reactions: dict[str, list[dict[str, Any]]] = {}
    outcomes: dict[str, list[dict[str, Any]]] = {}
    predictions: dict[str, dict[str, Any]] = {}
    with _connect_ro(state_db) as conn:
        rows = conn.execute(
            """SELECT r.object_type, r.created_at, r.payload_json
               FROM cognitive_state_revisions AS r
               JOIN cognitive_state_heads AS h ON h.revision_id=r.revision_id
               WHERE r.object_type IN (
                   'user_reaction_event', 'outcome_measurement', 'prediction_record'
               )"""
        ).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if row["object_type"] == "prediction_record":
            prediction_ref = dict(payload.get("delivery_ref") or {})
            event_id = str(prediction_ref.get("event_id") or "")
            if event_id:
                predictions[event_id] = payload
            continue
        event_id = str(dict(payload.get("delivery_ref") or {}).get("event_id") or "")
        if not event_id:
            continue
        target = reactions if row["object_type"] == "user_reaction_event" else outcomes
        target.setdefault(event_id, []).append({"created_at": str(row["created_at"]), "payload": payload})

    timeout_ids: set[str] = set()
    empty_delivery_event_id = 0
    if reminders["exists"] and {"status", "delivery_event_id", "resolved_choice"} <= set(reminders["columns"]):
        with _connect_ro(reminders_db) as conn:
            reminder_rows = conn.execute(
                "SELECT status, delivery_event_id, resolved_choice FROM dialog_reminders"
            ).fetchall()
        for row in reminder_rows:
            status = str(row["status"] or "")
            event_id = str(row["delivery_event_id"] or "")
            if status in {"routed", "pushed"} and not event_id:
                empty_delivery_event_id += 1
            if status == "expired" and str(row["resolved_choice"] or "") == "presentation_timeout" and event_id:
                timeout_ids.add(event_id)

    valid_reaction_ids: set[str] = set()
    valid_outcome_ids: set[str] = set()
    invalid_feedback_display_link = 0
    invalid_outcome_presentation_link = 0
    cross_scope_outcome_link = 0
    for event_id in acked_ids:
        receipt = receipts[event_id]
        for reaction in reactions.get(event_id, []):
            display_ref = dict(reaction["payload"].get("display_ref") or {})
            if (
                str(display_ref.get("display_id") or "") == str(receipt["receipt_hash"])
                and str(display_ref.get("content_hash") or "")
                == str(receipt["rendered_content_hash"])
            ):
                valid_reaction_ids.add(event_id)
            else:
                invalid_feedback_display_link += 1
        for outcome in outcomes.get(event_id, []):
            presentation_ref = dict(outcome["payload"].get("presentation_ref") or {})
            if (
                str(presentation_ref.get("receipt_hash") or "")
                != str(receipt["receipt_hash"])
                or str(presentation_ref.get("rendered_content_hash") or "")
                != str(receipt["rendered_content_hash"])
                or str(presentation_ref.get("delivery_event_hash") or "")
                != str(receipt["delivery_event_hash"])
            ):
                invalid_outcome_presentation_link += 1
                continue
            if _outcome_crosses_delivery_scope(
                outcome["payload"],
                delivery_metadata.get(event_id, {}),
                predictions.get(event_id, {}),
            ):
                cross_scope_outcome_link += 1
                continue
            valid_outcome_ids.add(event_id)
    terminal_ids = valid_reaction_ids | valid_outcome_ids | timeout_ids.intersection(acked_ids)
    timestamps = [str(receipts[event_id]["recorded_at"]) for event_id in terminal_ids if event_id in receipts]
    unshown_outcomes = set(outcomes).difference(acked_ids)
    result.update(
        _coverage(
            len(acked_ids),
            terminal_ids,
            freshness_at=_latest_timestamp(timestamps),
            window=freshness_window_seconds,
            now=now,
        )
    )
    result.update(
        {
            "schema_valid": True,
            "explicit_feedback_count": len(valid_reaction_ids),
            "linked_outcome_count": len(valid_outcome_ids),
            "typed_timeout_count": len(timeout_ids.intersection(acked_ids)),
            "unlinked_outcome_count": len(unshown_outcomes),
            "empty_delivery_event_id": empty_delivery_event_id,
            "routed_without_presentation_ack": len(routed_ids.difference(acked_ids)),
            "outcome_for_unshown_event": len(unshown_outcomes),
            "invalid_feedback_display_link": invalid_feedback_display_link,
            "invalid_outcome_presentation_link": invalid_outcome_presentation_link,
            "cross_scope_outcome_link": cross_scope_outcome_link,
            "lineage_refs": sorted(terminal_ids),
        }
    )
    return result


def _outcome_crosses_delivery_scope(
    outcome_payload: dict[str, Any],
    delivery_metadata: dict[str, Any],
    prediction_payload: dict[str, Any],
) -> bool:
    """Require an objective outcome to retain the presented route's owner/scope."""

    delivery_principal = delivery_metadata.get("delivery_principal")
    outcome_access = outcome_payload.get("access_control")
    prediction_access = prediction_payload.get("access_control")
    if not isinstance(delivery_principal, dict) or not isinstance(outcome_access, dict):
        return True
    owner = outcome_access.get("owner")
    if not isinstance(owner, dict):
        return True
    if (
        str(owner.get("principal_id") or "")
        != str(delivery_principal.get("principal_id") or "")
        or str(owner.get("agent") or "") != str(delivery_principal.get("agent") or "")
    ):
        return True
    if not isinstance(prediction_access, dict):
        return True
    outcome_scope = outcome_access.get("scope")
    prediction_scope = prediction_access.get("scope")
    return not isinstance(outcome_scope, dict) or not isinstance(prediction_scope, dict) or (
        str(outcome_scope.get("scope_type") or "")
        != str(prediction_scope.get("scope_type") or "")
        or str(outcome_scope.get("scope_id") or "")
        != str(prediction_scope.get("scope_id") or "")
    )


def observation_lineage_metric(
    raw_db_path: Path,
    observation_db_path: Path,
    *,
    freshness_window_seconds: int,
    now: datetime,
) -> dict[str, Any]:
    """Measure current Raw revision -> Observation terminal coverage.

    A row whose ``source_id`` merely resembles a Raw logical event is not
    lineage.  Coverage requires either (1) an exact Raw provenance edge whose
    target Observation names the same immutable current revision or (2) a
    typed, active ``intentional_no_observation`` receipt for that revision.
    """
    raw = _table_state(raw_db_path, "raw_turns")
    observations = _table_state(observation_db_path, "observations")
    edges = _table_state(raw_db_path, "raw_provenance_edges")
    gaps = _table_state(raw_db_path, "raw_provenance_gaps")
    result = _coverage(0, set(), freshness_at="", window=freshness_window_seconds, now=now)
    result.update(
        {
            "raw_table": raw,
            "observations_table": observations,
            "provenance_edges_table": edges,
            "provenance_gaps_table": gaps,
            "observation_created": 0,
            "intentional_no_observation": 0,
            "visible_raw_denominator": 0,
            "visible_raw_without_observation": 0,
            "all_visible_raw_skipped": 0,
            "invalid_edge_count": 0,
            "invalid_terminal_count": 0,
        }
    )
    required_observation_columns = {
        "id",
        "source_type",
        "source_id",
        "created_at",
        "updated_at",
    }
    required_edge_columns = {
        "source_revision_id",
        "consumer_type",
        "consumer_id",
        "span_start",
        "span_end",
        "created_at",
    }
    required_gap_columns = {
        "consumer_type",
        "consumer_id",
        "reason",
        "status",
        "source_agent",
        "session_id",
        "created_at",
    }
    if (
        not observations["exists"]
        or not required_observation_columns <= set(observations.get("columns", []))
        or not edges["exists"]
        or not required_edge_columns <= set(edges.get("columns", []))
        or not gaps["exists"]
        or not required_gap_columns <= set(gaps.get("columns", []))
    ):
        result["cold_start_state"] = "blocked"
        return result

    with _connect_ro(observation_db_path) as conn:
        observation_rows = conn.execute(
            """
            SELECT id, source_type, source_id, updated_at
            FROM observations
            WHERE LOWER(COALESCE(source_type, '')) = 'raw'
            """
        ).fetchall()
    observations_by_id = {
        str(row[0]): (str(row[2] or ""), str(row[3] or ""))
        for row in observation_rows
        if row[0]
    }

    with _connect_ro(raw_db_path) as conn:
        edge_rows = conn.execute(
            """
            SELECT e.source_revision_id, e.span_start, e.span_end, e.consumer_id, e.created_at
            FROM raw_provenance_edges AS e
            JOIN raw_turns AS t ON t.current_revision_id=e.source_revision_id
            WHERE e.consumer_type='observation'
            """
        ).fetchall()
        gap_rows = conn.execute(
            """
            SELECT g.consumer_id, g.reason, g.status, g.source_agent, g.session_id, g.created_at
            FROM raw_provenance_gaps AS g
            JOIN raw_turns AS t ON t.current_revision_id=g.consumer_id
            WHERE g.consumer_type='observation'
            """
        ).fetchall()

    # These maps contain only current-terminal candidates.  The unbounded Raw
    # payload itself is deliberately not retained: a production Raw database
    # may hold multi-gigabyte lossless snapshots.
    edges_by_revision: dict[str, list[sqlite3.Row]] = {}
    for row in edge_rows:
        edges_by_revision.setdefault(str(row[0] or ""), []).append(row)
    gaps_by_revision: dict[str, list[sqlite3.Row]] = {}
    for row in gap_rows:
        gaps_by_revision.setdefault(str(row[0] or ""), []).append(row)

    observation_created: set[str] = set()
    visible_observation_created: set[str] = set()
    intentional_no_observation: set[str] = set()
    freshness_values: list[str] = []
    invalid_edge_count = 0
    invalid_terminal_count = 0
    current_revision_ids: list[str] = []
    current_revision_count = 0
    visible_raw_denominator = 0

    try:
        current_turns = iter_current_raw_turns_readonly(
            raw_db_path,
            include_structured_payload=False,
        )
        for turn in current_turns:
            revision_id = turn.revision_id
            current_revision_count += 1
            if len(current_revision_ids) < LINEAGE_SAMPLE_LIMIT:
                current_revision_ids.append(revision_id)
            visible_text = canonical_observation_text(
                {
                    "user_content": turn.user_content,
                    "assistant_content": turn.assistant_content,
                }
            )
            if visible_text.strip():
                visible_raw_denominator += 1

            for row in edges_by_revision.get(revision_id, []):
                span_start = _int(row[1])
                span_end = _int(row[2])
                consumer_id = str(row[3] or "")
                target = observations_by_id.get(consumer_id)
                if target is None or not is_exact_current_raw_observation_edge(
                    observation_source_id=target[0],
                    source_revision_id=revision_id,
                    span_start=span_start,
                    span_end=span_end,
                    visible_text_length=len(visible_text),
                ):
                    invalid_edge_count += 1
                    continue
                observation_created.add(revision_id)
                if visible_text.strip():
                    visible_observation_created.add(revision_id)
                freshness_values.append(str(row[4] or target[1] or ""))

            for row in gaps_by_revision.get(revision_id, []):
                reason = str(row[1] or "")
                status = str(row[2] or "")
                if (
                    status != INTENTIONAL_NO_OBSERVATION_STATUS
                    or reason not in INTENTIONAL_NO_OBSERVATION_REASONS
                    or str(row[3] or "") != turn.source_agent
                    or str(row[4] or "") != turn.session_id
                ):
                    invalid_terminal_count += 1
                    continue
                intentional_no_observation.add(revision_id)
                freshness_values.append(str(row[5] or ""))
    except CanonicalRawReadError as exc:
        result["cold_start_state"] = "blocked"
        result["error"] = str(exc)
        return result

    if not current_revision_count:
        return result

    # An exact output edge supersedes an older no-observation terminal for the
    # same current revision.  Both are terminal evidence, but expose their
    # counts separately so an all-skip implementation cannot look healthy.
    covered = observation_created | intentional_no_observation
    result = _coverage(
        current_revision_count,
        covered,
        freshness_at=_latest_timestamp(freshness_values),
        window=freshness_window_seconds,
        now=now,
    )
    result.update(
        {
            "raw_table": raw,
            "observations_table": observations,
            "provenance_edges_table": edges,
            "provenance_gaps_table": gaps,
            "observation_created": len(observation_created),
            "intentional_no_observation": len(intentional_no_observation - observation_created),
            "visible_raw_denominator": visible_raw_denominator,
            "visible_raw_without_observation": max(
                0, visible_raw_denominator - len(visible_observation_created)
            ),
            "all_visible_raw_skipped": int(
                bool(visible_raw_denominator) and not observation_created
            ),
            "invalid_edge_count": invalid_edge_count,
            "invalid_terminal_count": invalid_terminal_count,
            "current_revision_ids": current_revision_ids,
            "current_revision_count": current_revision_count,
        }
    )
    return result


def is_exact_current_raw_observation_edge(
    *,
    observation_source_id: str | None,
    source_revision_id: str,
    span_start: int,
    span_end: int,
    visible_text_length: int,
) -> bool:
    """Return whether one Observation edge proves exact current Raw lineage.

    The readiness audit and operational reconcilers share this predicate so a
    cleanup cannot remove an edge that the readiness gate would accept (or
    retain one the gate would reject).  Callers pass ``None`` for a missing or
    non-Raw Observation target.
    """
    return bool(
        observation_source_id is not None
        and observation_source_id == source_revision_id
        and span_start >= 0
        and span_end > span_start
        and span_end <= visible_text_length
    )


def policy_driver_lineage_metric(
    database_dir: Path,
    policy_db_path: Path,
    *,
    freshness_window_seconds: int,
    now: datetime,
) -> dict[str, Any]:
    """Measure reflection/recap driver -> patch or explicit no-patch coverage."""
    drivers: set[str] = set()
    recap_effects: dict[str, str] = {}
    reflection_path = database_dir / "reflections.db"
    reflection_state = _table_state(reflection_path, "reflection_records")
    reflection_schema_valid = reflection_state["exists"] and "id" in set(
        reflection_state.get("columns", [])
    )
    if reflection_schema_valid:
        with _connect_ro(reflection_path) as conn:
            drivers.update(
                f"reflection:{row[0]}"
                for row in conn.execute("SELECT id FROM reflection_records").fetchall()
                if row[0]
            )
    recap_path = database_dir / "recap_tasks.db"
    recap_outcomes = _table_state(recap_path, "recap_consumption_outcomes")
    if recap_outcomes["exists"]:
        columns = set(recap_outcomes.get("columns", []))
        if {"recap_id", "consumer", "outcome"} <= columns:
            time_column = "created_at" if "created_at" in columns else "''"
            with _connect_ro(recap_path) as conn:
                rows = conn.execute(
                    f"""
                    SELECT recap_id, outcome, {time_column}
                    FROM recap_consumption_outcomes
                    WHERE consumer = 'policy_patch'
                    """  # nosec B608
                ).fetchall()
            drivers.update(f"retrospective:{row[0]}" for row in rows if row[0])
            recap_effects.update(
                {
                    f"retrospective:{row[0]}": str(row[2] or "")
                    for row in rows
                    if row[0]
                    and str(row[1] or "") in {"created", "applied", "consumed"}
                }
            )

    patch_state = _table_state(policy_db_path, "policy_patches")
    feedback_state = _table_state(policy_db_path, "policy_patch_feedback")
    if not reflection_schema_valid or not patch_state["exists"] or not feedback_state["exists"]:
        result = _coverage(
            len(drivers), set(), freshness_at="", window=freshness_window_seconds, now=now
        )
        result["cold_start_state"] = "blocked"
        return result
    if not {"source_type", "source_id", "created_at"} <= set(
        patch_state.get("columns", [])
    ) or not {
        "patch_id",
        "outcome",
        "created_at",
    } <= set(feedback_state.get("columns", [])):
        result = _coverage(
            len(drivers), set(), freshness_at="", window=freshness_window_seconds, now=now
        )
        result["cold_start_state"] = "blocked"
        return result

    covered: set[str] = set(recap_effects)
    timestamps: list[Any] = list(recap_effects.values())
    with _connect_ro(policy_db_path) as conn:
        patch_columns = set(patch_state.get("columns", []))
        if {"source_type", "source_id"} <= patch_columns:
            time_column = "created_at" if "created_at" in patch_columns else "''"
            rows = conn.execute(
                f"SELECT source_type, source_id, {time_column} FROM policy_patches"  # nosec B608
            ).fetchall()
            for source_type, source_id, created_at in rows:
                source_kind = str(source_type or "")
                if source_kind not in {"reflection", "reflection_shift", "retrospective"}:
                    continue
                normalized = "retrospective" if source_kind == "retrospective" else "reflection"
                key = f"{normalized}:{source_id}"
                if key in drivers:
                    covered.add(key)
                    timestamps.append(created_at)
        feedback_columns = set(feedback_state.get("columns", []))
        selected = [
            "patch_id",
            "outcome",
            "evidence_json" if "evidence_json" in feedback_columns else "'{}'",
            "source_event_id" if "source_event_id" in feedback_columns else "''",
            "created_at" if "created_at" in feedback_columns else "''",
        ]
        rows = conn.execute(
            f"SELECT {', '.join(selected)} FROM policy_patch_feedback "  # nosec B608
            "WHERE outcome = 'no_patch'"
        ).fetchall()
        for patch_id, _outcome, evidence_json, source_event_id, created_at in rows:
            record_id = _feedback_record_id(patch_id, evidence_json, source_event_id)
            key = f"reflection:{record_id}" if record_id else ""
            if key in drivers:
                covered.add(key)
                timestamps.append(created_at)
    return _coverage(
        len(drivers),
        covered,
        freshness_at=_latest_timestamp(timestamps),
        window=freshness_window_seconds,
        now=now,
    )


def consolidation_lineage_metric(
    db_path: Path,
    *,
    freshness_window_seconds: int,
    now: datetime,
) -> dict[str, Any]:
    """Measure candidate -> applied coverage; dry-runs remain uncovered."""
    runs = _table_state(db_path, "consolidation_runs")
    receipts = _table_state(db_path, "consolidation_coverage_receipts")
    if not runs["exists"] or not receipts["exists"]:
        result = _coverage(0, set(), freshness_at="", window=freshness_window_seconds, now=now)
        result["cold_start_state"] = "blocked"
        return result
    if not {"run_id", "applied", "raw_candidate_count", "created_at"} <= set(
        runs.get("columns", [])
    ) or not {"run_id", "source_event_id", "created_at"} <= set(
        receipts.get("columns", [])
    ):
        result = _coverage(0, set(), freshness_at="", window=freshness_window_seconds, now=now)
        result["cold_start_state"] = "blocked"
        return result
    with _connect_ro(db_path) as conn:
        coverage_columns = set(receipts.get("columns", []))
        time_column = "c.created_at" if "created_at" in coverage_columns else "''"
        required_receipt_columns = {
            "source_revision_id",
            "source_content_hash",
            "exact_source_ref",
            "covered_by",
            "method_content_hash",
            "mutation_id",
        }
        if not required_receipt_columns <= coverage_columns or "report_json" not in set(
            runs.get("columns", [])
        ):
            result = _coverage(0, set(), freshness_at="", window=freshness_window_seconds, now=now)
            result["cold_start_state"] = "blocked"
            return result
        run_rows = conn.execute(
            "SELECT run_id, report_json FROM consolidation_runs WHERE COALESCE(applied, 0) != 0"
        ).fetchall()
        expected: dict[str, tuple[str, str]] = {}
        expected_by_run: dict[tuple[str, str], tuple[str, str]] = {}
        malformed_plan_count = 0
        for run_id, report_json in run_rows:
            try:
                report = json.loads(str(report_json or "{}"))
                candidates = report["coverage"]["candidate_dispositions"]
            except (TypeError, ValueError, KeyError):
                malformed_plan_count += 1
                continue
            if not isinstance(candidates, list):
                malformed_plan_count += 1
                continue
            for item in candidates:
                if not isinstance(item, dict):
                    malformed_plan_count += 1
                    continue
                ref = str(item.get("exact_source_ref") or "")
                revision = str(item.get("source_revision_id") or "")
                content_hash = str(item.get("source_content_hash") or "")
                if not ref or not revision or not content_hash:
                    malformed_plan_count += 1
                    continue
                identity = (revision, content_hash)
                expected_by_run[(str(run_id), ref)] = identity
                prior = expected.setdefault(ref, identity)
                if prior != identity:
                    malformed_plan_count += 1
        rows = conn.execute(
            f"""
            SELECT c.run_id, c.exact_source_ref, c.source_revision_id,
                   c.source_content_hash, {time_column}
            FROM consolidation_coverage_receipts c
            JOIN consolidation_runs r ON r.run_id = c.run_id
            WHERE COALESCE(r.applied, 0) != 0
              AND TRIM(c.source_revision_id) != ''
              AND TRIM(c.source_content_hash) != ''
              AND TRIM(c.exact_source_ref) != ''
              AND TRIM(c.covered_by) != ''
              AND TRIM(c.method_content_hash) != ''
              AND TRIM(c.mutation_id) != ''
            """  # nosec B608
        ).fetchall()
    denominator = len(expected)
    refs: set[str] = set()
    invalid_receipt_count = 0
    receipt_counts: dict[str, int] = {}
    timestamps: list[Any] = []
    for run_id, ref, revision, content_hash, created_at in rows:
        key = (str(run_id or ""), str(ref or ""))
        identity = (str(revision or ""), str(content_hash or ""))
        if expected_by_run.get(key) != identity:
            invalid_receipt_count += 1
            continue
        refs.add(key[1])
        receipt_counts[key[1]] = receipt_counts.get(key[1], 0) + 1
        timestamps.append(created_at)
    duplicate_receipt_count = sum(max(0, count - 1) for count in receipt_counts.values())
    result = _coverage(
        denominator,
        refs,
        freshness_at=_latest_timestamp(timestamps),
        window=freshness_window_seconds,
        now=now,
    )
    result["covered"] = min(result["covered"], denominator)
    result["uncovered"] = max(0, denominator - result["covered"])
    result["coverage_ratio"] = _ratio(result["covered"], denominator)
    result["malformed_plan_count"] = malformed_plan_count
    result["invalid_receipt_count"] = invalid_receipt_count
    result["duplicate_receipt_count"] = duplicate_receipt_count
    # A malformed frozen plan means the auditor cannot establish the candidate
    # denominator.  Reporting the well-formed subset as a complete lineage
    # would turn a damaged producer record into a readiness green.
    if malformed_plan_count or invalid_receipt_count or duplicate_receipt_count:
        result["cold_start_state"] = "blocked"
        result["invalid_proof_count"] = (
            malformed_plan_count + invalid_receipt_count + duplicate_receipt_count
        )
    result["legacy_coverage_rejected"] = 0
    return result


def _consolidation_coverage_metric(db_path: Path) -> dict[str, Any]:
    """Expose receipt-backed coverage; old tables are intentionally non-certifying."""
    return _table_state(db_path, "consolidation_coverage_receipts")


def _public_lineage_metric(metric: dict[str, Any]) -> dict[str, Any]:
    return {
        key: metric.get(key, default)
        for key, default in (
            ("denominator", 0),
            ("covered", 0),
            ("uncovered", 0),
            ("coverage_ratio", 0.0),
            ("lineage_refs", []),
            ("lineage_ref_count", 0),
            ("lineage_refs_truncated", False),
            ("freshness_at", ""),
            ("freshness_state", "unavailable"),
            ("cold_start_state", "blocked"),
        )
    }


def _table_count_metric(db_path: Path, table: str) -> dict[str, Any]:
    return _table_state(db_path, table)


def _consolidation_runs_metric(db_path: Path) -> dict[str, Any]:
    base = _table_state(db_path, "consolidation_runs")
    defaults = {"raw_candidate_total": 0, "applied": 0, "dry_runs": 0, "latest_created_at": ""}
    if not base["exists"]:
        base.update(defaults)
        return base
    columns = set(base.get("columns", []))
    with _connect_ro(db_path) as conn:
        raw_candidate_total = (
            _scalar(conn, "SELECT COALESCE(SUM(raw_candidate_count), 0) FROM consolidation_runs")
            if "raw_candidate_count" in columns
            else 0
        )
        applied = (
            _scalar(conn, "SELECT COUNT(*) FROM consolidation_runs WHERE COALESCE(applied, 0) != 0")
            if "applied" in columns
            else 0
        )
        latest = (
            conn.execute("SELECT MAX(created_at) FROM consolidation_runs").fetchone()
            if "created_at" in columns
            else None
        )
    base.update(
        {
            "raw_candidate_total": raw_candidate_total,
            "applied": applied,
            "dry_runs": max(0, _int(base.get("total")) - applied),
            "latest_created_at": str(latest[0] or "") if latest else "",
        }
    )
    return base


def _policy_patch_feedback_metric(db_path: Path) -> dict[str, Any]:
    base = _table_state(db_path, "policy_patch_feedback")
    if not base["exists"]:
        base.update({"outcome_counts": {}, "no_patch": 0})
        return base
    if "outcome" not in set(base.get("columns", [])):
        base.update({"outcome_counts": {}, "no_patch": 0})
        return base
    with _connect_ro(db_path) as conn:
        rows = conn.execute(
            "SELECT outcome, COUNT(*) FROM policy_patch_feedback GROUP BY outcome"
        ).fetchall()
    counts = {str(row[0] or ""): int(row[1]) for row in rows}
    base.update({"outcome_counts": counts, "no_patch": counts.get("no_patch", 0)})
    return base


def _closed_status_count(status_counts: Any) -> int:
    if not isinstance(status_counts, dict):
        return 0
    return sum(
        _int(status_counts.get(status))
        for status in ("resolved", "ignored", "dismissed", "done", "archived")
    )


def _observation_status(raw_signal_count: int, observation_count: int) -> str:
    if observation_count > 0:
        return "producing"
    return "no_observations_from_available_raw" if raw_signal_count > 0 else "no_raw_input"


def _policy_patch_status(
    policy_driver_count: int, policy_patch_count: int, policy_no_patch_count: int
) -> str:
    if policy_patch_count > 0:
        return "has_policy_patches"
    if policy_no_patch_count > 0:
        return "no_patch_evidence_recorded"
    if policy_driver_count > 0:
        return "candidate_activity_without_policy_patch"
    return "no_patch_this_cycle_no_candidate_activity"


def _consolidation_status(
    raw_signal_count: int,
    consolidation_runs: dict[str, Any],
    consolidation_run_count: int,
) -> str:
    if _int(consolidation_runs.get("applied")) > 0:
        return "applied_with_coverage"
    if consolidation_run_count > 0:
        return "dry_run_only"
    if consolidation_runs.get("exists"):
        return "initialized_no_runs"
    return "not_initialized" if raw_signal_count > 0 else "no_raw_input"


def _scalar(conn: sqlite3.Connection, query: str) -> int:
    row = conn.execute(query).fetchone()
    return int(row[0]) if row else 0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _table_state(db_path: Path, table: str) -> dict[str, Any]:
    base: dict[str, Any] = {
        "exists": False,
        "path": str(db_path),
        "table": table,
        "total": 0,
        "columns": [],
    }
    if not db_path.exists():
        return base
    try:
        with _connect_ro(db_path) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                return base
            columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]  # nosec B608
            total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # nosec B608
    except (OSError, sqlite3.Error) as exc:
        base["error"] = f"{type(exc).__name__}: {exc}"
        return base
    base.update({"exists": True, "columns": columns, "total": int(total[0] if total else 0)})
    return base


def _coverage(
    denominator: int,
    covered_refs: Iterable[str],
    *,
    freshness_at: str,
    window: int,
    now: datetime,
) -> dict[str, Any]:
    refs = sorted({str(item) for item in covered_refs if str(item)})
    covered = min(max(0, int(denominator)), len(refs))
    return {
        "denominator": max(0, int(denominator)),
        "covered": covered,
        "uncovered": max(0, int(denominator) - covered),
        "coverage_ratio": _ratio(covered, int(denominator)),
        "lineage_refs": refs[:LINEAGE_SAMPLE_LIMIT],
        "lineage_ref_count": len(refs),
        "lineage_refs_truncated": len(refs) > LINEAGE_SAMPLE_LIMIT,
        "freshness_at": freshness_at,
        "freshness_state": _freshness_state(freshness_at, window=window, now=now),
        "cold_start_state": "observed" if denominator > 0 else "unobserved",
    }


def _feedback_record_id(patch_id: Any, evidence_json: Any, source_event_id: Any) -> str:
    source = str(source_event_id or "")
    for prefix in ("reflection:", "reflection-no-patch:"):
        if source.startswith(prefix):
            return source[len(prefix) :]
    try:
        evidence = json.loads(str(evidence_json or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        evidence = {}
    if isinstance(evidence, dict) and evidence.get("record_id"):
        return str(evidence["record_id"])
    patch = str(patch_id or "")
    prefix = "reflection-no-patch-"
    return patch[len(prefix) :] if patch.startswith(prefix) else ""


def _freshness_state(value: str, *, window: int, now: datetime) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return "unavailable"
    age_seconds = (now - parsed).total_seconds()
    if age_seconds < -300:
        return "invalid"
    return "fresh" if age_seconds <= window else "stale"


def _latest_timestamp(values: Iterable[Any]) -> str:
    parsed = [_parse_time(value) for value in values]
    present = [value for value in parsed if value is not None]
    return max(present).isoformat() if present else ""


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator > 0 else 0.0


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = connect_readonly_sqlite(db_path, timeout_seconds=5)
    conn.row_factory = sqlite3.Row
    return conn
