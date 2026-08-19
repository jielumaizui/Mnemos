#!/usr/bin/env python3
"""Generate the content-free COG-009 projection gap evidence artifact."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ops.durable_io import DurableIOError, regular_file_sha256
from scripts.audit_raw_projection_fidelity import audit_raw_projection_fidelity


SCHEMA_VERSION = "mnemos.cog009_raw_projection_gap_evidence.v3"
ARCHIVE_ENCODING = "gzip+base64(canonical-json)"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_artifact(
    *,
    raw_dir: Path,
    db_path: Path,
    canonical_db_identity: Path,
    expected_snapshot_sha256: str,
    expected_missing: int,
    expected_unexpected: int,
    expected_structural_errors: int,
    expected_reference_mismatches: int,
    expected_metric_aggregate_mismatches: int,
    expected_gap_hash: str,
    expected_evidence_decoded_sha256: str,
) -> dict[str, Any]:
    try:
        snapshot_sha256 = regular_file_sha256(db_path)
    except (DurableIOError, OSError):
        raise ValueError("checkpointed snapshot is unsafe or unreadable") from None
    if snapshot_sha256 != expected_snapshot_sha256:
        raise ValueError("checkpointed snapshot hash does not match expectation")
    report = audit_raw_projection_fidelity(
        raw_dir=raw_dir,
        db_path=db_path,
        canonical_db_identity=canonical_db_identity,
        include_gap_evidence=True,
    )
    expected_counts = {
        "missing_event_ids": expected_missing,
        "unexpected_event_ids": expected_unexpected,
        "structural_error_count": expected_structural_errors,
    }
    for field, expected in expected_counts.items():
        if report.get(field) != expected:
            raise ValueError(
                f"production evidence changed: {field}={report.get(field)!r}, "
                f"expected {expected}"
            )
    if report["gap_generation"].get("evidence_epoch_stable") is not True:
        raise ValueError("projection evidence epoch changed during generation")
    try:
        snapshot_after = regular_file_sha256(db_path)
    except (DurableIOError, OSError):
        raise ValueError("checkpointed snapshot changed during evidence generation") from None
    if snapshot_after != snapshot_sha256:
        raise ValueError("checkpointed snapshot changed during evidence generation")
    if (
        report.get("projection_reference_mismatch_count")
        != expected_reference_mismatches
        or report.get("projection_metric_aggregate_mismatch_count")
        != expected_metric_aggregate_mismatches
    ):
        raise ValueError(
            "production projection mismatch denominator changed: "
            f"reference={report.get('projection_reference_mismatch_count')}, "
            "metric_aggregate="
            f"{report.get('projection_metric_aggregate_mismatch_count')}"
        )
    if report["gap_generation"].get("gap_hash") != expected_gap_hash:
        raise ValueError(
            "production projection gap hash changed: "
            f"{report['gap_generation'].get('gap_hash')}"
        )
    evidence = {
        "missing_revision_evidence": report["missing_revision_evidence"],
        "projection_reference_mismatch_evidence": report[
            "projection_reference_mismatch_evidence"
        ],
        "projection_metric_aggregate_mismatch_evidence": report[
            "projection_metric_aggregate_mismatch_evidence"
        ],
        "structural_error_evidence": report["structural_error_evidence"],
        "unexpected_revision_evidence": report["unexpected_revision_evidence"],
    }
    decoded = _canonical_json_bytes(evidence)
    decoded_sha256 = _sha256_bytes(decoded)
    if decoded_sha256 != expected_evidence_decoded_sha256:
        raise ValueError(
            "production projection evidence archive changed: "
            f"{decoded_sha256}"
        )
    archive = gzip.compress(decoded, mtime=0)
    counts = {
        field: report[field]
        for field in (
            "expected_event_ids",
            "observed_event_ids",
            "missing_event_ids",
            "unexpected_event_ids",
            "paired_superseded_revision_count",
            "unpaired_superseded_revision_count",
            "logical_event_id_mismatch_count",
            "projection_metadata_mismatch_count",
            "projection_reference_mismatch_count",
            "projection_metric_aggregate_mismatch_count",
            "field_hash_mismatch_count",
            "truncated_marker_files",
            "structural_error_count",
            "error_count",
        )
        if field in report
    }
    counts["paired_superseded_revision_count"] = report["gap_generation"][
        "paired_superseded_revision_count"
    ]
    counts["unpaired_superseded_revision_count"] = report["gap_generation"][
        "unpaired_superseded_revision_count"
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "source_audit_schema": report["schema_version"],
        "source_snapshot_db_sha256": snapshot_sha256,
        "gap_generation": report["gap_generation"],
        "counts": counts,
        "evidence_archive": {
            "encoding": ARCHIVE_ENCODING,
            "decoded_sha256": decoded_sha256,
            "payload_base64": base64.b64encode(archive).decode("ascii"),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--canonical-db-identity", type=Path, required=True)
    parser.add_argument("--expected-snapshot-sha256", required=True)
    parser.add_argument("--expected-missing", type=int, required=True)
    parser.add_argument("--expected-unexpected", type=int, required=True)
    parser.add_argument("--expected-structural-errors", type=int, required=True)
    parser.add_argument("--expected-reference-mismatches", type=int, required=True)
    parser.add_argument(
        "--expected-metric-aggregate-mismatches", type=int, required=True
    )
    parser.add_argument("--expected-gap-hash", required=True)
    parser.add_argument("--expected-evidence-decoded-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--write", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    artifact = build_artifact(
        raw_dir=args.raw_dir,
        db_path=args.db_path,
        canonical_db_identity=args.canonical_db_identity,
        expected_snapshot_sha256=args.expected_snapshot_sha256,
        expected_missing=args.expected_missing,
        expected_unexpected=args.expected_unexpected,
        expected_structural_errors=args.expected_structural_errors,
        expected_reference_mismatches=args.expected_reference_mismatches,
        expected_metric_aggregate_mismatches=(
            args.expected_metric_aggregate_mismatches
        ),
        expected_gap_hash=args.expected_gap_hash,
        expected_evidence_decoded_sha256=args.expected_evidence_decoded_sha256,
    )
    if args.write:
        args.output.write_text(
            _canonical_json_bytes(artifact).decode("utf-8") + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "ok": True,
                "written": bool(args.write),
                "output": str(args.output),
                "gap_hash": artifact["gap_generation"]["gap_hash"],
                "decoded_sha256": artifact["evidence_archive"][
                    "decoded_sha256"
                ],
                "counts": artifact["counts"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
