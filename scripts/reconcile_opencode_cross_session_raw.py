#!/usr/bin/env python3
"""Quarantine provable OpenCode cross-session Raw identity misattributions.

The command compares only current OpenCode ``session_id × native_event_id``
identities with canonical Raw identity metadata.  It never selects, prints, or
rewrites transcript bodies.  A proven historical mismatch is preserved in Raw
and its immutable revision history, then made unavailable to normal current
consumers through an append-only native-contract observation.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agent_kit.source_support_manifest import get_agent_source_support_manifest
from core.config import get_config
from core.sync_framework.agent_source import AgentSource, SessionInfo, canonicalize_session_info
from core.sync_framework.native_event_identity import resolve_native_event_identity
from core.sync_framework.native_raw_contract_ledger import NativeRawContractLedger
from integrations.sources.opencode_source import OpenCodeSource


SCHEMA_VERSION = "mnemos.opencode_cross_session_raw_reconciliation.v2"
SOURCE_NAME = "opencode"
CROSS_SESSION_ERROR = "cross_session_native_identity"
RECONCILIATION_ERROR = "opencode_cross_session_reconciliation_v1"


class OpenCodeCrossSessionReconciliationError(RuntimeError):
    """A fail-closed condition that leaves canonical Raw unchanged."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _RawNativeRecord:
    event_id: str
    session_id: str
    revision_id: str
    native_event_id: str


@dataclass(frozen=True)
class _CrossSessionCandidate:
    raw: _RawNativeRecord
    expected_session_id: str


@dataclass(frozen=True)
class _Plan:
    report: dict[str, Any]
    candidates: tuple[_CrossSessionCandidate, ...]
    support_manifest_hash: str


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_hash(value: Any) -> str:
    return _sha256_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _pair_hash(session_id: str, native_event_id: str) -> str:
    return _sha256_text(f"{session_id}\0{native_event_id}")


def _read_only_connection(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)


def _required_raw_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _native_event_id(turn: Any) -> str:
    metadata = getattr(turn, "metadata", None)
    raw_refs = getattr(turn, "raw_event_refs", None)
    turn_number = int(getattr(turn, "turn_number", 0) or 0)
    identity = resolve_native_event_identity(
        metadata=metadata if isinstance(metadata, dict) else {},
        raw_event_refs=raw_refs if isinstance(raw_refs, list) else [],
        turn_number=turn_number,
    )
    return identity.value if identity.is_explicit else ""


def _collect_current_pairs(
    source: AgentSource,
) -> tuple[set[tuple[str, str]], dict[str, Any], list[str]]:
    """Build the exact current native identity set without retaining bodies."""
    errors: list[str] = []
    if str(getattr(source, "name", "")) != SOURCE_NAME:
        errors.append("unexpected_source_name")
    parse_session = getattr(type(source), "parse_session", None)
    if parse_session is AgentSource.parse_session:
        errors.append("session_aware_parser_required")

    try:
        sessions = list(source.discover_sessions())
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        sessions = []
        errors.append("source_discovery_failed")
    if not sessions:
        errors.append("source_discovery_empty")

    pairs: set[tuple[str, str]] = set()
    native_owners: dict[str, str] = {}
    canonical_sessions: set[str] = set()
    empty_sessions = 0
    parsed_turns = 0
    for session in sessions:
        if not isinstance(session, SessionInfo):
            errors.append("source_session_metadata_invalid")
            continue
        canonical_session_id = canonicalize_session_info(session).session_id
        if not canonical_session_id:
            errors.append("source_session_id_missing")
            continue
        if canonical_session_id in canonical_sessions:
            errors.append("source_canonical_session_duplicate")
            continue
        canonical_sessions.add(canonical_session_id)
        try:
            turns = source.parse_session(session)
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            errors.append("source_session_parse_failed")
            continue
        if not isinstance(turns, list):
            errors.append("source_session_parse_invalid")
            continue
        if not turns:
            empty_sessions += 1
            continue
        for turn in turns:
            native_event_id = _native_event_id(turn)
            if not native_event_id:
                errors.append("current_turn_native_identity_missing")
                continue
            pair = (canonical_session_id, native_event_id)
            if pair in pairs:
                errors.append("current_native_pair_duplicate")
                continue
            existing_owner = native_owners.get(native_event_id)
            if existing_owner is not None and existing_owner != canonical_session_id:
                errors.append("current_native_identity_multi_session")
                continue
            pairs.add(pair)
            native_owners[native_event_id] = canonical_session_id
            parsed_turns += 1
    if empty_sessions:
        errors.append("current_parse_empty_session")
    summary = {
        "sessions": len(canonical_sessions),
        "native_turns": parsed_turns,
        "empty_sessions": empty_sessions,
        "identity_hash": _stable_hash(sorted(_pair_hash(*pair) for pair in pairs)),
    }
    return pairs, summary, sorted(set(errors))


def _raw_native_records(
    db_path: Path,
) -> tuple[list[_RawNativeRecord], dict[str, Any], list[str]]:
    """Read only Raw identity metadata and current revision bindings."""
    if not db_path.is_file():
        raise OpenCodeCrossSessionReconciliationError("raw_database_missing")
    errors: list[str] = []
    records: list[_RawNativeRecord] = []
    raw_opencode_rows = 0
    legacy_rows = 0
    duplicate_pairs = 0
    try:
        with _read_only_connection(db_path) as conn:
            required = {
                "raw_turns",
                "raw_turn_revisions",
                "raw_native_contract_observations",
            }
            if not required.issubset(_required_raw_tables(conn)):
                errors.append("raw_native_contract_schema_missing")
                return records, {
                    "opencode_rows": 0,
                    "native_identity_rows": 0,
                    "legacy_rows": 0,
                    "native_pair_duplicates": 0,
                    "identity_hash": _stable_hash([]),
                }, errors
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                errors.append("raw_foreign_key_violation")
            rows = conn.execute(
                """
                SELECT t.event_id, t.session_id, t.current_revision_id,
                       t.metadata_json, r.logical_event_id
                FROM raw_turns AS t
                LEFT JOIN raw_turn_revisions AS r
                  ON r.revision_id=t.current_revision_id
                WHERE t.source_agent=?
                ORDER BY t.event_id
                """,
                (SOURCE_NAME,),
            ).fetchall()
    except (OSError, sqlite3.Error):
        raise OpenCodeCrossSessionReconciliationError("raw_database_unreadable") from None

    raw_opencode_rows = len(rows)
    pair_owners: dict[tuple[str, str], str] = {}
    for event_id, session_id, revision_id, metadata_json, revision_owner in rows:
        try:
            metadata = json.loads(str(metadata_json or "{}"))
        except (TypeError, json.JSONDecodeError):
            errors.append("raw_metadata_unreadable")
            continue
        if not isinstance(metadata, dict):
            errors.append("raw_metadata_unreadable")
            continue
        if str(metadata.get("logical_event_identity_kind") or "") != "native_event_id":
            legacy_rows += 1
            continue
        native_event_id = str(
            metadata.get("logical_event_identity") or metadata.get("native_event_id") or ""
        )
        if not native_event_id:
            errors.append("raw_native_identity_missing")
            continue
        logical_event_id = str(event_id or "")
        current_revision_id = str(revision_id or "")
        canonical_session_id = str(session_id or "")
        if not logical_event_id or not canonical_session_id or not current_revision_id:
            errors.append("raw_native_row_incomplete")
            continue
        if str(revision_owner or "") != logical_event_id:
            errors.append("raw_current_revision_unbound")
            continue
        pair = (canonical_session_id, native_event_id)
        if pair in pair_owners:
            duplicate_pairs += 1
            errors.append("raw_native_pair_duplicate")
            continue
        pair_owners[pair] = logical_event_id
        records.append(
            _RawNativeRecord(
                event_id=logical_event_id,
                session_id=canonical_session_id,
                revision_id=current_revision_id,
                native_event_id=native_event_id,
            )
        )
    summary = {
        "opencode_rows": raw_opencode_rows,
        "native_identity_rows": len(records),
        "legacy_rows": legacy_rows,
        "native_pair_duplicates": duplicate_pairs,
        "identity_hash": _stable_hash(
            [
                {
                    "event_id": record.event_id,
                    "revision_id": record.revision_id,
                    "pair_hash": _pair_hash(record.session_id, record.native_event_id),
                }
                for record in records
            ]
        ),
    }
    return records, summary, sorted(set(errors))


def _latest_cross_session_count(
    db_path: Path,
    candidates: tuple[_CrossSessionCandidate, ...],
) -> int:
    if not candidates:
        return 0
    try:
        with _read_only_connection(db_path) as conn:
            ledger = NativeRawContractLedger()
            return sum(
                1
                for candidate in candidates
                if (latest := ledger.latest(conn, candidate.raw.event_id)) is not None
                and latest["contract_state"] == "nonconforming"
                and CROSS_SESSION_ERROR in latest["contract_errors"]
            )
    except (OSError, sqlite3.Error):
        raise OpenCodeCrossSessionReconciliationError("raw_database_unreadable") from None


def _normal_visible_cross_session_count(
    db_path: Path,
    candidates: tuple[_CrossSessionCandidate, ...],
) -> int:
    if not candidates:
        return 0
    placeholders = ",".join("?" for _ in candidates)
    query = f"SELECT COUNT(*) FROM raw_turns AS t WHERE t.event_id IN ({placeholders})"  # nosec B608
    query += NativeRawContractLedger.current_event_visibility_predicate("t.event_id")
    try:
        with _read_only_connection(db_path) as conn:
            row = conn.execute(query, [item.raw.event_id for item in candidates]).fetchone()
    except (OSError, sqlite3.Error):
        raise OpenCodeCrossSessionReconciliationError("raw_database_unreadable") from None
    return int(row[0] or 0) if row else 0


def _build_plan(db_path: Path, *, source: AgentSource | None = None) -> _Plan:
    source = source or OpenCodeSource()
    pairs, source_summary, source_errors = _collect_current_pairs(source)
    records, raw_summary, raw_errors = _raw_native_records(db_path)
    native_owners = {native_event_id: session_id for session_id, native_event_id in pairs}
    exact_pair = 0
    unobserved = 0
    candidates: list[_CrossSessionCandidate] = []
    for record in records:
        pair = (record.session_id, record.native_event_id)
        if pair in pairs:
            exact_pair += 1
            continue
        expected_session_id = native_owners.get(record.native_event_id)
        if expected_session_id is None:
            unobserved += 1
            continue
        candidates.append(
            _CrossSessionCandidate(record, expected_session_id)
        )
    candidates.sort(key=lambda item: item.raw.event_id)
    candidate_tuple = tuple(candidates)
    missing_pairs = pairs - {
        (record.session_id, record.native_event_id) for record in records
    }
    blocking_errors = [*source_errors, *raw_errors]
    if missing_pairs:
        blocking_errors.append("expected_pair_missing_from_raw")
    support_manifest_hash = get_agent_source_support_manifest().manifest_hash
    already_quarantined = _latest_cross_session_count(db_path, candidate_tuple)
    normal_visible = _normal_visible_cross_session_count(db_path, candidate_tuple)
    classification = {
        "cross_session_native_identity": len(candidate_tuple),
        "current_parse_empty_sessions": source_summary["empty_sessions"],
        "exact_pair": exact_pair,
        "expected_pairs_missing_from_raw": len(missing_pairs),
        "raw_native_pair_duplicates": raw_summary["native_pair_duplicates"],
        "unobserved_native_identity": unobserved,
        "unobserved_native_identity_in_apply_set": 0,
    }
    candidate_receipt_hash = _stable_hash(
        {
            "schema_version": SCHEMA_VERSION,
            "support_manifest_hash": support_manifest_hash,
            "source_identity_hash": source_summary["identity_hash"],
            "classification": classification,
            "candidates": [
                {
                    "event_id": item.raw.event_id,
                    "revision_id": item.raw.revision_id,
                    "raw_pair_hash": _pair_hash(
                        item.raw.session_id, item.raw.native_event_id
                    ),
                    "expected_session_hash": _sha256_text(item.expected_session_id),
                }
                for item in candidate_tuple
            ],
        }
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry_run",
        "db_path": str(db_path),
        "source_name": SOURCE_NAME,
        "support_manifest_hash": support_manifest_hash,
        "source": source_summary,
        "raw": raw_summary,
        "classification": classification,
        "candidate_count": len(candidate_tuple),
        "already_quarantined_count": already_quarantined,
        "needs_apply_count": len(candidate_tuple) - already_quarantined,
        "normal_visible_cross_session_count": normal_visible,
        "candidate_receipt_hash": candidate_receipt_hash,
        "blocking_errors": sorted(set(blocking_errors)),
        "ok": not blocking_errors,
    }
    return _Plan(
        report=report,
        candidates=candidate_tuple,
        support_manifest_hash=support_manifest_hash,
    )


def inspect_reconciliation(
    db_path: Path,
    *,
    source: AgentSource | None = None,
) -> dict[str, Any]:
    """Return a content-free, read-only classification report."""
    return _build_plan(Path(db_path).expanduser(), source=source).report


def _backup_database(db_path: Path, backup_dir: Path) -> tuple[Path, str]:
    if not db_path.is_file():
        raise OpenCodeCrossSessionReconciliationError("raw_database_missing")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / (
        f"{db_path.stem}.{stamp}.{uuid.uuid4().hex[:12]}."
        "pre_opencode_cross_session_raw.sqlite"
    )
    try:
        source = _read_only_connection(db_path)
        destination = sqlite3.connect(str(backup_path))
        try:
            source.backup(destination)
            integrity = destination.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]) != "ok":
                raise OpenCodeCrossSessionReconciliationError("backup_integrity_check_failed")
        finally:
            destination.close()
            source.close()
    except OpenCodeCrossSessionReconciliationError:
        raise
    except (OSError, sqlite3.Error):
        raise OpenCodeCrossSessionReconciliationError("backup_failed") from None
    digest = _file_sha256(backup_path)
    return backup_path, digest


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    """Atomically update a content-free reconciliation receipt."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with open(temporary, "x", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _current_record_matches(conn: sqlite3.Connection, record: _RawNativeRecord) -> bool:
    row = conn.execute(
        """
        SELECT t.session_id, t.current_revision_id, t.metadata_json, r.logical_event_id
        FROM raw_turns AS t
        LEFT JOIN raw_turn_revisions AS r ON r.revision_id=t.current_revision_id
        WHERE t.event_id=? AND t.source_agent=?
        """,
        (record.event_id, SOURCE_NAME),
    ).fetchone()
    if not row:
        return False
    session_id, revision_id, metadata_json, revision_owner = row
    try:
        metadata = json.loads(str(metadata_json or "{}"))
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(metadata, dict):
        return False
    native_event_id = str(
        metadata.get("logical_event_identity") or metadata.get("native_event_id") or ""
    )
    return (
        str(session_id or "") == record.session_id
        and str(revision_id or "") == record.revision_id
        and str(revision_owner or "") == record.event_id
        and str(metadata.get("logical_event_identity_kind") or "") == "native_event_id"
        and native_event_id == record.native_event_id
    )


def _apply_candidates(
    db_path: Path,
    *,
    plan: _Plan,
) -> dict[str, int]:
    ledger = NativeRawContractLedger()
    observed_at = _utcnow()
    appended = 0
    already = 0
    try:
        with closing(sqlite3.connect(str(db_path))) as conn, conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            for candidate in plan.candidates:
                record = candidate.raw
                if not _current_record_matches(conn, record):
                    raise OpenCodeCrossSessionReconciliationError("candidate_changed_before_apply")
                latest = ledger.latest(conn, record.event_id)
                if (
                    latest is not None
                    and latest["contract_state"] == "nonconforming"
                    and CROSS_SESSION_ERROR in latest["contract_errors"]
                ):
                    already += 1
                    continue
                prior_errors = []
                if latest is not None and latest["contract_state"] == "nonconforming":
                    prior_errors = [str(error) for error in latest["contract_errors"]]
                errors = sorted(
                    {
                        *prior_errors,
                        CROSS_SESSION_ERROR,
                        RECONCILIATION_ERROR,
                    }
                )
                ledger.record_explicit(
                    conn,
                    logical_event_id=record.event_id,
                    revision_id=record.revision_id,
                    support_manifest_hash=plan.support_manifest_hash,
                    contract_state="nonconforming",
                    contract_errors=errors,
                    observed_at=observed_at,
                )
                ledger.refresh_effective_state(
                    conn,
                    logical_event_id=record.event_id,
                    observed_at=observed_at,
                )
                appended += 1
            for candidate in plan.candidates:
                latest = ledger.latest(conn, candidate.raw.event_id)
                if (
                    latest is None
                    or latest["contract_state"] != "nonconforming"
                    or CROSS_SESSION_ERROR not in latest["contract_errors"]
                ):
                    raise OpenCodeCrossSessionReconciliationError("quarantine_observation_missing")
            placeholders = ",".join("?" for _ in plan.candidates)
            if placeholders:
                query = (
                    "SELECT COUNT(*) FROM raw_turns AS t "
                    f"WHERE t.event_id IN ({placeholders})"  # nosec B608
                )
                query += NativeRawContractLedger.current_event_visibility_predicate(
                    "t.event_id"
                )
                visible = int(
                    conn.execute(
                        query,
                        [candidate.raw.event_id for candidate in plan.candidates],
                    ).fetchone()[0]
                )
                if visible:
                    raise OpenCodeCrossSessionReconciliationError("quarantine_visibility_failed")
    except OpenCodeCrossSessionReconciliationError:
        raise
    except (OSError, sqlite3.Error, ValueError):
        raise OpenCodeCrossSessionReconciliationError("apply_transaction_failed") from None
    return {"observations_appended": appended, "already_quarantined": already}


def _receipt_payload(
    *,
    status: str,
    db_path: Path,
    backup_path: Path,
    backup_sha256: str,
    plan: _Plan,
    effect: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "db_path": str(db_path),
        "backup_path": str(backup_path),
        "backup_sha256": backup_sha256,
        "support_manifest_hash": plan.support_manifest_hash,
        "candidate_receipt_hash": plan.report["candidate_receipt_hash"],
        "candidate_count": plan.report["candidate_count"],
        "classification": plan.report["classification"],
        "source": plan.report["source"],
        "raw": plan.report["raw"],
        "effect": effect or {"observations_appended": 0, "already_quarantined": 0},
        "written_at": _utcnow(),
    }


def apply_reconciliation(
    db_path: Path,
    *,
    backup_dir: Path,
    source: AgentSource | None = None,
) -> dict[str, Any]:
    """Back up first, then append only proven cross-session quarantine facts."""
    db_path = Path(db_path).expanduser()
    backup_dir = Path(backup_dir).expanduser()
    before_plan = _build_plan(db_path, source=source)
    if not before_plan.report["ok"]:
        raise OpenCodeCrossSessionReconciliationError("unsafe_reconciliation_plan")
    backup_path, backup_sha256 = _backup_database(db_path, backup_dir)
    rechecked_plan = _build_plan(db_path, source=source)
    if not rechecked_plan.report["ok"]:
        raise OpenCodeCrossSessionReconciliationError("evidence_changed_after_backup")
    if (
        rechecked_plan.report["candidate_receipt_hash"]
        != before_plan.report["candidate_receipt_hash"]
    ):
        raise OpenCodeCrossSessionReconciliationError("evidence_changed_after_backup")

    receipt_path = backup_dir / (
        "opencode-cross-session-raw-reconciliation-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    )
    _write_receipt(
        receipt_path,
        _receipt_payload(
            status="prepared",
            db_path=db_path,
            backup_path=backup_path,
            backup_sha256=backup_sha256,
            plan=rechecked_plan,
        ),
    )
    try:
        effect = _apply_candidates(db_path, plan=rechecked_plan)
    except OpenCodeCrossSessionReconciliationError:
        _write_receipt(
            receipt_path,
            _receipt_payload(
                status="rolled_back",
                db_path=db_path,
                backup_path=backup_path,
                backup_sha256=backup_sha256,
                plan=rechecked_plan,
            ),
        )
        raise

    after_plan = _build_plan(db_path, source=source)
    evidence_stable = (
        after_plan.report["candidate_receipt_hash"]
        == before_plan.report["candidate_receipt_hash"]
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply",
        "db_path": str(db_path),
        "backup_path": str(backup_path),
        "backup_sha256": backup_sha256,
        "receipt_path": str(receipt_path),
        "before": before_plan.report,
        "effect": effect,
        "after": after_plan.report,
        "ok": bool(
            after_plan.report["ok"]
            and evidence_stable
            and after_plan.report["normal_visible_cross_session_count"] == 0
        ),
    }
    _write_receipt(
        receipt_path,
        _receipt_payload(
            status="committed" if result["ok"] else "committed_with_evidence_drift",
            db_path=db_path,
            backup_path=backup_path,
            backup_sha256=backup_sha256,
            plan=after_plan,
            effect=effect,
        ),
    )
    return result


def _default_db_path() -> Path:
    config = get_config()
    configured = config.get("raw_event_store.db_path")
    return Path(configured or (config.database_dir / "raw_events.db")).expanduser()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=_default_db_path())
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.apply:
            if args.backup_dir is None:
                raise OpenCodeCrossSessionReconciliationError("backup_directory_required")
            result = apply_reconciliation(
                args.db,
                backup_dir=args.backup_dir,
            )
        else:
            result = inspect_reconciliation(args.db)
    except OpenCodeCrossSessionReconciliationError as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply" if args.apply else "dry_run",
            "db_path": str(args.db),
            "ok": False,
            "error_code": exc.code,
        }
    except (OSError, sqlite3.Error, ValueError):
        result = {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply" if args.apply else "dry_run",
            "db_path": str(args.db),
            "ok": False,
            "error_code": "reconciliation_failed",
        }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
