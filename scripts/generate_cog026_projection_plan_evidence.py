#!/usr/bin/env python3
"""Generate a content-free COG-026 production projection-plan artifact."""

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
from scripts.project_raw_vault import plan_projection


SCHEMA_VERSION = "mnemos.cog026_projection_plan_evidence.v2"
ARCHIVE_ENCODING = "gzip+base64(canonical-json;path-identities-redacted-v1)"
_PLAN_PATH_IDENTITIES = {
    "canonical_db": "${MNEMOS_CANONICAL_RAW_DB}",
    "raw_dir": "${MNEMOS_RAW_VAULT}",
    "backup_dir": "${MNEMOS_RAW_PROJECTION_BACKUP_DIR}",
}


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def redact_plan_path_identities(plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Return a replayable template without persisting machine-local paths."""
    redacted = dict(plan)
    path_identity_hashes: dict[str, str] = {}
    for field, identity in _PLAN_PATH_IDENTITIES.items():
        value = plan.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"projection plan {field} path identity is missing")
        path_identity_hashes[field] = _sha256_bytes(value.encode("utf-8"))
        redacted[field] = identity
    return redacted, path_identity_hashes


def build_artifact(args: argparse.Namespace) -> dict[str, Any]:
    try:
        snapshot_sha256 = regular_file_sha256(args.db_path)
    except (DurableIOError, OSError):
        raise ValueError("checkpointed snapshot is unsafe or unreadable") from None
    if snapshot_sha256 != args.expected_snapshot_sha256:
        raise ValueError("checkpointed snapshot hash does not match expectation")
    source, _chunks, stats = plan_projection(args)
    try:
        plan = stats["projection_plan"]
        source.assert_epoch_current()
        try:
            snapshot_after = regular_file_sha256(args.db_path)
        except (DurableIOError, OSError):
            raise ValueError(
                "checkpointed snapshot changed during plan generation"
            ) from None
        if snapshot_after != snapshot_sha256:
            raise ValueError("checkpointed snapshot changed during plan generation")
        observed_counts = {
            "candidate_turns": int(stats["candidate_turns"]),
            "projected_files": int(stats["projected_files"]),
            "changed_paths": len(plan["changed_paths"]),
            "stale_paths": len(plan["stale_paths"]),
            "index_changed_paths": len(plan["index_changed_paths"]),
            "index_deleted_paths": len(plan["index_deleted_paths"]),
        }
        expected_counts = {
            "candidate_turns": args.expected_candidate_turns,
            "projected_files": args.expected_projected_files,
            "changed_paths": args.expected_changed_paths,
            "stale_paths": args.expected_stale_paths,
            "index_changed_paths": args.expected_index_changed_paths,
            "index_deleted_paths": args.expected_index_deleted_paths,
        }
        if observed_counts != expected_counts:
            raise ValueError(
                "production projection plan changed: "
                f"observed={observed_counts!r}, expected={expected_counts!r}"
            )
        plan_bytes = _canonical_json_bytes(plan)
        redacted_plan, path_identity_hashes = redact_plan_path_identities(plan)
        archive_plan_bytes = _canonical_json_bytes(redacted_plan)
        plan_archive_decoded_sha256 = _sha256_bytes(archive_plan_bytes)
        expected_hashes = {
            "plan_hash": args.expected_plan_hash,
            "generation_hash": args.expected_generation_hash,
            "desired_index_generation_hash": (
                args.expected_desired_index_generation_hash
            ),
            "plan_archive_decoded_sha256": (
                args.expected_plan_archive_decoded_sha256
            ),
        }
        observed_hashes = {
            "plan_hash": plan["plan_hash"],
            "generation_hash": plan["generation_hash"],
            "desired_index_generation_hash": plan[
                "desired_index_generation_hash"
            ],
            "plan_archive_decoded_sha256": plan_archive_decoded_sha256,
        }
        if observed_hashes != expected_hashes:
            raise ValueError(
                "reviewed production projection plan changed: "
                f"observed={observed_hashes!r}, expected={expected_hashes!r}"
            )
        archive = gzip.compress(archive_plan_bytes, mtime=0)
        return {
            "schema_version": SCHEMA_VERSION,
            "source_snapshot_db_sha256": snapshot_sha256,
            "source_snapshot_path_persisted": False,
            "path_identities": dict(_PLAN_PATH_IDENTITIES),
            "path_identity_hashes": path_identity_hashes,
            "projection_contract": plan["projection_contract"],
            "plan_schema_version": plan["schema_version"],
            "plan_hash": plan["plan_hash"],
            "exact_plan_payload_sha256": _sha256_bytes(plan_bytes),
            "generation_hash": plan["generation_hash"],
            "source_epoch_hash": plan["source_epoch_hash"],
            "source_revision_set_hash": plan["source_revision_set_hash"],
            "desired_index_generation_hash": plan["desired_index_generation_hash"],
            "counts": observed_counts,
            "write_set_empty": plan["write_set_empty"],
            "apply_performed": False,
            "production_mutation": False,
            "plan_archive": {
                "encoding": ARCHIVE_ENCODING,
                "decoded_sha256": plan_archive_decoded_sha256,
                "replay_contract": (
                    "replace the three declared path identities, require their "
                    "sha256 hashes, then require exact_plan_payload_sha256 and plan_hash"
                ),
                "payload_base64": base64.b64encode(archive).decode("ascii"),
            },
        }
    finally:
        source.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--canonical-db-identity", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--expected-snapshot-sha256", required=True)
    parser.add_argument("--expected-candidate-turns", type=int, required=True)
    parser.add_argument("--expected-projected-files", type=int, required=True)
    parser.add_argument("--expected-changed-paths", type=int, required=True)
    parser.add_argument("--expected-stale-paths", type=int, required=True)
    parser.add_argument("--expected-index-changed-paths", type=int, required=True)
    parser.add_argument("--expected-index-deleted-paths", type=int, required=True)
    parser.add_argument("--expected-plan-hash", required=True)
    parser.add_argument("--expected-generation-hash", required=True)
    parser.add_argument("--expected-desired-index-generation-hash", required=True)
    parser.add_argument("--expected-plan-archive-decoded-sha256", required=True)
    parser.add_argument("--max-files", type=int, default=0)
    parser.add_argument("--chunk-turns", type=int, default=5)
    parser.add_argument("--max-turn-chars", type=int, default=0)
    parser.add_argument("--include-eligible-delete", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--write", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    artifact = build_artifact(args)
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
                "plan_hash": artifact["plan_hash"],
                "generation_hash": artifact["generation_hash"],
                "plan_archive_decoded_sha256": artifact["plan_archive"][
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
