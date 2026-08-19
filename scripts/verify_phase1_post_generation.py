#!/usr/bin/env python3
"""Verify governance-dependent Phase 1 oracles after one generation is published."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from core.ops.durable_io import (
    DurableIOError,
    read_native_bytes,
    secure_atomic_write_bytes,
)
from scripts import generate_phase0_governance_contracts as governance
from scripts.generate_phase1_baseline_execution_evidence import _run_nodes
from scripts.phase1_governance_execution_validation import (
    _phase1_execution_covers,
    _phase1_execution_has_noncredit,
    _phase1_execution_has_valid_hre,
)

SCHEMA_VERSION = "mnemos.phase1_post_generation_verification.v1"
OUTPUT_PATH = (
    governance.ACCEPTANCE / "phase1_post_generation_verification.json"
)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_json(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = read_native_bytes(path)
        payload = json.loads(raw.decode("utf-8"))
    except (DurableIOError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise RuntimeError("phase1_post_generation_binding_unavailable") from None
    if not isinstance(payload, dict):
        raise RuntimeError("phase1_post_generation_binding_invalid")
    return payload, raw


def _post_generation_nodes() -> tuple[str, ...]:
    summary = governance.phase1_requirement_revalidation_summary()
    by_requirement = summary.get("post_generation_node_ids_by_requirement")
    if not isinstance(by_requirement, dict):
        raise RuntimeError("phase1_post_generation_denominator_invalid")
    nodes = tuple(
        str(node)
        for requirement_id in sorted(by_requirement)
        for node in by_requirement[requirement_id]
    )
    if (
        not nodes
        or len(nodes) != len(set(nodes))
        or summary.get("post_generation_exact_node_count") != len(nodes)
    ):
        raise RuntimeError("phase1_post_generation_denominator_invalid")
    return nodes


def _current_generation_binding() -> dict[str, Any]:
    ledger, ledger_bytes = _read_json(governance.PHASE1_LEDGER_PATH)
    record_id = str(governance.PHASE1_REVALIDATION_SEQUENCE[-1][1])
    record = ledger.get(record_id)
    if not isinstance(record, dict):
        raise RuntimeError("phase1_post_generation_record_missing")
    requirement_revalidation = governance.phase1_requirement_revalidation_summary()
    if record.get("requirement_revalidation") != requirement_revalidation:
        raise RuntimeError("phase1_post_generation_requirement_binding_stale")

    evidence_path = (
        governance.ACCEPTANCE
        / "phase1_historical_defect_execution_evidence.json"
    )
    evidence, evidence_bytes = _read_json(evidence_path)
    claimed_evidence_hash = evidence.get("evidence_hash")
    unsigned_evidence = dict(evidence)
    unsigned_evidence.pop("evidence_hash", None)
    verification = record.get("verification")
    if (
        evidence.get("schema_version")
        != "mnemos.phase1_historical_defect_execution_evidence.v4"
        or claimed_evidence_hash != governance._hash(unsigned_evidence)
        or not isinstance(verification, dict)
        or verification.get("phase1_execution_evidence_hash")
        != claimed_evidence_hash
    ):
        raise RuntimeError("phase1_post_generation_execution_binding_stale")

    artifacts = record.get("artifacts")
    if not isinstance(artifacts, dict):
        raise RuntimeError("phase1_post_generation_artifact_binding_missing")
    source_artifacts: list[dict[str, str]] = []
    for relative in sorted(
        {node.split("::", 1)[0] for node in _post_generation_nodes()}
    ):
        identity = governance._phase1_path_identity(relative)
        artifact = artifacts.get(relative.replace("/", "::"))
        if (
            identity.get("kind") != "file"
            or not isinstance(artifact, dict)
            or artifact.get("path") != relative
            or artifact.get("sha256") != identity.get("sha256")
        ):
            raise RuntimeError("phase1_post_generation_source_binding_stale")
        source_artifacts.append(
            {"path": relative, "sha256": str(identity["sha256"])}
        )

    independent_bytes = read_native_bytes(governance.INDEPENDENT_DENOMINATOR_PATH)
    manifest_bytes = read_native_bytes(
        governance.ACCEPTANCE / "cognitive_requirement_test_manifest.json"
    )
    return {
        "record_id": record_id,
        "record_sha256": governance._hash(record),
        "ledger_file_sha256": _sha256(ledger_bytes),
        "execution_evidence_hash": claimed_evidence_hash,
        "execution_evidence_file_sha256": _sha256(evidence_bytes),
        "independent_denominator_file_sha256": _sha256(independent_bytes),
        "requirement_manifest_file_sha256": _sha256(manifest_bytes),
        "requirement_revalidation_sha256": governance._hash(
            requirement_revalidation
        ),
        "post_generation_source_artifacts": source_artifacts,
    }


def _post_generation_denominator(
    binding: dict[str, Any],
) -> dict[str, Any]:
    owners = {
        requirement_id: list(nodes)
        for requirement_id, nodes in sorted(
            governance.PHASE1_POST_GENERATION_TEST_NODE_IDS_BY_ROOT.items()
        )
    }
    nodes = _post_generation_nodes()
    return {
        "selection_mode": "exact_current_generation_nodes",
        "execution_platform": "darwin",
        "owner_count": len(owners),
        "node_count": len(nodes),
        "node_ids": list(nodes),
        "node_ids_sha256": governance._hash(nodes),
        "node_ids_by_requirement": owners,
        "source_artifacts": binding["post_generation_source_artifacts"],
    }


def _execution_errors(
    execution: object,
    selected_nodes: tuple[str, ...],
) -> list[str]:
    if not isinstance(execution, dict):
        return ["post-generation execution is invalid"]
    outcomes = execution.get("outcomes")
    selected = set(selected_nodes)
    if not isinstance(outcomes, dict):
        return ["post-generation outcomes are invalid"]
    errors: list[str] = []
    if execution.get("exit_code") != 0:
        errors.append("post-generation execution did not exit zero")
    if outcomes.get("failed"):
        errors.append("post-generation execution contains failures")
    if _phase1_execution_has_noncredit(execution):
        errors.append("post-generation execution contains noncredit outcomes")
    if not _phase1_execution_covers(execution, selected):
        errors.append("post-generation execution does not cover the exact denominator")
    if not _phase1_execution_has_valid_hre(execution):
        errors.append("post-generation execution is not hermetic")
    if execution.get("executed_node_count") != len(outcomes.get("passed", ())):
        errors.append("post-generation executed count is inconsistent")
    if "diagnostic_output_tail" in execution:
        errors.append("post-generation green artifact contains diagnostic output")
    return errors


def validate_artifact(payload: object) -> list[str]:
    """Validate a persisted result against the current generated assets."""

    if not isinstance(payload, dict):
        return ["post-generation artifact is not an object"]
    errors: list[str] = []
    claimed_hash = payload.get("evidence_hash")
    unsigned = dict(payload)
    unsigned.pop("evidence_hash", None)
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("post-generation schema version mismatch")
    if claimed_hash != governance._hash(unsigned):
        errors.append("post-generation evidence hash mismatch")
    if payload.get("ok") is not True or payload.get("release_eligible") is not False:
        errors.append("post-generation closure boundary mismatch")
    if payload.get("production_boundary") != {
        "production_data_read_or_written": False,
        "production_mutation_performed": False,
        "full_quick_executed": False,
    }:
        errors.append("post-generation production boundary mismatch")
    try:
        binding = _current_generation_binding()
        denominator = _post_generation_denominator(binding)
        snapshot = governance._phase1_execution_snapshot()
    except (DurableIOError, OSError, RuntimeError):
        errors.append("post-generation current binding is unavailable")
        return errors
    if payload.get("generation_binding") != binding:
        errors.append("post-generation generation binding is stale")
    if payload.get("denominator") != denominator:
        errors.append("post-generation denominator is stale")
    if payload.get("source_snapshot") != snapshot:
        errors.append("post-generation source snapshot is stale")
    errors.extend(
        _execution_errors(
            payload.get("execution"),
            tuple(denominator["node_ids"]),
        )
    )
    governance_errors = governance.validate_assets(desktop_mode="required")
    if governance_errors:
        errors.append("post-generation generated assets are not current")
    return errors


def build_artifact() -> dict[str, Any]:
    """Run the exact governance-dependent denominator without production effects."""

    if sys.platform != "darwin":
        raise RuntimeError("phase1_post_generation_requires_darwin")
    if governance.validate_assets(desktop_mode="required"):
        raise RuntimeError("phase1_post_generation_assets_not_current")
    binding = _current_generation_binding()
    denominator = _post_generation_denominator(binding)
    selected_nodes = tuple(denominator["node_ids"])
    snapshot = governance._phase1_execution_snapshot()
    execution = _run_nodes(
        governance.ROOT,
        selected_nodes,
        execution_id=f"{binding['record_id']}-post-generation",
        snapshot_hash=str(snapshot["sha256"]),
    )
    if _execution_errors(execution, selected_nodes):
        raise RuntimeError("phase1_post_generation_execution_failed")
    if governance._phase1_execution_snapshot() != snapshot:
        raise RuntimeError("phase1_post_generation_source_drift")
    if _current_generation_binding() != binding:
        raise RuntimeError("phase1_post_generation_binding_drift")
    if governance.validate_assets(desktop_mode="required"):
        raise RuntimeError("phase1_post_generation_assets_drift")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "generation_binding": binding,
        "denominator": denominator,
        "source_snapshot": snapshot,
        "execution": execution,
        "production_boundary": {
            "production_data_read_or_written": False,
            "production_mutation_performed": False,
            "full_quick_executed": False,
        },
        "release_eligible": False,
    }
    payload["evidence_hash"] = governance._hash(payload)
    errors = validate_artifact(payload)
    if errors:
        raise RuntimeError(
            "phase1_post_generation_artifact_invalid:"
            + ",".join(sorted(errors))
        )
    return payload


def _publish_artifact(output: Path, artifact: dict[str, Any]) -> None:
    try:
        secure_atomic_write_bytes(
            output.parent,
            output.name,
            _canonical_bytes(artifact) + b"\n",
        )
    except DurableIOError as exc:
        raise RuntimeError(
            f"phase1_post_generation_publish_failed:{exc}"
        ) from None
    except OSError:
        raise RuntimeError(
            "phase1_post_generation_publish_failed:os_error"
        ) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args(argv)
    artifact = build_artifact()
    if args.write:
        _publish_artifact(args.output, artifact)
    print(
        json.dumps(
            {
                "ok": True,
                "written": args.write,
                "output": str(args.output),
                "record_id": artifact["generation_binding"]["record_id"],
                "node_count": artifact["denominator"]["node_count"],
                "evidence_hash": artifact["evidence_hash"],
                "release_eligible": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
