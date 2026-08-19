# -*- coding: utf-8 -*-
"""Canonical static registries for :mod:`core.system_contracts`."""


def build_contract_registries(
    *,
    cognitive_asset_definition,
    quality_gate_definition,
    capability_definition,
    privacy_policy,
    lifecycle_mapping,
    domain_term,
    scorecard_dimension,
    quality_decisions,
):
    """Build registries after their typed value objects are defined."""
    CognitiveAssetDefinition = cognitive_asset_definition
    QualityGateDefinition = quality_gate_definition
    CapabilityDefinition = capability_definition
    PrivacyPolicy = privacy_policy
    LifecycleMapping = lifecycle_mapping
    DomainTerm = domain_term
    ScorecardDimension = scorecard_dimension
    QUALITY_DECISIONS = set(quality_decisions)
    LIFECYCLE_STATUSES = {
        "created",
        "captured",
        "normalized",
        "queued",
        "processing",
        "produced",
        "consumed",
        "verified",
        "degraded",
        "needs_user",
        "stale",
        "expired",
        "superseded",
        "failed_recoverable",
        "failed_terminal",
        "auto_disabled",
        "disabled_default",
        "disabled_cold_start",
        "eligible_to_enable",
        "auto_enabled",
        "manual_enabled",
        "registered_but_unwired",
        "stale_removed",
        "pending",
        "planned",
        "blocked",
        "applying",
        "applied",
        "rolled_back",
        "restoring",
        "partial_restored",
        "requested",
        "dry_run_planned",
        "frozen",
        "deleting",
        "deleted",
        "partially_deleted",
    }

    FAILURE_CLASSES = {
        "config",
        "permission",
        "network",
        "schema",
        "quality",
        "timeout",
        "rate_limit",
        "privacy",
        "conflict",
        "database_lock",
        "unknown",
    }

    ACTION_TYPES = {
        "distill_write",
        "auto_heal",
        "persona_update",
        "config_update",
        "wiki_route",
        "document_import",
        "golden_benchmark_observation",
        "quality_gate",
        "policy_patch",
        "module_toggle",
        "migration",
        "backup",
        "data_ownership",
        "toggle_activation_decision",
        "toggle_output_consumption",
        "toggle_auto_disable",
        "toggle_rollback",
        "migration_plan",
        "migration_apply",
        "migration_rollback",
        "snapshot_create",
        "restore_plan",
        "restore_apply",
        "data_inventory",
        "data_export",
        "data_freeze",
        "data_delete",
        "deletion_proof",
        "benchmark_consumer_verify",
        "install_setup",
        "install_upgrade",
        "install_uninstall",
        "install_repair_all",
        "wiki_quality_fix",
        "cognitive_readiness_gap",
        "raw_projection_recovered",
    }
    ACTION_STATUSES = {
        "created",
        "queued",
        "processing",
        "produced",
        "verified",
        "degraded",
        "needs_user",
        "failed_recoverable",
        "failed_terminal",
    }
    ACTIONS_REQUIRING_ROLLBACK = {
        "auto_heal",
        "config_update",
        "wiki_route",
        "document_import",
        "module_toggle",
        "migration",
        "migration_apply",
        "restore_apply",
        "data_delete",
        "install_upgrade",
    }

    COGNITIVE_ASSET_DEFINITIONS: dict[str, CognitiveAssetDefinition] = {
        "cognition_episode": CognitiveAssetDefinition(
            asset_type="cognition_episode",
            producers=("core.cognitive.cognition_episode_persistence",),
            consumers=("wiki", "knowledge_graph", "cognitive_graph"),
            source_fields=(
                "input_spec_hash",
                "cognition_context_hash",
                "source_event_ids",
                "source_spans",
            ),
            evidence_fields=("*.evidence_refs", "*.claim_ids"),
            privacy_level="cognition_asset",
            lifecycle_statuses=("produced", "consumed", "verified", "superseded"),
            revision_policy="immutable mnemos.cognition_episode.v2 revision and outbox; v1 is read-only",
        ),
        "wiki_page": CognitiveAssetDefinition(
            asset_type="wiki_page",
            producers=("hephaestus.wiki_builder", "core.sync_framework.file_ingestor"),
            consumers=("context_aware_search", "preflight_inject", "obsidian_ui"),
            source_fields=("source_refs", "frontmatter.来源"),
            evidence_fields=("evidence_refs", "backlinks", "artifact_uri"),
            privacy_level="wiki_page",
            lifecycle_statuses=("produced", "consumed", "stale", "superseded"),
            revision_policy="frontmatter revision + source_refs required",
        ),
        "persona_assertion": CognitiveAssetDefinition(
            asset_type="persona_assertion",
            producers=("core.persona", "core.application.persona"),
            consumers=("persona_behavior_prompt", "preflight_inject", "delivery_router"),
            source_fields=("source_refs", "signal_refs"),
            evidence_fields=("evidence_refs", "confidence_samples"),
            privacy_level="persona",
            lifecycle_statuses=("produced", "verified", "stale", "superseded"),
            revision_policy="decay and contradiction-aware refresh",
        ),
        "reflection_insight": CognitiveAssetDefinition(
            asset_type="reflection_insight",
            producers=("core.reflection", "core.app.forced_retrospective"),
            consumers=("preflight_inject", "guard_check", "check_pending_recaps"),
            source_fields=("session_id", "task_id", "source_refs"),
            evidence_fields=("evidence_refs", "recap_page"),
            privacy_level="reflection",
            lifecycle_statuses=("queued", "needs_user", "consumed", "verified"),
            revision_policy="owner-reviewed recap lifecycle",
        ),
        "policy_patch": CognitiveAssetDefinition(
            asset_type="policy_patch",
            producers=("core.cognitive.policy_patch", "core.kia.policy"),
            consumers=("guard_check", "preflight_inject", "quality_gate"),
            source_fields=("trigger_refs", "source_refs"),
            evidence_fields=("evidence_refs", "delivery_decision_id"),
            privacy_level="policy",
            lifecycle_statuses=("produced", "verified", "expired", "superseded"),
            revision_policy="policy patch version and sunset condition",
        ),
        "distill_claim": CognitiveAssetDefinition(
            asset_type="distill_claim",
            producers=("core.hephaestus.distillation_extractor", "core.hephaestus.distill_action_router"),
            consumers=("wiki_builder", "dispute_resolver", "knowledge_graph"),
            source_fields=("source_event_ids", "source_session_id"),
            evidence_fields=("claims[].evidence.artifact_ref_id", "artifact_uri"),
            privacy_level="document",
            lifecycle_statuses=("normalized", "produced", "verified", "degraded"),
            revision_policy="distill_output_v4 relation_to_existing",
        ),
        "cognitive_decision_asset": CognitiveAssetDefinition(
            asset_type="cognitive_decision_asset",
            producers=("core.hephaestus.cognition_asset_store", "core.kia.ixion (legacy projection)"),
            consumers=("wiki_builder", "wiki_search_index", "cognitive_decision_asset_proposal"),
            source_fields=("input_spec_hash", "extraction_output_hash", "source_spans", "acl"),
            evidence_fields=("canonical_extraction_output", "final_fragments", "evidence_refs"),
            privacy_level="cognition_asset",
            lifecycle_statuses=("produced", "consumed", "verified", "superseded"),
            revision_policy="immutable asset hash plus versioned derivative proposal receipt",
        ),
        "module_output": CognitiveAssetDefinition(
            asset_type="module_output",
            producers=("core.module_toggles", "daemon.service_registry", "core.pluggable"),
            consumers=("toggle_output_consumers", "scorecard", "health.module_toggles"),
            source_fields=("toggle_key", "activation_decision_id", "source_refs"),
            evidence_fields=("output_contract", "consumer_effect_metrics", "action_ledger_ref"),
            privacy_level="module_output",
            lifecycle_statuses=("produced", "consumed", "degraded", "auto_disabled"),
            revision_policy="toggle output contract revision with consumer readiness proof",
        ),
        "activation_decision": CognitiveAssetDefinition(
            asset_type="activation_decision",
            producers=("core.module_toggles", "daemon.adaptive_service"),
            consumers=("activation_quality_decision", "ActionLedger", "health.module_toggles"),
            source_fields=("toggle_key", "data_thresholds", "capability_refs"),
            evidence_fields=("activation_policy", "privacy_gate", "cost_gate", "rollback_ref"),
            privacy_level="activation_evidence",
            lifecycle_statuses=("disabled_cold_start", "eligible_to_enable", "auto_enabled", "auto_disabled"),
            revision_policy="decision hash plus policy version before runtime toggle change",
        ),
        "activation_evidence": CognitiveAssetDefinition(
            asset_type="activation_evidence",
            producers=("core.module_toggles", "core.ops.health_check"),
            consumers=("activation_quality_decision", "module_toggle_registry", "doctor.modules"),
            source_fields=("health_refs", "metric_refs", "privacy_refs"),
            evidence_fields=("capability_check", "consumer_readiness", "cost_budget", "mutual_exclusion"),
            privacy_level="activation_evidence",
            lifecycle_statuses=("created", "verified", "degraded", "stale"),
            revision_policy="evidence refreshed before every auto-enable attempt",
        ),
        "consumer_effect": CognitiveAssetDefinition(
            asset_type="consumer_effect",
            producers=("core.module_toggles", "scorecard", "ActionLedger"),
            consumers=("mnemos_scorecard", "toggle_auto_disable_policy", "doctor.modules"),
            source_fields=("toggle_key", "consumer_id", "action_id"),
            evidence_fields=("effect_metric", "before_ref", "after_ref", "rollback_ref"),
            privacy_level="consumer_effect",
            lifecycle_statuses=("produced", "consumed", "verified", "degraded"),
            revision_policy="effect metrics are append-only and tied to module output action",
        ),
        "migration_plan": CognitiveAssetDefinition(
            asset_type="migration_plan",
            producers=("core.migrations.registry", "mnemos migrate plan"),
            consumers=("mnemos migrate apply", "health.migrations", "ActionLedger"),
            source_fields=("migration_id", "from_version", "to_version", "affected_paths"),
            evidence_fields=("plan_hash", "capability_refs", "backup_required_before_apply"),
            privacy_level="migration_ledger",
            lifecycle_statuses=("planned", "blocked", "applied", "rolled_back", "failed_recoverable"),
            revision_policy="plan hash plus ledger record before apply",
        ),
        "snapshot_manifest": CognitiveAssetDefinition(
            asset_type="snapshot_manifest",
            producers=("core.backup.snapshot_manager", "mnemos backup create"),
            consumers=("mnemos restore plan", "migration.apply", "data_delete.apply"),
            source_fields=("snapshot_id", "scopes", "trigger_action"),
            evidence_fields=("file_entries.sha256", "database_entries.sha256", "restore_preconditions"),
            privacy_level="snapshot_manifest",
            lifecycle_statuses=("planned", "verified", "restoring", "partial_restored", "failed_recoverable"),
            revision_policy="immutable manifest with checksum verification",
        ),
        "data_export_manifest": CognitiveAssetDefinition(
            asset_type="data_export_manifest",
            producers=("core.privacy.data_ownership", "mnemos data export"),
            consumers=("user_export", "privacy_audit", "data_delete"),
            source_fields=("scope_kind", "scope_value", "domains"),
            evidence_fields=("storage_refs", "consumer_ids", "redaction_policy"),
            privacy_level="data_export",
            lifecycle_statuses=("dry_run_planned", "produced", "verified", "expired"),
            revision_policy="export manifest is regenerated per request and secret-redacted",
        ),
        "deletion_proof": CognitiveAssetDefinition(
            asset_type="deletion_proof",
            producers=("core.privacy.data_ownership", "mnemos data delete"),
            consumers=("privacy_audit", "user_report", "scorecard"),
            source_fields=("subject_hash", "affected_domains", "affected_consumers"),
            evidence_fields=("verification_results", "snapshot_ref", "action_ledger_ref"),
            privacy_level="deletion_proof",
            lifecycle_statuses=("verified", "partially_deleted", "failed_recoverable"),
            revision_policy="proof stores hashes and aggregate verification only",
        ),
        "benchmark_result": CognitiveAssetDefinition(
            asset_type="benchmark_result",
            producers=("core.benchmarks.golden", "scripts.run_golden_benchmark"),
            consumers=("mnemos_scorecard", "quality_regression_review", "preflight_inject"),
            source_fields=("sample_id", "manifest_path", "baseline_ref"),
            evidence_fields=("sample_results", "action_ledger", "scorecard_path"),
            privacy_level="benchmark_result",
            lifecycle_statuses=("produced", "consumed", "verified", "degraded"),
            revision_policy="deterministic fixture manifest plus committed baseline",
        ),
        "install_lifecycle_state": CognitiveAssetDefinition(
            asset_type="install_lifecycle_state",
            producers=("core.setup.install_lifecycle", "mnemos setup", "mnemos upgrade"),
            consumers=("mnemos health", "mnemos doctor repair-all", "mnemos_scorecard"),
            source_fields=("operation", "status", "steps", "migration_plan_hash", "backup_ref"),
            evidence_fields=("state_path", "action_ledger_ref", "repair_actions", "errors"),
            privacy_level="install_lifecycle",
            lifecycle_statuses=("planned", "verified", "degraded", "needs_user", "failed_recoverable"),
            revision_policy="state JSON plus ActionLedger record for every applied operation",
        ),
        "wiki_quality_report": CognitiveAssetDefinition(
            asset_type="wiki_quality_report",
            producers=("scripts.wiki_lint",),
            consumers=("mnemos_scorecard", "ActionLedger", "obsidian_ui", "content_reviewer"),
            source_fields=("vault_dir", "thresholds", "budget_file"),
            evidence_fields=("summary", "budgets", "manual_review", "action_ledger_ref"),
            privacy_level="wiki_quality",
            lifecycle_statuses=("verified", "degraded", "needs_user", "processing"),
            revision_policy="mnemos.wiki_quality.v1 summary plus budget owner or waiver per warning class",
        ),
    }

    QUALITY_GATE_DEFINITIONS: dict[str, QualityGateDefinition] = {
        "raw": QualityGateDefinition(
            "raw",
            "core.sync_framework.capture_service",
            "core.sync_framework.capture_queue",
            ("core/sync_framework/capture_service.py",),
            tuple(sorted(QUALITY_DECISIONS)),
        ),
        "document": QualityGateDefinition(
            "document",
            "core.sync_framework.file_ingestor",
            "core.application.facade.document_process",
            ("core/sync_framework/file_ingestor.py", "core/application/facade.py"),
            tuple(sorted(QUALITY_DECISIONS)),
        ),
        "distill": QualityGateDefinition(
            "distill",
            "core.hephaestus.quality_gate",
            "core.hephaestus.distill_action_router",
            ("core/hephaestus/quality_gate.py", "core/hephaestus/distillation_contract.py"),
            tuple(sorted(QUALITY_DECISIONS)),
        ),
        "wiki": QualityGateDefinition(
            "wiki",
            "scripts.wiki_lint",
            "core.vaults.content_audit",
            (
                "scripts/wiki_lint.py",
                "core/vaults/content_audit.py",
                "scripts/audit_wiki_quality_contract.py",
            ),
            tuple(sorted(QUALITY_DECISIONS)),
        ),
        "persona": QualityGateDefinition(
            "persona",
            "core.persona",
            "core.application.persona",
            ("core/persona", "core/application/persona.py"),
            tuple(sorted(QUALITY_DECISIONS)),
        ),
        "auto_heal": QualityGateDefinition(
            "auto_heal", "core.ops.auto_healing", "ActionLedger",
            ("core/ops/auto_healing.py", "core/kia/hygieia.py", "core/kia/issue_pipeline.py"),
            tuple(sorted(QUALITY_DECISIONS)),
        ),
        "activation_quality_decision": QualityGateDefinition(
            "activation_quality_decision",
            "core.module_toggles",
            "ActionLedger",
            ("core/module_toggles.py", "core/system_contracts.py"),
            tuple(sorted(QUALITY_DECISIONS)),
        ),
        "migration_quality_decision": QualityGateDefinition(
            "migration_quality_decision",
            "core.migrations.registry",
            "MigrationLedger",
            ("core/migrations/registry.py", "scripts/audit_migration_registry.py"),
            tuple(sorted(QUALITY_DECISIONS)),
        ),
        "restore_quality_decision": QualityGateDefinition(
            "restore_quality_decision",
            "core.backup.snapshot_manager",
            "ActionLedger",
            ("core/backup/snapshot_manager.py", "scripts/audit_backup_recovery_contract.py"),
            tuple(sorted(QUALITY_DECISIONS)),
        ),
        "data_ownership_quality_decision": QualityGateDefinition(
            "data_ownership_quality_decision",
            "core.privacy.data_ownership",
            "ActionLedger",
            ("core/privacy/data_ownership.py", "scripts/audit_data_ownership_contract.py"),
            tuple(sorted(QUALITY_DECISIONS)),
        ),
        "golden_benchmark_quality": QualityGateDefinition(
            "golden_benchmark_quality",
            "core.benchmarks.golden",
            "mnemos_scorecard",
            ("core/benchmarks/golden.py", "scripts/audit_golden_benchmark_contract.py"),
            tuple(sorted(QUALITY_DECISIONS)),
        ),
        "install_lifecycle_quality": QualityGateDefinition(
            "install_lifecycle_quality",
            "core.setup.install_lifecycle",
            "ActionLedger",
            ("core/setup/install_lifecycle.py", "scripts/audit_install_upgrade_contract.py"),
            tuple(sorted(QUALITY_DECISIONS)),
        ),
    }

    CAPABILITY_DEFINITIONS: dict[str, CapabilityDefinition] = {
        "llm": CapabilityDefinition(
            "llm",
            "core.llm_config",
            True,
            ("llm.api_key", "llm.base_url", "llm.model"),
            "health.api.models.llm",
            "scripts/auto_setup.py",
        ),
        "embedding": CapabilityDefinition(
            "embedding",
            "core.llm_config",
            True,
            ("embedding.api_key", "embedding.base_url", "embedding.model"),
            "health.api.models.embedding",
            "scripts/auto_setup.py",
        ),
        "reranker": CapabilityDefinition(
            "reranker",
            "core.llm_config",
            True,
            ("reranker.api_key", "reranker.base_url", "reranker.model"),
            "health.api.models.reranker",
            "scripts/auto_setup.py",
        ),
        "multimodal": CapabilityDefinition(
            "multimodal",
            "core.config",
            False,
            ("multimodal.enabled", "multimodal.model", "multimodal.api_key"),
            "health.api.models.multimodal",
            "docs/OPS_MANUAL.md",
            depends_on=("llm",),
        ),
        "obsidian_vault": CapabilityDefinition(
            "obsidian_vault",
            "core.config",
            True,
            ("vaults.mnemos.path", "wiki.vault_path"),
            "health.wiki",
            "mnemos init",
        ),
        "raw_vault": CapabilityDefinition(
            "raw_vault",
            "core.config",
            True,
            ("vaults.raw.path",),
            "health.storage",
            "mnemos init",
        ),
        "daemon": CapabilityDefinition(
            "daemon",
            "mnemos_daemon",
            False,
            ("daemon.services",),
            "health.daemon",
            "mnemos daemon start",
        ),
        "keyring": CapabilityDefinition(
            "keyring",
            "core.llm_key_pool",
            False,
            ("llm.api_key_source", "embedding.api_key_source", "reranker.api_key_source"),
            "security_audit.keyring_available",
            "scripts/auto_setup.py",
        ),
        "document_process": CapabilityDefinition(
            "document_process",
            "core.application.document_import_service",
            True,
            ("document_process.max_file_size_mb",),
            "mcp.document_process",
            "docs/AGENT_GUIDE.md",
        ),
        "module_toggle_registry": CapabilityDefinition(
            "module_toggle_registry",
            "core.module_toggles",
            True,
            ("daemon.services", "watchers.enabled", "features.enable_link_probe"),
            "health.module_toggles",
            "mnemos doctor modules --json",
        ),
        "migration_registry": CapabilityDefinition(
            "migration_registry",
            "core.migrations.registry",
            True,
            ("system.database_dir", "vaults.mnemos.path", "vaults.raw.path"),
            "health.migrations",
            "mnemos migrate status --json",
            depends_on=("obsidian_vault", "raw_vault", "snapshot_manager"),
        ),
        "snapshot_manager": CapabilityDefinition(
            "snapshot_manager",
            "core.backup.snapshot_manager",
            True,
            ("system.database_dir", "vaults.mnemos.path", "vaults.raw.path"),
            "health.backup",
            "mnemos backup create --dry-run --json",
            depends_on=("obsidian_vault", "raw_vault"),
        ),
        "data_ownership": CapabilityDefinition(
            "data_ownership",
            "core.privacy.data_ownership",
            True,
            ("raw_event_store.retention_days", "storage.retention_days", "model_call_ledger.daily_cost_cap"),
            "health.data_ownership",
            "mnemos data inventory --json",
            depends_on=("snapshot_manager", "migration_registry"),
        ),
        "golden_benchmark": CapabilityDefinition(
            "golden_benchmark",
            "core.benchmarks.golden",
            True,
            ("benchmarks.golden.manifest", "benchmarks.golden.baseline"),
            "health.golden_benchmark",
            "python3 scripts/run_golden_benchmark.py --strict --mock-llm",
            depends_on=("module_toggle_registry", "data_ownership"),
        ),
        "install_lifecycle": CapabilityDefinition(
            "install_lifecycle",
            "core.setup.install_lifecycle",
            True,
            ("system.database_dir", "vaults.mnemos.path", "vaults.raw.path"),
            "health.install_lifecycle",
            "mnemos setup --dry-run --json",
            depends_on=("migration_registry", "snapshot_manager", "data_ownership"),
        ),
    }

    PRIVACY_POLICIES: dict[str, PrivacyPolicy] = {
        "raw_event": PrivacyPolicy(
            "raw_event", "private", "sqlite", False, True, False, True, 180, "artifact_summary"
        ),
        "artifact": PrivacyPolicy(
            "artifact", "private", "filesystem", False, False, False, False, 30, "path_only"
        ),
        "persona": PrivacyPolicy(
            "persona", "sensitive", "sqlite/wiki", False, True, False, True, 180, "profile_summary"
        ),
        "document": PrivacyPolicy(
            "document", "private", "raw_vault/wiki", False, True, True, True, None, "source_refs_only"
        ),
        "reasoning": PrivacyPolicy(
            "reasoning", "restricted", "artifact", False, False, False, False, 30, "summary_only"
        ),
        "api_key": PrivacyPolicy(
            "api_key", "secret", "env_or_keyring", True, False, False, False, None, "never_log"
        ),
        "wiki_page": PrivacyPolicy(
            "wiki_page", "internal", "obsidian", False, True, True, True, None, "frontmatter_refs"
        ),
        "reflection": PrivacyPolicy(
            "reflection", "sensitive", "sqlite/wiki", False, True, True, True, 180, "recap_summary"
        ),
        "policy": PrivacyPolicy("policy", "internal", "sqlite/wiki", False, True, True, True, None, "evidence_ids"),
        "cognition_asset": PrivacyPolicy("cognition_asset", "private", "sqlite/wiki", False,
                                         True, True, True, None, "pii_credentials_only_v1"),
        "action_ledger": PrivacyPolicy(
            "action_ledger", "internal", "sqlite", False, True, False, False, None, "secret_redacted"
        ),
        "module_output": PrivacyPolicy(
            "module_output", "internal", "sqlite/json", False, True, False, True, 180, "contract_refs_only"
        ),
        "activation_evidence": PrivacyPolicy(
            "activation_evidence", "internal", "sqlite/json", False, True, False, False, 180, "metric_refs_only"
        ),
        "consumer_effect": PrivacyPolicy(
            "consumer_effect", "internal", "sqlite/json", False, True, False, False, 365, "aggregate_metrics"
        ),
        "migration_ledger": PrivacyPolicy(
            "migration_ledger", "internal", "sqlite", False, True, False, False, None, "path_and_hash_only"
        ),
        "snapshot_manifest": PrivacyPolicy(
            "snapshot_manifest", "restricted", "json", False, True, False, False, None, "hash_or_path_only"
        ),
        "data_export": PrivacyPolicy(
            "data_export", "sensitive", "json/archive", False, False, False, False, None, "secret_redacted_summary"
        ),
        "deletion_proof": PrivacyPolicy(
            "deletion_proof", "internal", "sqlite/json", False, True, False, False, None, "no_secret_no_pii"
        ),
        "frozen_data_request": PrivacyPolicy(
            "frozen_data_request", "sensitive", "sqlite", False, True, False, False, None, "scope_hash_only"
        ),
        "benchmark_result": PrivacyPolicy(
            "benchmark_result", "internal", "json/sqlite", False, True, False, False, None,
            "synthetic_fixture_only"
        ),
        "install_lifecycle": PrivacyPolicy(
            "install_lifecycle", "internal", "json/sqlite", False, True, False, False, None,
            "path_status_and_repair_actions_only"
        ),
        "wiki_quality": PrivacyPolicy(
            "wiki_quality", "internal", "json/sqlite", False, True, False, False, None,
            "path_samples_counts_and_owner_budget_only"
        ),
    }

    LIFECYCLE_MAPPINGS: dict[str, LifecycleMapping] = {
        "capture_events": LifecycleMapping(
            "capture_events",
            {
                "pending": "queued",
                "processing": "processing",
                "done": "captured",
                "failed": "failed_recoverable",
            },
            {"database is locked": "database_lock", "permission denied": "permission"},
        ),
        "sync_log": LifecycleMapping(
            "sync_log",
            {
                "pending": "queued",
                "synced": "produced",
                "skipped": "degraded",
                "failed": "failed_recoverable",
            },
            {"schema": "schema", "timeout": "timeout"},
        ),
        "distillation_tasks": LifecycleMapping(
            "distillation_tasks",
            {
                "pending": "queued",
                "processing": "processing",
                "done": "produced",
                "failed": "failed_recoverable",
                "archived": "expired",
            },
            {"invalid_json": "schema", "quality_reject": "quality", "rate_limit": "rate_limit"},
        ),
        "dialog_reminders": LifecycleMapping(
            "dialog_reminders",
            {"pending": "needs_user", "resolved": "consumed", "dismissed": "consumed", "expired": "expired"},
            {"owner_missing": "config", "user_skipped": "unknown"},
        ),
        "daemon_service": LifecycleMapping(
            "daemon_service",
            {
                "ok": "verified",
                "degraded": "degraded",
                "skipped": "degraded",
                "failed": "failed_recoverable",
                "auto_disabled": "auto_disabled",
            },
            {"OperationalError": "database_lock", "TimeoutError": "timeout"},
        ),
        "document_import": LifecycleMapping(
            "document_import",
            {
                "accepted": "queued",
                "parse_only": "produced",
                "rejected": "failed_terminal",
                "needs_review": "needs_user",
            },
            {"too_large": "quality", "privacy_block": "privacy", "parse_error": "schema"},
        ),
        "module_toggle": LifecycleMapping(
            "module_toggle",
            {
                "enabled": "verified",
                "disabled": "degraded",
                "disabled_default": "disabled_default",
                "disabled_cold_start": "disabled_cold_start",
                "eligible_to_enable": "eligible_to_enable",
                "auto_enabled": "auto_enabled",
                "manual_enabled": "manual_enabled",
                "auto_disabled": "auto_disabled",
                "registered_but_unwired": "registered_but_unwired",
                "stale_removed": "stale_removed",
                "manual_override": "needs_user",
            },
            {"missing_capability": "config", "regression": "quality"},
        ),
        "quality_decision": LifecycleMapping(
            "quality_decision",
            {
                "accept": "verified",
                "skip": "degraded",
                "degrade": "degraded",
                "needs_review": "needs_user",
                "auto_fix": "processing",
                "reject": "failed_terminal",
            },
            {"low_confidence": "quality", "privacy": "privacy"},
        ),
        "migration": LifecycleMapping(
            "migration",
            {
                "pending": "pending",
                "planned": "planned",
                "blocked": "blocked",
                "applying": "applying",
                "applied": "applied",
                "verified": "verified",
                "rolled_back": "rolled_back",
                "failed": "failed_recoverable",
            },
            {"missing_backup": "config", "schema_error": "schema", "permission": "permission"},
        ),
        "snapshot_restore": LifecycleMapping(
            "snapshot_restore",
            {
                "planned": "planned",
                "blocked": "blocked",
                "restoring": "restoring",
                "verified": "verified",
                "failed": "failed_recoverable",
                "partial_restored": "partial_restored",
            },
            {"checksum": "schema", "permission": "permission", "conflict": "conflict"},
        ),
        "data_ownership": LifecycleMapping(
            "data_ownership",
            {
                "requested": "requested",
                "dry_run_planned": "dry_run_planned",
                "frozen": "frozen",
                "deleting": "deleting",
                "deleted": "deleted",
                "blocked": "blocked",
                "partially_deleted": "partially_deleted",
                "verified": "verified",
            },
            {"missing_freeze": "privacy", "missing_snapshot": "privacy", "conflict": "conflict"},
        ),
        "golden_benchmark": LifecycleMapping(
            "golden_benchmark",
            {
                "captured": "captured",
                "accepted": "verified",
                "rejected": "degraded",
                "needs_review": "needs_user",
                "consumed": "consumed",
                "regression": "failed_recoverable",
            },
            {"schema": "schema", "quality": "quality", "baseline_regression": "quality"},
        ),
        "install_lifecycle": LifecycleMapping(
            "install_lifecycle",
            {
                "not_installed": "planned",
                "configuring": "processing",
                "installed_partial": "degraded",
                "installed_ready": "verified",
                "upgrade_available": "needs_user",
                "upgrading": "applying",
                "upgrade_failed": "failed_recoverable",
                "rollback_available": "needs_user",
                "uninstalled_preserve_data": "verified",
                "uninstall_blocked": "blocked",
                "uninstalled_purged": "verified",
            },
            {"missing_backup": "config", "missing_confirm": "privacy", "permission": "permission"},
        ),
        "wiki_quality": LifecycleMapping(
            "wiki_quality",
            {
                "ok": "verified",
                "within_budget": "degraded",
                "over_budget": "needs_user",
                "auto_fixable": "processing",
                "manual_review": "needs_user",
                "blocked_manual": "needs_user",
                "fixed": "verified",
            },
            {"missing_meta": "schema", "broken_link": "schema", "orphan": "quality", "stub": "quality"},
        ),
    }

    DOMAIN_TERMS: dict[str, DomainTerm] = {
        "cognitive_asset": DomainTerm(
            "cognitive_asset",
            "Any durable fact, decision, preference, method, anti-pattern, signal, validation result, "
            "or hypothesis that can change future behavior.",
            "core.system_contracts",
        ),
        "cognitive_decision_flywheel": DomainTerm(
            "cognitive_decision_flywheel",
            "The loop that turns repeated decisions, failures, validation recipes, and heuristics "
            "into reusable cognitive decision assets.",
            "core.kia.ixion",
            deprecated_aliases=("Skill 飞轮", "skill_flywheel"),
            migration_note="Automation skills are optional derivatives, not the primary product.",
        ),
        "trusted_user_document": DomainTerm(
            "trusted_user_document",
            "A user-specified document path intentionally supplied as high-value source material "
            "that should enter document_process and distillation gates.",
            "core.application.facade",
        ),
        "raw_event": DomainTerm(
            "raw_event",
            "Canonical captured source event before normalization, distillation, or projection.",
            "core.sync_framework",
            deprecated_aliases=("L1 memo", "external Memos row"),
            migration_note=(
                "Use raw_event for local canonical capture; external Memos/L1 is legacy storage "
                "compatibility only."
            ),
        ),
        "obsidian_presentation_layer": DomainTerm(
            "obsidian_presentation_layer",
            "The human-readable vault projection of cognitive assets, not the sole source of truth.",
            "core.hephaestus.wiki_builder",
        ),
        "quality_decision": DomainTerm(
            "quality_decision",
            "A structured admit, skip, degrade, review, auto-fix, or reject result with evidence and next action.",
            "core.system_contracts",
        ),
        "action_ledger": DomainTerm(
            "action_ledger",
            "A global append-only record of automatic actions, evidence, verification, and rollback references.",
            "core.system_contracts",
        ),
        "module_toggle": DomainTerm(
            "module_toggle",
            "A runtime capability switch with owner, default reason, auto-enable policy, "
            "disable policy, and manual override boundary.",
            "core.config",
        ),
        "cold_start": DomainTerm(
            "cold_start",
            "A default-off state where a module waits for data volume, consumer readiness, "
            "privacy, cost, and rollback proof.",
            "core.module_toggles",
        ),
        "auto_enable": DomainTerm(
            "auto_enable",
            "A quality-gated runtime activation that records an activation decision "
            "before changing an effective policy.",
            "core.module_toggles",
        ),
        "toggle_output_contract": DomainTerm(
            "toggle_output_contract",
            "The declaration of a module output type, storage reference, schema, "
            "consumers, effect metrics, and rollback.",
            "core.module_toggles",
            deprecated_aliases=("cold start output matrix",),
            migration_note=(
                "Use toggle_output_contract for the machine-readable registry; "
                "matrix is a report view only."
            ),
        ),
        "consumer_effect": DomainTerm(
            "consumer_effect",
            "A measured behavior change after a module output is consumed by a named downstream consumer.",
            "core.module_toggles",
        ),
        "migration_registry": DomainTerm(
            "migration_registry",
            "The canonical registry for config, database, vault, privacy, and system upgrade steps.",
            "core.migrations.registry",
            deprecated_aliases=("standalone migration script",),
            migration_note="Standalone scripts remain wrappers but must appear in the registry.",
        ),
        "snapshot_manifest": DomainTerm(
            "snapshot_manifest",
            "A checksum-backed manifest of config, SQLite, vault, ledger, and module state entries.",
            "core.backup.snapshot_manager",
        ),
        "data_ownership_contract": DomainTerm(
            "data_ownership_contract",
            "The user-facing contract for inventory, export, freeze, delete, and deletion proof.",
            "core.privacy.data_ownership",
        ),
        "deletion_proof": DomainTerm(
            "deletion_proof",
            "A secret-free proof that records deleted scope hash, affected domains, consumers, and verification.",
            "core.privacy.data_ownership",
        ),
        "golden_benchmark": DomainTerm(
            "golden_benchmark",
            "A deterministic fixture-backed benchmark that measures cognitive output quality and consumption effects.",
            "core.benchmarks.golden",
        ),
        "install_lifecycle": DomainTerm(
            "install_lifecycle",
            "The single user journey that connects setup, upgrade, uninstall, repair, migration, backup, "
            "verification, and ActionLedger evidence.",
            "core.setup.install_lifecycle",
        ),
        "wiki_quality_report": DomainTerm(
            "wiki_quality_report",
            "The stable mnemos.wiki_quality.v1 view of Wiki lint counts, budgets, unified lifecycle "
            "status, manual review queues, scorecard metrics, and auto-fix ledger evidence.",
            "scripts.wiki_lint",
        ),
    }

    SCORECARD_DIMENSIONS: dict[str, ScorecardDimension] = {
        "engineering_health": ScorecardDimension(
            "engineering_health",
            100,
            ("python3 scripts/run_tests.py quick", "python3 scripts/run_local_gates.py"),
            ("health.status", "local_gates.status"),
            ("6", "8", "9", "10", "14"),
            "scripts.audit_mnemos_scorecard",
        ),
        "deployment_migration": ScorecardDimension(
            "deployment_migration",
            100,
            (
                "python3 scripts/audit_install_upgrade_contract.py --strict",
                "python3 scripts/audit_migration_registry.py --strict",
                "python3 scripts/audit_backup_recovery_contract.py --strict",
                "python3 scripts/e2e_install_probe.py --tmp-home",
                "python3 scripts/e2e_upgrade_probe.py --tmp-home --preserve-existing",
            ),
            ("install_lifecycle.status", "migration_registry.status", "backup.status"),
            ("45", "46", "48"),
            "scripts.audit_mnemos_scorecard",
        ),
        "privacy_security": ScorecardDimension(
            "privacy_security",
            100,
            (
                ".venv/bin/python scripts/security_audit.py",
                "python3 scripts/audit_privacy_retention_policy.py --strict",
                "python3 scripts/audit_data_ownership_contract.py --strict",
            ),
            ("security.findings", "privacy_policy.coverage", "data_ownership.status"),
            ("15", "16", "17", "38", "49"),
            "scripts.audit_mnemos_scorecard",
        ),
        "data_pipeline": ScorecardDimension(
            "data_pipeline",
            100,
            (
                "python3 scripts/audit_data_interface_registry.py --strict",
                "python3 scripts/audit_runtime_producer_consumer_closure.py --strict",
            ),
            ("producer_consumer.closed_flows", "producer_consumer.orphan_outputs",
             "producer_consumer.no_source_consumers", "producer_consumer.item_mismatches",
             "producer_consumer.dead_letters", "duplicates.reconciled"),
            ("31", "34"), "scripts.audit_mnemos_scorecard",
        ),
        "cognitive_assets": ScorecardDimension(
            "cognitive_assets",
            100,
            (
                "python3 scripts/audit_cognitive_asset_schema.py --strict",
                "python3 scripts/audit_cognitive_readiness.py --json --budget",
                "python3 -m pytest tests/unit/test_distillation_json_metrics.py -q",
            ),
            (
                "asset_schema.coverage",
                "source_refs.coverage",
                "cognitive_readiness.budget_ok",
                "cognitive_readiness.failure_count",
                "distill_json_quality.final_failure_rate",
            ),
            ("1", "2", "11", "20", "21", "22", "35"),
            "scripts.audit_mnemos_scorecard",
        ),
        "user_profile": ScorecardDimension(
            "user_profile",
            100,
            ("python3 scripts/audit_persona_profile_contract.py --strict",),
            ("persona.assertions", "persona.consumption_effects"),
            ("29", "33"),
            "scripts.audit_mnemos_scorecard",
        ),
        "auto_healing": ScorecardDimension(
            "auto_healing",
            100,
            (
                "python3 -m pytest tests/unit/test_auto_healing_orchestrator.py -q",
                "python3 scripts/audit_action_ledger.py --strict",
                "python3 scripts/audit_lifecycle_status_contract.py --strict",
            ),
            ("auto_healing.user_intervention_budget", "action_ledger.verified_actions", "recoverable_failures.closed"),
            ("3", "4", "5", "26", "39", "40"),
            "scripts.audit_mnemos_scorecard",
        ),
        "obsidian_experience": ScorecardDimension(
            "obsidian_experience",
            100,
            (
                "python3 scripts/audit_wiki_quality_contract.py --strict",
                "python3 scripts/wiki_lint.py --summary --json --budget",
                "python3 scripts/reorganize_wiki.py --dry-run",
            ),
            ("wiki_quality.summary.errors", "wiki_quality.budgets.ok"),
            ("1", "23"),
            "scripts.audit_mnemos_scorecard",
        ),
        "wow_path": ScorecardDimension(
            "wow_path",
            150,
            ("python3 scripts/e2e_wow_probe.py --mock-llm", "python3 scripts/e2e_wow_probe.py --real-api"),
            ("wow.user_steps", "wow.behavior_change"),
            ("25", "32", "42"),
            "scripts.audit_mnemos_scorecard",
        ),
        "module_governance": ScorecardDimension(
            "module_governance",
            100,
            (
                "python3 scripts/audit_module_toggle_registry.py --strict",
                "python3 scripts/audit_toggle_output_consumers.py --strict",
            ),
            ("module_toggles.auto_enable_candidates", "module_toggles.consumer_effects"),
            ("43", "44"),
            "scripts.audit_mnemos_scorecard",
        ),
        "data_ownership": ScorecardDimension(
            "data_ownership",
            100,
            (
                "python3 scripts/audit_data_ownership_contract.py --strict",
                "python3 mnemos_cli.py data inventory --json",
            ),
            ("data_ownership.domains", "data_ownership.delete_status"),
            ("46", "49"),
            "scripts.audit_mnemos_scorecard",
        ),
        "golden_benchmark": ScorecardDimension(
            "golden_benchmark",
            100,
            (
                "python3 scripts/audit_golden_benchmark_contract.py --strict",
                "python3 scripts/run_golden_benchmark.py --strict --mock-llm",
            ),
            (
                "golden_benchmark.engineering_score",
                "golden_benchmark.cognitive_maturity_score",
                "golden_benchmark.wow_path_score",
            ),
            ("47",),
            "scripts.audit_mnemos_scorecard",
        ),
    }

    return {
        "lifecycle_statuses": LIFECYCLE_STATUSES,
        "failure_classes": FAILURE_CLASSES,
        "action_types": ACTION_TYPES,
        "action_statuses": ACTION_STATUSES,
        "actions_requiring_rollback": ACTIONS_REQUIRING_ROLLBACK,
        "cognitive_asset_definitions": COGNITIVE_ASSET_DEFINITIONS,
        "quality_gate_definitions": QUALITY_GATE_DEFINITIONS,
        "capability_definitions": CAPABILITY_DEFINITIONS,
        "privacy_policies": PRIVACY_POLICIES,
        "lifecycle_mappings": LIFECYCLE_MAPPINGS,
        "domain_terms": DOMAIN_TERMS,
        "scorecard_dimensions": SCORECARD_DIMENSIONS,
    }
