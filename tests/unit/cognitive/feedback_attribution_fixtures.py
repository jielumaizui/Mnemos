from __future__ import annotations

from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.feedback_contract import reaction_input_hash
from core.cognitive.state_contract import sha256_json


TARGET_IDS = (
    "belief_correction_proposal",
    "delivery_state",
    "persona_proposal",
    "policy_proposal",
    "reflection_evidence",
    "training_evidence",
    "trust_proposal",
)


def access_control() -> dict:
    return make_cognitive_access_envelope(
        owner_principal_id="user:feedback-test",
        owner_agent="mnemos",
        scope_type="session",
        scope_id="session-feedback",
        session_id="session-feedback",
        project="mnemos",
        purposes=("cognitive_state_read", "cognitive_state_write"),
        consent_provenance_refs=("raw-event:feedback#0:8",),
        sensitivity="sensitive",
        retention_policy="inherit_source",
        source_acl_lineage=("sha256:" + "a" * 64,),
    )


def _unavailable_ref() -> dict:
    return {
        "state": "unavailable",
        "id": "",
        "revision_id": "",
        "content_hash": "",
        "unavailable_reason": "not_applicable",
    }


def reaction_payload(*, kind: str = "accept") -> dict:
    fact_name = {
        "accept": "accepted",
        "ignore": "ignored",
        "dismiss": "dismissed",
        "inaccurate": "inaccurate",
        "outdated": "outdated",
        "accurate": "accurate",
        "useful": "useful",
        "insightful": "insightful",
        "irrelevant": "irrelevant",
        "opened": "opened",
        "clicked": "clicked",
        "read": "read",
        "repeated_query": "repeated_query",
        "no_click": "no_click",
        "silence_window_closed": "silence_window_closed",
    }.get(kind, "accepted")
    explicit = kind in {
        "accept",
        "ignore",
        "dismiss",
        "inaccurate",
        "outdated",
        "accurate",
        "useful",
        "insightful",
        "irrelevant",
    }
    payload = {
        "schema_version": "mnemos.user_reaction_event.v1",
        "reaction_id": "reaction-" + "1" * 32,
        "revision_state": "recorded",
        "reaction_input_hash": "",
        "source_event_ref": {
            "event_id": "feedback-source-event",
            "source_revision_id": "raw-feedback-revision",
            "content_hash": "sha256:" + "2" * 64,
        },
        "observed_at": "2026-07-18T00:00:00+00:00",
        "recorded_at": "2026-07-18T00:00:01+00:00",
        "supersedes_event_id": "",
        "correction_of_event_id": "",
        "principal_ref": {
            "principal_id": "user:feedback-test",
            "agent": "mnemos",
            "authorization_ref": "authz:feedback-test",
        },
        "scope": {
            "type": "session",
            "id": "session-feedback",
            "project": "mnemos",
            "session_id": "session-feedback",
        },
        "source_channel": "predictive_push",
        "authority_class": "explicit_user" if explicit else "tool_observation",
        "subject_ref": {"type": "delivery", "id": "delivery-1"},
        "decision_ref": _unavailable_ref(),
        "prediction_ref": _unavailable_ref(),
        "action_ref": _unavailable_ref(),
        "delivery_ref": {
            "state": "available",
            "event_id": "delivery-1",
            "event_payload_hash": "sha256:" + "3" * 64,
            "unavailable_reason": "",
        },
        "display_ref": {
            "state": "available",
            "display_id": "display-1",
            "content_hash": "sha256:" + "4" * 64,
            "unavailable_reason": "",
        },
        "search_ref": {
            "state": "unavailable",
            "session_id": "",
            "result_id": "",
            "exposure_id": "",
            "unavailable_reason": "not_a_search_interaction",
        },
        "interaction": {
            "kind": kind,
            "observed_facts": [{"name": fact_name, "value": True}],
        },
        "evidence": {
            "refs": ["raw-event:feedback#0:8"],
            "content_hashes": ["sha256:" + "5" * 64],
        },
        "observation_window": {
            "starts_at": "2026-07-18T00:00:00+00:00",
            "ends_at": "2026-07-18T00:00:00+00:00",
            "status": "closed",
        },
        "exposure": {
            "session_id": "session-feedback",
            "exposure_id": "exposure-1",
            "interface_id": "predictive-push-card",
            "was_visible": True,
        },
        "competing_causes": [],
        "source_completeness": {"state": "complete", "missing_refs": []},
        "attribution": {
            "method": "conservative_observation",
            "version": "v1",
            "code_hash": "sha256:" + "6" * 64,
            "spec_hash": "sha256:" + "7" * 64,
            "disposition": "record_only",
            "evidence_class": "explicit_preference",
        },
        "downstream": {
            "registry_version": "mnemos.feedback_target_registry.v1",
            "required_targets": list(TARGET_IDS),
            "eligible_targets": [],
            "exclusions": [
                {"target_id": target_id, "reason": "single_reaction_record_only"}
                for target_id in TARGET_IDS
            ],
        },
        "correction": {"state": "none", "target_ref": "", "reason": ""},
        "access_control": access_control(),
    }
    if kind in {"inaccurate", "outdated"}:
        payload["attribution"]["disposition"] = "correction_pending"
        payload["attribution"]["evidence_class"] = "explicit_correction"
    elif not explicit:
        payload["attribution"]["evidence_class"] = "weak_behavior"
    payload["reaction_input_hash"] = reaction_input_hash(payload)
    return payload


def attribution_payload() -> dict:
    reaction_refs = [
        {
            "reaction_id": "reaction-" + "1" * 32,
            "revision_id": "cogrev-" + "8" * 32,
            "payload_hash": "sha256:" + "9" * 64,
        }
    ]
    outcome_refs: list[dict] = []
    independence_keys = ["session:session-feedback|exposure:exposure-1"]
    method = {
        "name": "conservative_feedback_attribution",
        "version": "v1",
        "code_hash": "sha256:" + "a" * 64,
        "spec_hash": "sha256:" + "b" * 64,
        "config_hash": "sha256:" + "c" * 64,
    }
    registry = {
        "version": "mnemos.feedback_target_registry.v1",
        "registry_hash": sha256_json(
            {
                "version": "mnemos.feedback_target_registry.v1",
                "targets": list(TARGET_IDS),
            }
        ),
        "targets": list(TARGET_IDS),
    }
    input_set_hash = sha256_json(
        {
            "reaction_refs": reaction_refs,
            "outcome_refs": outcome_refs,
            "independence_keys": independence_keys,
            "method": method,
            "target_registry": registry,
        }
    )
    return {
        "schema_version": "mnemos.feedback_attribution_record.v1",
        "attribution_id": "feedback-attribution-" + "d" * 32,
        "revision_state": "current",
        "subject_ref": {"type": "delivery", "id": "delivery-1"},
        "scope": {
            "type": "session",
            "id": "session-feedback",
            "project": "mnemos",
            "session_id": "session-feedback",
        },
        "reaction_refs": reaction_refs,
        "outcome_refs": outcome_refs,
        "input_set_hash": input_set_hash,
        "independence_keys": independence_keys,
        "method": method,
        "evidence_class": "explicit_preference",
        "materiality": {
            "decision": "record_only",
            "observation_count": 1,
            "distinct_session_count": 1,
            "distinct_exposure_count": 1,
            "span_seconds": 0,
            "minimum_event_count": 3,
            "minimum_independence_count": 2,
            "minimum_span_seconds": 86400,
            "conflict_state": "clear",
        },
        "competing_causes": [],
        "uncertainty": {"kind": "conservative", "value": None},
        "disposition": "record_only",
        "post_neutralization_disposition": "record_only",
        "target_registry": registry,
        "target_dispositions": [
            {
                "target_id": target_id,
                "eligible": False,
                "exclusion_reason": "single_explicit_preference_record_only",
                "command_ref": {
                    "command_key": f"feedback-target:{target_id}:" + "e" * 32,
                    "command_type": "evaluate_feedback_target",
                },
            }
            for target_id in TARGET_IDS
        ],
        "supersedes_revision_id": "",
        "correction_of_revision_id": "",
        "access_control": access_control(),
    }
