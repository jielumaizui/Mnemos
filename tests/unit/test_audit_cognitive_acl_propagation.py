from scripts.audit_cognitive_acl_propagation import (
    REPORT_SCHEMA_VERSION,
    build_report,
)
from scripts.cognitive_acl_deletion_effect_matrix import FAILURE_MODES


def test_cognitive_acl_propagation_audit_executes_the_hermetic_effect_matrix():
    report = build_report()

    assert report["schema_version"] == REPORT_SCHEMA_VERSION
    assert report["acl_unknown"] == 0
    assert report["default_public"] == 0
    assert report["cross_scope_leak"] == 0
    assert report["pre_body_authorization_gap"] == 0
    assert report["active_acl_lineage_gap"] == 0
    assert report["active_acl_authorization_gap"] == 0
    assert report["active_acl_inventory"]["coverage_gap"] == []
    assert report["missing_object_types"] == []
    assert report["required_object_type_denominator"]["scoring"] == [
        "training_queue",
        "ground_truth",
        "search_session",
        "feedback_event",
        "model",
        "bayesian_state",
        "bayesian_feedback",
        "feedback_prompt",
    ]
    assert report["verified_without_physical_effect"] == 0
    assert report["active_subject_rows_after_verified"] == 0
    assert report["derived_projection_gap"] == 0
    assert report["wiki_subject_deletion_receipt_schema"] is True
    assert report["adapter_coverage"]["wiki"] is True
    assert report["ok"] is True
    assert report["ann_index_subject_delete_owner"] is True
    assert report["ann_index_subject_delete_owner_kind"] == "wiki_lifecycle_required_consumer"
    assert report["embedding_cache_subject_delete_owner"] is True
    assert report["adapter_coverage"]["persona"] is True
    assert report["adapter_coverage"]["consumer_access_log"] is True
    assert report["adapter_coverage"]["evidence_refs"] is True
    assert report["adapter_coverage"]["agent_source_metadata"] is True
    assert "persona" not in report["unimplemented_domains"]
    assert "consumer_access_log" not in report["unimplemented_domains"]
    assert "evidence_refs" not in report["unimplemented_domains"]
    assert "agent_source_metadata" not in report["unimplemented_domains"]
    assert report["adapter_coverage"]["metadata"] is True
    assert report["adapter_coverage"]["action_ledger"] is True
    assert report["adapter_coverage"]["scoring"] is True
    assert "metadata" not in report["unimplemented_domains"]
    assert "action_ledger" not in report["unimplemented_domains"]
    assert "scoring" not in report["unimplemented_domains"]
    assert "metadata" in report["physical_delete_owners"]
    assert "scoring" in report["physical_delete_owners"]
    assert "action_ledger" in report["tombstone_only_owners"]
    assert report["effect_matrix"]["final_delete_status"] == "verified"
    assert report["effect_matrix"]["snapshot_retained"] is True
    assert report["failure_probe_denominator"] == list(FAILURE_MODES)
    assert set(report["failure_probes"]) == set(FAILURE_MODES)
    assert all(probe["ok"] for probe in report["failure_probes"].values())
    assert report["target_failure_probe"]["final_delete_status"] in {
        "partially_deleted",
        "blocked",
    }
    assert report["errors"] == []
