"""Independent, content-free verification of one active source-to-Raw generation.

This module deliberately reads the daemon's durable denominator ledger and
canonical Raw database directly.  It never imports a parser or trusts a raw
sync result object, so neither active-source Raw coverage nor a host Agent Kit
receipt can be inferred from a binary installation, a source directory, or a
self-reported row count.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, cast

from core.agent_kit.runtime_probe_contract import runtime_probe_contract
from core.agent_kit.source_support_manifest import (
    CONTINUOUS_CAPTURE_CURSOR_COUNT_FIELDS,
    CONTINUOUS_CAPTURE_CURSOR_HASH_FIELDS,
    CONTINUOUS_CAPTURE_CURSOR_KIND,
    get_agent_source_support_manifest,
)
from core.ops.readiness_query_budget import connect_readonly_sqlite
from core.sync_framework.raw_event_identity_aliases import alias_table_exists
from core.sync_framework.native_raw_contract_ledger import NativeRawContractLedger
from core.sync_framework.raw_event_reader import (
    decode_raw_revision_snapshot,
    read_admissible_raw_revisions_readonly,
)

SOURCE_CAPTURE_EVIDENCE_SCHEMA_VERSION = "mnemos.agent_source_capture_evidence.v3"
RUNTIME_BOUND_SOURCE_CAPTURE_EVIDENCE_SCHEMA_VERSION = "mnemos.agent_source_capture_evidence.v4"
_MAX_RECEIPT_HEADER_BATCH_SIZE = 500
_MAX_STRUCTURED_JSON_FIELD_BYTES = 262_144
_MAX_STRUCTURED_JSON_TOTAL_BYTES = 1_048_576


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sha256(parts: list[str]) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _row_set_hash(rows: list[tuple[object, ...]]) -> str:
    rendered = json.dumps(
        [list(row) for row in sorted(rows)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


@contextmanager
def _read_only_connection(path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect_readonly_sqlite(path)
    try:
        yield connection
    finally:
        connection.close()


def _valid_snapshot_hash(value: object) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _decoded_json(
    value: object,
    *,
    decode_budget: list[int] | None = None,
) -> object | None:
    if not isinstance(value, str):
        return value
    field_bytes = len(value.encode("utf-8", errors="surrogatepass"))
    if field_bytes > _MAX_STRUCTURED_JSON_FIELD_BYTES:
        return None
    if decode_budget is not None:
        if decode_budget[0] + field_bytes > _MAX_STRUCTURED_JSON_TOTAL_BYTES:
            return None
        decode_budget[0] += field_bytes
    try:
        return cast(object, json.loads(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _decoded_mapping(
    value: object,
    *,
    decode_budget: list[int] | None = None,
) -> Mapping[str, Any]:
    decoded = _decoded_json(value, decode_budget=decode_budget)
    return decoded if isinstance(decoded, Mapping) else {}


def _iter_mappings(
    value: object,
    *,
    decode_budget: list[int] | None = None,
) -> list[Mapping[str, Any]]:
    """Flatten structured tool evidence, including JSON-encoded payload fields."""
    budget = decode_budget if decode_budget is not None else [0]
    pending: list[tuple[object, int]] = [(value, 0)]
    result: list[Mapping[str, Any]] = []
    seen_containers: set[int] = set()
    visited_nodes = 0
    while pending and visited_nodes < 2048:
        current, depth = pending.pop()
        visited_nodes += 1
        if depth > 32:
            continue
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            result.append(current)
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            pending.extend((child, depth + 1) for child in current)
        elif isinstance(current, str):
            decoded = _decoded_json(current, decode_budget=budget)
            if isinstance(decoded, Mapping):
                pending.append((decoded, depth + 1))
            elif isinstance(decoded, list):
                pending.append((decoded, depth + 1))
    return result


def _normalized_tool_name(value: object) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    for separator in ("__", "/", "."):
        if separator in text:
            text = text.rsplit(separator, 1)[-1]
    return text


def _runtime_probe_call_ids(
    tool_calls: object,
    *,
    health_check_ids_hash: str,
) -> set[str]:
    expected_sample = runtime_probe_contract()["sample"]
    call_ids: set[str] = set()
    decode_budget = [0]
    for candidate in _iter_mappings(tool_calls, decode_budget=decode_budget):
        function = _decoded_mapping(
            candidate.get("function"),
            decode_budget=decode_budget,
        )
        name = (
            candidate.get("name")
            or candidate.get("tool")
            or candidate.get("tool_name")
            or function.get("name")
        )
        if _normalized_tool_name(name) != "agent_runtime_probe":
            continue
        arguments = (
            candidate.get("arguments")
            or candidate.get("input")
            or candidate.get("args")
            or function.get("arguments")
        )
        decoded_arguments = _decoded_mapping(
            arguments,
            decode_budget=decode_budget,
        )
        if (
            decoded_arguments.get("health_check_ids_hash") == health_check_ids_hash
            and decoded_arguments.get("sample") == expected_sample
        ):
            call_id = str(
                candidate.get("id")
                or candidate.get("tool_call_id")
                or candidate.get("call_id")
                or ""
            )
            if call_id:
                call_ids.add(call_id)
    return call_ids


def _runtime_probe_result_call_ids(
    tool_results: object,
    *,
    receipt_id: str,
    runtime_canary_hash: str,
) -> set[str]:
    result_call_ids: set[str] = set()
    decode_budget = [0]
    for candidate in _iter_mappings(tool_results, decode_budget=decode_budget):
        call_id = str(
            candidate.get("tool_call_id") or candidate.get("call_id") or candidate.get("id") or ""
        )
        if not call_id:
            continue
        if any(
            nested.get("receipt_id") == receipt_id
            and nested.get("runtime_canary_hash") == runtime_canary_hash
            and nested.get("runtime_state") == "verified"
            and nested.get("success") is True
            for nested in _iter_mappings(candidate, decode_budget=decode_budget)
        ):
            result_call_ids.add(call_id)
    return result_call_ids


def _verify_runtime_canary_in_raw(
    *,
    source_name: str,
    raw_db_path: Path,
    revision_ids: list[str],
    runtime_receipt: Mapping[str, Any],
) -> tuple[bool, list[str], str]:
    receipt_id = str(runtime_receipt.get("receipt_id") or "")
    canary_hash = str(runtime_receipt.get("runtime_canary_hash") or "")
    health_hash = str(runtime_receipt.get("health_check_ids_hash") or "")
    if (
        runtime_receipt.get("runtime_state") != "verified"
        or runtime_receipt.get("success") is not True
        or not receipt_id
        or not _valid_snapshot_hash(canary_hash)
        or not _valid_snapshot_hash(health_hash)
    ):
        return False, ["runtime_canary_receipt_invalid"], ""
    call_ids_by_session: dict[str, set[str]] = {}
    result_ids_by_session: dict[str, set[str]] = {}
    revisions_by_session: dict[str, set[str]] = {}
    try:
        for offset in range(0, len(revision_ids), _MAX_RECEIPT_HEADER_BATCH_SIZE):
            turns = read_admissible_raw_revisions_readonly(
                raw_db_path,
                revision_ids[offset : offset + _MAX_RECEIPT_HEADER_BATCH_SIZE],
            )
            for turn in turns:
                if turn.source_agent != source_name:
                    continue
                call_ids = _runtime_probe_call_ids(
                    turn.tool_calls,
                    health_check_ids_hash=health_hash,
                )
                result_ids = _runtime_probe_result_call_ids(
                    turn.tool_results,
                    receipt_id=receipt_id,
                    runtime_canary_hash=canary_hash,
                )
                if call_ids:
                    call_ids_by_session.setdefault(turn.session_id, set()).update(call_ids)
                if result_ids:
                    result_ids_by_session.setdefault(turn.session_id, set()).update(result_ids)
                if call_ids or result_ids:
                    revisions_by_session.setdefault(turn.session_id, set()).add(turn.revision_id)
    except (OSError, RuntimeError, ValueError):
        return False, ["runtime_canary_raw_unreadable"], ""
    errors: list[str] = []
    if not call_ids_by_session:
        errors.append("runtime_canary_raw_call_missing")
    if not result_ids_by_session:
        errors.append("runtime_canary_raw_result_missing")
    matched_sessions = {
        session_id
        for session_id in set(call_ids_by_session) & set(result_ids_by_session)
        if call_ids_by_session[session_id] & result_ids_by_session[session_id]
    }
    if not errors and not matched_sessions:
        errors.append("runtime_canary_raw_call_result_mismatch")
    matched_revisions = sorted(
        {
            revision_id
            for session_id in matched_sessions
            for revision_id in revisions_by_session[session_id]
        }
    )
    return not errors, errors, _sha256(matched_revisions) if matched_revisions else ""


def _receipt_header_query(*, revision_count: int, exclude_aliases: bool) -> str:
    """Build a fixed Raw-header query using bind markers only.

    The only variable SQL segment is a count of locally generated ``?`` markers;
    both optional filters originate from fixed, validated Raw schema contracts.
    """
    if not 1 <= revision_count <= _MAX_RECEIPT_HEADER_BATCH_SIZE:
        raise ValueError("receipt header batch size is outside the fixed bound")
    placeholders = ",".join("?" for _ in range(revision_count))
    alias_filter = (
        """
            AND NOT EXISTS (
                SELECT 1
                FROM raw_event_identity_aliases a
                WHERE a.alias_event_id=t.event_id
            )
        """
        if exclude_aliases
        else ""
    )
    visibility_filter = str(
        NativeRawContractLedger.current_event_visibility_predicate("t.event_id")
    )
    query_prefix = """
        SELECT r.revision_id, t.source_agent, t.session_id,
               latest_contract.observed_revision_id,
               latest_contract.support_manifest_hash,
               latest_contract.contract_state,
               r.snapshot_blob
        FROM raw_turn_revisions AS r
        JOIN raw_turns AS t ON t.event_id=r.logical_event_id
        LEFT JOIN raw_native_contract_observations AS latest_contract
          ON latest_contract.logical_event_id=t.event_id
         AND latest_contract.rowid=(
             SELECT prior_contract.rowid
             FROM raw_native_contract_observations AS prior_contract
             WHERE prior_contract.logical_event_id=t.event_id
             ORDER BY prior_contract.rowid DESC
             LIMIT 1
         )
        WHERE r.revision_id IN ("""
    # Only bounded placeholders and fixed, owner-rendered predicates are joined.
    return (  # nosec B608
        query_prefix
        + placeholders
        + ")"
        + alias_filter
        + visibility_filter
    )


def verify_source_capture(
    *,
    source_name: str,
    coverage: Mapping[str, Any],
    cursor_db_path: Path,
    raw_db_path: Path,
    runtime_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reconcile a frozen native denominator to canonical Raw without bodies.

    The returned object holds counters and hashes only.  A host caller may
    persist it in an Agent Kit receipt; ingestion-only callers use it solely as
    Raw-coverage evidence.  Any unavailable/old/mismatched state remains a
    typed failure rather than a guessed success.
    """
    manifest = get_agent_source_support_manifest()
    spec = manifest.require_active_source(source_name)
    errors: list[str] = []
    sources = _mapping(coverage.get("sources"))
    entry = _mapping(sources.get(spec.name))
    cursor = _mapping(entry.get("cursor"))
    denominator_turns = cursor.get("denominator_turns")
    denominator_sessions = cursor.get("denominator_observed_sessions")
    capture_generation_id_claim = cursor.get("capture_generation_id")
    capture_roster_hash_claim = cursor.get("capture_roster_hash")
    capture_generation_eligible_claim = cursor.get("capture_generation_eligible")
    capture_claim_counts = {key: cursor.get(key) for key in CONTINUOUS_CAPTURE_CURSOR_COUNT_FIELDS}
    capture_claim_hashes = {key: cursor.get(key) for key in CONTINUOUS_CAPTURE_CURSOR_HASH_FIELDS}
    capture_claims_valid = (
        isinstance(capture_generation_id_claim, str)
        and bool(capture_generation_id_claim)
        and _valid_snapshot_hash(capture_roster_hash_claim)
        and isinstance(capture_generation_eligible_claim, bool)
        and all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in capture_claim_counts.values()
        )
        and all(_valid_snapshot_hash(value) for value in capture_claim_hashes.values())
    )
    discovery_covered = (
        coverage.get("support_manifest_hash") == manifest.manifest_hash
        and entry.get("owner") == "daemon.raw_sync"
        and entry.get("owner_service") == "raw_sync"
        and cursor.get("kind") == CONTINUOUS_CAPTURE_CURSOR_KIND
        and cursor.get("denominator_complete") is True
        and isinstance(denominator_sessions, int)
        and not isinstance(denominator_sessions, bool)
        and denominator_sessions >= 0
        and cursor.get("discovered_sessions") == denominator_sessions
        and isinstance(denominator_turns, int)
        and not isinstance(denominator_turns, bool)
        and denominator_turns >= 0
        and (denominator_sessions > 0 or denominator_turns == 0)
        and int(entry.get("native_turns") or 0) == denominator_turns
        and not str(entry.get("error") or "")
        and _valid_snapshot_hash(entry.get("native_source_snapshot_hash"))
        and capture_claims_valid
    )
    if not discovery_covered:
        errors.append("source_coverage_not_complete")

    expected_by_session: dict[str, dict[int, str]] = {}
    receipt_rows: list[tuple[object, ...]] = []
    denominator_state_rows: list[tuple[object, ...]] = []
    expected_state_rows: list[tuple[object, ...]] = []
    receipt_state_rows: list[tuple[object, ...]] = []
    exact_receipt_count = 0
    orphan_receipt_count = 0
    roster_hash = ""
    generation_id = ""
    generation_eligible = False
    try:
        with _read_only_connection(cursor_db_path) as cursor_conn:
            cursor_conn.execute("BEGIN")
            state = cursor_conn.execute(
                """
                SELECT roster_hash, session_count, observed_session_count,
                       observed_turn_count, complete
                FROM source_denominator_state WHERE source_name=?
                """,
                (spec.name,),
            ).fetchone()
            if state is None:
                errors.append("cursor_denominator_missing")
            else:
                roster_hash = str(state[0] or "")
                if (
                    bool(state[4]) is not True
                    or int(state[1])
                    != (denominator_sessions if isinstance(denominator_sessions, int) else -1)
                    or int(state[2])
                    != (denominator_sessions if isinstance(denominator_sessions, int) else -1)
                    or int(state[3])
                    != (denominator_turns if isinstance(denominator_turns, int) else -1)
                ):
                    errors.append("cursor_denominator_mismatch")
                rows = cursor_conn.execute(
                    """
                    SELECT canonical_session_id, turn_count, disposition,
                           disposition_reason, artifact_evidence_hash
                    FROM source_denominator_sessions
                    WHERE source_name=? AND roster_hash=?
                    ORDER BY canonical_session_id
                    """,
                    (spec.name, roster_hash),
                ).fetchall()
                denominator_counts: dict[str, int] = {}
                disposition_rows: list[str] = []
                typed_empty_sessions = 0
                evidence_excluded_sessions = 0
                for (
                    session_id,
                    turn_count,
                    disposition,
                    disposition_reason,
                    artifact_evidence_hash,
                ) in rows:
                    session_key = str(session_id)
                    count = int(turn_count)
                    disposition_text = str(disposition or "")
                    reason_text = str(disposition_reason or "")
                    evidence_hash = str(artifact_evidence_hash or "")
                    valid_evidence_hash = evidence_hash.startswith(
                        "sha256:"
                    ) and _valid_snapshot_hash(evidence_hash.removeprefix("sha256:"))
                    if disposition_text == "parsed":
                        valid = count > 0 and bool(reason_text) and valid_evidence_hash
                    elif disposition_text in {
                        "typed_empty",
                        "evidence_excluded",
                    }:
                        valid = count == 0 and bool(reason_text) and valid_evidence_hash
                    else:
                        valid = False
                    if not valid:
                        errors.append("cursor_session_disposition_invalid")
                    if disposition_text == "typed_empty":
                        typed_empty_sessions += 1
                    elif disposition_text == "evidence_excluded":
                        evidence_excluded_sessions += 1
                    denominator_counts[session_key] = count
                    denominator_state_rows.append(
                        (
                            session_key,
                            count,
                            disposition_text,
                            reason_text,
                            evidence_hash,
                        )
                    )
                    disposition_rows.append(
                        "\0".join(
                            (
                                session_key,
                                str(count),
                                disposition_text,
                                reason_text,
                                evidence_hash,
                            )
                        )
                    )
                expected_by_session = {session_id: {} for session_id in denominator_counts}
                if len(denominator_counts) != (
                    denominator_sessions if isinstance(denominator_sessions, int) else -1
                ):
                    errors.append("cursor_session_roster_mismatch")
                generation = cursor_conn.execute(
                    """
                    SELECT generation_id, roster_hash, native_source_snapshot_hash,
                           snapshot_binding_eligible
                    FROM source_capture_generations WHERE source_name=?
                    """,
                    (spec.name,),
                ).fetchone()
                if generation is None or str(generation[1] or "") != roster_hash:
                    errors.append("cursor_capture_generation_mismatch")
                else:
                    generation_id = str(generation[0] or "")
                    generation_eligible = bool(generation[3])
                    if generation[3] != 1:
                        errors.append("cursor_snapshot_binding_ineligible")
                    if str(generation[2] or "") != str(
                        entry.get("native_source_snapshot_hash") or ""
                    ):
                        errors.append("cursor_snapshot_binding_mismatch")
                    expected_rows = cursor_conn.execute(
                        """
                        SELECT canonical_session_id, turn_number, turn_fingerprint
                        FROM source_capture_expected_turns
                        WHERE source_name=? AND generation_id=?
                        ORDER BY canonical_session_id, turn_number
                        """,
                        (spec.name, generation_id),
                    ).fetchall()
                    for session_id, turn_number, turn_fingerprint in expected_rows:
                        normalized_session = str(session_id)
                        normalized_turn = int(turn_number)
                        fingerprint = str(turn_fingerprint or "")
                        if not _valid_snapshot_hash(fingerprint):
                            errors.append("cursor_capture_turn_fingerprint_invalid")
                        expected_state_rows.append(
                            (
                                normalized_session,
                                normalized_turn,
                                fingerprint,
                            )
                        )
                        expected_by_session.setdefault(normalized_session, {})[
                            normalized_turn
                        ] = fingerprint
                    if set(expected_by_session) != set(denominator_counts):
                        errors.append("cursor_capture_expected_roster_mismatch")
                    for session_id, expected_count in denominator_counts.items():
                        if len(expected_by_session.get(session_id, {})) != expected_count:
                            errors.append("cursor_capture_expected_turns_mismatch")
                    receipt_rows = cursor_conn.execute(
                        """
                        SELECT canonical_session_id, turn_number, raw_revision_id,
                               turn_fingerprint
                        FROM source_capture_raw_receipts
                        WHERE source_name=? AND generation_id=?
                        ORDER BY canonical_session_id, turn_number
                        """,
                        (spec.name, generation_id),
                    ).fetchall()
                    receipt_state_rows = []
                    for (
                        session_id,
                        turn_number,
                        raw_revision_id,
                        turn_fingerprint,
                    ) in receipt_rows:
                        if not isinstance(turn_number, int) or isinstance(
                            turn_number,
                            bool,
                        ):
                            errors.append("cursor_capture_receipt_turn_invalid")
                            continue
                        receipt_state_rows.append(
                            (
                                str(session_id),
                                turn_number,
                                str(raw_revision_id or ""),
                                str(turn_fingerprint or ""),
                            )
                        )
                    receipt_state = {
                        (session_id, turn_number): (
                            revision_id,
                            fingerprint,
                        )
                        for (
                            session_id,
                            turn_number,
                            revision_id,
                            fingerprint,
                        ) in receipt_state_rows
                    }
                    expected_state = {
                        (session_id, turn_number): fingerprint
                        for session_id, turn_number, fingerprint in expected_state_rows
                    }
                    exact_receipt_count = sum(
                        1
                        for key, fingerprint in expected_state.items()
                        if key in receipt_state and receipt_state[key][1] == fingerprint
                    )
                    orphan_receipt_count = len(set(receipt_state) - set(expected_state))
                    capture_state_matches = (
                        capture_claims_valid
                        and generation_id == capture_generation_id_claim
                        and roster_hash == capture_roster_hash_claim
                        and bool(generation[3]) is capture_generation_eligible_claim
                        and capture_claim_counts
                        == {
                            "capture_expected_turn_count": len(expected_state_rows),
                            "capture_receipt_count": len(receipt_state_rows),
                            "capture_exact_receipt_count": exact_receipt_count,
                            "capture_pending_turn_count": (
                                len(expected_state_rows) - exact_receipt_count
                            ),
                            "capture_orphan_receipt_count": (orphan_receipt_count),
                        }
                        and capture_claim_hashes
                        == {
                            "capture_roster_hash": roster_hash,
                            "capture_denominator_session_set_hash": (
                                _row_set_hash(denominator_state_rows)
                            ),
                            "capture_expected_turn_fingerprint_set_hash": (
                                _row_set_hash(expected_state_rows)
                            ),
                            "capture_receipt_binding_set_hash": (_row_set_hash(receipt_state_rows)),
                        }
                    )
                    if not capture_state_matches:
                        errors.append("cursor_capture_state_mismatch")
    except (OSError, sqlite3.Error, ValueError):
        errors.append("cursor_denominator_unreadable")

    raw_revision_ids: list[str] = []
    raw_committed_turns = 0
    try:
        with _read_only_connection(raw_db_path) as raw_conn:
            exclude_aliases = alias_table_exists(raw_conn)
            receipts_by_session: dict[str, dict[int, str]] = {}
            receipt_fingerprints_by_session: dict[str, dict[int, str]] = {}
            for (
                session_id,
                turn_number,
                revision_id,
                turn_fingerprint,
            ) in receipt_rows:
                normalized_session = str(session_id)
                if not isinstance(turn_number, int) or isinstance(
                    turn_number,
                    bool,
                ):
                    errors.append("raw_capture_receipt_turn_invalid")
                    continue
                normalized_turn = turn_number
                receipts_by_session.setdefault(normalized_session, {})[normalized_turn] = str(
                    revision_id or ""
                )
                receipt_fingerprints_by_session.setdefault(
                    normalized_session,
                    {},
                )[
                    normalized_turn
                ] = str(turn_fingerprint or "")
            if set(receipts_by_session) - set(expected_by_session):
                errors.append("raw_capture_receipt_set_mismatch")
            receipt_bindings: list[tuple[str, int, str]] = []
            for session_id, expected_turns in expected_by_session.items():
                receipts = receipts_by_session.get(session_id, {})
                receipt_fingerprints = receipt_fingerprints_by_session.get(
                    session_id,
                    {},
                )
                if (
                    set(receipts) != set(expected_turns)
                    or set(receipt_fingerprints) != set(expected_turns)
                    or any(not value for value in receipts.values())
                    or any(
                        receipt_fingerprints.get(turn_number) != expected_turns[turn_number]
                        for turn_number in expected_turns
                    )
                ):
                    errors.append("raw_capture_receipt_set_mismatch")
                    continue
                receipt_bindings.extend(
                    (session_id, turn_number, receipts[turn_number])
                    for turn_number in sorted(expected_turns)
                )
            if len({revision_id for _session, _turn, revision_id in receipt_bindings}) != len(
                receipt_bindings
            ):
                errors.append("raw_capture_receipt_duplicate_revision")
            verified_headers: dict[
                str,
                tuple[str, str, str, str, str, bytes | None],
            ] = {}
            revision_ids = [revision_id for _session, _turn, revision_id in receipt_bindings]
            for offset in range(0, len(revision_ids), 500):
                batch = revision_ids[offset : offset + 500]
                if not batch:
                    continue
                rows = raw_conn.execute(
                    _receipt_header_query(
                        revision_count=len(batch),
                        exclude_aliases=exclude_aliases,
                    ),
                    batch,
                ).fetchall()
                for (
                    revision_id,
                    source_agent,
                    session_id,
                    observed_revision_id,
                    contract_manifest_hash,
                    contract_state,
                    snapshot_blob,
                ) in rows:
                    verified_headers[str(revision_id)] = (
                        str(source_agent or ""),
                        str(session_id or ""),
                        str(observed_revision_id or ""),
                        str(contract_manifest_hash or ""),
                        str(contract_state or ""),
                        snapshot_blob,
                    )
            for session_id, turn_number, revision_id in receipt_bindings:
                header = verified_headers.get(revision_id)
                if header is None:
                    errors.append("raw_capture_receipt_unverified")
                    continue
                (
                    source_agent,
                    raw_session_id,
                    observed_revision_id,
                    contract_manifest_hash,
                    contract_state,
                    snapshot_blob,
                ) = header
                try:
                    raw_metadata = _mapping(
                        decode_raw_revision_snapshot(snapshot_blob).get("metadata")
                    )
                except ValueError:
                    errors.append("raw_capture_receipt_binding_mismatch")
                    continue
                expected_fingerprint = expected_by_session.get(
                    session_id,
                    {},
                ).get(turn_number, "")
                # Native event identity is canonical.  ``raw_turns.turn_number``
                # is a historical projection that can legitimately differ after
                # parser ordinal repair or native-id alias reconciliation; the
                # generation ledger already proves the current ordinal domain.
                if (
                    source_agent != spec.name
                    or raw_session_id != session_id
                    or observed_revision_id != revision_id
                    or contract_manifest_hash != manifest.manifest_hash
                    or contract_state != "conformant"
                    or not _valid_snapshot_hash(expected_fingerprint)
                    or raw_metadata.get("native_turn_fingerprint") != expected_fingerprint
                ):
                    errors.append("raw_capture_receipt_binding_mismatch")
                    continue
                raw_committed_turns += 1
                raw_revision_ids.append(f"{session_id}\0{turn_number}\0{revision_id}")
    except (OSError, sqlite3.Error, ValueError):
        errors.append("canonical_raw_unreadable")

    content_parsed = discovery_covered and not any(error.startswith("cursor_") for error in errors)
    raw_committed = (
        content_parsed
        and raw_committed_turns == int(denominator_turns or 0)
        and not any(error.startswith("raw_capture_receipt_") for error in errors)
        and "canonical_raw_unreadable" not in errors
    )
    runtime_canary_verified = False
    runtime_canary_raw_revision_ids_hash = ""
    if runtime_receipt is not None:
        (
            runtime_canary_verified,
            runtime_canary_errors,
            runtime_canary_raw_revision_ids_hash,
        ) = _verify_runtime_canary_in_raw(
            source_name=spec.name,
            raw_db_path=raw_db_path,
            revision_ids=[binding.rsplit("\0", 1)[-1] for binding in raw_revision_ids],
            runtime_receipt=runtime_receipt,
        )
        errors.extend(runtime_canary_errors)
    completeness = {
        "schema_version": (
            RUNTIME_BOUND_SOURCE_CAPTURE_EVIDENCE_SCHEMA_VERSION
            if runtime_receipt is not None
            else SOURCE_CAPTURE_EVIDENCE_SCHEMA_VERSION
        ),
        "discovery_covered": discovery_covered,
        "content_parsed": content_parsed,
        "raw_committed": raw_committed,
        "discovered_sessions": int(denominator_sessions or 0),
        "native_turns": int(denominator_turns or 0),
        "parsed_turns": int(denominator_turns or 0) if content_parsed else 0,
        "raw_committed_turns": raw_committed_turns,
        "typed_empty_sessions": locals().get(
            "typed_empty_sessions",
            0,
        ),
        "evidence_excluded_sessions": locals().get(
            "evidence_excluded_sessions",
            0,
        ),
        "session_disposition_hash": _sha256(sorted(locals().get("disposition_rows", []))),
        "raw_revision_ids_hash": _sha256(sorted(raw_revision_ids)),
        "cursor_roster_hash": roster_hash,
        "capture_generation_id": generation_id,
        "capture_generation_eligible": generation_eligible,
        "capture_expected_turn_count": len(expected_state_rows),
        "capture_receipt_count": len(receipt_state_rows),
        "capture_exact_receipt_count": exact_receipt_count,
        "capture_pending_turn_count": (len(expected_state_rows) - exact_receipt_count),
        "capture_orphan_receipt_count": orphan_receipt_count,
        "capture_denominator_session_set_hash": _row_set_hash(denominator_state_rows),
        "capture_expected_turn_fingerprint_set_hash": _row_set_hash(expected_state_rows),
        "capture_receipt_binding_set_hash": _row_set_hash(receipt_state_rows),
    }
    if runtime_receipt is not None:
        completeness.update(
            {
                "runtime_canary_verified": runtime_canary_verified,
                "runtime_canary_hash": str(runtime_receipt.get("runtime_canary_hash") or ""),
                "runtime_receipt_id_hash": hashlib.sha256(
                    str(runtime_receipt.get("receipt_id") or "").encode("utf-8")
                ).hexdigest(),
                "runtime_canary_raw_revision_ids_hash": (runtime_canary_raw_revision_ids_hash),
            }
        )
    return {
        "schema_version": completeness["schema_version"],
        "source_name": spec.name,
        "support_manifest_hash": manifest.manifest_hash,
        "native_source_snapshot_hash": str(entry.get("native_source_snapshot_hash") or ""),
        "capture_completeness": completeness,
        "ok": raw_committed and (runtime_receipt is None or runtime_canary_verified),
        "errors": list(dict.fromkeys(errors)),
    }
