from __future__ import annotations

import pytest

from scripts.cognitive_acl_deletion_effect_matrix import FAILURE_MODES, run_effect_matrix


def test_hermetic_effect_matrix_requires_real_targets_in_every_declared_domain():
    report = run_effect_matrix()

    assert report["ok"] is True, report
    assert report["first_delete_status"] == "partially_deleted"
    assert report["final_delete_status"] == "verified"
    assert report["acl_unknown"] == 0
    assert report["default_public"] == 0
    assert report["verified_without_physical_effect"] == 0
    assert report["active_subject_rows_after_verified"] == 0
    assert report["derived_projection_gap"] == 0
    assert set(report["domain_target_counts"]) == set(report["required_domains"])
    assert all(count > 0 for count in report["domain_target_counts"].values())
    assert set(report["scoring_object_effect_counts"]) == {
        "training_queue",
        "ground_truth",
        "search_session",
        "feedback_event",
        "model",
        "bayesian_state",
        "bayesian_feedback",
        "feedback_prompt",
    }
    assert all(report["scoring_object_effect_counts"].values())
    assert report["active_acl_inventory"]["active_acl_authorization_gap"] == 0
    assert report["active_acl_inventory"]["object_type_denominator"]["scoring"] == [
        "training_queue",
        "ground_truth",
        "search_session",
        "feedback_event",
        "model",
        "bayesian_state",
        "bayesian_feedback",
        "feedback_prompt",
    ]
    assert report["wiki_required_consumers_terminal"] is True
    assert report["snapshot_retained"] is True
    assert report["snapshot_verification"]["valid"] is True
    relation_target = report["relation_embedding_target"]
    assert relation_target["before_relation_count"] > 0
    assert relation_target["before_embedding_count"] > 0
    assert relation_target["after_relation_count"] == 0
    assert relation_target["after_embedding_count"] == 0
    assert relation_target["after_outbox_count"] == 0


def test_effect_matrix_recovers_pending_delete_after_runtime_restart():
    report = run_effect_matrix()

    recovery = report["restart_recovery"]
    assert recovery["event_bus_reopened"] is True
    assert recovery["pending_before_restart"] > 0
    assert recovery["processing_after_restart"] > 0
    assert recovery["pending_after_dispatch"] == 0
    assert recovery["processing_after_dispatch"] == 0
    assert report["final_delete_status"] == "verified"


def test_effect_matrix_restricts_every_multi_source_acl_merge():
    report = run_effect_matrix()

    merge = report["multi_source_acl_merge"]
    assert merge["compatible_merge_resolved"] is True
    assert merge["source_lineage_complete"] is True
    assert merge["purpose_intersection_enforced"] is True
    assert merge["strictest_sensitivity_inherited"] is True
    assert merge["broader_purpose_restricted"] is True


def test_effect_matrix_propagates_user_correction_into_subject_deletion():
    report = run_effect_matrix()

    correction = report["user_correction"]
    assert correction["original_revision_preserved"] is True
    assert correction["corrected_revision_was_current"] is True
    assert correction["correction_link_exact"] is True
    assert correction["current_revision_after_delete"] is False


def test_effect_matrix_revokes_every_previously_authorized_typed_object():
    report = run_effect_matrix()

    revocation = report["authorization_revocation"]
    assert revocation["domain_denominator"] == [
        "cognitive_graph",
        "cognitive_state",
        "observation",
        "persona",
        "reflection",
        "reflection_layer5",
    ]
    assert all(revocation["authorized_before_delete"].values())
    assert not any(revocation["authorized_after_delete"].values())
    assert revocation["post_delete_authorization_leak"] == 0


def test_effect_matrix_freeze_blocks_new_ids_for_every_typed_cognitive_owner():
    report = run_effect_matrix()

    barrier = report["freeze_resurrection_barrier"]
    assert barrier["domain_denominator"] == [
        "action_ledger",
        "cognitive_graph",
        "cognitive_state",
        "event_bus",
        "observation",
        "persona",
        "reflection",
        "scoring",
    ]
    assert all(barrier["blocked"].values())
    assert barrier["resurrection_gap"] == 0
    assert barrier["object_denominator"] == [
        "scoring_bayesian_feedback_state",
        "scoring_feedback_event",
        "scoring_feedback_prompt",
        "scoring_model",
        "scoring_search_session",
        "scoring_training_queue_ground_truth",
    ]
    assert all(barrier["object_blocked"].values())
    assert barrier["object_resurrection_gap"] == 0


def test_effect_matrix_rejects_but_retains_an_expired_delete_snapshot():
    report = run_effect_matrix()

    expiry = report["expired_snapshot_lifecycle"]
    assert expiry["verification_valid"] is False
    assert expiry["retention_status"] == "expired"
    assert expiry["retained_until_explicit_prune"] is True
    assert expiry["explicit_prune_candidate"] is True


@pytest.mark.parametrize("failure_mode", FAILURE_MODES)
def test_hermetic_effect_matrix_never_verifies_any_injected_target_failure(
    failure_mode,
):
    report = run_effect_matrix(failure_mode=failure_mode)

    assert report["ok"] is True, report
    assert report["failure_mode"] == failure_mode
    assert report["final_delete_status"] in {"partially_deleted", "blocked"}
    assert report["verified_without_physical_effect"] == 0
    assert report["snapshot_retained"] is True
    if failure_mode.startswith("wiki_consumer:"):
        assert report["derived_projection_gap"] > 0
