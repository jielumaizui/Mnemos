"""Historical and current ledger validation for generated governance assets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from core.ops.durable_io import DurableIOError
from core.ops.durable_io import read_native_bytes
from scripts.phase0_governance_constants import (
    PHASE0_FOLLOWUP_RESIDUAL_DISPOSITIONS,
)
from scripts.phase0_governance_inventory import _hash


def _stable_bytes(path: Path) -> bytes | None:
    try:
        return read_native_bytes(path)
    except (DurableIOError, OSError):
        return None


def _stable_sha256(path: Path) -> str | None:
    content = _stable_bytes(path)
    return hashlib.sha256(content).hexdigest() if content is not None else None


@dataclass(frozen=True)
class LedgerValidationDependencies:
    requirement_revalidation_is_current: Callable[[Mapping[str, Any]], bool]
    execution_evidence_binding_is_current: Callable[..., bool]
    current_generation_artifact_paths: Callable[[], tuple[str, ...]]
    acceptance: Path
    root: Path
    phase0_ledger_path: Path
    phase1_ledger_path: Path
    cog008_redacted_baseline_path: str
    phase1_revalidation_sequence: tuple[tuple[str, str], ...]
    phase1_closure_boundaries: Mapping[str, Mapping[str, Any]]
    phase1_revalidation_boundary_overrides: Mapping[str, Mapping[str, Any]]
    immutable_historical_artifacts: Mapping[str, Mapping[str, Any]]
    independent_denominator: Callable[[], dict[str, Any]]
    git_blob_bytes: Callable[[str, str], bytes | None]


def validate_phase0_ledger_evidence(
    expected_assets: dict[Path, str],
    *,
    dependencies: LedgerValidationDependencies,
) -> list[str]:
    errors: list[str] = []
    ledger_bytes = _stable_bytes(dependencies.phase0_ledger_path)
    phase1_ledger_bytes = _stable_bytes(dependencies.phase1_ledger_path)
    if ledger_bytes is None or phase1_ledger_bytes is None:
        return ["Phase governance ledger is unavailable"]
    ledger = json.loads(ledger_bytes.decode("utf-8"))
    phase1_ledger = json.loads(phase1_ledger_bytes.decode("utf-8"))
    latest_phase1_key = dependencies.phase1_revalidation_sequence[-1][1]
    cog045_revalidation = phase1_ledger.get(latest_phase1_key)
    superseded_artifact_paths: set[str] = set()
    if isinstance(cog045_revalidation, dict):
        artifacts = cog045_revalidation.get("artifacts")
        if isinstance(artifacts, dict):
            for artifact in artifacts.values():
                if not isinstance(artifact, dict):
                    continue
                path_value = artifact.get("path")
                digest = artifact.get("sha256")
                if isinstance(path_value, str) and isinstance(digest, str):
                    path = dependencies.root / path_value
                    if _stable_sha256(path) == digest:
                        superseded_artifact_paths.add(path_value)
    for generation_id in (
        "phase0_cog025_followup_repair_20260725",
        "phase0_cog040_followup_revalidation_20260725",
        "phase0_cog046_followup_repair_20260725",
    ):
        generation = ledger.get(generation_id)
        artifacts = generation.get("artifacts") if isinstance(generation, dict) else None
        if not isinstance(artifacts, dict):
            continue
        for artifact in artifacts.values():
            if not isinstance(artifact, dict):
                continue
            path_value = artifact.get("path")
            digest = artifact.get("sha256")
            if isinstance(path_value, str) and isinstance(digest, str):
                path = dependencies.root / path_value
                if _stable_sha256(path) == digest:
                    superseded_artifact_paths.add(path_value)
    independent = dependencies.independent_denominator()
    expected_ledger_keys = {
        "audit_contract",
        "authorization_boundary",
        "challenger_review",
        "cog025_live_restart_snapshot",
        "contract_authority_resolution_20260724",
        "contract_governance_migration_20260724",
        "current_evidence_generation",
        "desktop_system_map_status",
        "functional_matrix_baseline",
        "next_required_authorization",
        "performance_baseline",
        "phase0_cog025_evidence_revalidation_20260724",
        "phase0_cog025_followup_repair_20260725",
        "phase0_cog040_baseline_20260724",
        "phase0_cog040_contract_revalidation_20260724",
        "phase0_cog040_followup_revalidation_20260725",
        "phase0_cog046_denominator_lock_20260724",
        "phase0_cog046_gate_hardening_20260724",
        "phase0_cog046_followup_repair_20260725",
        "phase0_reopen_20260724",
        "production_reconcile_dry_runs",
        "recorded_at",
        "rolling_log_snapshot",
        "root_status",
        "runtime_snapshot",
        "schema_version",
        "state",
        "updated_at",
        "verification",
    }
    if set(ledger) != expected_ledger_keys:
        errors.append("unexpected Phase 0 ledger top-level keys")
    expected_current_generation = "phase0_cog046_followup_repair_20260725"
    known_phase0_generations = {
        "phase0_cog025_evidence_revalidation_20260724",
        "phase0_cog040_contract_revalidation_20260724",
        "phase0_cog046_gate_hardening_20260724",
        "phase0_cog025_followup_repair_20260725",
        "phase0_cog040_followup_revalidation_20260725",
        expected_current_generation,
    }
    unknown_phase0_generations = {
        key
        for key, value in ledger.items()
        if isinstance(value, dict)
        and str(value.get("record_type", "")).startswith("append_only_phase0_")
        and key not in known_phase0_generations
    }
    if unknown_phase0_generations:
        errors.append("unknown append-only Phase 0 evidence generation")
    if ledger.get("current_evidence_generation") != expected_current_generation:
        errors.append("current Phase 0 evidence generation mismatch")

    def contains_overclaim(value: object) -> bool:
        if isinstance(value, dict):
            for key, child in value.items():
                if (
                    key
                    in {
                        "cog046_phase7_closed",
                        "performance_certificate_eligible",
                        "phase7_root_closed",
                        "readiness_certified",
                        "release_eligible",
                    }
                    and child is not False
                ):
                    return True
                if key == "production_effect" and child not in {
                    "not verified",
                    "not reverified",
                }:
                    return True
                if key == "production_mutation" and child != "not authorized and not performed":
                    return True
                if contains_overclaim(child):
                    return True
        elif isinstance(value, list):
            return any(contains_overclaim(child) for child in value)
        return False

    def validate_generation_schema(
        record: dict[str, Any],
        *,
        label: str,
        allowed_keys: set[str],
    ) -> None:
        if set(record) != allowed_keys:
            errors.append(f"{label} evidence generation schema mismatch")
        if contains_overclaim(record):
            errors.append("Phase 0 evidence generation overclaim")

    def walk_artifacts(value: object, label: str) -> None:
        if isinstance(value, dict):
            path_value = value.get("path")
            digest = value.get("sha256")
            if isinstance(path_value, str) and isinstance(digest, str):
                path = dependencies.root / path_value
                if path_value in superseded_artifact_paths:
                    pass
                elif _stable_sha256(path) != digest:
                    errors.append(f"stale Phase 0 ledger artifact: {label}")
            for key, child in value.items():
                walk_artifacts(child, f"{label}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk_artifacts(child, f"{label}[{index}]")

    historical_hashes = independent.get("historical_evidence_hashes")
    if not isinstance(historical_hashes, dict):
        errors.append("missing independent historical evidence hashes")
        historical_hashes = {}
    for record_id, expected_hash in historical_hashes.items():
        record = ledger.get(record_id)
        if not isinstance(record, dict) or _hash(record) != expected_hash:
            errors.append(f"historical Phase 0 ledger record drift: {record_id}")
    superseded_generation_hashes = independent.get("superseded_phase0_generation_hashes")
    if not isinstance(superseded_generation_hashes, dict):
        errors.append("missing independent superseded Phase 0 generation hashes")
        superseded_generation_hashes = {}
    for record_id, expected_hash in superseded_generation_hashes.items():
        superseded_record = ledger.get(record_id)
        if not isinstance(superseded_record, dict) or _hash(superseded_record) != expected_hash:
            errors.append(f"superseded Phase 0 generation drift: {record_id}")

    record = ledger.get("contract_governance_migration_20260724")
    if not isinstance(record, dict):
        return [*errors, "missing append-only contract governance ledger evidence"]
    if record.get("historical_record_hashes") != historical_hashes:
        errors.append("contract governance historical record hash mismatch")
    if record.get("supersedes_evidence_record") != "phase0_cog046_denominator_lock_20260724":
        errors.append("contract governance supersedes chain mismatch")

    cog025_revalidation = ledger.get("phase0_cog025_evidence_revalidation_20260724")
    if not isinstance(cog025_revalidation, dict):
        errors.append("missing append-only COG-025 evidence revalidation")
    else:
        validate_generation_schema(
            cog025_revalidation,
            label="COG-025",
            allowed_keys={
                "record_type",
                "recorded_at",
                "root_id",
                "work_package",
                "implementation_commit_owner",
                "supersedes_evidence_record",
                "reason",
                "direct_path",
                "verification",
                "closure_boundary",
            },
        )
        walk_artifacts(
            cog025_revalidation,
            "phase0_cog025_evidence_revalidation_20260724",
        )
        if (
            cog025_revalidation.get("supersedes_evidence_record")
            != "contract_governance_migration_20260724"
        ):
            errors.append("COG-025 evidence revalidation supersedes chain mismatch")
        if cog025_revalidation.get("closure_boundary") != {
            "historical_closure_record_rewritten": False,
            "production_effect": "not reverified",
            "production_mutation": "not authorized and not performed",
            "readiness_certified": False,
            "release_eligible": False,
        }:
            errors.append("COG-025 evidence revalidation closure boundary overclaim")

    cog040_revalidation = ledger.get("phase0_cog040_contract_revalidation_20260724")
    if not isinstance(cog040_revalidation, dict):
        errors.append("missing append-only COG-040 contract revalidation")
    else:
        validate_generation_schema(
            cog040_revalidation,
            label="COG-040",
            allowed_keys={
                "record_type",
                "recorded_at",
                "root_id",
                "work_package",
                "implementation_commit_owner",
                "supersedes_evidence_record",
                "reason",
                "baseline_contract",
                "verification",
                "closure_boundary",
            },
        )
        walk_artifacts(
            cog040_revalidation,
            "phase0_cog040_contract_revalidation_20260724",
        )
        if (
            cog040_revalidation.get("supersedes_evidence_record")
            != "phase0_cog025_evidence_revalidation_20260724"
        ):
            errors.append("COG-040 contract revalidation supersedes chain mismatch")
        if cog040_revalidation.get("closure_boundary") != {
            "baseline_semantics_changed": False,
            "performance_certificate_eligible": False,
            "phase7_root_closed": False,
            "production_mutation": "not authorized and not performed",
            "release_eligible": False,
        }:
            errors.append("COG-040 contract revalidation closure boundary overclaim")

    cog046_hardening = ledger.get("phase0_cog046_gate_hardening_20260724")
    if not isinstance(cog046_hardening, dict):
        errors.append("missing append-only COG-046 gate hardening evidence")
    else:
        validate_generation_schema(
            cog046_hardening,
            label="COG-046",
            allowed_keys={
                "record_type",
                "recorded_at",
                "root_id",
                "work_package",
                "implementation_commit_owner",
                "supersedes_evidence_record",
                "reason",
                "artifacts",
                "verification",
                "denominator_revalidation",
                "projection_revalidation",
                "closure_boundary",
            },
        )
        walk_artifacts(
            cog046_hardening,
            "phase0_cog046_gate_hardening_20260724",
        )
        if (
            cog046_hardening.get("supersedes_evidence_record")
            != "phase0_cog040_contract_revalidation_20260724"
        ):
            errors.append("COG-046 gate hardening supersedes chain mismatch")
        if cog046_hardening.get("closure_boundary") != {
            "cog046_phase7_closed": False,
            "production_effect": "not verified",
            "production_mutation": "not authorized and not performed",
            "readiness_certified": False,
            "release_eligible": False,
        }:
            errors.append("COG-046 gate hardening closure boundary overclaim")

    cog025_followup = ledger.get("phase0_cog025_followup_repair_20260725")
    if not isinstance(cog025_followup, dict):
        errors.append("missing append-only COG-025 follow-up repair")
        cog025_followup = {}
    else:
        validate_generation_schema(
            cog025_followup,
            label="COG-025 follow-up",
            allowed_keys={
                "record_type",
                "recorded_at",
                "root_id",
                "work_package",
                "implementation_commit_owner",
                "supersedes_evidence_record",
                "reason",
                "artifacts",
                "evidence_epoch_contract",
                "verification",
                "closure_boundary",
            },
        )
        walk_artifacts(cog025_followup, "phase0_cog025_followup_repair_20260725")
        if (
            cog025_followup.get("record_type") != "append_only_phase0_followup_repair_generation"
            or cog025_followup.get("root_id") != "COG-025"
            or cog025_followup.get("work_package") != "WP-COG-025-SAFETY"
            or cog025_followup.get("supersedes_evidence_record")
            != "phase0_cog046_gate_hardening_20260724"
        ):
            errors.append("COG-025 follow-up repair identity mismatch")
        if cog025_followup.get("evidence_epoch_contract") != {
            "schema_version": "mnemos.audit_evidence_epoch.v1",
            "database_count": 2,
            "same_common_cutoff_required": True,
            "private_hash_bound_snapshots": True,
            "live_immutable_forbidden": True,
            "writer_quiescence_check": "before_and_after_backup",
        }:
            errors.append("COG-025 evidence epoch contract mismatch")
        if cog025_followup.get("closure_boundary") != {
            "historical_closure_record_rewritten": False,
            "production_effect": "not reverified",
            "production_mutation": "not authorized and not performed",
            "readiness_certified": False,
            "release_eligible": False,
        }:
            errors.append("COG-025 follow-up closure boundary overclaim")

    cog040_followup = ledger.get("phase0_cog040_followup_revalidation_20260725")
    if not isinstance(cog040_followup, dict):
        errors.append("missing append-only COG-040 follow-up revalidation")
        cog040_followup = {}
    else:
        validate_generation_schema(
            cog040_followup,
            label="COG-040 follow-up",
            allowed_keys={
                "record_type",
                "recorded_at",
                "root_id",
                "work_package",
                "implementation_commit_owner",
                "supersedes_evidence_record",
                "reason",
                "artifacts",
                "verification",
                "closure_boundary",
            },
        )
        walk_artifacts(
            cog040_followup,
            "phase0_cog040_followup_revalidation_20260725",
        )
        if (
            cog040_followup.get("record_type")
            != "append_only_phase0_followup_revalidation_generation"
            or cog040_followup.get("root_id") != "COG-040"
            or cog040_followup.get("work_package") != "WP-COG-040-P0-BASELINE"
            or cog040_followup.get("supersedes_evidence_record")
            != "phase0_cog025_followup_repair_20260725"
        ):
            errors.append("COG-040 follow-up revalidation identity mismatch")
        if cog040_followup.get("closure_boundary") != {
            "baseline_semantics_changed": False,
            "performance_certificate_eligible": False,
            "phase7_root_closed": False,
            "production_mutation": "not authorized and not performed",
            "release_eligible": False,
        }:
            errors.append("COG-040 follow-up closure boundary overclaim")

    cog046_followup = ledger.get("phase0_cog046_followup_repair_20260725")
    if not isinstance(cog046_followup, dict):
        errors.append("missing append-only COG-046 follow-up repair")
        cog046_followup = {}
    else:
        validate_generation_schema(
            cog046_followup,
            label="COG-046 follow-up",
            allowed_keys={
                "record_type",
                "recorded_at",
                "root_id",
                "work_package",
                "implementation_commit_owner",
                "supersedes_evidence_record",
                "reason",
                "artifacts",
                "requirement_revalidation",
                "portability_revalidation",
                "projection_revalidation",
                "residual_dispositions",
                "verification",
                "closure_boundary",
            },
        )
        walk_artifacts(cog046_followup, "phase0_cog046_followup_repair_20260725")
        if (
            cog046_followup.get("record_type") != "append_only_phase0_followup_repair_generation"
            or cog046_followup.get("root_id") != "COG-046"
            or cog046_followup.get("work_package") != "WP-COG-046-P0-DENOMINATOR-LOCK"
            or cog046_followup.get("supersedes_evidence_record")
            != "phase0_cog040_followup_revalidation_20260725"
        ):
            errors.append("COG-046 follow-up repair identity mismatch")
        if cog046_followup.get("residual_dispositions") != list(
            PHASE0_FOLLOWUP_RESIDUAL_DISPOSITIONS
        ):
            errors.append("Phase 0 residual disposition owner mismatch")
        if cog046_followup.get("portability_revalidation") != {
            "ci_calibration_mode": "isolated_static_contract",
            "repo_governance_desktop_mode": "skip",
            "local_desktop_mode": "required",
            "production_mode_non_darwin": "fail_closed",
            "platform_neutral_gate_tests_module_skipped": False,
        }:
            errors.append("Phase 0 portability revalidation mismatch")
        if cog046_followup.get("closure_boundary") != {
            "cog046_phase7_closed": False,
            "production_effect": "not verified",
            "production_mutation": "not authorized and not performed",
            "readiness_certified": False,
            "release_eligible": False,
        }:
            errors.append("COG-046 follow-up closure boundary overclaim")

    denominators = record.get("denominators")
    if not isinstance(denominators, dict):
        return [*errors, "missing contract governance denominator evidence"]
    denominator_revalidation = (
        cog046_hardening.get("denominator_revalidation")
        if isinstance(cog046_hardening, dict)
        else None
    )
    if not isinstance(denominator_revalidation, dict):
        errors.append("missing COG-046 denominator revalidation")
        denominator_revalidation = {}

    def generated_payload(name: str) -> tuple[dict[str, Any], str]:
        path = dependencies.acceptance / name
        content = expected_assets[path]
        return json.loads(content), hashlib.sha256(content.encode()).hexdigest()

    root_dag, root_dag_hash = generated_payload("cognitive_root_dag.json")
    budgets, budgets_hash = generated_payload("cognitive_root_change_budgets.json")
    finding, finding_hash = generated_payload("cognitive_finding_overlay.json")
    closure_text = expected_assets[
        dependencies.acceptance / "cognitive_root_closures.jsonl"
    ]
    closure_hash = hashlib.sha256(closure_text.encode()).hexdigest()
    _, closure_index_hash = generated_payload("cognitive_root_closure_index.json")
    schema, schema_hash = generated_payload("schema_owner_manifest.json")
    requirements, requirements_hash = generated_payload("cognitive_requirement_test_manifest.json")
    artifacts, artifacts_hash = generated_payload("audit_artifact_registry.json")
    migrations, migrations_hash = generated_payload("cognitive_migration_manifest.json")
    release, release_hash = generated_payload("cognitive_release_manifest.json")

    expected_fields = {
        ("root_dag", "count"): root_dag["root_count"],
        ("root_dag", "sha256"): root_dag_hash,
        ("root_change_budgets", "count"): budgets["root_count"],
        ("root_change_budgets", "sha256"): budgets_hash,
        ("finding_overlay", "count"): finding["finding_count"],
        ("finding_overlay", "sha256"): finding_hash,
        ("root_closure_projection", "count"): len(closure_text.splitlines()),
        ("root_closure_projection", "jsonl_sha256"): closure_hash,
        ("root_closure_projection", "index_sha256"): closure_index_hash,
        ("schema_inventory", "count"): schema["inventory_count"],
        ("schema_inventory", "unregistered"): schema["unregistered_count"],
        ("schema_inventory", "sha256"): schema_hash,
        ("requirement_test", "count"): requirements["requirement_count"],
        ("requirement_test", "unregistered"): requirements["unregistered_count"],
        (
            "requirement_test",
            "phase0_support_requirement_count",
        ): requirements["phase0_support_requirement_count"],
        (
            "requirement_test",
            "phase0_support_registered_count",
        ): requirements["phase0_support_registered_count"],
        (
            "requirement_test",
            "phase0_exact_node_count",
        ): requirements["phase0_exact_node_count"],
        ("requirement_test", "sha256"): requirements_hash,
        ("audit_artifacts", "count"): artifacts["artifact_count"],
        ("audit_artifacts", "unregistered"): artifacts["unregistered_count"],
        ("audit_artifacts", "sha256"): artifacts_hash,
        ("migrations", "count"): migrations["migration_count"],
        ("migrations", "sha256"): migrations_hash,
        ("release_certificates", "required"): len(release["certificates"]),
        ("release_certificates", "missing"): sum(
            item["status"] == "MISSING" for item in release["certificates"]
        ),
        ("release_certificates", "sha256"): release_hash,
    }
    for (section, field), expected in expected_fields.items():
        governance_revalidation = (
            cog045_revalidation.get("governance_revalidation")
            if isinstance(cog045_revalidation, dict)
            else None
        )
        if (
            isinstance(governance_revalidation, dict)
            and section in governance_revalidation
        ):
            actual_section = governance_revalidation.get(section)
        elif section == "requirement_test":
            actual_section = cog046_followup.get("requirement_revalidation")
        elif section == "root_closure_projection":
            projection = cog046_followup.get("projection_revalidation")
            actual_section = projection.get("current") if isinstance(projection, dict) else None
        else:
            actual_section = denominator_revalidation.get(section, denominators.get(section))
        actual = actual_section.get(field) if isinstance(actual_section, dict) else None
        if actual != expected:
            errors.append(f"stale contract governance ledger evidence: {section}.{field}")

    previous_projection_revalidation = (
        cog046_hardening.get("projection_revalidation")
        if isinstance(cog046_hardening, dict)
        else None
    )
    projection_generation = (
        cog046_followup.get("projection_revalidation")
        if isinstance(cog046_followup, dict)
        else None
    )
    if not isinstance(previous_projection_revalidation, dict) or not isinstance(
        projection_generation, dict
    ):
        errors.append("missing root closure projection generation chain")
    else:
        expected_previous = previous_projection_revalidation.get("current")
        expected_current = {
            "schema_version": "mnemos.cognitive_root_closure_projection.v2",
            "count": len(closure_text.splitlines()),
            "jsonl_sha256": closure_hash,
            "index_sha256": closure_index_hash,
        }
        if projection_generation.get("previous") != expected_previous:
            errors.append("root closure projection previous generation mismatch")
        latest_projection = (
            governance_revalidation.get("root_closure_projection")
            if isinstance(governance_revalidation, dict)
            else None
        )
        if latest_projection != expected_current:
            errors.append("root closure projection current generation mismatch")
        if (
            projection_generation.get("mutation_policy")
            != "replaceable_generated_current_index_with_append_only_generation_chain"
        ):
            errors.append("root closure projection mutation policy mismatch")

    document_manifest_path = dependencies.acceptance / "document_asset_manifest.json"
    document_assets = record.get("document_assets")
    document_manifest_hash = _stable_sha256(document_manifest_path)
    if document_manifest_hash is None:
        errors.append("document asset manifest is unavailable")
        document_manifest_hash = ""
    expected_document_assets = {
        "manifest_sha256": document_manifest_hash,
        "external_governing_assets": 1,
        "external_historical_assets": 1,
        "renamed_predecessors": 1,
        "final_byte_hash_owner": "detached_closure_bundle_only",
        "preexisting_desktop_strict_status": (
            "14 stale generated findings remain; no generated 86-99 hand patch"
        ),
    }
    if (
        document_assets != expected_document_assets
        and "docs/acceptance/document_asset_manifest.json" not in superseded_artifact_paths
    ):
        errors.append("stale contract governance document asset evidence")

    closure_boundary = record.get("closure_boundary")
    if closure_boundary != {
        "phase1_started": False,
        "production_effect": "not verified",
        "production_mutation": "not authorized and not performed",
        "readiness_certified": False,
        "release_eligible": False,
    }:
        errors.append("contract governance closure boundary overclaim")

    return errors


def validate_phase1_historical_artifacts(
    ledger: dict[str, Any],
    *,
    dependencies: LedgerValidationDependencies,
) -> list[str]:
    errors: list[str] = []
    contract = dependencies.immutable_historical_artifacts[
        "cog008_review_baseline_v1"
    ]
    record = ledger.get(contract["ledger_record"])
    if not isinstance(record, dict):
        return ["missing immutable COG-008 historical evidence record"]
    artifact = record.get("artifacts", {}).get("baseline_failure_evidence")
    if (
        record.get("implementation_commit_owner") != contract["implementation_commit_owner"]
        or record.get("verification", {}).get("baseline_artifact") != contract["path"]
        or artifact
        != {
            "path": contract["path"],
            "sha256": contract["sha256"],
        }
    ):
        errors.append("immutable COG-008 historical evidence binding mismatch")

    historical_blob = dependencies.git_blob_bytes(
        contract["implementation_commit"],
        contract["path"],
    )
    if historical_blob is None or hashlib.sha256(historical_blob).hexdigest() != contract["sha256"]:
        errors.append("immutable COG-008 historical Git blob mismatch")
        historical_payload: object = None
    else:
        try:
            historical_payload = json.loads(historical_blob)
        except (UnicodeError, json.JSONDecodeError):
            errors.append("immutable COG-008 historical Git blob is invalid JSON")
            historical_payload = None

    if (dependencies.root / contract["path"]).exists():
        errors.append("sensitive COG-008 historical evidence must not remain in current tree")

    redacted_path = (
        dependencies.root / dependencies.cog008_redacted_baseline_path
    )
    try:
        redacted_bytes = _stable_bytes(redacted_path)
        if redacted_bytes is None:
            raise OSError("redacted evidence unavailable")
        redacted = json.loads(redacted_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [*errors, "redacted COG-008 baseline evidence is unavailable"]
    expected_supersession = {
        "record_type": "typed_sensitive_path_redaction",
        "historical_ledger_record": contract["ledger_record"],
        "historical_commit": contract["implementation_commit"],
        "historical_path": contract["path"],
        "historical_sha256": contract["sha256"],
        "changed_fields": [
            "schema_version",
            "execution_boundary.runtime",
        ],
        "semantic_equivalence": (
            "The historical Git blob remains the immutable v1 evidence. This v2 "
            "projection changes only the schema marker and machine-local runtime spelling; "
            "selected nodes, oracle blobs, execution outcomes, mutation kills, and claim "
            "boundary are byte-equivalent after canonical normalization."
        ),
    }
    if (
        redacted.get("schema_version") != "mnemos.phase1_cog008_review_baseline_failure_evidence.v2"
        or redacted.get("supersession") != expected_supersession
        or redacted.get("execution_boundary", {}).get("runtime") != ".venv/bin/python"
    ):
        errors.append("redacted COG-008 baseline evidence contract mismatch")
    if isinstance(historical_payload, dict):
        normalized = dict(redacted)
        normalized.pop("supersession", None)
        normalized["schema_version"] = historical_payload.get("schema_version")
        execution_boundary = dict(normalized.get("execution_boundary", {}))
        execution_boundary["runtime"] = historical_payload.get("execution_boundary", {}).get(
            "runtime"
        )
        normalized["execution_boundary"] = execution_boundary
        if normalized != historical_payload:
            errors.append("redacted COG-008 baseline evidence semantic drift")

    latest = ledger.get(dependencies.phase1_revalidation_sequence[-1][1])
    expected_latest_binding = {
        "record_type": "typed_sensitive_path_redaction_supersession",
        "historical_ledger_record": contract["ledger_record"],
        "historical_git_blob": {
            "commit": contract["implementation_commit"],
            "path": contract["path"],
            "sha256": contract["sha256"],
        },
        "current_redacted_projection": {
            "path": dependencies.cog008_redacted_baseline_path,
            "sha256": hashlib.sha256(redacted_bytes).hexdigest(),
        },
        "allowed_changed_fields": [
            "schema_version",
            "execution_boundary.runtime",
        ],
        "semantic_equivalence_verified": True,
    }
    if (
        not isinstance(latest, dict)
        or latest.get("historical_evidence_supersession") != expected_latest_binding
    ):
        errors.append("current Phase 1 historical evidence supersession binding mismatch")

    return errors


def validate_phase1_cog045_evidence(
    expected_assets: dict[Path, str],
    *,
    dependencies: LedgerValidationDependencies,
) -> list[str]:
    errors: list[str] = []
    try:
        ledger_bytes = _stable_bytes(dependencies.phase1_ledger_path)
        if ledger_bytes is None:
            raise OSError("Phase 1 ledger unavailable")
        ledger = json.loads(ledger_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"Phase 1 COG-045 ledger unavailable: {exc}"]
    errors.extend(
        validate_phase1_historical_artifacts(
            ledger,
            dependencies=dependencies,
        )
    )
    record = ledger.get("phase1_cog045_contract_revalidation_20260725")
    if not isinstance(record, dict):
        return ["missing append-only COG-045 contract revalidation"]
    superseded_hashes = dependencies.independent_denominator().get(
        "superseded_phase1_generation_hashes"
    )
    if not isinstance(superseded_hashes, dict) or superseded_hashes.get(
        "phase1_cog045_contract_revalidation_20260725"
    ) != _hash(record):
        errors.append(
            "superseded Phase 1 generation drift: " "phase1_cog045_contract_revalidation_20260725"
        )
    if set(record) != {
        "record_type",
        "recorded_at",
        "root_id",
        "work_package",
        "implementation_commit_owner",
        "supersedes_evidence_record",
        "denominator_contract",
        "cursor_schema_migration",
        "verification",
        "production_observation",
        "governance_revalidation",
        "artifacts",
        "closure_boundary",
    }:
        errors.append("COG-045 contract revalidation schema mismatch")
    if record.get("record_type") != "append_only_phase1_contract_revalidation":
        errors.append("COG-045 contract revalidation record type mismatch")
    if record.get("root_id") != "COG-045" or record.get("work_package") != "P1-WP1":
        errors.append("COG-045 contract revalidation identity mismatch")
    if record.get("supersedes_evidence_record") != "phase0_cog046_gate_hardening_20260724":
        errors.append("COG-045 contract revalidation supersedes chain mismatch")
    if record.get("denominator_contract") != {
        "active_sources": 12,
        "host_sources": 8,
        "ingestion_only_sources": 4,
        "active_source_rule": (
            "Every active source must prove SupportManifest -> NativeSourceSnapshot -> "
            "cursor-bound canonical Raw coverage."
        ),
        "host_capability_rule": (
            "Only the eight host sources enter runtime/full-power capability evidence."
        ),
        "ingestion_only_rule": (
            "The four ingestion-only sources must close Native -> Snapshot -> Raw but never "
            "enter the eight-host full-power denominator."
        ),
    }:
        errors.append("COG-045 denominator contract mismatch")
    if record.get("closure_boundary") != {
        "code_contract_verified": True,
        "live_cursor_schema_migrated": False,
        "live_snapshot_raw_rebuilt": False,
        "next_root_started": False,
        "production_effect": "not verified",
        "production_mutation": "not authorized and not performed",
        "readiness_certified": False,
        "release_eligible": False,
        "root_closed": False,
    }:
        errors.append("COG-045 contract revalidation closure boundary overclaim")

    governance_record = ledger.get("phase1_cog045_governance_revalidation_20260725")
    if not isinstance(governance_record, dict):
        return [*errors, "missing append-only COG-045 governance revalidation"]
    if set(governance_record) != {
        "record_type",
        "recorded_at",
        "root_id",
        "work_package",
        "implementation_commit_owner",
        "supersedes_evidence_record",
        "reason",
        "verification",
        "governance_revalidation",
        "artifacts",
        "closure_boundary",
    }:
        errors.append("COG-045 governance revalidation schema mismatch")
    if (
        governance_record.get("record_type") != "append_only_phase1_governance_revalidation"
        or governance_record.get("root_id") != "COG-045"
        or governance_record.get("work_package") != "P1-WP1"
        or governance_record.get("supersedes_evidence_record")
        != "phase1_cog045_contract_revalidation_20260725"
    ):
        errors.append("COG-045 governance revalidation identity mismatch")
    if governance_record.get("closure_boundary") != {
        "code_contract_verified": True,
        "live_cursor_schema_migrated": False,
        "live_snapshot_raw_rebuilt": False,
        "next_root_started": False,
        "production_effect": "not verified",
        "production_mutation": "not authorized and not performed",
        "readiness_certified": False,
        "release_eligible": False,
        "root_closed": False,
    }:
        errors.append("COG-045 governance revalidation closure boundary overclaim")

    frozen = dependencies.independent_denominator().get(
        "superseded_phase1_generation_hashes",
        {},
    )
    for key in (
        "phase1_cog045_contract_revalidation_20260725",
        "phase1_cog045_governance_revalidation_20260725",
        "phase1_cog045_exact_plan_authorization_hardening_20260725",
        "phase1_cog045_migration_contract_completion_20260725",
        "phase1_cog009_contract_revalidation_20260725",
        *(key for _, key in dependencies.phase1_revalidation_sequence[:-1]),
    ):
        generation = ledger.get(key)
        if not isinstance(generation, dict) or frozen.get(key) != _hash(generation):
            errors.append(f"superseded Phase 1 generation drift: {key}")

    if not dependencies.phase1_revalidation_sequence:
        return [*errors, "missing Phase 1 current revalidation sequence"]
    predecessor = "phase1_cog045_migration_contract_completion_20260725"
    for root_id, key in dependencies.phase1_revalidation_sequence:
        generation = ledger.get(key)
        sequence_predecessor = (
            generation.get("sequence_predecessor") if isinstance(generation, dict) else None
        )
        if (
            not isinstance(generation, dict)
            or generation.get("root_id") != root_id
            or (sequence_predecessor or generation.get("supersedes_evidence_record")) != predecessor
            or generation.get("closure_boundary")
            != dependencies.phase1_revalidation_boundary_overrides.get(
                key,
                dependencies.phase1_closure_boundaries.get(root_id),
            )
        ):
            errors.append(f"invalid Phase 1 revalidation sequence: {key}")
        predecessor = key
    latest_root_id, latest_key = dependencies.phase1_revalidation_sequence[-1]
    latest = ledger.get(latest_key)
    if not isinstance(latest, dict):
        return [*errors, f"missing current Phase 1 revalidation: {latest_key}"]
    if (
        latest.get("record_type") != "append_only_phase1_root_requirement_revalidation"
        or latest.get("root_id") != latest_root_id
        or latest.get("state")
        != dependencies.independent_denominator()
        .get("closure_states", {})
        .get(latest_root_id)
    ):
        errors.append("current Phase 1 revalidation identity mismatch")
    if not dependencies.requirement_revalidation_is_current(latest):
        errors.append("current Phase 1 requirement revalidation summary mismatch")
    if not dependencies.execution_evidence_binding_is_current(latest):
        errors.append("current Phase 1 execution evidence binding mismatch")
    artifacts = latest.get("artifacts")
    if not isinstance(artifacts, dict):
        errors.append("current Phase 1 revalidation artifacts missing")
    else:
        artifact_paths = {
            artifact.get("path") for artifact in artifacts.values() if isinstance(artifact, dict)
        }
        if artifact_paths != set(dependencies.current_generation_artifact_paths()):
            errors.append("current Phase 1 governance artifact denominator mismatch")
        for name, artifact in artifacts.items():
            path_value = artifact.get("path") if isinstance(artifact, dict) else None
            digest = artifact.get("sha256") if isinstance(artifact, dict) else None
            path = dependencies.root / str(path_value)
            if (
                not isinstance(path_value, str)
                or not isinstance(digest, str)
                or _stable_sha256(path) != digest
            ):
                errors.append(f"stale current Phase 1 governance artifact: {name}")

    governance_revalidation = latest.get("governance_revalidation")
    schema_content = expected_assets[
        dependencies.acceptance / "schema_owner_manifest.json"
    ]
    schema_payload = json.loads(schema_content)
    closure_content = expected_assets[
        dependencies.acceptance / "cognitive_root_closures.jsonl"
    ]
    closure_index_content = expected_assets[
        dependencies.acceptance / "cognitive_root_closure_index.json"
    ]
    requirement_content = expected_assets[
        dependencies.acceptance / "cognitive_requirement_test_manifest.json"
    ]
    requirement_payload = json.loads(requirement_content)
    audit_artifact_content = expected_assets[
        dependencies.acceptance / "audit_artifact_registry.json"
    ]
    audit_artifact_payload = json.loads(audit_artifact_content)
    migration_content = expected_assets[
        dependencies.acceptance / "cognitive_migration_manifest.json"
    ]
    migration_payload = json.loads(migration_content)
    release_content = expected_assets[
        dependencies.acceptance / "cognitive_release_manifest.json"
    ]
    release_payload = json.loads(release_content)
    root_dag_content = expected_assets[
        dependencies.acceptance / "cognitive_root_dag.json"
    ]
    root_dag_payload = json.loads(root_dag_content)
    finding_content = expected_assets[
        dependencies.acceptance / "cognitive_finding_overlay.json"
    ]
    finding_payload = json.loads(finding_content)
    expected_governance = {
        "root_dag": {
            "count": root_dag_payload["root_count"],
            "sha256": hashlib.sha256(root_dag_content.encode()).hexdigest(),
        },
        "root_change_budgets": {
            "count": json.loads(
                expected_assets[
                    dependencies.acceptance
                    / "cognitive_root_change_budgets.json"
                ]
            )["root_count"],
            "sha256": hashlib.sha256(
                expected_assets[
                    dependencies.acceptance
                    / "cognitive_root_change_budgets.json"
                ].encode()
            ).hexdigest(),
        },
        "finding_overlay": {
            "count": finding_payload["finding_count"],
            "sha256": hashlib.sha256(finding_content.encode()).hexdigest(),
        },
        "schema_inventory": {
            "count": schema_payload["inventory_count"],
            "unregistered": schema_payload["unregistered_count"],
            "sha256": hashlib.sha256(schema_content.encode()).hexdigest(),
        },
        "root_closure_projection": {
            "schema_version": "mnemos.cognitive_root_closure_projection.v2",
            "count": len(closure_content.splitlines()),
            "jsonl_sha256": hashlib.sha256(closure_content.encode()).hexdigest(),
            "index_sha256": hashlib.sha256(closure_index_content.encode()).hexdigest(),
        },
        "requirement_test": {
            "count": requirement_payload["requirement_count"],
            "unregistered": requirement_payload["unregistered_count"],
            "phase0_support_requirement_count": requirement_payload[
                "phase0_support_requirement_count"
            ],
            "phase0_support_registered_count": requirement_payload[
                "phase0_support_registered_count"
            ],
            "phase0_exact_node_count": requirement_payload["phase0_exact_node_count"],
            "phase1_revalidated_requirement_count": requirement_payload[
                "phase1_revalidated_requirement_count"
            ],
            "phase1_revalidated_exact_node_count": requirement_payload[
                "phase1_revalidated_exact_node_count"
            ],
            "sha256": hashlib.sha256(requirement_content.encode()).hexdigest(),
        },
        "audit_artifacts": {
            "count": audit_artifact_payload["artifact_count"],
            "unregistered": audit_artifact_payload["unregistered_count"],
            "sha256": hashlib.sha256(audit_artifact_content.encode()).hexdigest(),
        },
        "migrations": {
            "count": migration_payload["migration_count"],
            "sha256": hashlib.sha256(migration_content.encode()).hexdigest(),
        },
        "release_certificates": {
            "required": len(release_payload["certificates"]),
            "missing": sum(
                item["status"] == "MISSING"
                for item in release_payload["certificates"]
            ),
            "sha256": hashlib.sha256(release_content.encode()).hexdigest(),
        },
    }
    if governance_revalidation != expected_governance:
        errors.append("stale current Phase 1 governance generation")
    return errors
