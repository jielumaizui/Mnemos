"""Independent, read-only reference proof for cognitive consolidation effects.

This verifier intentionally does not import the readiness lineage reducer or the
consolidator.  It recomputes the frozen candidate denominator from the run
ledger, binds each candidate to the canonical current Raw revision, and checks
that a receipt names a real immutable Wiki target with the recorded content
hash.  A producer's receipt alone is therefore never completion evidence.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "mnemos.cognitive_readiness_reference.v1"


def build_consolidation_reference_audit(
    *, database_dir: Path, wiki_dir: Path
) -> dict[str, Any]:
    """Recompute immutable candidate-to-target evidence without producer code."""
    db_path = Path(database_dir) / "cognitive_consolidation.db"
    raw_path = Path(database_dir) / "raw_events.db"
    required = {
        "consolidation_runs": {"run_id", "applied", "report_json"},
        "consolidation_coverage_receipts": {
            "run_id", "source_revision_id", "source_content_hash", "exact_source_ref",
            "covered_by", "method_content_hash", "mutation_id",
        },
        "raw_turns": {"current_revision_id", "content_hash"},
    }
    schemas = {
        "consolidation_runs": _columns(db_path, "consolidation_runs"),
        "consolidation_coverage_receipts": _columns(db_path, "consolidation_coverage_receipts"),
        "raw_turns": _columns(raw_path, "raw_turns"),
    }
    missing = sorted(name for name, columns in required.items() if not columns <= schemas[name])
    if missing:
        return _result(
            expected={}, covered=set(), invalid=0, duplicates=0, missing_tables=missing
        )

    with _connect_ro(raw_path) as conn:
        raw_hashes = {
            str(row[0]): str(row[1])
            for row in conn.execute(
                "SELECT current_revision_id, content_hash FROM raw_turns"
            ).fetchall()
            if row[0] and row[1]
        }
    expected: dict[tuple[str, str], tuple[str, str]] = {}
    canonical: dict[str, tuple[str, str]] = {}
    invalid = 0
    with _connect_ro(db_path) as conn:
        for run_id, report_json in conn.execute(
            "SELECT run_id, report_json FROM consolidation_runs WHERE COALESCE(applied, 0) != 0"
        ).fetchall():
            try:
                candidates = json.loads(str(report_json))["coverage"]["candidate_dispositions"]
            except (TypeError, ValueError, KeyError):
                invalid += 1
                continue
            if not isinstance(candidates, list):
                invalid += 1
                continue
            for item in candidates:
                if not isinstance(item, dict):
                    invalid += 1
                    continue
                ref = str(item.get("exact_source_ref") or "")
                revision = str(item.get("source_revision_id") or "")
                content_hash = str(item.get("source_content_hash") or "")
                if (
                    not ref
                    or not revision
                    or not content_hash
                    or ref != f"raw-revision:{revision}"
                    or raw_hashes.get(revision) != content_hash
                ):
                    invalid += 1
                    continue
                identity = (revision, content_hash)
                expected[(str(run_id), ref)] = identity
                prior = canonical.setdefault(ref, identity)
                if prior != identity:
                    invalid += 1

        covered: set[str] = set()
        receipt_counts: dict[str, int] = {}
        for row in conn.execute(
            """
            SELECT run_id, source_revision_id, source_content_hash, exact_source_ref,
                   covered_by, method_content_hash, mutation_id
            FROM consolidation_coverage_receipts
            """
        ).fetchall():
            run_id, revision, content_hash, ref, target, target_hash, mutation_id = map(
                lambda value: str(value or ""), row
            )
            if (
                expected.get((run_id, ref)) != (revision, content_hash)
                or not mutation_id
                or not _target_matches(wiki_dir, target, target_hash)
            ):
                invalid += 1
                continue
            covered.add(ref)
            receipt_counts[ref] = receipt_counts.get(ref, 0) + 1
    duplicates = sum(max(0, count - 1) for count in receipt_counts.values())
    return _result(
        expected=canonical,
        covered=covered,
        invalid=invalid,
        duplicates=duplicates,
        missing_tables=[],
    )


def _result(
    *,
    expected: dict[str, tuple[str, str]],
    covered: set[str],
    invalid: int,
    duplicates: int,
    missing_tables: list[str],
) -> dict[str, Any]:
    snapshot = [
        {"exact_source_ref": ref, "source_revision_id": pair[0], "source_content_hash": pair[1]}
        for ref, pair in sorted(expected.items())
    ]
    snapshot_hash = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    denominator = len(expected)
    covered_count = len(covered.intersection(expected))
    uncovered = max(0, denominator - covered_count)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not missing_tables and denominator > 0 and uncovered == 0 and invalid == 0 and duplicates == 0,
        "candidate_denominator": denominator,
        "covered": covered_count,
        "uncovered": uncovered,
        "invalid_proof_count": invalid,
        "duplicate_receipt_count": duplicates,
        "missing_tables": missing_tables,
        "snapshot": snapshot,
        "snapshot_hash": snapshot_hash,
    }


def _target_matches(wiki_dir: Path, target: str, expected_hash: str) -> bool:
    if not target or not expected_hash:
        return False
    root = Path(wiki_dir).resolve()
    candidate = (root / target).resolve()
    if candidate != root and root not in candidate.parents:
        return False
    try:
        return candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest() == expected_hash
    except OSError:
        return False


def _columns(path: Path, table: str) -> set[str]:
    try:
        with _connect_ro(path) as conn:
            return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
