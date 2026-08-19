"""Immutable paths, registries, and Root-order constants for governance."""

from __future__ import annotations

from pathlib import Path

from core.ops.git_repository_lock import git_common_lock_path
ROOT = Path(__file__).resolve().parents[1]
GOVERNANCE_REFRESH_LOCK_PATH = git_common_lock_path(
    ROOT,
    "mnemos_phase1_governance_refresh.lock",
)
ACCEPTANCE = ROOT / "docs" / "acceptance"
INDEPENDENT_DENOMINATOR_PATH = ACCEPTANCE / "cognitive_phase0_independent_denominator.json"
PHASE0_LEDGER_PATH = ACCEPTANCE / "cognitive_remediation_phase_0_ledger.json"
PHASE1_LEDGER_PATH = ACCEPTANCE / "cognitive_remediation_phase_1_ledger.json"
DESKTOP_ROOT = (
    ROOT.parent / "Desktop" if (ROOT.parent / "Desktop").is_dir() else Path.home() / "Desktop"
)
GOVERNING_CONTRACT_ASSET_ID = "desktop:mnemos-phase0-7-global-contract-2026-07-24"
GOVERNING_CONTRACT_PATH = DESKTOP_ROOT / "Mnemos-Phase0-7全局工程修复合同-2026-07-24.md"
GOVERNING_CONTRACT_PREDECESSOR_ASSET_ID = "desktop:mnemos-phase0-7-global-handoff-2026-07-24"
GOVERNING_CONTRACT_PREDECESSOR_PATH = (
    DESKTOP_ROOT / "Mnemos-Phase0-7全局深度审核与修复交接-2026-07-24.md"
)
GOVERNING_CONTRACT_PREDECESSOR_SHA256 = (
    "c630cb1571c5b1d13bbe12582629075a9b17fcedba2371b97fda6935be5e50be"
)
HISTORICAL_SOURCE_ASSET_ID = "desktop:mnemos-cognitive-remediation-governing-2026-07-12"
HISTORICAL_SOURCE_PATH = DESKTOP_ROOT / "Mnemos认知链路审计与全量修复方案-2026-07-12.md"
HISTORICAL_BODY_HEADING = "# Mnemos 认知链路审计与全量修复方案（2026-07-12）"
IMPORTED_CORPUS_START = "<!-- BEGIN IMPORTED CONTRACT CORPUS"
IMPORTED_CORPUS_END = "<!-- END IMPORTED CONTRACT CORPUS -->"
SCHEMA_VERSION = "mnemos.cognitive_phase0_governance.v1"
MIGRATION_PREFIXES = (
    "reconcile_",
    "migrate_",
    "migration_",
    "rebuild_",
    "replay_",
    "backfill_",
    "repair_",
    "project_",
    "recompact_",
    "init_",
)
REQUIREMENT_FIELDS = (
    "requirement_id",
    "root_id",
    "finding_id",
    "requirement_kind",
    "work_package_id",
    "coverage_scope",
    "risk_level",
    "runner_kind",
    "entrypoint",
    "argv",
    "node_ids",
    "fixture_id",
    "fixture_hash",
    "oracle_symbol",
    "oracle_source_hash",
    "baseline_expected_failure",
    "baseline_artifact_ref",
    "candidate_artifact_ref",
    "test_lanes",
    "mutation_operator_ids",
    "required_population_policy",
    "production_artifact_type",
    "invalidates",
    "status",
)
CERTIFICATE_IDS = (
    "OfflineReleaseCertificate",
    "ProductionCognitiveCertificate",
    "MigrationCertificate",
    "PerformanceCertificate",
    "DocumentationCertificate",
    "SecurityCertificate",
)
PHASE1_IMMUTABLE_HISTORICAL_ARTIFACTS = {
    "cog008_review_baseline_v1": {
        "ledger_record": "phase1_cog008_deep_review_repair_20260726",
        "implementation_commit": "57404c80e44a3d1934ee2fa7dc3fb61086e7e23d",
        "implementation_commit_owner": "57404c80",
        "path": "docs/acceptance/phase1_cog008_review_baseline_failure_evidence.json",
        "sha256": "02c30175c6734e44eedcf5c94464def71fca79cb9e9d7bf957d7c4f6e0f8ebed",
    }
}
COG008_REDACTED_BASELINE_PATH = (
    "docs/acceptance/phase1_cog008_review_baseline_failure_evidence_v2.json"
)

ROOT_ORDER = (
    ("COG-025", "P0-01"),
    ("COG-045", "P1-01"),
    ("COG-001", "P1-02"),
    ("COG-002", "P1-03"),
    ("COG-004", "P1-04"),
    ("COG-005", "P1-05"),
    ("COG-006", "P1-06"),
    ("COG-007", "P1-07"),
    ("COG-008", "P1-08"),
    ("COG-003", "P1-09"),
    ("COG-009", "P1-10"),
    ("COG-026", "P1-11"),
    ("COG-027", "P2-01"),
    ("COG-018", "P2-02"),
    ("COG-011", "P2-03"),
    ("COG-028", "P2-04"),
    ("COG-029", "P2-05"),
    ("COG-044", "P2-06"),
    ("COG-047", "P2-07"),
    ("COG-010", "P2-08"),
    ("COG-012", "P2-09"),
    ("COG-013", "P2-10"),
    ("COG-049", "P2-11"),
    ("COG-043", "P3-01"),
    ("COG-035", "P3-02"),
    ("COG-036", "P3-03"),
    ("COG-037", "P3-04"),
    ("COG-038", "P3-05"),
    ("COG-048", "P3-06"),
    ("COG-014", "P4-01"),
    ("COG-030", "P4-02"),
    ("COG-015", "P4-03"),
    ("COG-050", "P4-04"),
    ("COG-039", "P4-05"),
    ("COG-034", "P4-06"),
    ("COG-031", "P4-07"),
    ("COG-024", "P4-08"),
    ("COG-016", "P5-01"),
    ("COG-017", "P5-02"),
    ("COG-019", "P5-03"),
    ("COG-032", "P5-04"),
    ("COG-020", "P5-05"),
    ("COG-021", "P5-06"),
    ("COG-022", "P6-01"),
    ("COG-023", "P6-02"),
    ("COG-033", "P6-03"),
    ("COG-040", "P7-01"),
    ("COG-041", "P7-02"),
    ("COG-046", "P7-03"),
    ("COG-042", "P7-04"),
)

ROOT_CHANGE_BUDGET_OVERRIDES = {
    "COG-001": {
        "allowed_interface_delta": 3,
        "allowed_schema_delta": 0,
        "allowed_migration_delta": 0,
        "allowed_gate_delta": 0,
        "approved_expansions": [
            "tri_state_agent_path_watcher_contract",
            "fail_closed_file_watcher_snapshot_contract",
            "polling_path_inspection_availability_contract",
            "atomic_polling_state_snapshot_contract",
        ],
    },
    "COG-045": {
        "allowed_interface_delta": 8,
        "allowed_schema_delta": 2,
        "allowed_migration_delta": 2,
        "allowed_gate_delta": 0,
        "approved_expansions": [
            "append_only_native_session_identity_reconciliation_ledger",
            "raw_rebuild_exact_plan_identity_preflight_and_rollback",
            "native_session_disposition_conservation_cursor_v4",
            "tri_state_native_source_discovery_and_coverage_contract",
            "native_container_residual_evidence_contract",
            "canonical_raw_structured_field_single_copy_contract",
            "typed_sync_log_and_backend_dedup_unavailable_contract",
            "fail_closed_native_source_path_predicate_contract",
            "fail_closed_snapshot_path_lifecycle_contract",
            "fail_closed_sanitize_configuration_contract",
            "atomic_phase1_sqlite_schema_initialization_contract",
        ],
    },
    "COG-002": {
        "allowed_interface_delta": 0,
        "allowed_schema_delta": 0,
        "allowed_migration_delta": 0,
        "allowed_gate_delta": 0,
        "approved_expansions": [
            "atomic_metadata_deletion_receipt_schema_preflight",
        ],
    },
    "COG-008": {
        "allowed_interface_delta": 5,
        "allowed_schema_delta": 2,
        "allowed_migration_delta": 2,
        "allowed_gate_delta": 0,
        "approved_expansions": [
            "nonterminal_runtime_stage_and_failed_terminal_adapters",
            "runtime_flow_receipts_v4_to_v5_in_progress_status",
            "coupled_cognitive_state_and_search_projection_migration",
            "amphora_terminal_outbox_independent_anchor_and_reviewed_reconciliation",
            "exact_amphora_task_claim_and_payload_availability_contract",
            "per_claim_timeout_ownership_and_retry_contract",
            "committed_terminal_secondary_outbox_failure_boundary",
        ],
    },
    "COG-003": {
        "allowed_interface_delta": 6,
        "allowed_schema_delta": 0,
        "allowed_migration_delta": 0,
        "allowed_gate_delta": 0,
        "approved_expansions": [
            "typed_runtime_receipt_store_availability_contract",
            "active_and_passive_runtime_probe_availability_contract",
            "single_snapshot_agent_diagnostics_contract",
            "typed_workflow_and_active_adapter_inventory_contract",
            "atomic_agent_control_receipt_schema_initialization_contract",
        ],
    },
    "COG-026": {
        "allowed_interface_delta": 1,
        "allowed_schema_delta": 0,
        "allowed_migration_delta": 0,
        "allowed_gate_delta": 0,
        "approved_expansions": [
            "atomic_raw_index_schema_initialization_contract",
        ],
    },
}

FINDING_OWNERS = {
    "GLOB-001": ("COG-025",),
    "GLOB-002": ("COG-042",),
    "GLOB-003": ("COG-046", "COG-042"),
    "GLOB-004": ("COG-046", "COG-042"),
    "P12-001": ("COG-044",),
    "P12-002": ("COG-008", "COG-047"),
    "P12-003": ("COG-009", "COG-026"),
    "P12-004": ("COG-045", "COG-003", "COG-039", "COG-034"),
    "P12-005": ("COG-008", "COG-027", "COG-030", "COG-015", "COG-050", "COG-024"),
    "P12-006": ("COG-046", "COG-042"),
    "P12-007": ("COG-025", "COG-047", "COG-010"),
    "P3-001": ("COG-035", "COG-037", "COG-038", "COG-048"),
    "P3-002": ("COG-036", "COG-014"),
    "P3-003": ("COG-035", "COG-036", "COG-037", "COG-038", "COG-048"),
    "P4-001": ("COG-030",),
    "P4-002": ("COG-015",),
    "P4-003": ("COG-050",),
    "P4-004": ("COG-039", "COG-034"),
    "P4-005": ("COG-034", "COG-031", "COG-024"),
    "P5-001": ("COG-019", "COG-032"),
    "P5-002": ("COG-019", "COG-044"),
    "P5-003": ("COG-020",),
    "P5-004": ("COG-017",),
    "P5-005": ("COG-017",),
    "P5-006": ("COG-016", "COG-020", "COG-021"),
    "P5-007": ("COG-046", "COG-042"),
    "P6-001": ("COG-022", "COG-023"),
    "P6-002": ("COG-033",),
    "P6-003": ("COG-022", "COG-023", "COG-033"),
    "P6-004": ("COG-033", "COG-042"),
    "P7-001": ("COG-040",),
    "P7-002": ("COG-040",),
    "P7-003": ("COG-041",),
    "P7-004": ("COG-041",),
    "P7-005": ("COG-046", "COG-042"),
    "P7-006": ("COG-042",),
    "P7-007": ("COG-046", "COG-042"),
    "P7-008": ("COG-046", "COG-042"),
}

SUPPORT_WPS = {
    "WP-COG-025-SAFETY": "COG-025",
    "WP-COG-040-P0-BASELINE": "COG-040",
    "WP-COG-046-P0-DENOMINATOR-LOCK": "COG-046",
}
SUPPORT_WP_PREREQUISITES = {
    "WP-COG-025-SAFETY": (),
    "WP-COG-040-P0-BASELINE": ("WP-COG-025-SAFETY",),
    "WP-COG-046-P0-DENOMINATOR-LOCK": ("WP-COG-040-P0-BASELINE",),
}

PHASE1_BASELINE_COMMITS = {
    "COG-045": "706d13a8b9168a10152c7a0c9680efb5dfa8769c",
    "COG-001": "6e412430a14ece1c906ccfe8ebb4377f1e114df7",
    "COG-002": "b05b040c43954571c0ceba2366624fabd4517b7a",
    "COG-004": "fa7a9ad3ee29f88f51d6279e512f62f914e9bad6",
    "COG-005": "fe377876fd87b83135c18b2bd32f71c0f9cbf14c",
    "COG-006": "4c80531fffb25e45922e9d3dda79bedf2f78a18c",
    "COG-007": "32713fd30e0ab55d291200eaf7d7931a4710d4a5",
    "COG-008": "f4e7d75b0a012cbd42be13519ab6d1f2303332cf",
    "COG-003": "9aeb8d346ed3eb167cb70f07e656834a29150b91",
    "COG-009": "b8e518758311e6f77f4acbfbcf80412fe006a388",
    "COG-026": "b99d2e66632dca02ec427dc6bae0b7c1499741cb",
}


from scripts.phase1_governance_data import (  # noqa: F401
    PHASE0_SUPPORT_REQUIREMENT_SPECS,
    PHASE1_CHANGED_TEST_NODE_IDS_BY_ROOT,
    PHASE1_CLOSURE_BOUNDARIES,
    PHASE1_EXPLICIT_SOURCE_MUTATIONS,
    PHASE1_MUTATION_ORACLE_NODES,
    PHASE1_POST_GENERATION_TEST_NODE_IDS_BY_ROOT,
    PHASE1_REMOVED_TEST_SUPERSESSIONS,
    PHASE1_REVALIDATION_BOUNDARY_OVERRIDES,
    PHASE1_REVALIDATION_SEQUENCE,
    PHASE1_ROOT_REQUIREMENT_SPECS,
)

PHASE0_FOLLOWUP_RESIDUAL_DISPOSITIONS = (
    {
        "failed_node": (
            "tests/integration/test_wiki_read_authorization.py::"
            "test_same_agent_private_page_remains_readable"
        ),
        "failure": (
            "SignalStore is uninitialized; use an explicit bootstrap or " "reconciliation command"
        ),
        "root_cause": (
            "authorized wiki_read unconditionally attempts persona SignalStore "
            "knowledge-signal persistence before that canonical store is initialized"
        ),
        "owner_root_id": "COG-020",
        "owner_phase_order": "P5-05",
        "finding_ids": ["P5-003", "P5-006"],
        "status": "DEFERRED_TO_OWNING_ROOT",
        "next_allowed": "Phase 5 COG-020",
        "invalidates": ["COG-021", "COG-024", "COG-042"],
        "phase0_action": (
            "retain the failing test and fail-closed store boundary; do not seed, "
            "bootstrap, swallow, or mutate production state"
        ),
    },
)


FINDING_SUPPORT = {
    "GLOB-001": ("WP-COG-025-SAFETY",),
    "GLOB-002": ("WP-COG-046-P0-DENOMINATOR-LOCK",),
    "GLOB-003": ("WP-COG-046-P0-DENOMINATOR-LOCK",),
    "GLOB-004": ("WP-COG-046-P0-DENOMINATOR-LOCK",),
    "P12-006": ("WP-COG-046-P0-DENOMINATOR-LOCK",),
    "P12-007": ("WP-COG-025-SAFETY",),
    "P3-003": ("WP-COG-046-P0-DENOMINATOR-LOCK",),
    "P5-007": ("WP-COG-046-P0-DENOMINATOR-LOCK",),
    "P6-004": ("WP-COG-046-P0-DENOMINATOR-LOCK",),
    "P7-001": ("WP-COG-040-P0-BASELINE",),
    "P7-002": ("WP-COG-040-P0-BASELINE",),
    "P7-005": ("WP-COG-046-P0-DENOMINATOR-LOCK",),
    "P7-006": ("WP-COG-046-P0-DENOMINATOR-LOCK",),
    "P7-007": ("WP-COG-046-P0-DENOMINATOR-LOCK",),
    "P7-008": ("WP-COG-046-P0-DENOMINATOR-LOCK",),
}

INVALIDATED_ROOTS = {
    "GLOB-001": ("COG-049",),
    "P12-001": (
        "COG-027",
        "COG-010",
        "COG-011",
        "COG-012",
        "COG-013",
        "COG-014",
        "COG-019",
        "COG-020",
        "COG-021",
        "COG-032",
        "COG-035",
        "COG-036",
        "COG-037",
        "COG-038",
        "COG-048",
        "COG-049",
    ),
    "P12-002": ("COG-027", "COG-010", "COG-030", "COG-014", "COG-024"),
    "P12-003": ("COG-027", "COG-030", "COG-015", "COG-050", "COG-024"),
    "P12-004": ("COG-001", "COG-024", "COG-040", "COG-046", "COG-042"),
    "P12-005": ("COG-034", "COG-039", "COG-046", "COG-042"),
    "P12-007": tuple(root for root, _ in ROOT_ORDER),
    "P3-001": (
        "COG-014",
        "COG-030",
        "COG-015",
        "COG-050",
        "COG-039",
        "COG-034",
        "COG-031",
        "COG-024",
    ),
    "P3-002": ("COG-030", "COG-034", "COG-024"),
    "P3-003": (
        "COG-014",
        "COG-030",
        "COG-015",
        "COG-050",
        "COG-039",
        "COG-034",
        "COG-031",
        "COG-024",
        "COG-042",
    ),
    "P4-001": ("COG-015", "COG-050", "COG-039", "COG-034", "COG-024"),
    "P4-002": ("COG-039", "COG-034", "COG-024", "COG-042"),
    "P4-003": ("COG-039", "COG-034", "COG-031", "COG-024", "COG-042"),
    "P4-004": ("COG-024", "COG-040", "COG-046", "COG-042"),
    "P4-005": ("COG-016", "COG-017", "COG-019", "COG-032", "COG-020", "COG-021", "COG-042"),
    "P5-001": ("COG-020", "COG-021"),
    "P5-002": ("COG-020", "COG-021"),
    "P5-003": ("COG-021", "COG-024", "COG-042"),
    "P5-004": ("COG-016", "COG-024", "COG-040"),
    "P5-005": ("COG-016", "COG-024"),
    "P5-006": ("COG-034", "COG-024", "COG-042"),
    "P5-007": ("COG-016", "COG-017", "COG-019", "COG-032", "COG-020", "COG-021"),
    "P6-001": ("COG-033", "COG-024", "COG-042"),
    "P6-002": ("COG-042",),
    "P6-003": ("COG-024", "COG-042"),
    "P6-004": ("COG-042",),
    "P7-001": ("COG-042",),
    "P7-002": ("COG-042",),
    "P7-003": ("COG-040", "COG-042"),
    "P7-004": ("COG-046", "COG-042"),
    "P7-005": tuple(root for root, _ in ROOT_ORDER),
    "P7-006": ("COG-042",),
    "P7-007": ("COG-042",),
    "P7-008": ("COG-046", "COG-042"),
}

APPLIES_TO_ALL = {"GLOB-002", "GLOB-003", "GLOB-004", "P12-006", "P7-005", "P7-007"}
