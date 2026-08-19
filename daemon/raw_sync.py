# -*- coding: utf-8 -*-
"""Continuous, denominator-complete Raw synchronization for AgentSource data."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping

from core.agent_kit.source_support_manifest import (
    AgentSourceSupportManifestError,
    build_source_support_runtime_report,
)
from core.sync_framework.agent_source import (
    SessionParseResult,
    canonicalize_session_info,
    parse_discovered_session,
    parse_discovered_session_result,
)
from core.sync_framework.source_support import build_native_raw_metadata
from daemon.agent_source_coverage import (
    initialize_source_coverage,
    mark_source_not_detected,
    record_source_observation,
)
from daemon.agent_sync_cursor import (
    AgentSyncCursorError,
    AgentSyncCursorStore,
    SourceCaptureFingerprintState,
)

RAW_SYNC_RECOVERABLE_ERRORS = (
    OSError,
    ValueError,
    TypeError,
    KeyError,
    ImportError,
    AttributeError,
    RuntimeError,
)


def continuous_sync_limits(config: Any | None = None) -> Dict[str, int]:
    """Return throughput budgets, never a definition of synchronization completeness."""
    defaults = {
        "tail_sessions_per_source": 10,
        "reconciliation_sessions_per_source": 10,
        "turns_per_session": 100,
    }
    getter = getattr(config, "get", None)
    if not callable(getter):
        return defaults
    return {
        "tail_sessions_per_source": getter(
            "sync.raw_sync_sessions_per_source",
            defaults["tail_sessions_per_source"],
        ),
        "reconciliation_sessions_per_source": getter(
            "sync.raw_sync_sessions_per_source",
            defaults["reconciliation_sessions_per_source"],
        ),
        "turns_per_session": getter(
            "sync.raw_sync_turns_per_session",
            defaults["turns_per_session"],
        ),
    }


def _positive_limit(limits: Mapping[str, Any], name: str) -> int:
    value = limits.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"continuous sync limit {name} must be a positive integer")
    return value


def _session_mtime(session_info: Any) -> float:
    value = getattr(session_info, "mtime", None)
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _canonical_sessions(sessions: Iterable[Any]) -> dict[str, Any]:
    """Resolve discovery aliases through the shared pure canonical resolver."""
    canonical_sessions: dict[str, Any] = {}
    for session_info in sessions:
        canonical = canonicalize_session_info(session_info)
        canonical_id = str(getattr(canonical, "session_id", "") or "")
        if not canonical_id:
            raise AgentSyncCursorError("canonical session id is required")
        existing = canonical_sessions.get(canonical_id)
        if existing is not None:
            raise AgentSyncCursorError("canonical_session_duplicate")
        canonical_sessions[canonical_id] = canonical
    return canonical_sessions


def _sorted_turns(source: Any, session_info: Any) -> list[Any]:
    turns = list(parse_discovered_session(source, session_info) or [])
    return sorted(turns, key=lambda turn: int(turn.turn_number))


def _reconciliation_turn_batch(
    turns: list[Any],
    *,
    pending_turn_numbers: Iterable[int],
    limit: int,
) -> list[Any]:
    pending_numbers = {int(number) for number in pending_turn_numbers}
    pending = [turn for turn in turns if int(turn.turn_number) in pending_numbers]
    return pending[:limit]


def _tail_turn_batch(turns: list[Any], *, limit: int) -> list[Any]:
    return turns[-limit:]


def _raw_event_id(result: Any) -> str:
    value = getattr(result, "raw_event_id", None)
    return value if isinstance(value, str) else ""


def _native_turn_fingerprint(
    source: Any,
    session_info: Any,
    turn: Any,
) -> str:
    """Bind current native identity and every Raw-relevant structured field."""
    metadata = build_native_raw_metadata(source, session_info, turn)
    metadata.pop("native_turn_fingerprint", None)
    payload = {
        "source_name": str(source.name),
        "canonical_session_id": str(session_info.session_id),
        "turn_number": int(turn.turn_number),
        "native_event_id": str(getattr(turn, "native_event_id", "") or ""),
        "user_content": str(getattr(turn, "user_content", "") or ""),
        "assistant_content": str(getattr(turn, "assistant_content", "") or ""),
        "timestamp": str(getattr(turn, "timestamp", "") or ""),
        "model_tag": str(getattr(source, "model_tag", "") or ""),
        "reasoning": str(getattr(turn, "reasoning", "") or ""),
        "tool_calls": list(getattr(turn, "tool_calls", None) or []),
        "tool_results": list(getattr(turn, "tool_results", None) or []),
        "attachments": list(getattr(turn, "attachments", None) or []),
        "raw_event_refs": list(getattr(turn, "raw_event_refs", None) or []),
        "source_files": [str(path) for path in (getattr(turn, "source_files", None) or [])],
        "source_path": str(getattr(session_info, "source_path", "") or ""),
        "completeness": dict(getattr(turn, "completeness", None) or {}),
        "metadata": metadata,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _advance_raw_high_water(
    cursor_store: AgentSyncCursorStore,
    *,
    source_name: str,
    canonical_session_id: str,
    all_turns: list[Any],
    results: list[Any],
    pending_turn_numbers: Iterable[int],
) -> tuple[int | None, bool, int]:
    """Project exact receipt state into the compatibility high-water."""
    cursor = cursor_store.get_session_raw_cursor(source_name, canonical_session_id)
    previous = cursor.next_turn_number
    raw_committed = sum(1 for result in results if _raw_event_id(result))
    pending = sorted({int(number) for number in pending_turn_numbers})
    proposed: int | None
    if pending:
        proposed = pending[0]
    elif all_turns:
        proposed = max(int(turn.turn_number) for turn in all_turns) + 1
    else:
        proposed = previous
    if (
        proposed is None
        or proposed == previous
        or (previous is None and proposed == 0)
        or (previous is not None and proposed < previous)
    ):
        return previous, False, raw_committed
    persisted = cursor_store.advance_session_raw_cursor(
        source_name,
        canonical_session_id,
        next_turn_number=proposed,
    )
    return persisted.next_turn_number, True, raw_committed


def _resolved_source_roots(source: Any) -> list[Path]:
    """Return observed roots for a runtime snapshot, not a source definition."""
    roots: list[Path] = []
    try:
        observed_roots = getattr(source, "observed_roots", None)
        if callable(observed_roots):
            roots.extend(Path(root) for root in observed_roots())
        data_dir = source.data_dir
    except AgentSourceSupportManifestError:
        raise
    except RAW_SYNC_RECOVERABLE_ERRORS:
        data_dir = None
    if not roots and isinstance(data_dir, (str, Path)):
        roots.append(Path(data_dir))
    if not roots:
        try:
            from core.sync_framework.registry import PathDiscover

            discovered = PathDiscover.find(source.name)
        except AgentSourceSupportManifestError:
            raise
        except RAW_SYNC_RECOVERABLE_ERRORS:
            discovered = None
        if discovered is not None:
            roots.append(Path(discovered))
    return list(dict.fromkeys(roots))


def _block_requires_raw_receipts(selected_turns: list[Any], results: list[Any]) -> None:
    receipts = {int(result.turn_number): _raw_event_id(result) for result in results}
    missing = [
        int(turn.turn_number) for turn in selected_turns if not receipts.get(int(turn.turn_number))
    ]
    if missing:
        raise AgentSyncCursorError(
            "canonical Raw commit missing for selected turns: "
            + ",".join(str(number) for number in missing[:5])
        )


def sync_source_continuously(
    source: Any,
    engine: Any,
    cursor_store: AgentSyncCursorStore,
    limits: Mapping[str, Any],
    *,
    include_reconciliation: bool = True,
) -> dict[str, Any]:
    """Run bounded tail work plus durable round-robin reconciliation for one source.

    Batches are deliberately bounded for latency.  The per-session Raw cursor
    and source denominator cursor make every discovered session/turn eligible
    again until its canonical Raw receipt has advanced the high-water mark.
    """
    tail_limit = _positive_limit(limits, "tail_sessions_per_source")
    reconcile_limit = _positive_limit(limits, "reconciliation_sessions_per_source")
    turn_limit = _positive_limit(limits, "turns_per_session")

    discovered = list(source.discover_sessions() or [])
    sessions_by_id = _canonical_sessions(discovered)
    denominator = cursor_store.begin_source_denominator(source.name, sessions_by_id)
    ordered_by_recency = sorted(
        sessions_by_id.values(),
        key=lambda item: (-_session_mtime(item), str(item.session_id)),
    )
    tail_ids = [str(item.session_id) for item in ordered_by_recency[:tail_limit]]
    reconciliation_ids = (
        cursor_store.select_reconciliation_session_ids(
            source.name,
            sessions_by_id,
            limit=reconcile_limit,
        )
        if include_reconciliation
        else []
    )

    parsed_turns: dict[str, list[Any]] = {}
    parsed_turn_fingerprints: dict[str, dict[int, str]] = {}
    parsed_results: dict[str, SessionParseResult] = {}
    selected_turn_numbers: dict[str, set[int]] = {}
    raw_completed_sessions: set[str] = set()
    handoff_sessions: set[str] = set()
    errors: list[Exception] = []
    raw_committed_turns = 0
    advanced_sessions = 0

    def turns_for(session_id: str) -> list[Any]:
        nonlocal denominator
        if session_id not in parsed_turns:
            parse_result = parse_discovered_session_result(
                source,
                sessions_by_id[session_id],
            )
            parsed_results[session_id] = parse_result
            parsed_turns[session_id] = sorted(
                [copy.deepcopy(turn) for turn in parse_result.turns],
                key=lambda turn: int(turn.turn_number),
            )
            fingerprints: dict[int, str] = {}
            for turn in parsed_turns[session_id]:
                turn_number = int(turn.turn_number)
                if turn_number in fingerprints:
                    raise AgentSyncCursorError("canonical_session_duplicate_turn_number")
                fingerprint = _native_turn_fingerprint(
                    source,
                    sessions_by_id[session_id],
                    turn,
                )
                fingerprints[turn_number] = fingerprint
                metadata = dict(getattr(turn, "metadata", None) or {})
                metadata["native_turn_fingerprint"] = fingerprint
                turn.metadata = metadata
            parsed_turn_fingerprints[session_id] = fingerprints
            denominator = cursor_store.record_denominator_session(
                source.name,
                session_id,
                turn_count=len(parsed_turns[session_id]),
                turn_numbers=[int(turn.turn_number) for turn in parsed_turns[session_id]],
                turn_fingerprints=fingerprints,
                disposition=parse_result.disposition,
                disposition_reason=parse_result.reason_code,
                artifact_evidence_hash=parse_result.artifact_evidence_hash,
            )
        return parsed_turns[session_id]

    def sync_block(session_id: str, selected: list[Any]) -> None:
        nonlocal raw_committed_turns, advanced_sessions
        if not selected:
            return
        session_info = sessions_by_id[session_id]
        all_turns = turns_for(session_id)
        results = engine.sync_turns(
            source,
            session_info,
            selected,
            incremental=False,
            enqueue_distillation=False,
        )
        if len(results) != len(selected):
            raise AgentSyncCursorError("SyncEngine returned incomplete selected-turn results")
        cursor_store.record_raw_capture_receipts(
            source.name,
            session_id,
            [
                (
                    int(result.turn_number),
                    raw_event_id,
                    parsed_turn_fingerprints[session_id][int(result.turn_number)],
                )
                for result in results
                if (raw_event_id := _raw_event_id(result))
            ],
        )
        remaining_turn_numbers = cursor_store.pending_session_turn_numbers(
            source.name,
            session_id,
        )
        _next_turn_number, advanced, committed = _advance_raw_high_water(
            cursor_store,
            source_name=source.name,
            canonical_session_id=session_id,
            all_turns=all_turns,
            results=results,
            pending_turn_numbers=remaining_turn_numbers,
        )
        raw_committed_turns += committed
        selected_turn_numbers.setdefault(session_id, set()).update(
            int(turn.turn_number) for turn in selected
        )
        raw_completed_sessions.add(session_id)
        if advanced:
            advanced_sessions += 1
        # A successful prefix is already durable.  Report any later missing
        # receipt without rolling that cursor back; replay resumes at the first
        # unconfirmed turn and Raw upsert remains idempotent.
        _block_requires_raw_receipts(selected, results)
        if session_id not in handoff_sessions and not remaining_turn_numbers:
            enqueue = getattr(engine, "enqueue_session_for_distillation", None)
            if callable(enqueue):
                enqueue(source, session_info, all_turns)
            handoff_sessions.add(session_id)

    # Reconciliation progresses the durable prefix for every source session.
    for session_id in reconciliation_ids:
        try:
            turns = turns_for(session_id)
            sync_block(
                session_id,
                _reconciliation_turn_batch(
                    turns,
                    pending_turn_numbers=cursor_store.pending_session_turn_numbers(
                        source.name,
                        session_id,
                    ),
                    limit=turn_limit,
                ),
            )
        except RAW_SYNC_RECOVERABLE_ERRORS as exc:
            errors.append(exc)
        finally:
            # Parsed turns can carry complete transcript bodies.  Retaining
            # one list for every reconciliation member turns a bounded cursor
            # batch into an unbounded memory accumulator on large histories.
            # The denominator/cursor facts are already durable; a later tail
            # block deliberately reparses only its own bounded session.
            parsed_turns.pop(session_id, None)
            parsed_results.pop(session_id, None)
            parsed_turn_fingerprints.pop(session_id, None)

    if reconciliation_ids:
        # This is a scheduling position, not a Raw completeness claim.
        cursor_store.advance_reconciliation_after(source.name, reconciliation_ids[-1])

    # Tail work keeps recent writes prompt.  It never replaces reconciliation.
    for session_id in tail_ids:
        try:
            turns = turns_for(session_id)
            pending_turn_numbers = set(
                cursor_store.pending_session_turn_numbers(
                    source.name,
                    session_id,
                )
            )
            selected = [
                turn
                for turn in _tail_turn_batch(turns, limit=turn_limit)
                if int(turn.turn_number) in pending_turn_numbers
            ]
            already_selected = selected_turn_numbers.get(session_id, set())
            if selected and all(int(turn.turn_number) in already_selected for turn in selected):
                continue
            sync_block(session_id, selected)
        except RAW_SYNC_RECOVERABLE_ERRORS as exc:
            errors.append(exc)
        finally:
            parsed_turns.pop(session_id, None)
            parsed_results.pop(session_id, None)
            parsed_turn_fingerprints.pop(session_id, None)

    capture_state = cursor_store.source_capture_fingerprint_state(source.name)
    coverage_cursor = {
        "kind": "continuous_tail_reconcile_v1",
        "tail_sessions_per_source": tail_limit,
        "reconciliation_sessions_per_source": reconcile_limit,
        "turns_per_session": turn_limit,
        "discovered_sessions": denominator.session_count,
        "reconciliation_selected_sessions": len(reconciliation_ids),
        "tail_selected_sessions": len(tail_ids),
        "raw_committed_turns": raw_committed_turns,
        "advanced_sessions": advanced_sessions,
        "denominator_complete": denominator.complete,
        "denominator_observed_sessions": denominator.observed_session_count,
        "denominator_turns": denominator.observed_turn_count,
        "denominator_completed_at": denominator.completed_at,
        **capture_state.to_cursor_fields(),
    }
    return {
        "native_sessions": denominator.session_count,
        # Coverage reports how many native turns have been observed in the
        # current roster.  The snapshot below exposes an exact denominator
        # only after every canonical session has been reconciled.
        "native_turns": denominator.observed_turn_count,
        "native_denominator_turns": (
            denominator.observed_turn_count if denominator.complete else 0
        ),
        "captured_sessions": len(raw_completed_sessions),
        "raw_committed_turns": raw_committed_turns,
        "errors": errors,
        "cursor": coverage_cursor,
        "_capture_state": capture_state,
    }


def run_service(
    log_service_error: Callable[[str, Exception], None],
    *,
    engine_factory: Callable[[], Any],
    continuous_sync_limits_func: Callable[[], Dict[str, int]] = continuous_sync_limits,
    cursor_store: AgentSyncCursorStore | None = None,
    now_func: Callable[[], float] = time.time,
    previous_source_coverage: Mapping[str, Any] | None = None,
    coverage_state_sink: Callable[[Mapping[str, Any]], None] | None = None,
    source_registry: Any | None = None,
) -> Dict[str, Any]:
    """Run the manifest-owned continuous Raw owner with coverage evidence.

    ``engine_factory`` is an explicit ownership seam. The daemon runtime and
    controlled reconciliation inject a Raw-only engine that cannot enqueue
    downstream semantic work.
    ``source_registry`` is likewise an explicit bounded-recovery seam: callers
    may supply a manifest-filtered roster without changing the normal registry
    owner or its default all-active-source behavior.
    """
    result: Dict[str, Any] = {
        "synced": 0,
        "skipped": 0,
        "errors": 0,
        "source_snapshots": {},
        "unmanifested_sources": [],
    }
    engine: Any | None = None
    try:
        from core.sync_framework.registry import SourceRegistry
        from core.agent_kit.source_support_manifest import (
            build_native_source_snapshot,
            get_agent_source_support_manifest,
            native_source_snapshot_hash,
        )

        now_ts = now_func()
        observed_at = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()
        limits = continuous_sync_limits_func()
        support_manifest = get_agent_source_support_manifest()
        source_coverage = initialize_source_coverage(
            support_manifest,
            previous=previous_source_coverage,
            observed_at=observed_at,
        )
        engine = engine_factory()
        if cursor_store is None:
            db_path = getattr(engine, "db_path", None)
            if not isinstance(db_path, Path):
                raise AgentSyncCursorError("SyncEngine must expose a durable sync database path")
            cursor_store = AgentSyncCursorStore(db_path.parent)
        registry = SourceRegistry() if source_registry is None else source_registry
        discovered_source_names: set[str] = set()

        for source in registry.list_sources():
            try:
                spec = support_manifest.require_active_source(source.name)
            except AgentSourceSupportManifestError:
                result["unmanifested_sources"].append(source.name)
                result["errors"] += 1
                log_service_error(
                    f"raw_sync:{getattr(source, 'name', '?')}",
                    AgentSourceSupportManifestError("undeclared native source rejected"),
                )
                continue
            discovered_source_names.add(spec.name)
            source_error: Exception | None = None
            outcome: dict[str, Any] = {
                "native_sessions": 0,
                "native_turns": 0,
                "native_denominator_turns": 0,
                "captured_sessions": 0,
                "cursor": {"kind": "continuous_tail_reconcile_v1"},
                "errors": [],
            }
            try:
                outcome = sync_source_continuously(source, engine, cursor_store, limits)
                source_errors = outcome["errors"]
                if source_errors:
                    source_error = source_errors[0]
                    for error in source_errors:
                        log_service_error(f"raw_sync:{source.name}", error)
                    result["errors"] += len(source_errors)
                result["synced"] += int(outcome["captured_sessions"])
            except RAW_SYNC_RECOVERABLE_ERRORS as exc:
                source_error = exc
                log_service_error(f"raw_sync:{getattr(source, 'name', '?')}", exc)
                result["errors"] += 1
            finally:
                try:
                    snapshot = build_native_source_snapshot(
                        source.name,
                        resolved_roots=_resolved_source_roots(source),
                        cursor=outcome["cursor"],
                        native_denominator={
                            "sessions": int(outcome["native_sessions"]),
                            "turns": int(outcome["native_denominator_turns"]),
                        },
                        manifest=support_manifest,
                    )
                    result["source_snapshots"][source.name] = snapshot.to_dict()
                    snapshot_hash = native_source_snapshot_hash(snapshot)
                    capture_state = outcome.get("_capture_state")
                    if not isinstance(
                        capture_state,
                        SourceCaptureFingerprintState,
                    ):
                        raise AgentSyncCursorError("source capture fingerprint state is missing")
                    if (
                        outcome["cursor"].get("denominator_complete") is True
                        and capture_state.complete
                    ):
                        cursor_store.bind_native_source_snapshot(
                            source.name,
                            snapshot_hash,
                            expected_capture_state=capture_state,
                        )
                        outcome["native_source_snapshot_hash"] = snapshot_hash
                except AgentSourceSupportManifestError as exc:
                    source_error = source_error or exc
                    result["errors"] += 1
                    log_service_error(
                        f"raw_sync:{getattr(source, 'name', '?')}",
                        exc,
                    )
                except AgentSyncCursorError as exc:
                    source_error = source_error or exc
                    result["errors"] += 1
                    log_service_error(
                        f"raw_sync:{getattr(source, 'name', '?')}",
                        exc,
                    )
                record_source_observation(
                    source_coverage,
                    spec.name,
                    observed_at=observed_at,
                    cursor=outcome["cursor"],
                    native_sessions=int(outcome["native_sessions"]),
                    native_turns=int(outcome["native_turns"]),
                    captured_sessions=int(outcome["captured_sessions"]),
                    native_source_snapshot_hash=str(
                        outcome.get("native_source_snapshot_hash") or ""
                    ),
                    error=source_error,
                )

        for source_name in support_manifest.active_source_names:
            if source_name not in discovered_source_names:
                mark_source_not_detected(
                    source_coverage,
                    source_name,
                    observed_at=observed_at,
                )
        result["source_coverage"] = source_coverage
        if coverage_state_sink is not None:
            try:
                coverage_state_sink(source_coverage)
                result["coverage_state_persisted"] = True
            except RAW_SYNC_RECOVERABLE_ERRORS as exc:
                log_service_error("raw_sync:coverage_state", exc)
                result["errors"] += 1
                result["coverage_state_persisted"] = False
    except RAW_SYNC_RECOVERABLE_ERRORS as exc:
        log_service_error("raw_sync", exc)
        result["errors"] += 1
    finally:
        close = getattr(engine, "close", None)
        if callable(close):
            try:
                close()
            except RAW_SYNC_RECOVERABLE_ERRORS as exc:
                log_service_error("raw_sync:close", exc)
                result["errors"] += 1
    return dict(build_source_support_runtime_report(result, producer="daemon.raw_sync"))
