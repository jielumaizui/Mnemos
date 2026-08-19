"""Private implementation module for successor_d0_verification.runner."""

from __future__ import annotations

from dataclasses import asdict

from pathlib import Path

from typing import Any

from typing import Mapping

from typing import Sequence

import argparse

import json

import subprocess

import tarfile

import tempfile

from .closure import (
    _independent_closure_counts,
    _verify_edges,
    _verify_global_identity,
    _verify_independent_census,
    _verify_snapshot_evidence,
)

from .snapshot import (
    _materialize_snapshot,
    _verify_bindings,
    _verify_config_snapshot,
    _verify_generator_identity,
    _verify_snapshot,
)

from .wire import (
    ARTIFACT_ORDER,
    CANONICALIZATION_CONTRACT,
    CLOSURE_COUNT_FIELDS,
    CLOSURE_FIELDS,
    CLOSURE_SCHEMA,
    CLOSURE_SUPPLEMENTAL_COUNT_FIELDS,
    DEFAULT_BUNDLE,
    Finding,
    INTEGRITY_CODES,
    INVENTORY_METRIC_FIELDS,
    MAIN_CLI_METRIC_FIELDS,
    MANIFEST_SCHEMA,
    MANIFEST_FIELDS,
    MAX_EXTERNAL_BINDING_BYTES,
    MAX_MANIFEST_BYTES,
    MCP_METRIC_FIELDS,
    REPORT_SCHEMA,
    REQUIRED_ZERO_FIELDS,
    ROOT,
    _VerifierResourceLimit,
    _canonical_json_bytes,
    _finding,
    _json_loads,
    _read_bounded_regular_file,
    _read_artifact,
    _sha256,
    _verify_manifest_findings,
)

_VERIFIER_IMPLEMENTATION_PATHS = tuple(
    sorted(
        {
            "scripts/audit_successor_d0_catalog.py",
            "scripts/successor_d0_verifier.py",
            "scripts/successor_d0_verification/__init__.py",
            "scripts/successor_d0_verification/census.py",
            "scripts/successor_d0_verification/closure.py",
            "scripts/successor_d0_verification/runner.py",
            "scripts/successor_d0_verification/snapshot.py",
            "scripts/successor_d0_verification/wire.py",
        }
    )
)


def verify_bundle(
    bundle_dir: Path = DEFAULT_BUNDLE,
    *,
    repo_root: Path = ROOT,
    external_bindings: Mapping[str, Path] | None = None,
    config_snapshot_path: Path | None = None,
) -> dict[str, Any]:
    """Verify one exact D0 bundle and return a detached report."""

    findings: list[Finding] = []
    verified_closure_counts: dict[str, int] | None = None
    bundle_dir = Path(bundle_dir)
    repo_root = Path(repo_root)
    external_bindings = dict(external_bindings or {})
    manifest_path = bundle_dir / "manifest.json"
    try:
        manifest_bytes = _read_bounded_regular_file(
            manifest_path,
            max_bytes=MAX_MANIFEST_BYTES,
            label="manifest",
        )
        manifest = _json_loads(manifest_bytes.decode("utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be a JSON object")
    except _VerifierResourceLimit as exc:
        findings.append(
            _finding(
                "RESOURCE_LIMIT_EXCEEDED",
                "manifest",
                str(exc),
                "reduce manifest.json to the fixed verifier resource budget",
                source_ref=str(manifest_path),
            )
        )
        return _report(
            manifest_sha256=None,
            legacy_snapshot=None,
            records={},
            inventory={"ok": False, "diffs": {"manifest": str(exc)}},
            verified_closure_counts=None,
            findings=findings,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "manifest",
                str(exc),
                "restore a canonical manifest.json before verification",
                source_ref=str(manifest_path),
            )
        )
        return _report(
            manifest_sha256=None,
            legacy_snapshot=None,
            records={},
            inventory={"ok": False, "diffs": {"manifest": str(exc)}},
            verified_closure_counts=None,
            findings=findings,
        )

    try:
        canonical_manifest = _canonical_json_bytes(manifest)
    except (TypeError, ValueError, UnicodeError) as exc:
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "manifest",
                f"manifest is outside the canonical I-JSON subset: {exc}",
                "remove non-finite numbers and invalid Unicode, then regenerate",
                source_ref=str(manifest_path),
            )
        )
        return _report(
            manifest_sha256=_sha256(manifest_bytes),
            legacy_snapshot=None,
            records={},
            inventory={"ok": False, "diffs": {"manifest": "non-canonical I-JSON"}},
            verified_closure_counts=None,
            findings=findings,
        )

    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "manifest",
                f"unexpected schema_version {manifest.get('schema_version')!r}",
                f"regenerate using {MANIFEST_SCHEMA}",
            )
        )
    manifest_fields = set(manifest)
    if manifest_fields != MANIFEST_FIELDS:
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "manifest",
                "manifest top-level fields are not the closed v1 contract: "
                f"missing={sorted(MANIFEST_FIELDS - manifest_fields)} "
                f"unknown={sorted(manifest_fields - MANIFEST_FIELDS)}",
                "emit exactly the verifier-owned v1 manifest fields",
            )
        )
    if canonical_manifest != manifest_bytes:
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "manifest",
                "manifest.json is not canonical UTF-8 JSON with one terminal LF",
                "serialize the manifest with sorted keys and compact separators",
                source_ref=str(manifest_path),
            )
        )
    if manifest.get("canonicalization") != CANONICALIZATION_CONTRACT:
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "canonicalization",
                "canonicalization differs from the verifier-owned exact v1 contract",
                "restore every canonicalization field and value exactly",
            )
        )
    inventory_metrics = manifest.get("inventory_metrics")
    inventory_shape_valid = False
    if isinstance(inventory_metrics, dict):
        main_cli_metrics = inventory_metrics.get("main_cli")
        mcp_metrics = inventory_metrics.get("mcp")
        inventory_shape_valid = (
            set(inventory_metrics) == INVENTORY_METRIC_FIELDS
            and isinstance(main_cli_metrics, dict)
            and set(main_cli_metrics) == MAIN_CLI_METRIC_FIELDS
            and isinstance(mcp_metrics, dict)
            and set(mcp_metrics) == MCP_METRIC_FIELDS
        )
    if not inventory_shape_valid:
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "inventory_metrics",
                "inventory_metrics fields are missing, unknown, or malformed",
                "emit exactly the closed main_cli and mcp metric contracts",
            )
        )
    for field, expected in (
        ("release_eligible", False),
        ("denominator_frozen", False),
        ("denominator_approved", False),
    ):
        if manifest.get(field) is not expected:
            findings.append(
                _finding(
                    "MANIFEST_INVALID",
                    "manifest",
                    f"draft catalog requires {field}={expected!r}",
                    "do not encode verification or approval inside the self-hashed manifest",
                )
            )
    expected_scope = {
        "mode": "DISCOVERY_ONLY",
        "freeze_capable": False,
        "missing_mechanisms": [
            "typed_adjudication_receipts",
            "canonical_owner_registry",
            "effect_target_registry",
            "independent_oracle_receipts",
            "config_applicability_attestation",
            "complete_reverse_state_effect_inventory",
        ],
    }
    if manifest.get("verification_scope") != expected_scope:
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "manifest",
                "v1 verification_scope must remain the fixed discovery-only contract",
                "regenerate v1 without claiming a freeze-capable evaluator",
            )
        )
    _verify_manifest_findings(manifest, findings)
    if tuple(manifest.get("artifact_order", ())) != ARTIFACT_ORDER:
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "manifest",
                "artifact_order differs from the D0 wire contract",
                "regenerate all five denominator books and coverage edges",
            )
        )

    snapshot_value = manifest.get("legacy_snapshot")
    snapshot = snapshot_value if isinstance(snapshot_value, dict) else {}
    resolved_snapshot = _verify_snapshot(repo_root, snapshot, findings)
    legacy_commit = resolved_snapshot[0] if resolved_snapshot else str(snapshot.get("commit") or "")
    _verify_bindings(
        manifest.get("source_bindings"),
        repo_root=repo_root,
        legacy_commit=legacy_commit,
        external_bindings=external_bindings,
        findings=findings,
    )
    _verify_config_snapshot(
        manifest.get("config_snapshot"),
        override=config_snapshot_path,
        findings=findings,
    )
    _verify_generator_identity(
        manifest.get("generator_identity"),
        repo_root=repo_root,
        findings=findings,
    )

    artifact_metadata = manifest.get("artifacts")
    if not isinstance(artifact_metadata, list):
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "manifest",
                "artifacts must be a list",
                "regenerate artifact metadata in canonical artifact_order",
            )
        )
        artifact_metadata = []
    metadata_by_id = {
        str(item.get("artifact_id")): item
        for item in artifact_metadata
        if isinstance(item, dict) and item.get("artifact_id")
    }
    if [item.get("artifact_id") for item in artifact_metadata if isinstance(item, dict)] != list(
        ARTIFACT_ORDER
    ):
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "manifest",
                "artifact metadata is missing, extra, or out of canonical order",
                "regenerate the complete five-artifact manifest",
            )
        )

    records: dict[str, list[dict[str, Any]]] = {}
    for artifact_id in ARTIFACT_ORDER:
        metadata = metadata_by_id.get(artifact_id)
        if metadata is None:
            findings.append(
                _finding(
                    "ARTIFACT_MISSING",
                    artifact_id,
                    "manifest has no artifact metadata",
                    "regenerate the complete bundle",
                )
            )
            records[artifact_id] = []
            continue
        records[artifact_id] = _read_artifact(bundle_dir, artifact_id, metadata, findings)

    _verify_global_identity(records, findings)
    _verify_edges(records, findings)
    inventory: dict[str, Any]
    if resolved_snapshot is None:
        inventory = {"ok": False, "diffs": {"snapshot": "unavailable"}}
    else:
        with tempfile.TemporaryDirectory(prefix="mnemos-d0-verifier-") as temporary:
            try:
                snapshot_root = _materialize_snapshot(
                    repo_root,
                    resolved_snapshot[0],
                    Path(temporary),
                )
                _verify_snapshot_evidence(snapshot_root, records, findings)
                inventory = _verify_independent_census(
                    snapshot_root,
                    records,
                    findings,
                    inventory_metrics=manifest.get("inventory_metrics"),
                )
            except (
                OSError,
                ValueError,
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                tarfile.TarError,
            ) as exc:
                findings.append(
                    _finding(
                        "ENUMERATOR_FAILED",
                        "independent_inventory",
                        str(exc),
                        "restore the legacy Git object and rerun independent enumeration",
                    )
                )
                inventory = {"ok": False, "diffs": {"snapshot_materialization": str(exc)}}

    inventory_diff = 0
    if not inventory.get("complete") or inventory.get("diffs"):
        inventory_diff = max(1, len(inventory.get("diffs", {})))
    verified_closure_counts = _independent_closure_counts(
        records,
        source_bindings=manifest.get("source_bindings"),
        config_snapshot=manifest.get("config_snapshot"),
        generator_findings=manifest.get("findings"),
        independent_inventory_diff=inventory_diff,
    )

    closure = manifest.get("closure")
    if not isinstance(closure, dict):
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "manifest",
                "closure must be a typed object",
                "regenerate the fail-closed closure summary",
            )
        )
    else:
        closure_fields = set(closure)
        if closure_fields != CLOSURE_FIELDS:
            findings.append(
                _finding(
                    "MANIFEST_INVALID",
                    "closure",
                    "closure fields are not the closed v1 contract: "
                    f"missing={sorted(CLOSURE_FIELDS - closure_fields)} "
                    f"unknown={sorted(closure_fields - CLOSURE_FIELDS)}",
                    "emit exactly the verifier-owned closure fields",
                )
            )
        if closure.get("schema_version") != CLOSURE_SCHEMA:
            findings.append(
                _finding(
                    "MANIFEST_INVALID",
                    "closure",
                    f"closure.schema_version must equal {CLOSURE_SCHEMA}",
                    "regenerate the closure under the fixed v1 schema",
                )
            )
        counts = closure.get("counts")
        required_zero = closure.get("required_zero_fields")
        if not isinstance(counts, dict) or not isinstance(required_zero, list):
            findings.append(
                _finding(
                    "MANIFEST_INVALID",
                    "manifest",
                    "closure counts/required_zero_fields are malformed",
                    "regenerate the closure predicate from typed findings",
                )
            )
        else:
            count_fields = set(counts)
            if count_fields != set(CLOSURE_COUNT_FIELDS):
                findings.append(
                    _finding(
                        "MANIFEST_INVALID",
                        "closure",
                        "closure.counts fields are not the closed v1 contract: "
                        f"missing={sorted(set(CLOSURE_COUNT_FIELDS) - count_fields)} "
                        f"unknown={sorted(count_fields - set(CLOSURE_COUNT_FIELDS))}",
                        "emit every required-zero and supplemental count exactly once",
                    )
                )
            if tuple(required_zero) != REQUIRED_ZERO_FIELDS:
                findings.append(
                    _finding(
                        "MANIFEST_INVALID",
                        "manifest",
                        "closure.required_zero_fields differs from the complete D0 freeze predicate",
                        "restore every canonical required-zero field in fixed order",
                    )
                )
            for field in REQUIRED_ZERO_FIELDS:
                value = counts.get(field)
                verified_value = verified_closure_counts[field]
                if field in {
                    "independent_inventory_diff",
                    "independent_inventory_pending_family",
                }:
                    if value is not None:
                        findings.append(
                            _finding(
                                "CLOSURE_MISMATCH",
                                "closure",
                                f"generator must leave {field}=null",
                                "leave independent inventory to the detached verifier",
                            )
                        )
                elif type(value) is not int or value != verified_value:
                    findings.append(
                        _finding(
                            "CLOSURE_MISMATCH",
                            "closure",
                            f"{field} declared={value!r} independently_recomputed={verified_value}",
                            "repair the writer reducer or exact source records and regenerate",
                        )
                    )
                if verified_value != 0:
                    findings.append(
                        _finding(
                            field.upper(),
                            "closure",
                            f"required-zero field {field}={verified_value}",
                            "resolve every exact record or attach an approved adjudication",
                        )
                    )
            for field in CLOSURE_SUPPLEMENTAL_COUNT_FIELDS:
                value = counts.get(field)
                verified_value = verified_closure_counts[field]
                if type(value) is not int or value != verified_value:
                    findings.append(
                        _finding(
                            "CLOSURE_MISMATCH",
                            "closure",
                            f"{field} declared={value!r} "
                            f"independently_recomputed={verified_value}",
                            "derive supplemental test counts from the exact test-file denominator",
                        )
                    )
            expected_local_ok = all(
                verified_closure_counts[field] == 0
                for field in REQUIRED_ZERO_FIELDS
                if field
                not in {
                    "independent_inventory_diff",
                    "independent_inventory_pending_family",
                }
            )
            if closure.get("local_ok") is not expected_local_ok:
                findings.append(
                    _finding(
                        "CLOSURE_MISMATCH",
                        "closure",
                        "closure.local_ok differs from independently recomputed local counts",
                        "derive local_ok only from the fixed required-zero predicate",
                    )
                )
        if (
            closure.get("verification_pending") is not True
            or closure.get("frozen_eligible") is not False
        ):
            findings.append(
                _finding(
                    "MANIFEST_INVALID",
                    "manifest",
                    "generator manifest must remain verification_pending and not frozen_eligible",
                    "keep verification and user approval in detached receipts",
                )
            )

    return _report(
        manifest_sha256=_sha256(manifest_bytes),
        legacy_snapshot=snapshot if resolved_snapshot is not None else None,
        records=records,
        inventory=inventory,
        verified_closure_counts=verified_closure_counts,
        findings=findings,
    )


def _report(
    *,
    manifest_sha256: str | None,
    legacy_snapshot: Mapping[str, Any] | None,
    records: Mapping[str, Sequence[Mapping[str, Any]]],
    inventory: Mapping[str, Any],
    verified_closure_counts: Mapping[str, int] | None,
    findings: Sequence[Finding],
) -> dict[str, Any]:
    finding_rows = [asdict(finding) for finding in findings]
    blocking_count = len(finding_rows)
    integrity_blocking_count = sum(row["code"] in INTEGRITY_CODES for row in finding_rows)
    implementation_files: list[dict[str, Any]] = []
    for relative in _VERIFIER_IMPLEMENTATION_PATHS:
        payload = _read_bounded_regular_file(
            ROOT / relative,
            max_bytes=MAX_EXTERNAL_BINDING_BYTES,
            label=f"verifier implementation {relative}",
            require_absolute=True,
        )
        implementation_files.append(
            {
                "path": relative,
                "sha256": _sha256(payload),
                "byte_length": len(payload),
            }
        )
    identity_tuples = [
        [row["path"], row["sha256"], row["byte_length"]] for row in implementation_files
    ]
    return {
        "schema_version": REPORT_SCHEMA,
        "verification_status": "BLOCKED",
        "discovery_status": (
            "DISCOVERY_VALID" if integrity_blocking_count == 0 else "DISCOVERY_BLOCKED"
        ),
        "ok": False,
        "integrity_ok": integrity_blocking_count == 0,
        "freeze_ready": False,
        "freeze_protocol_implemented": False,
        "manifest_sha256": manifest_sha256,
        "verifier_identity": {
            "code_identity_version": "exact-file-set-v1",
            "entry_symbol": "scripts.successor_d0_verification.runner.verify_bundle",
            "implementation_files": implementation_files,
            "implementation_root_sha256": _sha256(_canonical_json_bytes(identity_tuples)),
            "implementation_version": "independent-d0-verifier-v1",
        },
        "legacy_snapshot": dict(legacy_snapshot or {}),
        "artifact_record_counts": {
            artifact_id: len(artifact_records) for artifact_id, artifact_records in records.items()
        },
        "independent_inventory": dict(inventory),
        "verified_closure_counts": dict(verified_closure_counts or {}),
        "blocking_count": blocking_count,
        "integrity_blocking_count": integrity_blocking_count,
        "warning_count": 0,
        "findings": finding_rows,
    }


def _parse_binding_overrides(values: Sequence[str]) -> dict[str, Path]:
    overrides: dict[str, Path] = {}
    for value in values:
        binding_id, separator, path = value.partition("=")
        if not separator or not binding_id or not path:
            raise ValueError("--binding must use BINDING_ID=/absolute/path")
        overrides[binding_id] = Path(path)
    return overrides


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument(
        "--binding",
        action="append",
        default=[],
        metavar="BINDING_ID=PATH",
        help="override one external source binding for exact-byte verification",
    )
    parser.add_argument(
        "--config-snapshot",
        type=Path,
        help="exact external config snapshot when manifest mode is EXACT_FILE",
    )
    parser.add_argument("--json", action="store_true", help="emit the full JSON report")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero unless the exact bundle is freeze-ready",
    )
    args = parser.parse_args(argv)
    try:
        overrides = _parse_binding_overrides(args.binding)
    except ValueError as exc:
        parser.error(str(exc))
    report = verify_bundle(
        args.bundle_dir,
        repo_root=args.repo_root,
        external_bindings=overrides,
        config_snapshot_path=args.config_snapshot,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            "Successor D0 verification: "
            f"status={report['verification_status']} "
            f"integrity_ok={report['integrity_ok']} "
            f"blocking={report['blocking_count']}"
        )
        for finding in report["findings"]:
            print(f"- {finding['code']}: {finding['message']}")
    return 1 if not report["ok"] else 0
