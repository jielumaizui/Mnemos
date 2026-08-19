"""Static denominators for the COG-036 decision/effect audit."""

from __future__ import annotations

import re

REPORT_SCHEMA_VERSION = "mnemos.decision_trace_effect_audit.v1"
ZERO_METRICS = (
    "decision_without_action_terminal",
    "action_without_decision",
    "decision_without_value_context",
    "value_context_revision_missing",
    "decision_snapshot_unresolvable",
    "snapshot_hash_mismatch",
    "decision_snapshot_source_purpose_contract_gap",
    "value_ref_missing",
)
PROHIBITED_REASONING_FIELDS = frozenset(
    {
        "chain_of_thought",
        "scratchpad",
        "private_reasoning",
        "hidden_reasoning",
        "reasoning_trace",
    }
)
CANONICAL_GUARD_MODULE = "core.cognitive.decision_trace"
CANONICAL_GUARD_CALLS = frozenset(
    {
        "require_material_action",
        "require_material_action_projection",
        "resolve_material_action_authorization",
        "material_action_resolution_scope",
    }
)


# Exact production seams.  A directory/file marker is never sufficient: each
# named callable must contain its own canonical authorization call.  Local
# helpers are deliberately not authorization evidence: a conditional helper can
# contain a real guard while still returning without executing it.
SINK_CONTRACTS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "core/cognitive/delivery_router.py",
        "route_candidate",
        "require_material_action",
        ("_log_event",),
    ),
    (
        "core/trust/vault_mutation_service.py",
        "submit_markdown",
        "resolve_material_action_authorization",
        ("submit_candidate",),
    ),
    (
        "core/trust/vault_mutation_service.py",
        "commit_trusted_markdown",
        "require_material_action",
        ("atomic_write_text",),
    ),
    (
        "core/trust/vault_mutation_service.py",
        "commit_trusted_markdown_delete",
        "require_material_action",
        ("unlink",),
    ),
    (
        "core/trust/vault_mutation_service.py",
        "commit_trusted_markdown_move",
        "require_material_action",
        ("atomic_write_text", "unlink"),
    ),
    (
        "core/trust/knowledge_vault_writer.py",
        "write_proposal",
        "require_material_action",
        ("write", "_delete_native_store_record"),
    ),
    ("core/cognitive/policy_patch.py", "propose", "require_material_action", ("sql_write",)),
    (
        "core/cognitive/policy_patch.py",
        "reconcile_trigger_terms",
        "require_material_action",
        ("sql_write",),
    ),
    (
        "core/cognitive/policy_patch.py",
        "record_feedback",
        "require_material_action",
        ("sql_write",),
    ),
    (
        "core/persona/psyche_persona.py",
        "save_persona_version",
        "require_material_action",
        ("sql_write",),
    ),
    ("core/kia/chronos.py", "schedule", "require_material_action", ("_insert_task",)),
    ("core/kia/chronos.py", "_run_step", "require_material_action", ("func",)),
    ("core/ops/auto_healing.py", "_apply_handler", "require_material_action", ("execute",)),
    (
        "core/ops/action_ledger.py",
        "_record_primary_material",
        "require_material_action",
        ("_persist_action_ledger_record",),
    ),
    (
        "core/ops/action_ledger.py",
        "_record_material_projection",
        "require_material_action_projection",
        ("_persist_action_ledger_record",),
    ),
    (
        "core/trust/formal_cognitive_mutation.py",
        "record",
        "require_material_action_projection",
        ("sql_write",),
    ),
    (
        "core/kia/knowledge_graph.py",
        "add_relation",
        "require_material_action",
        ("upsert_relation_row",),
    ),
    (
        "core/kia/relation_manager.py",
        "_commit_relation_actions",
        "require_material_action",
        ("upsert_relation_row",),
    ),
    (
        "core/kia/relation_manager.py",
        "update_confidence",
        "require_material_action",
        ("sql_write",),
    ),
    (
        "core/cognitive_graph/store_mutations.py",
        "add_relation",
        "require_material_action",
        ("sql_write",),
    ),
    (
        "core/cognitive_graph/store_mutations.py",
        "add_relations_atomic",
        "require_material_action",
        ("sql_write",),
    ),
    (
        "core/cognitive_graph/store_mutations.py",
        "add_canonical_node",
        "require_material_action",
        ("_upsert_canonical_node_in_conn",),
    ),
    (
        "core/cognitive_graph/store_mutations.py",
        "mark_stale",
        "require_material_action",
        ("sql_write",),
    ),
    (
        "core/cognitive_graph/store_mutations.py",
        "delete_relation",
        "require_material_action",
        ("sql_write",),
    ),
    (
        "core/hephaestus/cognitive_action_targets.py",
        "_execute_plan",
        "require_material_action",
        ("apply_service",),
    ),
    (
        "daemon/wiki_projection_handlers.py",
        "_kg_handler",
        "require_material_action",
        ("on_distilled",),
    ),
    (
        "daemon/wiki_projection_handlers.py",
        "_kg_page_updated_handler",
        "require_material_action",
        ("on_page_updated",),
    ),
    (
        "daemon/wiki_projection_handlers.py",
        "_metrics_page_updated_handler",
        "require_material_action",
        ("reconcile_page_lifecycle",),
    ),
    (
        "daemon/wiki_projection_handlers.py",
        "_relation_embeddings_handler",
        "require_material_action",
        ("repair_relation_embedding_orphans", "rebuild_relation_index"),
    ),
    (
        "daemon/wiki_projection_handlers.py",
        "_moc_navigation_handler",
        "require_material_action",
        ("apply_navigation_plan",),
    ),
    (
        "daemon/wiki_projection_handlers.py",
        "_wiki_search_index_handler",
        "require_material_action",
        ("build_index",),
    ),
)

# These public entrypoints construct immutable successors and delegate the only
# material write to ``save_persona_version``, which is permit-dominated above.
# They remain in the audit denominator: a direct write or a dropped
# authorization handoff must fail closed.
DELEGATED_SINK_CONTRACTS: tuple[tuple[str, str, str, str], ...] = (
    (
        "core/persona/psyche_persona.py",
        "update_blindspot_profile",
        "save_persona_version",
        "material_action",
    ),
    (
        "core/persona/psyche_persona.py",
        "record_persona_calibration",
        "save_persona_version",
        "material_action",
    ),
)
ACTION_LEDGER_PERSISTENCE_CALL = "_persist_action_ledger_record"
ACTION_LEDGER_PERSISTENCE_CALLERS = frozenset(
    {
        ("core/ops/action_ledger.py", "_record_diagnostic_observation"),
        ("core/ops/action_ledger.py", "_record_primary_material"),
        ("core/ops/action_ledger.py", "_record_material_projection"),
    }
)
ACTION_LEDGER_OBSERVATION_CALL = "record_observation"
ACTION_LEDGER_OBSERVATION_CALLERS = frozenset(
    {
        ("core/benchmarks/golden.py", "_record_action"),
        (
            "core/hephaestus/distillation_quality.py",
            "_record_quality_gate_action",
        ),
        (
            "core/ops/cognitive_readiness.py",
            "record_cognitive_readiness_gaps",
        ),
        ("scripts/audit_cognitive_state_store.py", "_synthetic_state_audit"),
        ("scripts/audit_orphan_modules.py", "_record_repo_report_action"),
        (
            "scripts/cognitive_acl_deletion_effect_fixtures.py",
            "_seed_non_wiki_domains",
        ),
    }
)
ACTION_LEDGER_OBSERVATION_CALL_COUNTS = {
    caller: (
        2 if caller == ("scripts/audit_cognitive_state_store.py", "_synthetic_state_audit") else 1
    )
    for caller in ACTION_LEDGER_OBSERVATION_CALLERS
}
ACTION_LEDGER_DIRECT_SQL_SITES = frozenset(
    {
        (
            "core/ops/action_ledger.py",
            "_persist_action_ledger_record",
            "insert",
        ),
        (
            "core/ops/action_ledger_schema.py",
            "reconcile_action_ledger_schema",
            "insert",
        ),
        (
            "scripts/audit_cognitive_state_store.py",
            "_synthetic_state_audit",
            "update",
        ),
        (
            "scripts/audit_cognitive_state_store.py",
            "_synthetic_state_audit",
            "delete",
        ),
    }
)
_ACTION_LEDGER_DML_PATTERN = re.compile(
    r"\b(?P<kind>insert(?:\s+or\s+\w+)?\s+into|replace\s+into|"
    r"update|delete\s+from)\s+[\"'`\[]?action_ledger\b",
    re.IGNORECASE,
)
TARGET_EFFECT_LEDGER_FAMILIES = frozenset(
    {
        ("policy_patch", "policy_patch_store", "policy_patch_propose"),
        ("policy_patch", "policy_patch_store", "policy_patch_reconcile"),
        ("policy_patch", "policy_patch_store", "policy_patch_feedback"),
        ("cognitive_graph", "cognitive_graph_store", "upsert_relation"),
        ("cognitive_graph", "cognitive_graph_store", "mark_relation_stale"),
        ("cognitive_graph", "cognitive_graph_store", "delete_relation"),
        ("cognitive_graph", "cognitive_graph_store", "upsert_canonical_node"),
        ("knowledge_graph", "knowledge_graph", "upsert_relation"),
        ("knowledge_graph", "relation_manager", "upsert_relation"),
        ("persona", "signal_store", "save_persona_version"),
        ("persona", "signal_store", "update_persona_blindspot"),
        ("persona", "signal_store", "calibrate_persona"),
    }
)
