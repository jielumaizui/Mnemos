"""Independent source selection for historical feedback audit inventory."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping


def audit_history_specs(
    database_class: str,
    conn: sqlite3.Connection,
) -> tuple[tuple[str, str], ...]:
    """Return the independent table/predicate inventory for one database."""

    if database_class == "delivery_events":
        columns = _column_names(conn, "delivery_events")
        predicate = " OR ".join(
            f'COALESCE("{name}", \'\')<>\'\''
            for name in ("feedback", "outcome_id")
            if name in columns
        ) or "0"
        return (
            ("delivery_events", predicate),
            ("feedback_events", ""),
            ("feedback_receipts", ""),
            ("cognitive_outcomes", ""),
            ("outcome_feedback_events", ""),
            ("outcome_projection_receipts", ""),
        )
    if database_class == "feedback_signals":
        return (("feedback_signals", ""),)
    if database_class == "scoring":
        columns = _column_names(conn, "search_sessions")
        predicate = " OR ".join(
            f'COALESCE("{name}", \'\')<>\'\''
            for name in (
                "clicked_path",
                "clicked_at",
                "opened_path",
                "opened_at",
                "ignored_at",
                "outcome_status",
                "outcome_at",
            )
            if name in columns
        ) or "0"
        return (
            ("search_sessions", predicate),
            (
                "ground_truth_signals",
                "signal_type IN ('search_click','search_ignore') "
                "OR session_id LIKE 'feedback-%'",
            ),
            (
                "scorer_training_queue",
                "session_id LIKE 'feedback-%' OR "
                "json_extract(features_json, '$.source') IN "
                "('push_feedback','search_click','search_ignore',"
                "'dialog_reminder','reflection_feedback','delivery_feedback')",
            ),
            ("scorer_feedback_events", ""),
            ("bayesian_feedback", ""),
        )
    if database_class == "reflections":
        return (
            (
                "reflection_records",
                "COALESCE(feedback_type,'')<>'' OR "
                "COALESCE(implicit_feedback_type,'')<>''",
            ),
            ("layer5_experiences", "type='outcome_feedback'"),
            ("cognitive_shifts", "shift_type='outcome_feedback'"),
        )
    if database_class == "rule_weight_optimizer":
        prefix = (
            "rule_name LIKE 'push_feedback:%' OR "
            "rule_name LIKE 'search_click:%' OR "
            "rule_name LIKE 'search_ignore:%' OR "
            "rule_name LIKE 'dialog_reminder:%' OR "
            "rule_name LIKE 'reflection_feedback:%' OR "
            "rule_name LIKE 'delivery_feedback:%'"
        )
        return (
            ("rule_outcomes", f"COALESCE(source_event_id,'')<>'' OR {prefix}"),
            ("optimize_log", f"COALESCE(source_event_id,'')<>'' OR {prefix}"),
            ("weight_history", prefix),
        )
    raise ValueError("unknown feedback history database class")


def audit_history_source_refs(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract cross-table references without importing migration code."""

    refs: set[str] = set()
    for field in (
        "feedback_event_id",
        "delivery_event_id",
        "event_id",
        "outcome_id",
        "source_event_id",
        "session_id",
        "related_reflection_id",
        "rule_name",
        "target_ref",
    ):
        value = row.get(field)
        if value in {None, ""}:
            continue
        refs.add(f"{field}:{value}")
        if field == "event_id":
            refs.add(f"delivery_event_id:{value}")
        elif field == "source_event_id" and str(value).startswith("feedback-"):
            refs.add(f"feedback_event_id:{value}")
        elif field == "target_ref" and str(value).startswith("delivery-"):
            refs.add(f"delivery_event_id:{value}")
    for field in (
        "metadata_json",
        "result_json",
        "receipt_json",
        "features_json",
        "context_json",
    ):
        value = row.get(field)
        if not isinstance(value, str) or not value:
            continue
        loaded = _json(value, default=None)
        if not isinstance(loaded, Mapping):
            raise ValueError(f"malformed feedback history JSON: {field}")
        for ref_field in (
            "feedback_event_id",
            "delivery_event_id",
            "outcome_id",
            "session_id",
            "source_event_id",
            "reflection_id",
        ):
            ref_value = loaded.get(ref_field)
            if ref_value in {None, ""}:
                continue
            refs.add(f"{ref_field}:{ref_value}")
            if ref_field == "source_event_id" and str(ref_value).startswith(
                "feedback-"
            ):
                refs.add(f"feedback_event_id:{ref_value}")
    return tuple(sorted(refs))


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {
        str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')
    }


def _json(value: Any, *, default: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None
