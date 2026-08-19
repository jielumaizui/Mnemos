#!/usr/bin/env python3
"""Audit ACL-first four-channel cognitive retrieval and live migration state."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.access_policy import validate_acl_envelope
from core.cognition_episode_contract import COGNITION_EPISODE_SCHEMA_VERSION
from core.cognitive.access_control import validate_cognitive_access_envelope
from core.cognitive.search_exclusion_ledger import (
    SEARCH_EXCLUSION_SCHEMA_VERSION,
    build_sqlite_exclusion_candidate,
    build_wiki_exclusion_candidate,
    load_search_exclusion_keys,
)
from core.cognitive.search_state_headers import inspect_state_search_headers
from core.frontmatter import normalize_frontmatter, read_frontmatter_only
from core.utils import EXCLUDED_DIRS
from scripts.cognitive_search_benchmark import (
    DEFAULT_FIXTURE,
    build_environment,
    evaluate_benchmark,
    load_fixture,
    scan_answer_leakage,
)

_TRUSTED_STATUSES = {
    "canonical_raw_index",
    "proven",
    "provenance_write",
    "server_principal",
}
_MISSING_REASONS = {
    "acl_metadata_missing",
    "acl_schema_unsupported",
    "acl_reconciliation_status_missing",
    "acl_scope_missing",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _load_exclusion_keys(
    exclusion_db: Path | None,
) -> tuple[set[bytes], dict[str, Any]]:
    if exclusion_db is None:
        return set(), {
            "schema_version": SEARCH_EXCLUSION_SCHEMA_VERSION,
            "schema_present": False,
            "row_count": 0,
            "ok": False,
        }
    keys, validation = load_search_exclusion_keys(exclusion_db)
    return set(keys), dict(validation)


def audit_wiki_acl(
    wiki_dir: Path,
    *,
    exclusion_db: Path | None = None,
) -> dict[str, Any]:
    """Classify active Wiki ACLs and count every unresolved quarantine."""

    result: dict[str, Any] = {
        "wiki_dir": str(wiki_dir),
        "page_count": 0,
        "active_page_count": 0,
        "restricted_unknown_page_count": 0,
        "historical_excluded_count": 0,
        "unresolved_restricted_unknown_count": 0,
        "quarantined_restricted_unknown_count": 0,
        "acl_metadata_missing": 0,
        "acl_reconciliation_required": 0,
        "acl_unknown": 0,
        "parse_error_count": 0,
        "source_changed_during_audit_count": 0,
        "reasons": {},
    }
    exclusion_keys, ledger_validation = _load_exclusion_keys(exclusion_db)
    result["exclusion_ledger"] = ledger_validation
    if not wiki_dir.is_dir():
        result["wiki_unavailable"] = True
        return result
    reasons: dict[str, int] = {}
    root = wiki_dir.expanduser().resolve(strict=False)
    for path in sorted(root.rglob("*.md")):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if path.is_symlink() or any(
            part in EXCLUDED_DIRS or part.startswith(".") for part in relative.parts
        ):
            continue
        result["page_count"] += 1
        try:
            before = path.stat()
            frontmatter = normalize_frontmatter(read_frontmatter_only(path, errors="strict"))
            source_row_hash = _sha256_file(path)
            after = path.stat()
        except (OSError, UnicodeError, ValueError):
            result["parse_error_count"] += 1
            continue
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if before_identity != after_identity:
            result["source_changed_during_audit_count"] += 1
            continue
        status = str(frontmatter.get("acl_reconciliation_status") or "").strip().lower()
        scope = str(frontmatter.get("scope") or "").strip().lower()
        schema = frontmatter.get("acl_schema_version")
        complete = frontmatter.get("acl_metadata_complete") is True
        if status == "restricted_unknown" and scope == "restricted":
            if schema == 1 and complete:
                result["restricted_unknown_page_count"] += 1
                candidate = build_wiki_exclusion_candidate(
                    wiki_dir=root,
                    relative_path=relative,
                    frontmatter=frontmatter,
                    source_row_hash=source_row_hash,
                )
                if candidate is not None and candidate.identity_key in exclusion_keys:
                    result["historical_excluded_count"] += 1
                    reasons["historical_acl_unavailable"] = (
                        reasons.get("historical_acl_unavailable", 0) + 1
                    )
                else:
                    result["unresolved_restricted_unknown_count"] += 1
                    result["quarantined_restricted_unknown_count"] += 1
                    result["acl_reconciliation_required"] += 1
                    result["acl_unknown"] += 1
                    reasons["restricted_unknown"] = reasons.get("restricted_unknown", 0) + 1
            else:
                result["acl_metadata_missing"] += 1
            continue
        decision = validate_acl_envelope(
            {"page_path": relative.as_posix(), "frontmatter": frontmatter}
        )
        reasons[decision.reason] = reasons.get(decision.reason, 0) + 1
        if decision.allowed:
            result["active_page_count"] += 1
            continue
        if decision.reason in _MISSING_REASONS or not complete or schema != 1:
            result["acl_metadata_missing"] += 1
        elif decision.reason == "acl_reconciliation_required" or status not in _TRUSTED_STATUSES:
            result["acl_reconciliation_required"] += 1
        else:
            result["acl_unknown"] += 1
    result["reasons"] = dict(sorted(reasons.items()))
    result["wiki_unavailable"] = False
    return result


def _read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.expanduser().resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=10,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _audit_acl_table(
    connection: sqlite3.Connection,
    *,
    db_path: Path,
    channel: str,
    table: str,
    id_column: str,
    exclusion_keys: set[bytes],
    where: str = "1 = 1",
) -> dict[str, int]:
    if table not in {
        "cognitive_relations",
        "canonical_nodes",
        "evidence_nodes",
        "evidence_edges",
    }:
        raise ValueError("unsupported cognitive ACL inventory table")
    total = 0
    valid = 0
    blank = 0
    malformed = 0
    historical_excluded = 0
    for row in connection.execute(
        f"SELECT * FROM {table} WHERE {where}"  # nosec B608 - fixed identifiers
    ):
        total += 1
        raw_acl = str(row["access_control"] or "").strip()
        if not raw_acl:
            blank += 1
            candidate = build_sqlite_exclusion_candidate(
                db_path=db_path,
                channel=channel,
                table=table,
                id_column=id_column,
                row=row,
            )
            if candidate is not None and candidate.identity_key in exclusion_keys:
                historical_excluded += 1
            continue
        try:
            validate_cognitive_access_envelope(json.loads(raw_acl))
            valid += 1
        except (TypeError, ValueError, json.JSONDecodeError):
            malformed += 1
    uncovered_blank = blank - historical_excluded
    return {
        "row_count": total,
        "valid_acl_count": valid,
        "blank_acl_count": blank,
        "historical_excluded_count": historical_excluded,
        "uncovered_blank_acl_count": uncovered_blank,
        "malformed_acl_count": malformed,
        "acl_unknown_count": uncovered_blank + malformed,
    }


def audit_store_inventory(
    *,
    state_db: Path,
    cognitive_graph_db: Path,
    evidence_graph_db: Path,
    exclusion_db: Path | None = None,
) -> dict[str, Any]:
    """Inspect live source stores without creating, repairing, or hydrating search results."""

    inventory: dict[str, Any] = {}
    exclusion_keys, ledger_validation = _load_exclusion_keys(exclusion_db)
    inventory["exclusion_ledger"] = ledger_validation
    if not state_db.is_file():
        inventory["state"] = {"unavailable": True, "ok": False}
    else:
        try:
            with _read_only_connection(state_db) as connection:
                headers = inspect_state_search_headers(connection)
                current_episode_count = int(connection.execute("""
                        SELECT COUNT(*)
                        FROM cognitive_state_heads AS h
                        JOIN cognitive_state_revisions AS r
                          ON r.revision_id=h.revision_id
                        WHERE r.object_type='cognition_episode'
                        """).fetchone()[0])
                legacy_episode_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM cognitive_state_heads AS h
                        JOIN cognitive_state_revisions AS r
                          ON r.revision_id=h.revision_id
                        WHERE r.object_type='cognition_episode'
                          AND COALESCE(
                            json_extract(r.payload_json, '$.schema_version'), ''
                          ) <> ?
                        """,
                        (COGNITION_EPISODE_SCHEMA_VERSION,),
                    ).fetchone()[0]
                )
            inventory["state"] = {
                "unavailable": False,
                "search_headers": headers,
                "current_cognition_episode_count": current_episode_count,
                "legacy_current_cognition_episode_count": legacy_episode_count,
                "ok": bool(headers.get("ok") and legacy_episode_count == 0),
            }
        except (OSError, ValueError, sqlite3.Error) as exc:
            inventory["state"] = {
                "unavailable": False,
                "ok": False,
                "error_category": type(exc).__name__,
            }

    graph_tables: dict[str, dict[str, int]] = {}
    if cognitive_graph_db.is_file():
        try:
            with _read_only_connection(cognitive_graph_db) as connection:
                graph_tables["cognitive_relations"] = _audit_acl_table(
                    connection,
                    db_path=cognitive_graph_db,
                    channel="cognitive_graph",
                    table="cognitive_relations",
                    id_column="id",
                    exclusion_keys=exclusion_keys,
                    where="stale = 0",
                )
                graph_tables["canonical_nodes"] = _audit_acl_table(
                    connection,
                    db_path=cognitive_graph_db,
                    channel="cognitive_graph",
                    table="canonical_nodes",
                    id_column="canonical_id",
                    exclusion_keys=exclusion_keys,
                )
            graph_unknown = sum(row["acl_unknown_count"] for row in graph_tables.values())
            inventory["cognitive_graph"] = {
                "unavailable": False,
                "tables": graph_tables,
                "historical_excluded_count": sum(
                    row["historical_excluded_count"] for row in graph_tables.values()
                ),
                "acl_unknown_count": graph_unknown,
                "ok": graph_unknown == 0,
            }
        except (OSError, ValueError, sqlite3.Error) as exc:
            inventory["cognitive_graph"] = {
                "unavailable": False,
                "ok": False,
                "error_category": type(exc).__name__,
            }
    else:
        inventory["cognitive_graph"] = {"unavailable": True, "ok": False}

    evidence_tables: dict[str, dict[str, int]] = {}
    if evidence_graph_db.is_file():
        try:
            with _read_only_connection(evidence_graph_db) as connection:
                evidence_tables["evidence_nodes"] = _audit_acl_table(
                    connection,
                    db_path=evidence_graph_db,
                    channel="evidence_graph",
                    table="evidence_nodes",
                    id_column="id",
                    exclusion_keys=exclusion_keys,
                )
                evidence_tables["evidence_edges"] = _audit_acl_table(
                    connection,
                    db_path=evidence_graph_db,
                    channel="evidence_graph",
                    table="evidence_edges",
                    id_column="id",
                    exclusion_keys=exclusion_keys,
                )
            evidence_unknown = sum(row["acl_unknown_count"] for row in evidence_tables.values())
            inventory["evidence_graph"] = {
                "unavailable": False,
                "tables": evidence_tables,
                "historical_excluded_count": sum(
                    row["historical_excluded_count"] for row in evidence_tables.values()
                ),
                "acl_unknown_count": evidence_unknown,
                "ok": evidence_unknown == 0,
            }
        except (OSError, ValueError, sqlite3.Error) as exc:
            inventory["evidence_graph"] = {
                "unavailable": False,
                "ok": False,
                "error_category": type(exc).__name__,
            }
    else:
        inventory["evidence_graph"] = {"unavailable": True, "ok": False}

    inventory["ok"] = all(
        bool(inventory[channel].get("ok"))
        for channel in ("state", "cognitive_graph", "evidence_graph")
    )
    return inventory


def runtime_channel_population_report(
    wiki_acl: Mapping[str, Any],
    store_inventory: Mapping[str, Any],
) -> dict[str, Any]:
    """Expose the minimum live population needed for four-channel claims."""

    graph_tables = store_inventory.get("cognitive_graph", {}).get("tables", {})
    evidence_tables = store_inventory.get("evidence_graph", {}).get("tables", {})
    counts = {
        "wiki_page": int(wiki_acl.get("active_page_count", 0) or 0),
        "cognitive_state": int(
            store_inventory.get("state", {}).get(
                "current_cognition_episode_count",
                0,
            )
            or 0
        ),
        "cognitive_graph": sum(
            int(row.get("valid_acl_count", 0) or 0)
            for row in graph_tables.values()
            if isinstance(row, Mapping)
        ),
        "evidence_graph": sum(
            int(row.get("valid_acl_count", 0) or 0)
            for row in evidence_tables.values()
            if isinstance(row, Mapping)
        ),
    }
    missing_channels = sorted(channel for channel, count in counts.items() if count <= 0)
    return {
        "counts": counts,
        "minimum_population_ready": not missing_channels,
        "missing_channels": missing_channels,
        "claim_boundary": (
            "minimum population only; benchmark metrics remain hermetic and do not "
            "certify production traffic"
        ),
    }


def strict_failures(report: Mapping[str, Any]) -> list[str]:
    benchmark = report["benchmark"]
    leakage = report["answer_leakage"]
    failures: list[str] = []
    checks = (
        (int(benchmark["query_count"]) >= 30, "benchmark_query_count_below_30"),
        (int(benchmark["holdout_query_count"]) >= 24, "holdout_query_count_below_24"),
        (float(benchmark["critical_recall_at_5"]) == 1.0, "critical_recall_at_5"),
        (float(benchmark["recall_at_10"]) >= 0.95, "recall_at_10"),
        (float(benchmark["mrr"]) >= 0.90, "mrr"),
        (int(benchmark["unauthorized_hit_count"]) == 0, "unauthorized_hit_count"),
        (int(benchmark["field_trace_gap"]) == 0, "field_trace_gap"),
        (int(benchmark["source_trace_gap"]) == 0, "source_trace_gap"),
        (int(benchmark["current_revision_gap"]) == 0, "current_revision_gap"),
        (
            int(benchmark["superseded_belief_hit_count"]) == 0,
            "superseded_belief_hit_count",
        ),
        (benchmark["query_order_invariant"] is True, "query_order_invariant"),
        (int(leakage["answer_leakage_count"]) == 0, "answer_leakage_count"),
    )
    failures.extend(code for passed, code in checks if not passed)

    acl = report["wiki_acl"]
    expected_quarantine = int(report.get("expected_fixture_quarantine_count", 0))
    acl_checks = (
        (not bool(acl.get("wiki_unavailable")), "wiki_unavailable"),
        (int(acl["parse_error_count"]) == 0, "wiki_acl_parse_error"),
        (
            int(acl["source_changed_during_audit_count"]) == 0,
            "wiki_source_changed_during_audit",
        ),
        (int(acl["acl_metadata_missing"]) == 0, "acl_metadata_missing"),
        (
            int(acl["quarantined_restricted_unknown_count"]) == expected_quarantine,
            "restricted_unknown_quarantine",
        ),
        (
            int(acl["acl_reconciliation_required"]) == expected_quarantine,
            "acl_reconciliation_required",
        ),
        (int(acl["acl_unknown"]) == expected_quarantine, "acl_unknown"),
    )
    failures.extend(code for passed, code in acl_checks if not passed)

    stores = report["store_inventory"]
    expected_store_unknown = report.get("expected_fixture_store_acl_unknown", {})
    store_checks = (
        (bool(stores.get("state", {}).get("ok")), "state_inventory"),
        (
            not bool(stores.get("cognitive_graph", {}).get("unavailable"))
            and int(stores.get("cognitive_graph", {}).get("acl_unknown_count", -1))
            == int(expected_store_unknown.get("cognitive_graph", 0)),
            "cognitive_graph_inventory",
        ),
        (
            not bool(stores.get("evidence_graph", {}).get("unavailable"))
            and int(stores.get("evidence_graph", {}).get("acl_unknown_count", -1))
            == int(expected_store_unknown.get("evidence_graph", 0)),
            "evidence_graph_inventory",
        ),
    )
    failures.extend(code for passed, code in store_checks if not passed)
    if report.get("mode") == "production":
        if not bool(report.get("runtime_channel_population", {}).get("minimum_population_ready")):
            failures.append("runtime_channel_population")
        historical_excluded = int(acl.get("historical_excluded_count", 0)) + sum(
            int(stores.get(channel, {}).get("historical_excluded_count", 0))
            for channel in ("cognitive_graph", "evidence_graph")
        )
        ledger_validation = stores.get("exclusion_ledger", {})
        if historical_excluded and not bool(ledger_validation.get("ok")):
            failures.append("search_exclusion_ledger")
    return failures


def run_audit(
    *,
    fixture_path: Path = DEFAULT_FIXTURE,
    production: bool = False,
    wiki_dir: Path | None = None,
    state_db: Path | None = None,
    cognitive_graph_db: Path | None = None,
    evidence_graph_db: Path | None = None,
    exclusion_db: Path | None = None,
) -> dict[str, Any]:
    fixture = load_fixture(fixture_path)
    with tempfile.TemporaryDirectory(prefix="mnemos-cognitive-search-audit-") as temp_dir:
        environment = build_environment(Path(temp_dir) / "benchmark", fixture)
        benchmark = evaluate_benchmark(fixture, environment)
        hermetic_wiki_acl = audit_wiki_acl(environment.wiki_dir)
        hermetic_inventory = audit_store_inventory(
            state_db=environment.state_db,
            cognitive_graph_db=environment.cognitive_graph_db,
            evidence_graph_db=environment.evidence_graph_db,
        )

    expected_quarantine = sum(
        1
        for case in fixture["negative_cases"]
        if case.get("channel") == "wiki_page" and case.get("acl_mode") == "unknown"
    )
    expected_store_unknown = {
        channel: sum(
            1
            for case in fixture["negative_cases"]
            if case.get("channel") == channel and case.get("acl_mode") == "unknown"
        )
        for channel in ("cognitive_graph", "evidence_graph")
    }
    if production:
        if any(
            value is None for value in (wiki_dir, state_db, cognitive_graph_db, evidence_graph_db)
        ):
            from core.config import get_config

            config = get_config()
            database_dir = Path(config.database_dir)
            wiki_dir = Path(wiki_dir or config.wiki_dir)
            state_db = Path(state_db or database_dir / "producer_consumer_ledger.db")
            cognitive_graph_db = Path(cognitive_graph_db or database_dir / "cognitive_graph.db")
            evidence_graph_db = Path(evidence_graph_db or database_dir / "evidence_graph.db")
        assert wiki_dir is not None
        assert state_db is not None
        assert cognitive_graph_db is not None
        assert evidence_graph_db is not None
        if exclusion_db is None:
            exclusion_db = Path(state_db).parent / "cognitive_search_exclusions.db"
        wiki_acl = audit_wiki_acl(Path(wiki_dir), exclusion_db=Path(exclusion_db))
        store_inventory = audit_store_inventory(
            state_db=Path(state_db),
            cognitive_graph_db=Path(cognitive_graph_db),
            evidence_graph_db=Path(evidence_graph_db),
            exclusion_db=Path(exclusion_db),
        )
        expected_quarantine = 0
        expected_store_unknown = {"cognitive_graph": 0, "evidence_graph": 0}
    else:
        wiki_acl = hermetic_wiki_acl
        store_inventory = hermetic_inventory

    report: dict[str, Any] = {
        "schema_version": "mnemos.cognitive_search_gate.v2",
        "mode": "production" if production else "hermetic",
        "benchmark": benchmark,
        "answer_leakage": scan_answer_leakage(fixture, repo_root=ROOT),
        "wiki_acl": wiki_acl,
        "store_inventory": store_inventory,
        "runtime_channel_population": runtime_channel_population_report(
            wiki_acl,
            store_inventory,
        ),
        "expected_fixture_quarantine_count": expected_quarantine,
        "expected_fixture_store_acl_unknown": expected_store_unknown,
    }
    failures = strict_failures(report)
    report["failures"] = failures
    report["ok"] = not failures
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--wiki-dir", type=Path)
    parser.add_argument("--state-db", type=Path)
    parser.add_argument("--cognitive-graph-db", type=Path)
    parser.add_argument("--evidence-graph-db", type=Path)
    parser.add_argument("--exclusion-db", type=Path)
    args = parser.parse_args(argv)
    explicit_paths = (
        args.wiki_dir,
        args.state_db,
        args.cognitive_graph_db,
        args.evidence_graph_db,
        args.exclusion_db,
    )
    if not args.production and any(value is not None for value in explicit_paths):
        parser.error("live store paths require --production")
    report = run_audit(
        fixture_path=args.fixture,
        production=args.production,
        wiki_dir=args.wiki_dir,
        state_db=args.state_db,
        cognitive_graph_db=args.cognitive_graph_db,
        evidence_graph_db=args.evidence_graph_db,
        exclusion_db=args.exclusion_db,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "cognitive search: "
            f"mode={report['mode']} "
            f"Recall@10={report['benchmark']['recall_at_10']:.3f} "
            f"MRR={report['benchmark']['mrr']:.3f} "
            f"ACL violations={report['benchmark']['unauthorized_hit_count']} "
            f"status={'PASS' if report['ok'] else 'FAIL'}"
        )
    return 1 if args.strict and not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
