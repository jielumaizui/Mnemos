#!/usr/bin/env python3
"""Reconcile dangling current Raw-to-Observation provenance edges.

The normal Observation writer records an exact Raw provenance edge only after
the corresponding Observation row is durable.  Older replay generations may
contain edges pointing at discarded batch candidates.  This tool is deliberately
narrow: it considers only *current* canonical Raw revisions and deletes only
edges that the cognitive-readiness contract rejects.  Historical revision edges
and all valid current evidence remain untouched.

Dry-run is the default.  ``--apply`` requires a SQLite backup directory,
re-scans after the backup to fail closed on drift, uses one transaction for the
edge deletion and Raw metric repair, then verifies both integrity and zero
remaining current invalid edges.  Stop the daemon before applying it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config
from core.ops.cognitive_readiness_lineage import is_exact_current_raw_observation_edge
from core.sync_framework.raw_event_store import (
    CanonicalRawReadError,
    canonical_observation_text,
    iter_current_raw_turns_readonly,
)


SCHEMA_VERSION = "mnemos.observation_provenance_edge_reconcile.v1"


class ObservationProvenanceReconcileError(RuntimeError):
    """Raised when the narrow reconciliation cannot be proven safe."""


@dataclass(frozen=True)
class _InvalidEdge:
    edge_id: str
    logical_event_id: str


@dataclass(frozen=True)
class _Snapshot:
    current_revision_count: int
    current_revision_fingerprint: str
    current_observation_edge_count: int
    candidates: tuple[_InvalidEdge, ...]


_REQUIRED_RAW_COLUMNS = {
    "raw_turns": {"event_id", "current_revision_id"},
    "raw_turn_revisions": {"revision_id", "logical_event_id"},
    "raw_provenance_edges": {
        "edge_id",
        "source_revision_id",
        "span_start",
        "span_end",
        "consumer_type",
        "consumer_id",
    },
    "raw_metrics": {"event_id", "reference_count", "updated_at"},
}
_REQUIRED_OBSERVATION_COLUMNS = {"id", "source_type", "source_id"}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(values: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(values)).encode("utf-8")).hexdigest()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=30)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if exists is None:
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}  # nosec B608


def _require_schema(raw_db_path: Path, observation_db_path: Path) -> None:
    if not raw_db_path.is_file():
        raise ObservationProvenanceReconcileError("raw_events_database_missing")
    if not observation_db_path.is_file():
        raise ObservationProvenanceReconcileError("observations_database_missing")
    try:
        with _connect_read_only(raw_db_path) as raw_conn:
            for table, required_columns in _REQUIRED_RAW_COLUMNS.items():
                missing = required_columns - _table_columns(raw_conn, table)
                if missing:
                    raise ObservationProvenanceReconcileError(
                        f"raw_schema_missing_{table}_{','.join(sorted(missing))}"
                    )
        with _connect_read_only(observation_db_path) as observation_conn:
            missing = _REQUIRED_OBSERVATION_COLUMNS - _table_columns(
                observation_conn, "observations"
            )
            if missing:
                raise ObservationProvenanceReconcileError(
                    "observation_schema_missing_" + ",".join(sorted(missing))
                )
    except ObservationProvenanceReconcileError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise ObservationProvenanceReconcileError(
            f"schema_inspection_failed_{exc.__class__.__name__}"
        ) from None


def _current_raw_lengths(raw_db_path: Path) -> dict[str, tuple[str, int]]:
    """Return only revision identity and visible span length, never Raw text."""
    current: dict[str, tuple[str, int]] = {}
    try:
        for turn in iter_current_raw_turns_readonly(
            raw_db_path,
            include_structured_payload=False,
        ):
            visible_text = canonical_observation_text(
                {
                    "user_content": turn.user_content,
                    "assistant_content": turn.assistant_content,
                }
            )
            current[turn.revision_id] = (turn.logical_event_id, len(visible_text))
    except CanonicalRawReadError as exc:
        raise ObservationProvenanceReconcileError(
            f"canonical_raw_read_failed_{exc.__class__.__name__}"
        ) from None
    return current


def _scan(raw_db_path: Path, observation_db_path: Path) -> _Snapshot:
    _require_schema(raw_db_path, observation_db_path)
    current = _current_raw_lengths(raw_db_path)
    try:
        with _connect_read_only(observation_db_path) as observation_conn:
            observation_rows = observation_conn.execute(
                """
                SELECT id, source_id
                FROM observations
                WHERE LOWER(COALESCE(source_type, ''))='raw'
                """
            ).fetchall()
        observations_by_id = {
            str(row[0]): str(row[1] or "") for row in observation_rows if row[0]
        }
        with _connect_read_only(raw_db_path) as raw_conn:
            edge_rows = raw_conn.execute(
                """
                SELECT edge_id, source_revision_id, span_start, span_end, consumer_id
                FROM raw_provenance_edges
                WHERE consumer_type='observation'
                """
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        raise ObservationProvenanceReconcileError(
            f"provenance_scan_failed_{exc.__class__.__name__}"
        ) from None

    current_edge_count = 0
    candidates: list[_InvalidEdge] = []
    for edge_id, source_revision_id, span_start, span_end, consumer_id in edge_rows:
        revision_id = str(source_revision_id or "")
        current_target = current.get(revision_id)
        if current_target is None:
            # Old revisions are immutable audit history and intentionally out
            # of scope for the readiness denominator.
            continue
        current_edge_count += 1
        logical_event_id, visible_text_length = current_target
        try:
            start = int(span_start)
            end = int(span_end)
        except (TypeError, ValueError):
            start, end = -1, -1
        consumer_id_text = str(consumer_id or "")
        if not is_exact_current_raw_observation_edge(
            observation_source_id=observations_by_id.get(consumer_id_text),
            source_revision_id=revision_id,
            span_start=start,
            span_end=end,
            visible_text_length=visible_text_length,
        ):
            candidates.append(
                _InvalidEdge(edge_id=str(edge_id or ""), logical_event_id=logical_event_id)
            )

    if any(not item.edge_id or not item.logical_event_id for item in candidates):
        raise ObservationProvenanceReconcileError("candidate_identity_missing")
    candidates.sort(key=lambda item: item.edge_id)
    return _Snapshot(
        current_revision_count=len(current),
        current_revision_fingerprint=_fingerprint(list(current)),
        current_observation_edge_count=current_edge_count,
        candidates=tuple(candidates),
    )


def _report(snapshot: _Snapshot, *, mode: str, raw_db_path: Path, observation_db_path: Path) -> dict[str, Any]:
    candidate_ids = [candidate.edge_id for candidate in snapshot.candidates]
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "raw_events_db": str(raw_db_path),
        "observations_db": str(observation_db_path),
        "status": "reconciliation_required" if candidate_ids else "clean",
        "ok": True,
        "current_revision_count": snapshot.current_revision_count,
        "current_revision_fingerprint": snapshot.current_revision_fingerprint,
        "current_observation_edge_count": snapshot.current_observation_edge_count,
        "invalid_current_edge_count": len(candidate_ids),
        "invalid_current_edge_fingerprint": _fingerprint(candidate_ids),
    }


def inspect(
    raw_db_path: Path | str,
    observation_db_path: Path | str,
) -> dict[str, Any]:
    """Inspect current invalid edges without mutating either database."""
    raw_path = Path(raw_db_path).expanduser()
    observation_path = Path(observation_db_path).expanduser()
    try:
        snapshot = _scan(raw_path, observation_path)
    except ObservationProvenanceReconcileError as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "dry_run",
            "raw_events_db": str(raw_path),
            "observations_db": str(observation_path),
            "status": "blocked",
            "ok": False,
            "error": str(exc),
            "invalid_current_edge_count": 0,
        }
    return _report(
        snapshot,
        mode="dry_run",
        raw_db_path=raw_path,
        observation_db_path=observation_path,
    )


def _backup_sqlite(raw_db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / (
        f"{raw_db_path.stem}."
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}."
        "pre_observation_provenance_reconcile.sqlite"
    )
    try:
        with _connect_read_only(raw_db_path) as source, sqlite3.connect(str(target)) as target_conn:
            source.backup(target_conn)
            integrity = target_conn.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]) != "ok":
                raise ObservationProvenanceReconcileError("backup_integrity_check_failed")
        os.chmod(target, 0o600)
    except ObservationProvenanceReconcileError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise ObservationProvenanceReconcileError(
            f"sqlite_backup_failed_{exc.__class__.__name__}"
        ) from None
    return target


def _apply_exact_candidates(raw_db_path: Path, candidates: tuple[_InvalidEdge, ...]) -> dict[str, int]:
    if not candidates:
        return {"deleted_edges": 0, "recomputed_metrics": 0}
    try:
        with sqlite3.connect(str(raw_db_path), timeout=30) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    CREATE TEMP TABLE observation_provenance_reconcile_targets (
                        edge_id TEXT PRIMARY KEY,
                        logical_event_id TEXT NOT NULL
                    )
                    """
                )
                conn.executemany(
                    """
                    INSERT INTO observation_provenance_reconcile_targets
                    (edge_id, logical_event_id) VALUES (?, ?)
                    """,
                    [(item.edge_id, item.logical_event_id) for item in candidates],
                )
                selected = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM observation_provenance_reconcile_targets"
                    ).fetchone()[0]
                )
                if selected != len(candidates):
                    raise ObservationProvenanceReconcileError("candidate_set_changed")
                missing_metrics = int(
                    conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM (
                            SELECT DISTINCT logical_event_id
                            FROM observation_provenance_reconcile_targets
                        ) AS target
                        LEFT JOIN raw_metrics AS metrics
                          ON metrics.event_id=target.logical_event_id
                        WHERE metrics.event_id IS NULL
                        """
                    ).fetchone()[0]
                )
                if missing_metrics:
                    raise ObservationProvenanceReconcileError("raw_metrics_row_missing")
                deleted_edges = conn.execute(
                    """
                    DELETE FROM raw_provenance_edges
                    WHERE edge_id IN (
                        SELECT edge_id FROM observation_provenance_reconcile_targets
                    )
                    """
                ).rowcount
                if int(deleted_edges or 0) != len(candidates):
                    raise ObservationProvenanceReconcileError("candidate_edges_changed")
                recomputed_metrics = conn.execute(
                    """
                    UPDATE raw_metrics AS metrics
                    SET reference_count=(
                            SELECT COUNT(*)
                            FROM raw_provenance_edges AS edge
                            JOIN raw_turn_revisions AS revision
                              ON revision.revision_id=edge.source_revision_id
                            WHERE revision.logical_event_id=metrics.event_id
                        ),
                        updated_at=?
                    WHERE metrics.event_id IN (
                        SELECT DISTINCT logical_event_id
                        FROM observation_provenance_reconcile_targets
                    )
                    """,
                    (_utcnow(),),
                ).rowcount
                integrity = conn.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or str(integrity[0]) != "ok":
                    raise ObservationProvenanceReconcileError("post_apply_integrity_check_failed")
                conn.commit()
            except (ObservationProvenanceReconcileError, sqlite3.Error, OSError):
                conn.rollback()
                raise
    except ObservationProvenanceReconcileError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise ObservationProvenanceReconcileError(
            f"reconciliation_transaction_failed_{exc.__class__.__name__}"
        ) from None
    return {
        "deleted_edges": int(deleted_edges or 0),
        "recomputed_metrics": int(recomputed_metrics or 0),
    }


def reconcile(
    raw_db_path: Path | str,
    observation_db_path: Path | str,
    *,
    apply: bool,
    backup_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Dry-run or atomically delete only proven-invalid current edges."""
    raw_path = Path(raw_db_path).expanduser()
    observation_path = Path(observation_db_path).expanduser()
    before = _scan(raw_path, observation_path)
    report = _report(
        before,
        mode="apply" if apply else "dry_run",
        raw_db_path=raw_path,
        observation_db_path=observation_path,
    )
    report.update({"backup": "", "deleted_edges": 0, "recomputed_metrics": 0})
    if not apply:
        return report
    if backup_dir is None:
        raise ObservationProvenanceReconcileError("backup_directory_required")
    if not before.candidates:
        return report

    backup_path = _backup_sqlite(raw_path, Path(backup_dir).expanduser())
    rechecked = _scan(raw_path, observation_path)
    if rechecked != before:
        raise ObservationProvenanceReconcileError("state_changed_after_backup")
    applied = _apply_exact_candidates(raw_path, before.candidates)
    after = _scan(raw_path, observation_path)
    if after.current_revision_fingerprint != before.current_revision_fingerprint:
        raise ObservationProvenanceReconcileError("current_raw_changed_during_reconciliation")
    if after.candidates:
        raise ObservationProvenanceReconcileError("invalid_current_edges_remain")
    report.update(
        {
            "status": "clean",
            "backup": str(backup_path),
            "deleted_edges": applied["deleted_edges"],
            "recomputed_metrics": applied["recomputed_metrics"],
            "after": _report(
                after,
                mode="apply",
                raw_db_path=raw_path,
                observation_db_path=observation_path,
            ),
        }
    )
    return report


def _default_raw_db_path(config: Any) -> Path:
    configured = config.get("raw_event_store.db_path")
    return Path(configured or (Path(config.database_dir) / "raw_events.db")).expanduser()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-events-db", type=Path)
    parser.add_argument("--observations-db", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    config = get_config()
    raw_db_path = args.raw_events_db or _default_raw_db_path(config)
    observation_db_path = args.observations_db or (Path(config.database_dir) / "observations.db")
    try:
        result = reconcile(
            raw_db_path,
            observation_db_path,
            apply=args.apply,
            backup_dir=args.backup_dir,
        )
    except ObservationProvenanceReconcileError as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply" if args.apply else "dry_run",
            "status": "blocked",
            "ok": False,
            "error": str(exc),
        }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            "Observation provenance reconciliation: "
            f"status={result['status']} "
            f"invalid_current_edge_count={result.get('invalid_current_edge_count', 0)} "
            f"deleted_edges={result.get('deleted_edges', 0)}"
        )
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
