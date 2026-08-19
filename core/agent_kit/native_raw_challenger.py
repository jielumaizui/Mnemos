"""Independent, content-free Native-to-Raw challenger for host AgentSources.

The daemon cursor ledger proves that it observed a complete roster.  This
challenger separately reparses that roster and compares the native logical
event identities with current, normally visible Raw rows.  It deliberately
does not trust a daemon result object, cursor counter, or parser-provided
summary, and it never returns transcript bodies or source paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, cast

from core.agent_kit.source_support_manifest import (
    AgentSourceSupportManifest,
    AgentSourceSupportManifestError,
    get_agent_source_support_manifest,
)
from core.ops.durable_io import DurableIOError, inspect_path_kind
from core.sync_framework.agent_source import (
    NativeSourceContractError,
    canonicalize_session_info,
    parse_discovered_session_result,
)
from core.sync_framework.native_event_identity import resolve_native_event_identity
from core.sync_framework.native_artifact_inventory import (
    NativeArtifactInventoryError,
)
from core.sync_framework.native_sqlite import connect_native_sqlite_readonly
from core.sync_framework.native_raw_contract_ledger import NativeRawContractLedger
from core.sync_framework.raw_event_identity_aliases import (
    RawEventIdentityAliasError,
    alias_table_exists,
    resolve_canonical_event_id,
)
from core.sync_framework.raw_event_store import compute_logical_event_id
from core.sync_framework.source_support import build_native_raw_metadata

SCHEMA_VERSION = "mnemos.agent_source_native_raw_challenger.v3"


class NativeRawChallengerError(RuntimeError):
    """A fail-closed condition while producing a challenger report."""


def _hash_values(values: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for value in sorted(str(item) for item in values):
        digest.update(value.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("native challenger worker write made no progress")
        offset += written


def _read_only_connection(path: Path) -> sqlite3.Connection:
    return cast(
        sqlite3.Connection,
        connect_native_sqlite_readonly(path),
    )


def _source_files(session: Any, turn: Any) -> list[str]:
    """Mirror the canonical Raw writer's source-artifact selection."""
    files = [str(path) for path in (getattr(turn, "source_files", None) or [])]
    source_path = getattr(session, "source_path", None)
    if not files and source_path:
        files.append(str(source_path))
    return files


def _logical_event_id(
    source_name: str,
    session_id: str,
    session: Any,
    turn: Any,
    metadata: dict[str, Any],
) -> tuple[str, str]:
    """Recompute the exact identity path used by ``RawEventStore.upsert_turn``."""
    identity_metadata = dict(metadata)
    source_path = getattr(session, "source_path", None)
    source_files = _source_files(session, turn)
    if source_path:
        identity_metadata.setdefault("source_path", str(source_path))
    if source_files:
        identity_metadata.setdefault("source_artifact_id", source_files[0])
    identity = resolve_native_event_identity(
        metadata=identity_metadata,
        raw_event_refs=getattr(turn, "raw_event_refs", None),
        turn_number=int(getattr(turn, "turn_number", 0) or 0),
    )
    if identity.is_explicit:
        return (
            compute_logical_event_id(
                source_name,
                session_id,
                int(turn.turn_number),
                native_event_id=identity.value,
            ),
            identity.kind,
        )
    if identity.has_auditable_fallback:
        return (
            compute_logical_event_id(
                source_name,
                session_id,
                int(turn.turn_number),
                parser=identity.parser,
                parser_version=identity.parser_version,
                source_artifact_id=identity.source_artifact_id,
                artifact_offset=identity.artifact_offset,
            ),
            identity.kind,
        )
    # RawEventStore deliberately retains the historical session-plus-ordinal
    # fallback when the source itself cannot provide a stronger artifact fact.
    # The challenger must audit that real contract, while exposing the weaker
    # identity count instead of misclassifying it as an impossible Raw miss.
    return (
        compute_logical_event_id(source_name, session_id, int(turn.turn_number)),
        identity.kind,
    )


def _expected_event_identity(
    source: Any,
    canonical_session: Any,
    session_id: str,
    turn: Any,
) -> tuple[str, str]:
    """Resolve one Turn without retaining its shallow-copied metadata graph."""
    metadata = build_native_raw_metadata(source, canonical_session, turn)
    return _logical_event_id(
        source.name,
        session_id,
        canonical_session,
        turn,
        metadata,
    )


_SESSION_IDENTITY_WORKER_MAX_BYTES = 256 * 1024 * 1024
_SESSION_IDENTITY_WORKER_SCHEMA = "mnemos.native_challenger_session_identity.v1"


def _expected_session_evidence(
    source: Any,
    canonical_session: Any,
    session_id: str,
    session: Any,
) -> dict[str, Any]:
    """Project one parsed session to content-free identity facts."""

    parse_result = parse_discovered_session_result(source, session)
    turns = list(parse_result.turns)
    events: list[list[str]] = []
    errors: list[str] = []
    for turn in turns:
        try:
            event_id, identity_kind = _expected_event_identity(
                source,
                canonical_session,
                session_id,
                turn,
            )
        except (
            AgentSourceSupportManifestError,
            NativeRawChallengerError,
            TypeError,
            ValueError,
            AttributeError,
        ):
            errors.append("native_turn_identity_invalid")
            continue
        events.append([event_id, identity_kind])
    return {
        "turn_count": len(turns),
        "disposition": parse_result.disposition,
        "reason_code": parse_result.reason_code,
        "artifact_evidence_hash": parse_result.artifact_evidence_hash,
        "events": events,
        "errors": errors,
        "infrastructure_attempt_count": (parse_result.infrastructure_attempt_count),
        "recovered_infrastructure_failure": dict(parse_result.recovered_infrastructure_failure),
    }


def _isolated_expected_session_evidence(
    source: Any,
    canonical_session: Any,
    session_id: str,
    session: Any,
) -> dict[str, Any]:
    """Compute one session's identities in an exit-reclaimed child process."""

    if not hasattr(os, "fork"):
        raise NativeRawChallengerError("native_session_identity_worker_unavailable")
    read_descriptor, write_descriptor = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no branch - child exits without parent heap retention
        exit_code = 93
        try:
            os.close(read_descriptor)
            try:
                payload = {
                    "schema_version": _SESSION_IDENTITY_WORKER_SCHEMA,
                    "status": "ok",
                    "evidence": _expected_session_evidence(
                        source,
                        canonical_session,
                        session_id,
                        session,
                    ),
                }
            except NativeArtifactInventoryError as exc:
                payload = {
                    "schema_version": _SESSION_IDENTITY_WORKER_SCHEMA,
                    "status": "typed_failure",
                    "code": exc.code,
                    "details": dict(exc.details),
                }
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > _SESSION_IDENTITY_WORKER_MAX_BYTES:
                os._exit(92)
            _write_all(write_descriptor, encoded)
            os.close(write_descriptor)
            exit_code = 0
        finally:
            os._exit(exit_code)
    os.close(write_descriptor)
    chunks: list[bytes] = []
    consumed = 0
    over_budget = False
    try:
        while True:
            chunk = os.read(read_descriptor, 1024 * 1024)
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > _SESSION_IDENTITY_WORKER_MAX_BYTES:
                over_budget = True
                os.kill(pid, 9)
                break
            chunks.append(chunk)
    finally:
        os.close(read_descriptor)
    _waited_pid, status = os.waitpid(pid, 0)
    exit_code = os.waitstatus_to_exitcode(status)
    if over_budget or exit_code == 92:
        raise NativeRawChallengerError("native_session_identity_worker_budget_exceeded")
    if exit_code != 0:
        raise NativeRawChallengerError("native_session_identity_worker_failed")
    try:
        payload = json.loads(b"".join(chunks))
    except (UnicodeError, json.JSONDecodeError):
        raise NativeRawChallengerError("native_session_identity_worker_result_invalid") from None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != _SESSION_IDENTITY_WORKER_SCHEMA
    ):
        raise NativeRawChallengerError("native_session_identity_worker_result_invalid")
    if payload.get("status") == "typed_failure":
        code = payload.get("code")
        details = payload.get("details")
        if not isinstance(code, str) or not code or not isinstance(details, dict):
            raise NativeRawChallengerError("native_session_identity_worker_result_invalid")
        raise NativeArtifactInventoryError(code, details=details)
    if payload.get("status") != "ok":
        raise NativeRawChallengerError("native_session_identity_worker_failed")
    evidence = payload.get("evidence")
    if (
        not isinstance(evidence, dict)
        or set(evidence)
        != {
            "artifact_evidence_hash",
            "disposition",
            "errors",
            "events",
            "infrastructure_attempt_count",
            "reason_code",
            "recovered_infrastructure_failure",
            "turn_count",
        }
        or not isinstance(evidence.get("events"), list)
        or not isinstance(evidence.get("errors"), list)
        or not all(
            isinstance(item, list)
            and len(item) == 2
            and all(isinstance(value, str) and value for value in item)
            for item in evidence["events"]
        )
        or not all(item == "native_turn_identity_invalid" for item in evidence["errors"])
        or isinstance(evidence.get("turn_count"), bool)
        or not isinstance(evidence.get("turn_count"), int)
        or evidence["turn_count"] < len(evidence["events"])
        or not isinstance(
            evidence.get("recovered_infrastructure_failure"),
            dict,
        )
    ):
        raise NativeRawChallengerError("native_session_identity_worker_result_invalid")
    return evidence


def _expected_native_events(source: Any) -> tuple[set[str], dict[str, Any], list[str]]:
    """Independently parse one source, retaining only logical identity facts."""
    expected: set[str] = set()
    errors: list[str] = []
    canonical_sessions: set[str] = set()
    parsed_turns = 0
    empty_sessions = 0
    excluded_sessions = 0
    disposition_evidence: list[str] = []
    session_turn_upper_bound = 0
    identity_duplicates = 0
    identity_kinds: dict[str, int] = {}
    parse_failures: list[dict[str, Any]] = []
    identity_isolated_sessions = 0
    recovered_parse_sessions: list[dict[str, Any]] = []

    def content_free_parse_failure(
        *,
        error_code: str,
        session_id: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "error_code": str(error_code),
            "source_name": str(getattr(source, "name", "") or ""),
            "session_id_hash": (
                "sha256:" + hashlib.sha256(session_id.lower().encode("utf-8")).hexdigest()
            ),
        }
        for key in (
            "attempt_count",
            "exception_type",
            "failure_class",
            "os_errno",
            "reason_code",
            "signal",
            "sqlite_errorcode",
            "sqlite_errorname",
        ):
            value = details.get(key)
            if isinstance(value, (str, int)) and not isinstance(
                value,
                bool,
            ):
                evidence[key] = value
        return evidence

    try:
        sessions = list(source.discover_sessions() or [])
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        return (
            expected,
            {
                "native_sessions": 0,
                "native_parsed_turns": 0,
                "native_empty_sessions": 0,
                "native_evidence_excluded_sessions": 0,
                "native_session_disposition_hash": _hash_values(()),
                "native_session_turn_upper_bound": 0,
                "native_identity_duplicates": 0,
                "native_identity_isolated_sessions": 0,
                "native_parse_recovered_sessions": 0,
                "native_parse_recovery_evidence": [],
                "expected_identity_hash": _hash_values(()),
            },
            ["native_discovery_failed"],
        )
    for session in sessions:
        try:
            canonical = canonicalize_session_info(session)
            session_id = str(canonical.session_id or "")
        except (AttributeError, TypeError, ValueError):
            errors.append("native_session_metadata_invalid")
            continue
        if not session_id:
            errors.append("native_session_id_missing")
            continue
        if session_id in canonical_sessions:
            errors.append("native_canonical_session_duplicate")
            continue
        canonical_sessions.add(session_id)
        try:
            identity_isolated = (
                getattr(
                    source,
                    "_native_challenger_identity_isolation",
                    False,
                )
                is True
            )
            session_evidence = (
                _isolated_expected_session_evidence(
                    source,
                    canonical,
                    session_id,
                    session,
                )
                if identity_isolated
                else _expected_session_evidence(
                    source,
                    canonical,
                    session_id,
                    session,
                )
            )
            if identity_isolated:
                identity_isolated_sessions += 1
        except NativeArtifactInventoryError as exc:
            parse_failures.append(
                content_free_parse_failure(
                    error_code=exc.code,
                    session_id=session_id,
                    details=dict(exc.details),
                )
            )
            errors.append("native_session_parse_failed")
            continue
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ) as exc:
            details: dict[str, Any] = {
                "attempt_count": 1,
                "exception_type": type(exc).__name__,
            }
            if isinstance(exc, NativeSourceContractError):
                details["reason_code"] = exc.code
                details.update(
                    {
                        key: value
                        for key, value in exc.details.items()
                        if key
                        in {
                            "failure_class",
                            "os_errno",
                            "sqlite_errorcode",
                            "sqlite_errorname",
                        }
                    }
                )
            elif isinstance(exc, NativeRawChallengerError) and re.fullmatch(
                r"[a-z][a-z0-9_]{2,127}",
                str(exc),
            ):
                details["reason_code"] = str(exc)
            parse_failures.append(
                content_free_parse_failure(
                    error_code="native_session_parser_exception",
                    session_id=session_id,
                    details=details,
                )
            )
            errors.append("native_session_parse_failed")
            continue
        turn_count = int(session_evidence["turn_count"])
        session_turn_upper_bound = max(session_turn_upper_bound, turn_count)
        errors.extend(session_evidence["errors"])
        if int(session_evidence["infrastructure_attempt_count"]) > 1:
            recovered_parse_sessions.append(
                {
                    "attempt_count": int(session_evidence["infrastructure_attempt_count"]),
                    "session_id_hash": (
                        "sha256:" + hashlib.sha256(session_id.lower().encode("utf-8")).hexdigest()
                    ),
                    **dict(session_evidence["recovered_infrastructure_failure"]),
                }
            )
        if turn_count == 0:
            if (
                session_evidence["disposition"] not in {"typed_empty", "evidence_excluded"}
                or not session_evidence["artifact_evidence_hash"]
            ):
                errors.append("native_session_disposition_unverified")
                del session_evidence
                continue
            if session_evidence["disposition"] == "typed_empty":
                empty_sessions += 1
            else:
                excluded_sessions += 1
            disposition_evidence.append(
                "\0".join(
                    (
                        hashlib.sha256(session_id.encode("utf-8")).hexdigest(),
                        session_evidence["disposition"],
                        session_evidence["reason_code"],
                        session_evidence["artifact_evidence_hash"],
                    )
                )
            )
            del session_evidence
            continue
        parsed_turns += turn_count
        for event_id, identity_kind in session_evidence["events"]:
            identity_kinds[identity_kind] = identity_kinds.get(identity_kind, 0) + 1
            if event_id in expected:
                identity_duplicates += 1
                errors.append("native_logical_identity_duplicate")
                continue
            expected.add(event_id)
        del session_evidence
    summary = {
        "native_sessions": len(canonical_sessions),
        "native_parsed_turns": parsed_turns,
        "native_empty_sessions": empty_sessions,
        "native_evidence_excluded_sessions": excluded_sessions,
        "native_session_disposition_hash": _hash_values(disposition_evidence),
        "native_session_turn_upper_bound": session_turn_upper_bound,
        "native_identity_duplicates": identity_duplicates,
        "native_identity_kinds": dict(sorted(identity_kinds.items())),
        "native_legacy_identity_turns": identity_kinds.get("legacy_turn_number", 0),
        "native_identity_isolated_sessions": (identity_isolated_sessions),
        "native_parse_recovered_sessions": len(recovered_parse_sessions),
        "native_parse_recovery_evidence": sorted(
            recovered_parse_sessions,
            key=lambda item: str(item.get("session_id_hash") or ""),
        ),
        "native_session_parse_failures": sorted(
            parse_failures,
            key=lambda item: (
                str(item.get("source_name") or ""),
                str(item.get("session_id_hash") or ""),
                str(item.get("error_code") or ""),
            ),
        ),
        "expected_identity_hash": _hash_values(expected),
    }
    return expected, summary, sorted(set(errors))


def _visible_raw_events(raw_db_path: Path, source_name: str) -> tuple[set[str], list[str]]:
    try:
        raw_kind = inspect_path_kind(Path(raw_db_path))
    except DurableIOError:
        return set(), ["raw_database_unavailable"]
    if raw_kind == "missing":
        return set(), ["raw_database_missing"]
    if raw_kind != "file":
        return set(), ["raw_database_not_regular"]
    try:
        with _read_only_connection(raw_db_path) as conn:
            required = {"raw_turns", "raw_native_contract_observations"}
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not required.issubset(tables):
                return set(), ["raw_native_contract_schema_missing"]
            alias_filter = ""
            if alias_table_exists(conn):
                alias_filter = (
                    " AND NOT EXISTS (SELECT 1 FROM raw_event_identity_aliases AS a "
                    "WHERE a.alias_event_id=t.event_id)"
                )
            query = "SELECT t.event_id FROM raw_turns AS t WHERE t.source_agent=?"
            query += alias_filter
            query += NativeRawContractLedger.current_event_visibility_predicate("t.event_id")
            event_ids: set[str] = set()
            for (event_id,) in conn.execute(query, (source_name,)).fetchall():
                event_ids.add(resolve_canonical_event_id(conn, str(event_id or "")))
            return event_ids, []
    except (OSError, sqlite3.Error, RawEventIdentityAliasError):
        return set(), ["raw_database_unreadable"]


def audit_native_to_raw(
    sources: Iterable[Any],
    *,
    raw_db_path: Path,
    manifest: AgentSourceSupportManifest | None = None,
    require_all_host_sources: bool = True,
    source_scope: str = "host",
) -> dict[str, Any]:
    """Compare a manifest-owned source scope to Raw without exposing bodies."""
    if source_scope not in {"host", "active"}:
        raise NativeRawChallengerError("unsupported_source_scope")
    support_manifest = manifest or get_agent_source_support_manifest()
    reports: dict[str, dict[str, Any]] = {}
    present: set[str] = set()
    for source in sources:
        source_name = str(getattr(source, "name", "") or "")
        try:
            spec = (
                support_manifest.require_host_agent(source_name)
                if source_scope == "host"
                else support_manifest.require_active_source(source_name)
            )
        except AgentSourceSupportManifestError:
            reports[source_name or "<unknown>"] = {
                "status": "blocked",
                "errors": [f"{source_scope}_source_not_declared"],
            }
            continue
        present.add(spec.name)
        expected, native_summary, native_errors = _expected_native_events(source)
        observed, raw_errors = _visible_raw_events(raw_db_path, spec.name)
        missing = expected - observed
        report_errors = sorted(set([*native_errors, *raw_errors]))
        reports[spec.name] = {
            **native_summary,
            "visible_raw_events": len(observed),
            "visible_identity_hash": _hash_values(observed),
            "expected_visible_match": len(expected & observed),
            "expected_visible_missing": len(missing),
            "visible_unobserved_or_legacy": len(observed - expected),
            "errors": report_errors,
            "status": "ok" if not report_errors and not missing else "blocked",
        }
    if require_all_host_sources:
        required_names = (
            support_manifest.host_agent_names
            if source_scope == "host"
            else support_manifest.active_source_names
        )
        for source_name in required_names:
            if source_name not in present:
                reports[source_name] = {
                    "status": "blocked",
                    "errors": [f"{source_scope}_source_not_detected"],
                }
    blocking = sorted(name for name, report in reports.items() if report.get("status") != "ok")
    return {
        "schema_version": SCHEMA_VERSION,
        "support_manifest_hash": support_manifest.manifest_hash,
        "source_scope": source_scope,
        "sources": {name: reports[name] for name in sorted(reports)},
        "blocking_sources": blocking,
        "ok": not blocking,
    }
