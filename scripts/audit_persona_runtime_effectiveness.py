#!/usr/bin/env python3
"""Read-only effectiveness audit for the production persona v2 store.

This intentionally never seeds assertions, signals, or usage rows.  The
structural contract remains in ``audit_persona_profile_contract.py``; this
script proves only what is present in the selected production snapshot.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from core.cognitive.access_control import validate_cognitive_access_envelope

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


REQUIRED_TABLES = (
    "profile_signals",
    "profile_assertions",
    "profile_assertion_revisions",
    "profile_usage_log",
)
REQUIRED_LEDGER_TABLES = (
    "profile_assertion_heads",
    "profile_assertion_revision_delete_permits",
    "profile_read_authorizations",
    "profile_usage_outbox",
    "mnemos_schema_registry",
)

REQUIRED_USAGE_COLUMNS = {
    "consumer",
    "profile_fields_used",
    "profile_revision_ids",
    "matched_assertion_revisions",
    "scope_snapshot",
    "read_purpose",
    "read_authorization_token",
    "action_changed",
    "outcome",
    "request_id",
    "decision_id",
    "baseline_hash",
    "persona_enabled_hash",
    "expected_delta",
    "actual_target_delta",
    "target_receipt",
    "target_receipt_hash",
    "terminal_status",
    "idempotency_key",
}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({name})")}


def audit_persona_runtime_effectiveness(db_path: Path) -> dict[str, Any]:
    """Inspect one immutable production snapshot without any write-capable open."""

    resolved = db_path.expanduser().resolve(strict=False)
    payload: dict[str, Any] = {
        "schema_version": "mnemos.persona_runtime_effectiveness.v2",
        "db_path": str(resolved),
        "read_only": True,
        "seeded_by_audit": False,
        "ok": False,
        "errors": [],
    }
    if not resolved.is_file():
        payload["errors"].append("persona_signal_store_uninitialized")
        return payload
    # ``mode=ro`` sees a current WAL snapshot without acquiring write
    # capability.  ``immutable=1`` would silently ignore recent WAL pages.
    with sqlite3.connect(f"file:{resolved}?mode=ro", uri=True) as conn:
        missing = [
            table
            for table in (*REQUIRED_TABLES, *REQUIRED_LEDGER_TABLES)
            if not _table_exists(conn, table)
        ]
        if missing:
            payload["errors"].append(f"missing_tables:{','.join(missing)}")
            return payload
        from core.persona.profile_assertion_schema import inspect_profile_assertion_schema

        schema_state = inspect_profile_assertion_schema(conn)
        payload["assertion_schema"] = schema_state.as_dict()
        if not schema_state.ok:
            payload["errors"].append(
                "profile_assertion_schema_drift:" + ",".join(schema_state.errors)
            )
        missing_usage_columns = sorted(
            REQUIRED_USAGE_COLUMNS - _table_columns(conn, "profile_usage_log")
        )
        if missing_usage_columns:
            payload["errors"].append(
                "profile_usage_log_missing_columns:" + ",".join(missing_usage_columns)
            )
            return payload
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in REQUIRED_TABLES
        }
        active = conn.execute("""SELECT assertion_id, current_revision_id, supporting_signals,
                      confidence, access_control,
                      dimension, claim, contradicting_signals, privacy_level,
                      last_verified_at, revision_policy, status
               FROM profile_assertions WHERE status='active'""").fetchall()
        revision_count = int(
            conn.execute("SELECT COUNT(*) FROM profile_assertion_revisions").fetchone()[0]
        )
        projection_without_revision = int(conn.execute("""
                SELECT COUNT(*)
                FROM profile_assertions AS current
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM profile_assertion_heads AS head
                    JOIN profile_assertion_revisions AS revision
                      ON revision.revision_id=head.revision_id
                     AND revision.assertion_id=head.assertion_id
                    WHERE head.assertion_id=current.assertion_id
                      AND head.revision_id=current.current_revision_id
                )
                """).fetchone()[0])
        assertions_without_evidence = 0
        assertions_without_scope = 0
        projection_revision_drift = 0
        for (
            assertion_id,
            current_revision_id,
            supporting,
            confidence,
            access,
            dimension,
            claim,
            contradicting,
            privacy_level,
            last_verified_at,
            revision_policy,
            status,
        ) in active:
            if not _has_live_assertion_evidence(conn, supporting, confidence):
                assertions_without_evidence += 1
            try:
                envelope = validate_cognitive_access_envelope(json.loads(str(access or "")))
                if (
                    envelope["consent"]["status"] != "granted"
                    or not envelope["consent"]["provenance_refs"]
                    or not envelope["source_acl_lineage"]
                ):
                    assertions_without_scope += 1
            except (TypeError, ValueError, json.JSONDecodeError):
                assertions_without_scope += 1
            head = conn.execute(
                """SELECT dimension, claim, supporting_signals, contradicting_signals,
                          confidence, privacy_level, last_verified_at, revision_policy,
                          status, access_control
                   FROM profile_assertion_heads AS heads
                   JOIN profile_assertion_revisions AS revision
                     ON revision.revision_id=heads.revision_id
                    AND revision.assertion_id=heads.assertion_id
                   WHERE heads.assertion_id=? AND heads.revision_id=?""",
                (assertion_id, current_revision_id),
            ).fetchone()
            current_fields = (
                dimension,
                claim,
                supporting,
                contradicting,
                confidence,
                privacy_level,
                last_verified_at,
                revision_policy,
                status,
                access,
            )
            if head is None or tuple(head) != current_fields:
                projection_revision_drift += 1
        usage = conn.execute("""SELECT consumer, profile_fields_used, profile_revision_ids,
                      matched_assertion_revisions, scope_snapshot, read_purpose,
                      read_authorization_token, action_changed, outcome,
                      baseline_hash, persona_enabled_hash,
                      expected_delta, actual_target_delta, request_id, decision_id,
                      target_receipt, target_receipt_hash, terminal_status, idempotency_key,
                      created_at
               FROM profile_usage_log""").fetchall()
        usage_without_fields = sum(
            1 for row in usage if not str(row[1] or "[]") or str(row[1] or "[]") == "[]"
        )
        usage_without_outcome = sum(1 for row in usage if not str(row[8] or ""))
        usage_without_revisions = sum(1 for row in usage if not _json_nonempty_list(row[2]))
        usage_without_scope = sum(1 for row in usage if not _json_scope_snapshot(row[4]))
        usage_revision_drift = 0
        historical_valid_usage_count = 0
        historical_valid_usage_marked_drift = 0
        future_revision_usage = 0
        usage_without_exact_matched_revision = 0
        usage_contains_unmatched_assertion = 0
        effect_without_target_receipt = 0
        usage_action_changed_without_counterfactual_delta = 0
        usage_action_changed_without_delta = 0
        effect_receipt_oracle_gap = 0
        before_hash_equals_after_hash_marked_changed = 0
        receipt_fields_not_emitted = 0
        prompt_changed_without_hash_delta = 0
        matched_assertion_revision_gap = 0
        rank_receipt_without_rank_delta = 0
        filtered_candidate_counted_as_effect = 0
        cross_query_profile_evidence_leak = 0
        usage_recorded_before_final_render = 0
        usage_without_read_authorization_token = 0
        usage_purpose_acl_mismatch = 0
        partial_unknown_field_acceptance = 0
        assertion_revision_mapping_ambiguity = 0
        context_search_query_ids: list[str] = []
        expired_or_conflicted_effect = 0
        correction_makes_old_assertion_effect = 0
        for (
            consumer,
            fields,
            _revisions,
            matched_revisions,
            _scope,
            _purpose,
            read_authorization_token,
            changed,
            _outcome,
            before_hash,
            after_hash,
            expected_delta,
            actual_target_delta,
            request_id,
            decision_id,
            target_receipt,
            target_receipt_hash,
            terminal_status,
            idempotency_key,
            usage_created_at,
        ) in usage:
            temporal_status = _usage_temporal_status(
                conn,
                fields_value=fields,
                revision_ids_value=_revisions,
                mapping_value=matched_revisions,
                read_authorization_token=read_authorization_token,
                usage_created_at=usage_created_at,
            )
            exact_mapping_ok = temporal_status in {
                "valid_current",
                "valid_historical",
            }
            row_contains_unmatched_assertion = not exact_mapping_ok
            if temporal_status == "valid_historical":
                historical_valid_usage_count += 1
            if not exact_mapping_ok:
                usage_revision_drift += 1
                usage_without_exact_matched_revision += 1
                usage_contains_unmatched_assertion += 1
                matched_assertion_revision_gap += 1
                partial_unknown_field_acceptance += 1
                assertion_revision_mapping_ambiguity += 1
                if temporal_status == "future_revision":
                    future_revision_usage += 1
                if changed and temporal_status == "stale_at_read":
                    correction_makes_old_assertion_effect += 1
            if temporal_status == "ineligible_at_read":
                expired_or_conflicted_effect += 1
            receipt_ok = _target_receipt_is_valid(
                consumer=consumer,
                read_purpose=_purpose,
                mapping=matched_revisions,
                before_hash=before_hash,
                after_hash=after_hash,
                expected_delta=expected_delta,
                actual_target_delta=actual_target_delta,
                request_id=request_id,
                decision_id=decision_id,
                target_receipt=target_receipt,
                target_receipt_hash=target_receipt_hash,
                terminal_status=terminal_status,
                idempotency_key=idempotency_key,
                action_changed=bool(changed),
            )
            if not receipt_ok:
                effect_without_target_receipt += 1
            oracle_ok = receipt_ok and _consumer_effect_oracle_is_valid(
                consumer=consumer,
                mapping=matched_revisions,
                target_receipt=target_receipt,
                expected_delta=expected_delta,
            )
            if not oracle_ok:
                effect_receipt_oracle_gap += 1
            read_authorization_status = _read_authorization_status(
                conn,
                consumer=consumer,
                read_purpose=_purpose,
                token_id=read_authorization_token,
                matched_revisions=matched_revisions,
                scope_snapshot=_scope,
                idempotency_key=idempotency_key,
            )
            if read_authorization_status == "missing":
                usage_without_read_authorization_token += 1
            elif read_authorization_status != "ok":
                usage_purpose_acl_mismatch += 1
            if changed and (
                not str(before_hash or "")
                or not str(after_hash or "")
                or str(before_hash) == str(after_hash)
                or str(terminal_status) != "committed"
                or not receipt_ok
            ):
                usage_action_changed_without_counterfactual_delta += 1
                usage_action_changed_without_delta += 1
            if changed and str(before_hash or "") == str(after_hash or ""):
                before_hash_equals_after_hash_marked_changed += 1
            if str(consumer) == "preflight_builder":
                expected_delta_payload = _json_object(expected_delta) or {}
                emitted_revisions = expected_delta_payload.get("emitted_assertion_revisions")
                matched_mapping = _json_string_mapping(matched_revisions) or {}
                if (
                    not isinstance(emitted_revisions, dict)
                    or dict(emitted_revisions) != matched_mapping
                ):
                    receipt_fields_not_emitted += 1
                    usage_recorded_before_final_render += 1
                if changed and (
                    not str(before_hash or "")
                    or not str(after_hash or "")
                    or str(before_hash) == str(after_hash)
                ):
                    prompt_changed_without_hash_delta += 1
            if str(consumer) == "persona_behavior_prompt":
                expected_delta_payload = _json_object(expected_delta) or {}
                rendered_revisions = expected_delta_payload.get("rendered_assertion_revisions")
                matched_mapping = _json_string_mapping(matched_revisions) or {}
                if (
                    not isinstance(rendered_revisions, dict)
                    or dict(rendered_revisions) != matched_mapping
                ):
                    usage_recorded_before_final_render += 1
            if str(consumer) == "context_search":
                expected_delta_payload = _json_object(expected_delta) or {}
                changed_candidates = expected_delta_payload.get("changed_candidates")
                eligible_candidate_ids = expected_delta_payload.get("eligible_candidate_ids")
                baseline_ranking = expected_delta_payload.get("baseline_ranking")
                enabled_ranking = expected_delta_payload.get("persona_enabled_ranking")
                query_id = str(expected_delta_payload.get("query_id") or "")
                changed_ids = {
                    str(row.get("candidate_id") or "")
                    for row in (changed_candidates if isinstance(changed_candidates, list) else [])
                    if isinstance(row, dict) and str(row.get("candidate_id") or "")
                }
                eligible_ids = {
                    str(candidate_id)
                    for candidate_id in (
                        eligible_candidate_ids if isinstance(eligible_candidate_ids, list) else []
                    )
                    if str(candidate_id)
                }
                baseline_rank_mapping = _rank_mapping(baseline_ranking)
                enabled_rank_mapping = _rank_mapping(enabled_ranking)
                baseline_ranks = baseline_rank_mapping or {}
                enabled_ranks = enabled_rank_mapping or {}
                derived_changed_ids = {
                    candidate_id
                    for candidate_id in set(baseline_ranks) | set(enabled_ranks)
                    if baseline_ranks.get(candidate_id) != enabled_ranks.get(candidate_id)
                }
                if (
                    not changed
                    or not changed_ids
                    or baseline_rank_mapping is None
                    or enabled_rank_mapping is None
                    or changed_ids != derived_changed_ids
                    or baseline_ranks == enabled_ranks
                    or str(before_hash or "") == str(after_hash or "")
                ):
                    rank_receipt_without_rank_delta += 1
                    usage_recorded_before_final_render += 1
                filtered_candidate_counted_as_effect += len(changed_ids - eligible_ids)
                delta_matches = {
                    str(assertion_id)
                    for row in (changed_candidates if isinstance(changed_candidates, list) else [])
                    if isinstance(row, dict)
                    for assertion_id in row.get("matched_assertion_ids") or ()
                    if str(assertion_id)
                }
                matched_mapping = _json_string_mapping(matched_revisions) or {}
                expected_mapping = expected_delta_payload.get("matched_assertion_revisions")
                if (
                    delta_matches != set(matched_mapping) or expected_mapping != matched_mapping
                ) and not row_contains_unmatched_assertion:
                    usage_contains_unmatched_assertion += 1
                if query_id:
                    context_search_query_ids.append(query_id)
                else:
                    cross_query_profile_evidence_leak += 1
        cross_query_profile_evidence_leak += len(context_search_query_ids) - len(
            set(context_search_query_ids)
        )
        persona_effect_without_usage_outbox = int(conn.execute("""
                SELECT COUNT(*)
                FROM profile_usage_log AS usage
                LEFT JOIN profile_usage_outbox AS outbox
                  ON outbox.idempotency_key=usage.idempotency_key
                 AND outbox.target_receipt_hash=usage.target_receipt_hash
                 AND outbox.usage_id=usage.id
                 AND outbox.status='committed'
                WHERE outbox.command_id IS NULL
                """).fetchone()[0])
        committed_effect_without_usage_receipt = int(conn.execute("""
                SELECT COUNT(*)
                FROM profile_usage_outbox AS outbox
                LEFT JOIN profile_usage_log AS usage
                  ON usage.id=outbox.usage_id
                 AND usage.idempotency_key=outbox.idempotency_key
                 AND usage.target_receipt_hash=outbox.target_receipt_hash
                WHERE outbox.status='committed' AND usage.id IS NULL
                """).fetchone()[0])
        pending_profile_usage_outbox = int(
            conn.execute(
                "SELECT COUNT(*) FROM profile_usage_outbox WHERE status='pending'"
            ).fetchone()[0]
        )
        silent_usage_write_failure = int(conn.execute("""
                SELECT COUNT(*) FROM profile_usage_outbox
                WHERE status='pending' AND attempts > 0 AND last_error=''
                """).fetchone()[0])
        payload.update(
            {
                "counts": counts,
                "active_assertion_count": len(active),
                "assertion_revision_count": revision_count,
                "projection_without_revision": projection_without_revision,
                "assertions_without_evidence": assertions_without_evidence,
                "assertions_without_scope": assertions_without_scope,
                "projection_revision_drift": projection_revision_drift,
                "usage_without_fields": usage_without_fields,
                "usage_without_outcome": usage_without_outcome,
                "usage_without_revisions": usage_without_revisions,
                "usage_without_scope": usage_without_scope,
                "usage_revision_drift": usage_revision_drift,
                "historical_valid_usage_count": historical_valid_usage_count,
                "historical_valid_usage_marked_drift": (historical_valid_usage_marked_drift),
                "future_revision_usage": future_revision_usage,
                "usage_without_exact_matched_revision": (usage_without_exact_matched_revision),
                "usage_contains_unmatched_assertion": usage_contains_unmatched_assertion,
                "effect_without_target_receipt": effect_without_target_receipt,
                "usage_action_changed_without_counterfactual_delta": (
                    usage_action_changed_without_counterfactual_delta
                ),
                "usage_action_changed_without_delta": (usage_action_changed_without_delta),
                "effect_receipt_oracle_gap": effect_receipt_oracle_gap,
                "before_hash_equals_after_hash_marked_changed": (
                    before_hash_equals_after_hash_marked_changed
                ),
                "receipt_fields_not_emitted": receipt_fields_not_emitted,
                "prompt_changed_without_hash_delta": prompt_changed_without_hash_delta,
                "matched_assertion_revision_gap": matched_assertion_revision_gap,
                "rank_receipt_without_rank_delta": rank_receipt_without_rank_delta,
                "filtered_candidate_counted_as_effect": (filtered_candidate_counted_as_effect),
                "cross_query_profile_evidence_leak": (cross_query_profile_evidence_leak),
                "usage_recorded_before_final_render": (usage_recorded_before_final_render),
                "usage_without_read_authorization_token": (usage_without_read_authorization_token),
                "usage_purpose_acl_mismatch": usage_purpose_acl_mismatch,
                "partial_unknown_field_acceptance": (partial_unknown_field_acceptance),
                "assertion_revision_mapping_ambiguity": (assertion_revision_mapping_ambiguity),
                "expired_or_conflicted_effect": expired_or_conflicted_effect,
                "correction_makes_old_assertion_effect": (correction_makes_old_assertion_effect),
                "persona_effect_without_usage_outbox": (persona_effect_without_usage_outbox),
                "committed_effect_without_usage_receipt": (committed_effect_without_usage_receipt),
                "silent_usage_write_failure": silent_usage_write_failure,
                "pending_profile_usage_outbox": pending_profile_usage_outbox,
                "usage_without_read_purpose": sum(
                    1 for row in usage if not str(row[5] or "").strip()
                ),
                "effective_consumer_count": len({str(row[0]) for row in usage}),
            }
        )
    if not payload["counts"]["profile_signals"]:
        payload["errors"].append("production_signal_denominator_zero")
    if not payload["active_assertion_count"]:
        payload["errors"].append("production_active_assertion_denominator_zero")
    if not payload["counts"]["profile_usage_log"]:
        payload["errors"].append("production_usage_denominator_zero")
    for key in (
        "assertions_without_evidence",
        "assertions_without_scope",
        "projection_without_revision",
        "projection_revision_drift",
        "usage_without_fields",
        "usage_without_outcome",
        "usage_without_revisions",
        "usage_without_scope",
        "usage_revision_drift",
        "historical_valid_usage_marked_drift",
        "future_revision_usage",
        "usage_without_read_purpose",
        "usage_without_exact_matched_revision",
        "usage_contains_unmatched_assertion",
        "effect_without_target_receipt",
        "usage_action_changed_without_counterfactual_delta",
        "usage_action_changed_without_delta",
        "effect_receipt_oracle_gap",
        "before_hash_equals_after_hash_marked_changed",
        "receipt_fields_not_emitted",
        "prompt_changed_without_hash_delta",
        "matched_assertion_revision_gap",
        "rank_receipt_without_rank_delta",
        "filtered_candidate_counted_as_effect",
        "cross_query_profile_evidence_leak",
        "usage_recorded_before_final_render",
        "usage_without_read_authorization_token",
        "usage_purpose_acl_mismatch",
        "partial_unknown_field_acceptance",
        "assertion_revision_mapping_ambiguity",
        "expired_or_conflicted_effect",
        "correction_makes_old_assertion_effect",
        "persona_effect_without_usage_outbox",
        "committed_effect_without_usage_receipt",
        "silent_usage_write_failure",
        "pending_profile_usage_outbox",
    ):
        if payload[key]:
            payload["errors"].append(f"{key}:{payload[key]}")
    payload["ok"] = not payload["errors"]
    return payload


def _json_nonempty_list(value: Any) -> bool:
    try:
        decoded = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return (
        isinstance(decoded, list)
        and bool(decoded)
        and all(isinstance(item, str) and item for item in decoded)
    )


def _rank_mapping(value: Any) -> dict[str, int] | None:
    if not isinstance(value, list) or not value:
        return None
    ranks: dict[str, int] = {}
    for row in value:
        if not isinstance(row, dict):
            return None
        candidate_id = str(row.get("candidate_id") or "")
        rank = row.get("rank")
        if (
            not candidate_id
            or isinstance(rank, bool)
            or not isinstance(rank, int)
            or rank <= 0
            or candidate_id in ranks
        ):
            return None
        ranks[candidate_id] = rank
    if set(ranks.values()) != set(range(1, len(ranks) + 1)):
        return None
    return ranks


def _read_authorization_status(
    conn: sqlite3.Connection,
    *,
    consumer: Any,
    read_purpose: Any,
    token_id: Any,
    matched_revisions: Any,
    scope_snapshot: Any,
    idempotency_key: Any,
) -> str:
    normalized_token = str(token_id or "").strip()
    if not normalized_token:
        return "missing"
    row = conn.execute(
        """
        SELECT consumer, read_purpose, scope_snapshot,
               authorized_assertion_revisions, assertion_access_hashes,
               access_control,
               status, consumed_command_id
        FROM profile_read_authorizations WHERE token_id=?
        """,
        (normalized_token,),
    ).fetchone()
    if row is None:
        return "missing"
    (
        token_consumer,
        token_purpose,
        raw_read_scope,
        raw_authorized_revisions,
        raw_assertion_access_hashes,
        raw_access,
        token_status,
        consumed_command_id,
    ) = tuple(row)
    try:
        read_scope = json.loads(str(raw_read_scope))
        authorized_revisions = json.loads(str(raw_authorized_revisions))
        assertion_access_hashes = json.loads(str(raw_assertion_access_hashes))
        usage_scope = json.loads(str(scope_snapshot))
        from core.cognitive.access_control import (
            cognitive_access_hash,
            validate_cognitive_access_envelope,
        )

        access = validate_cognitive_access_envelope(json.loads(str(raw_access)))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "corrupt"
    mapping = _json_string_mapping(matched_revisions)
    if (
        not isinstance(read_scope, dict)
        or not isinstance(usage_scope, dict)
        or not isinstance(assertion_access_hashes, dict)
        or not mapping
    ):
        return "corrupt"
    for assertion_id, revision_id in mapping.items():
        assertion_row = conn.execute(
            """
            SELECT access_control
            FROM profile_assertion_revisions
            WHERE assertion_id=? AND revision_id=?
            """,
            (assertion_id, revision_id),
        ).fetchone()
        if assertion_row is None:
            return "mismatch"
        try:
            assertion_access = validate_cognitive_access_envelope(
                json.loads(str(assertion_row[0] or ""))
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return "mismatch"
        if assertion_access_hashes.get(assertion_id) != cognitive_access_hash(assertion_access):
            return "mismatch"
    usage_read_scope = dict(usage_scope.get("read_authorization") or {})
    usage_read_scope.pop("token_id", None)
    expected_command_id = "profile-usage-command:" + str(idempotency_key or "").removeprefix(
        "profile-usage:"
    )
    if (
        str(token_consumer) != str(consumer)
        or str(token_purpose) != str(read_purpose)
        or str(read_purpose) not in set(access.get("purposes") or ())
        or not isinstance(authorized_revisions, dict)
        or any(
            authorized_revisions.get(assertion_id) != revision_id
            for assertion_id, revision_id in mapping.items()
        )
        or usage_read_scope != read_scope
        or str(token_status) != "consumed"
        or str(consumed_command_id) != expected_command_id
    ):
        return "mismatch"
    return "ok"


def _json_string_mapping(value: Any) -> dict[str, str] | None:
    try:
        decoded = json.loads(
            str(value or ""),
            object_pairs_hook=lambda pairs: pairs,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, list) or not decoded:
        return None
    keys = [key for key, _item in decoded]
    if len(keys) != len(set(keys)):
        return None
    if any(
        not isinstance(key, str) or not key or not isinstance(item, str) or not item
        for key, item in decoded
    ):
        return None
    return dict(decoded)


def _parse_timestamp(value: Any) -> datetime | None:
    normalized = str(value or "").strip().replace("Z", "+00:00")
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _usage_temporal_status(
    conn: sqlite3.Connection,
    *,
    fields_value: Any,
    revision_ids_value: Any,
    mapping_value: Any,
    read_authorization_token: Any,
    usage_created_at: Any,
) -> str:
    fields = _json_string_list(fields_value)
    revision_ids = _json_string_list(revision_ids_value)
    mapping = _json_string_mapping(mapping_value)
    if (
        not fields
        or len(fields) != len(set(fields))
        or not revision_ids
        or len(revision_ids) != len(set(revision_ids))
        or not mapping
        or sorted(fields) != sorted(mapping)
        or sorted(revision_ids) != sorted(mapping.values())
    ):
        return "mapping_mismatch"
    token_row = conn.execute(
        """
        SELECT authorized_assertion_revisions, issued_at, status
        FROM profile_read_authorizations WHERE token_id=?
        """,
        (str(read_authorization_token or ""),),
    ).fetchone()
    if token_row is None:
        return "token_missing"
    authorized_mapping = _json_string_mapping(token_row[0])
    issued_at = _parse_timestamp(token_row[1])
    usage_at = _parse_timestamp(usage_created_at)
    if (
        not authorized_mapping
        or issued_at is None
        or usage_at is None
        or str(token_row[2] or "") != "consumed"
    ):
        return "time_invalid"
    # SQLite CURRENT_TIMESTAMP has one-second precision. Treat the complete
    # stored second as the usage bucket so an authorization issued within that
    # same second is not falsely classified as future.
    if issued_at.replace(microsecond=0) > usage_at.replace(microsecond=0):
        return "time_invalid"

    current_head_matches = True
    for assertion_id, revision_id in mapping.items():
        row = conn.execute(
            """
            SELECT assertion_id, revision_number, created_at, status,
                   contradicting_signals, supporting_signals
            FROM profile_assertion_revisions
            WHERE revision_id=?
            """,
            (revision_id,),
        ).fetchone()
        if row is None or str(row[0]) != assertion_id:
            return "mapping_mismatch"
        revision_created_at = _parse_timestamp(row[2])
        if revision_created_at is None:
            return "time_invalid"
        if revision_created_at.replace(microsecond=0) > issued_at.replace(microsecond=0):
            return "future_revision"
        if authorized_mapping.get(assertion_id) != revision_id:
            return "stale_at_read"
        successor = conn.execute(
            """
            SELECT created_at
            FROM profile_assertion_revisions
            WHERE assertion_id=? AND revision_number>?
            ORDER BY revision_number ASC LIMIT 1
            """,
            (assertion_id, int(row[1])),
        ).fetchone()
        if successor is not None:
            successor_at = _parse_timestamp(successor[0])
            if successor_at is None:
                return "time_invalid"
            if successor_at.replace(microsecond=0) < issued_at.replace(microsecond=0):
                return "stale_at_read"
        if str(row[3] or "") != "active":
            return "ineligible_at_read"
        try:
            contradicting = json.loads(str(row[4] or "[]"))
            supporting = json.loads(str(row[5] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return "ineligible_at_read"
        if contradicting or not isinstance(supporting, list) or not supporting:
            return "ineligible_at_read"
        signal_ids: list[int] = []
        for reference in supporting:
            prefix, separator, raw_id = str(reference).partition(":")
            if prefix != "profile_signals" or not separator or not raw_id.isdigit():
                return "ineligible_at_read"
            signal_ids.append(int(raw_id))
        placeholders = ",".join("?" for _ in signal_ids)
        signal_rows = conn.execute(
            "SELECT id, observed_at, expires_at FROM profile_signals "
            f"WHERE id IN ({placeholders})",  # nosec B608: generated placeholders
            tuple(signal_ids),
        ).fetchall()
        if len(signal_rows) != len(set(signal_ids)):
            return "ineligible_at_read"
        for _signal_id, observed_at, expires_at in signal_rows:
            observed = _parse_timestamp(observed_at)
            expiry = _parse_timestamp(expires_at) if expires_at else None
            if observed is None or observed.replace(microsecond=0) > issued_at.replace(
                microsecond=0
            ):
                return "ineligible_at_read"
            if expiry is not None and expiry < issued_at:
                return "ineligible_at_read"
        head = conn.execute(
            "SELECT revision_id FROM profile_assertion_heads WHERE assertion_id=?",
            (assertion_id,),
        ).fetchone()
        if head is None or str(head[0]) != revision_id:
            current_head_matches = False
    return "valid_current" if current_head_matches else "valid_historical"


def _consumer_effect_oracle_is_valid(
    *,
    consumer: Any,
    mapping: Any,
    target_receipt: Any,
    expected_delta: Any,
) -> bool:
    try:
        from core.persona.profile_effect import parse_profile_target_effect_receipt

        receipt = parse_profile_target_effect_receipt(json.loads(str(target_receipt or "")))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    matched = _json_string_mapping(mapping)
    expected = _json_object(expected_delta)
    if not matched or not expected or dict(receipt.matched_assertion_revisions) != matched:
        return False
    contract = {
        "preflight_builder": (
            "prompt",
            "preflight_persona_section",
            "prompt_append",
            "emitted_assertion_revisions",
        ),
        "persona_behavior_prompt": (
            "prompt",
            "persona_behavior_prompt",
            "prompt_list",
            "rendered_assertion_revisions",
        ),
        "context_search": (
            "ranking",
            "context_search_persona_candidates",
            "rank_score_delta",
            "matched_assertion_revisions",
        ),
    }.get(str(consumer))
    if contract is None:
        return False
    target_type, target_id, delta_kind, mapping_field = contract
    if str(consumer) == "preflight_builder":
        extra_contract_ok = expected.get("section") == "user_cognitive_profile_v2"
    elif str(consumer) == "persona_behavior_prompt":
        extra_contract_ok = expected.get("section") == "behavior_prompts"
    else:
        extra_contract_ok = _context_search_effect_oracle_is_valid(
            receipt=receipt,
            expected=expected,
            matched=matched,
        )
    return bool(
        receipt.owner == str(consumer)
        and receipt.target_type == target_type
        and receipt.target_id == target_id
        and expected.get("kind") == delta_kind
        and expected.get(mapping_field) == matched
        and extra_contract_ok
    )


def _context_search_effect_oracle_is_valid(
    *,
    receipt: Any,
    expected: dict[str, Any],
    matched: dict[str, str],
) -> bool:
    baseline_ranking = expected.get("baseline_ranking")
    enabled_ranking = expected.get("persona_enabled_ranking")
    changed_candidates = expected.get("changed_candidates")
    eligible_candidate_ids = expected.get("eligible_candidate_ids")
    baseline = _rank_mapping(baseline_ranking)
    enabled = _rank_mapping(enabled_ranking)
    if (
        expected.get("target") != "context_search_persona_candidates"
        or not str(expected.get("query_id") or "")
        or baseline is None
        or enabled is None
        or baseline == enabled
        or not isinstance(changed_candidates, list)
        or not changed_candidates
        or not isinstance(eligible_candidate_ids, list)
        or not eligible_candidate_ids
        or any(
            not isinstance(candidate_id, str) or not candidate_id
            for candidate_id in eligible_candidate_ids
        )
        or len(eligible_candidate_ids) != len(set(eligible_candidate_ids))
        or receipt.baseline_hash != _sha256_json(baseline_ranking)
        or receipt.persona_enabled_hash != _sha256_json(enabled_ranking)
        or receipt.action_changed is not True
        or receipt.terminal_status != "committed"
        or expected.get("matched_assertion_revisions") != matched
    ):
        return False
    derived_changed_ids = {
        candidate_id
        for candidate_id in set(baseline) | set(enabled)
        if baseline.get(candidate_id) != enabled.get(candidate_id)
    }
    declared_changed_ids: set[str] = set()
    declared_matches: set[str] = set()
    for row in changed_candidates:
        if not isinstance(row, dict):
            return False
        candidate_id = str(row.get("candidate_id") or "")
        matched_ids = row.get("matched_assertion_ids")
        if (
            not candidate_id
            or candidate_id in declared_changed_ids
            or row.get("baseline_rank") != baseline.get(candidate_id)
            or row.get("persona_enabled_rank") != enabled.get(candidate_id)
            or not isinstance(matched_ids, list)
            or any(
                not isinstance(assertion_id, str) or not assertion_id
                for assertion_id in matched_ids
            )
            or len(matched_ids) != len(set(matched_ids))
        ):
            return False
        declared_changed_ids.add(candidate_id)
        declared_matches.update(matched_ids)
    eligible = set(eligible_candidate_ids)
    return bool(
        declared_changed_ids == derived_changed_ids
        and declared_changed_ids <= eligible
        and set(baseline) | set(enabled) <= eligible
        and declared_matches == set(matched)
    )


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _target_receipt_is_valid(
    *,
    consumer: Any,
    read_purpose: Any,
    mapping: Any,
    before_hash: Any,
    after_hash: Any,
    expected_delta: Any,
    actual_target_delta: Any,
    request_id: Any,
    decision_id: Any,
    target_receipt: Any,
    target_receipt_hash: Any,
    terminal_status: Any,
    idempotency_key: Any,
    action_changed: bool,
) -> bool:
    try:
        decoded = json.loads(str(target_receipt or ""))
        from core.persona.profile_effect import (
            parse_profile_target_effect_receipt,
            profile_usage_idempotency_key,
        )

        receipt = parse_profile_target_effect_receipt(decoded)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        receipt.owner == str(consumer)
        and dict(receipt.matched_assertion_revisions) == (_json_string_mapping(mapping) or {})
        and receipt.baseline_hash == str(before_hash or "")
        and receipt.persona_enabled_hash == str(after_hash or "")
        and dict(receipt.expected_delta) == (_json_object(expected_delta) or {})
        and dict(receipt.actual_target_delta) == (_json_object(actual_target_delta) or {})
        and receipt.request_id == str(request_id or "")
        and receipt.decision_id == str(decision_id or "")
        and receipt.receipt_hash == str(target_receipt_hash or "")
        and receipt.terminal_status == str(terminal_status or "")
        and profile_usage_idempotency_key(
            consumer=str(consumer),
            read_purpose=str(read_purpose or ""),
            receipt=receipt,
        )
        == str(idempotency_key or "")
        and receipt.action_changed is action_changed
    )


def _json_object(value: Any) -> dict[str, Any] | None:
    try:
        decoded = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return dict(decoded) if isinstance(decoded, dict) and decoded else None


def _json_scope_snapshot(value: Any) -> bool:
    try:
        decoded = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(decoded, dict)
        and isinstance(decoded.get("owner"), dict)
        and isinstance(decoded.get("scope"), dict)
        and isinstance(decoded.get("purposes"), list)
        and decoded["purposes"]
        and isinstance(decoded.get("read_purpose"), str)
        and decoded["read_purpose"]
    )


def _json_string_list(value: Any) -> list[str]:
    try:
        decoded = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(decoded, list) or not all(isinstance(item, str) and item for item in decoded):
        return []
    return decoded


def _has_live_assertion_evidence(
    conn: sqlite3.Connection,
    supporting: Any,
    confidence: Any,
) -> bool:
    if float(confidence or 0.0) <= 0.0:
        return False
    refs = _json_string_list(supporting)
    if not refs:
        return False
    ids: list[int] = []
    for ref in refs:
        prefix, separator, raw_id = ref.partition(":")
        if prefix != "profile_signals" or not separator or not raw_id.isdigit():
            return False
        ids.append(int(raw_id))
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT id, source_event_id, evidence, status, expires_at "
        f"FROM profile_signals WHERE id IN ({placeholders})",  # nosec B608
        tuple(ids),
    ).fetchall()
    if len(rows) != len(set(ids)):
        return False
    now = datetime.now().isoformat()
    return all(
        str(source_event_id or "")
        and str(evidence or "")
        and str(status or "") == "active"
        and (not expires_at or str(expires_at) >= now)
        for _id, source_event_id, evidence, status, expires_at in rows
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default="")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.db_path:
        db_path = Path(args.db_path)
    else:
        from core.config import get_config

        db_path = Path(get_config().database_dir) / "user_signals.db"
    payload = audit_persona_runtime_effectiveness(db_path)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(payload)
    return 0 if payload["ok"] or not args.strict else 1


if __name__ == "__main__":
    raise SystemExit(main())
