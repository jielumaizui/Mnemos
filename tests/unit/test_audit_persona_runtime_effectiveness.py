from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_persona_runtime_effectiveness.py"


def _module():
    spec = importlib.util.spec_from_file_location("persona_runtime_effectiveness", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_preflight_usage(
    db_path: Path,
    *,
    before: object = "base",
    after: object = "base\nprofile",
    target_type: str = "prompt",
    target_id: str = "preflight_persona_section",
    receipt_id: str = "runtime-temporal-target",
) -> tuple[str, str]:
    from core.access_policy import AccessNarrowing, PrincipalEnvelope
    from core.cognitive.access_control import make_cognitive_access_envelope
    from core.persona.profile_effect import compare_profile_effect
    from core.persona.psyche import (
        ProfileAssertion,
        ProfileSignal,
        ProfileUsageLog,
        SignalStore,
    )

    store = SignalStore(initialize_schema=True, db_path=db_path)
    signal_id = store.record_profile_signal(
        ProfileSignal(
            source_event_id="session:runtime-temporal",
            signal_type="explicit_preference",
            dimension="judgment_standard",
            value="需要证据",
            evidence="explicit user evidence",
            confidence=0.9,
            privacy_level="local",
            observed_at="2026-07-23T00:00:00+00:00",
            access_control=make_cognitive_access_envelope(
                owner_principal_id="mcp:codex:runtime-temporal",
                owner_agent="codex",
                scope_type="session",
                scope_id="runtime-temporal-session",
                session_id="runtime-temporal-session",
                purposes=("persona_preflight_read", "persona_usage_metrics"),
                consent_provenance_refs=("session:runtime-temporal",),
                sensitivity="sensitive",
                retention_policy="persona_retention",
                source_acl_lineage=("sha256:runtime-temporal",),
                visibility="agent",
            ),
        )
    )
    assertion_id = store.upsert_profile_assertion(
        ProfileAssertion(
            assertion_id="pa_runtime_temporal",
            dimension="judgment_standard",
            claim="用户要求回答附带证据。",
            supporting_signals=[f"profile_signals:{signal_id}"],
            confidence=0.9,
            privacy_level="local",
            last_verified_at="2026-07-23T00:00:00+00:00",
        )
    )
    revision_id = str(store.get_profile_assertion_revisions(assertion_id)[-1]["revision_id"])
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:runtime-temporal",
        agent="codex",
        host_kind="codex",
        capability_id="runtime-temporal",
        capabilities=frozenset({"memory_read"}),
    )
    narrowing = AccessNarrowing(session_id="runtime-temporal-session")
    _profile, access = store.build_authorized_user_cognitive_profile_v2(
        principal=principal,
        narrowing=narrowing,
        purpose="persona_preflight_read",
        consumer="preflight_builder",
    )
    receipt = compare_profile_effect(
        owner="preflight_builder",
        target_type=target_type,
        target_id=target_id,
        matched_assertion_revisions={assertion_id: revision_id},
        baseline_output=before,
        persona_enabled_output=after,
        expected_delta={
            "kind": "prompt_append",
            "section": "user_cognitive_profile_v2",
            "emitted_assertion_revisions": {assertion_id: revision_id},
        },
        receipt_id=receipt_id,
    )
    store.record_profile_usage(
        ProfileUsageLog(
            consumer="preflight_builder",
            profile_fields_used=[assertion_id],
            read_purpose="persona_preflight_read",
            read_authorization_token=str(access["read_authorization_token"]),
            target_receipt=receipt,
            outcome="persona_section_augmented",
        ),
        principal=principal,
        narrowing=narrowing,
    )
    store.close()
    return assertion_id, revision_id


def test_runtime_effectiveness_audit_never_creates_an_uninitialized_store(tmp_path):
    db_path = tmp_path / "missing-user-signals.db"

    payload = _module().audit_persona_runtime_effectiveness(db_path)

    assert payload["read_only"] is True
    assert payload["seeded_by_audit"] is False
    assert payload["errors"] == ["persona_signal_store_uninitialized"]
    assert not db_path.exists()


def test_runtime_effectiveness_audit_reads_existing_store_without_seeding(tmp_path):
    from core.persona.psyche import SignalStore

    db_path = tmp_path / "user_signals.db"
    store = SignalStore(initialize_schema=True, db_path=db_path)
    try:
        before = db_path.read_bytes()
        payload = _module().audit_persona_runtime_effectiveness(db_path)
        after = db_path.read_bytes()
    finally:
        store.close()

    assert after == before
    assert payload["counts"] == {
        "profile_signals": 0,
        "profile_assertions": 0,
        "profile_assertion_revisions": 0,
        "profile_usage_log": 0,
    }
    assert "production_signal_denominator_zero" in payload["errors"]
    assert "production_active_assertion_denominator_zero" in payload["errors"]
    assert "production_usage_denominator_zero" in payload["errors"]


def test_runtime_effectiveness_audit_accepts_real_revision_and_scope_receipts(tmp_path):
    from core.access_policy import AccessNarrowing, PrincipalEnvelope
    from core.cognitive.access_control import make_cognitive_access_envelope
    from core.persona.psyche import (
        ProfileAssertion,
        ProfileSignal,
        ProfileUsageLog,
        SignalStore,
    )

    db_path = tmp_path / "user_signals.db"
    store = SignalStore(initialize_schema=True, db_path=db_path)
    try:
        signal_id = store.record_profile_signal(
            ProfileSignal(
                source_event_id="session:runtime-audit",
                signal_type="explicit_preference",
                dimension="judgment_standard",
                value="需要证据",
                evidence="explicit user evidence",
                confidence=0.9,
                privacy_level="local",
                observed_at="2026-07-23T00:00:00",
                access_control=make_cognitive_access_envelope(
                    owner_principal_id="mcp:codex:runtime-audit",
                    owner_agent="codex",
                    scope_type="session",
                    scope_id="runtime-audit-session",
                    session_id="runtime-audit-session",
                    purposes=(
                        "persona_preflight_read",
                        "persona_usage_metrics",
                    ),
                    consent_provenance_refs=("session:runtime-audit",),
                    sensitivity="sensitive",
                    retention_policy="persona_retention",
                    source_acl_lineage=("sha256:runtime-audit",),
                    visibility="agent",
                ),
            )
        )
        assertion_id = store.upsert_profile_assertion(
            ProfileAssertion(
                assertion_id="pa_runtime_audit",
                dimension="judgment_standard",
                claim="用户要求回答附带证据。",
                supporting_signals=[f"profile_signals:{signal_id}"],
                confidence=0.9,
                privacy_level="local",
                last_verified_at="2026-07-23T00:00:00",
            )
        )
        from core.persona.profile_effect import compare_profile_effect

        revision_id = str(store.get_profile_assertion_revisions(assertion_id)[-1]["revision_id"])
        read_principal = PrincipalEnvelope(
            principal_id="mcp:codex:runtime-audit",
            agent="codex",
            host_kind="codex",
            capability_id="runtime-audit",
            capabilities=frozenset({"memory_read"}),
        )
        read_narrowing = AccessNarrowing(session_id="runtime-audit-session")
        _profile, access = store.build_authorized_user_cognitive_profile_v2(
            principal=read_principal,
            narrowing=read_narrowing,
            purpose="persona_preflight_read",
            consumer="preflight_builder",
        )
        receipt = compare_profile_effect(
            owner="preflight_builder",
            target_type="prompt",
            target_id="preflight_persona_section",
            matched_assertion_revisions={assertion_id: revision_id},
            baseline_output="base",
            persona_enabled_output="base\nprofile",
            expected_delta={
                "kind": "prompt_append",
                "section": "user_cognitive_profile_v2",
                "emitted_assertion_revisions": {
                    assertion_id: revision_id,
                },
            },
            receipt_id="runtime-audit-target",
        )
        store.record_profile_usage(
            ProfileUsageLog(
                consumer="preflight_builder",
                profile_fields_used=[assertion_id],
                read_purpose="persona_preflight_read",
                read_authorization_token=str(access["read_authorization_token"]),
                target_receipt=receipt,
                outcome="persona_section_augmented",
            ),
            principal=read_principal,
            narrowing=read_narrowing,
        )
    finally:
        store.close()

    payload = _module().audit_persona_runtime_effectiveness(db_path)

    assert payload["ok"] is True
    assert payload["usage_without_revisions"] == 0
    assert payload["usage_without_scope"] == 0
    assert payload["usage_action_changed_without_counterfactual_delta"] == 0
    assert payload["usage_without_exact_matched_revision"] == 0
    assert payload["effect_without_target_receipt"] == 0
    assert payload["before_hash_equals_after_hash_marked_changed"] == 0
    assert payload["expired_or_conflicted_effect"] == 0
    assert payload["correction_makes_old_assertion_effect"] == 0
    assert payload["persona_effect_without_usage_outbox"] == 0
    assert payload["committed_effect_without_usage_receipt"] == 0
    assert payload["silent_usage_write_failure"] == 0
    assert payload["pending_profile_usage_outbox"] == 0
    assert payload["receipt_fields_not_emitted"] == 0
    assert payload["prompt_changed_without_hash_delta"] == 0
    assert payload["matched_assertion_revision_gap"] == 0
    assert payload["usage_recorded_before_final_render"] == 0
    assert payload["usage_without_read_authorization_token"] == 0
    assert payload["usage_purpose_acl_mismatch"] == 0
    assert payload["partial_unknown_field_acceptance"] == 0
    assert payload["assertion_revision_mapping_ambiguity"] == 0

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        original_access_hashes = conn.execute(
            "SELECT assertion_access_hashes FROM profile_read_authorizations"
        ).fetchone()[0]
        conn.execute("UPDATE profile_read_authorizations SET assertion_access_hashes='{}'")
    acl_binding_drift = _module().audit_persona_runtime_effectiveness(db_path)
    assert acl_binding_drift["usage_purpose_acl_mismatch"] == 1
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE profile_read_authorizations SET assertion_access_hashes=?",
            (original_access_hashes,),
        )

    with sqlite3.connect(db_path) as conn:
        conn.execute('UPDATE profile_usage_log SET expected_delta=\'{"kind":"forged"}\'')
    outer_receipt_drift = _module().audit_persona_runtime_effectiveness(db_path)
    assert outer_receipt_drift["effect_without_target_receipt"] == 1
    assert outer_receipt_drift["receipt_fields_not_emitted"] == 1
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE profile_usage_log SET expected_delta=?",
            (json.dumps(dict(receipt.expected_delta), separators=(",", ":")),),
        )

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE profile_usage_log SET persona_enabled_hash=baseline_hash")
    false_green = _module().audit_persona_runtime_effectiveness(db_path)
    assert false_green["usage_action_changed_without_counterfactual_delta"] == 1
    assert false_green["before_hash_equals_after_hash_marked_changed"] == 1
    assert false_green["prompt_changed_without_hash_delta"] == 1
    assert false_green["effect_without_target_receipt"] == 1

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE profile_usage_log SET profile_revision_ids='[\"forged\"]'")
    drifted = _module().audit_persona_runtime_effectiveness(db_path)
    assert "usage_revision_drift:1" in drifted["errors"]

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE profile_assertions SET current_revision_id='forged-revision' "
            "WHERE assertion_id=?",
            (assertion_id,),
        )
    projection_drifted = _module().audit_persona_runtime_effectiveness(db_path)
    assert "projection_without_revision:1" in projection_drifted["errors"]
    assert "projection_revision_drift:1" in projection_drifted["errors"]
    assert projection_drifted["matched_assertion_revision_gap"] == 1

    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM profile_usage_outbox")
    missing_outbox = _module().audit_persona_runtime_effectiveness(db_path)
    assert missing_outbox["persona_effect_without_usage_outbox"] == 1


def test_runtime_audit_keeps_usage_valid_after_a_normal_assertion_revision(tmp_path):
    from core.persona.psyche import ProfileAssertion, SignalStore

    db_path = tmp_path / "user_signals.db"
    assertion_id, original_revision_id = _seed_preflight_usage(db_path)
    store = SignalStore(initialize_schema=True, db_path=db_path)
    store.upsert_profile_assertion(
        ProfileAssertion(
            assertion_id=assertion_id,
            dimension="judgment_standard",
            claim="用户要求回答附带证据、命令和风险边界。",
            supporting_signals=["profile_signals:1"],
            confidence=0.95,
            privacy_level="local",
            last_verified_at="2026-07-23T01:00:00+00:00",
            expected_revision_id=original_revision_id,
        )
    )
    store.close()

    payload = _module().audit_persona_runtime_effectiveness(db_path)

    assert payload["historical_valid_usage_count"] == 1
    assert payload["historical_valid_usage_marked_drift"] == 0
    assert payload["usage_revision_drift"] == 0
    assert payload["future_revision_usage"] == 0


def test_runtime_audit_rejects_future_and_unrelated_assertion_revisions(tmp_path):
    import sqlite3

    db_path = tmp_path / "user_signals.db"
    assertion_id, original_revision_id = _seed_preflight_usage(db_path)
    with sqlite3.connect(db_path) as conn:
        original = conn.execute(
            """
            SELECT dimension, claim, supporting_signals, contradicting_signals,
                   confidence, privacy_level, last_verified_at, revision_policy,
                   status, access_control
            FROM profile_assertion_revisions WHERE revision_id=?
            """,
            (original_revision_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO profile_assertion_revisions (
                revision_id, assertion_id, revision_number, content_hash,
                supersedes_revision_id, dimension, claim, supporting_signals,
                contradicting_signals, confidence, privacy_level,
                last_verified_at, revision_policy, status, access_control,
                created_at
            ) VALUES (?, ?, 2, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "par_future_revision",
                assertion_id,
                "future-content-hash",
                original_revision_id,
                *original,
                "2099-01-01 00:00:00",
            ),
        )
        conn.execute(
            """
            UPDATE profile_usage_log
            SET profile_revision_ids='["par_future_revision"]',
                matched_assertion_revisions=?
            """,
            (json.dumps({assertion_id: "par_future_revision"}),),
        )

    future = _module().audit_persona_runtime_effectiveness(db_path)
    assert future["future_revision_usage"] == 1
    assert future["usage_revision_drift"] == 1

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE profile_usage_log
            SET profile_revision_ids=?,
                matched_assertion_revisions=?
            """,
            (
                json.dumps([original_revision_id]),
                json.dumps({"unrelated_assertion": original_revision_id}),
            ),
        )
    unrelated = _module().audit_persona_runtime_effectiveness(db_path)
    assert unrelated["assertion_revision_mapping_ambiguity"] == 1


def test_runtime_audit_applies_consumer_oracle_and_changed_delta_matrix(tmp_path):
    import sqlite3

    from core.persona.profile_effect import compare_profile_effect

    no_effect_db = tmp_path / "no_effect.db"
    _seed_preflight_usage(
        no_effect_db,
        before="same",
        after="same",
        receipt_id="runtime-no-effect-target",
    )
    no_effect = _module().audit_persona_runtime_effectiveness(no_effect_db)
    assert no_effect["usage_action_changed_without_delta"] == 0
    assert no_effect["effect_receipt_oracle_gap"] == 0

    with sqlite3.connect(no_effect_db) as conn:
        conn.execute("UPDATE profile_usage_log SET action_changed=1")
    forged_changed = _module().audit_persona_runtime_effectiveness(no_effect_db)
    assert forged_changed["usage_action_changed_without_delta"] == 1
    assert forged_changed["effect_receipt_oracle_gap"] == 1

    wrong_target_db = tmp_path / "wrong_target.db"
    _seed_preflight_usage(
        wrong_target_db,
        target_type="ranking",
        target_id="wrong_preflight_target",
        receipt_id="runtime-wrong-target",
    )
    wrong_target = _module().audit_persona_runtime_effectiveness(wrong_target_db)
    assert wrong_target["effect_receipt_oracle_gap"] == 1

    rank_delta = {
        "kind": "rank_score_delta",
        "target": "context_search_persona_candidates",
        "query_id": "context-search:oracle-mismatch",
        "baseline_ranking": [{"candidate_id": "wiki:a", "rank": 1}],
        "persona_enabled_ranking": [{"candidate_id": "wiki:b", "rank": 1}],
        "changed_candidates": [
            {
                "candidate_id": "wiki:a",
                "baseline_rank": 1,
                "persona_enabled_rank": None,
                "matched_assertion_ids": ["assertion-a"],
            },
            {
                "candidate_id": "wiki:b",
                "baseline_rank": None,
                "persona_enabled_rank": 1,
                "matched_assertion_ids": ["assertion-a"],
            },
        ],
        "eligible_candidate_ids": ["wiki:a", "wiki:b"],
        "matched_assertion_revisions": {"assertion-a": "revision-a"},
    }
    rank_receipt = compare_profile_effect(
        owner="context_search",
        target_type="ranking",
        target_id="context_search_persona_candidates",
        matched_assertion_revisions={"assertion-a": "revision-a"},
        baseline_output=["not-the-declared-baseline"],
        persona_enabled_output=["not-the-declared-enabled-ranking"],
        expected_delta=rank_delta,
        receipt_id="context-rank-oracle-mismatch",
    )
    assert not _module()._consumer_effect_oracle_is_valid(
        consumer="context_search",
        mapping=json.dumps({"assertion-a": "revision-a"}),
        target_receipt=json.dumps(rank_receipt.as_dict()),
        expected_delta=json.dumps(rank_delta),
    )


def test_runtime_effectiveness_audit_validates_context_search_rank_delta(tmp_path):
    import sqlite3

    from core.cognitive.access_control import make_cognitive_access_envelope
    from core.persona.profile_effect import compare_profile_effect
    from core.persona.psyche import (
        ProfileAssertion,
        ProfileSignal,
        ProfileUsageLog,
        SignalStore,
    )

    db_path = tmp_path / "user_signals.db"
    store = SignalStore(initialize_schema=True, db_path=db_path)
    signal_id = store.record_profile_signal(
        ProfileSignal(
            source_event_id="session:context-rank-audit",
            signal_type="explicit_preference",
            dimension="judgment_standard",
            value="需要证据",
            evidence="explicit user evidence",
            confidence=0.9,
            privacy_level="local",
            observed_at="2026-07-23T00:00:00",
            access_control=make_cognitive_access_envelope(
                owner_principal_id="mcp:codex:context-rank-audit",
                owner_agent="codex",
                scope_type="session",
                scope_id="context-rank-audit-session",
                session_id="context-rank-audit-session",
                purposes=("context_search_profile", "persona_usage_metrics"),
                consent_provenance_refs=("session:context-rank-audit",),
                sensitivity="sensitive",
                retention_policy="persona_retention",
                source_acl_lineage=("sha256:context-rank-audit",),
                visibility="agent",
            ),
        )
    )
    assertion_id = store.upsert_profile_assertion(
        ProfileAssertion(
            assertion_id="pa_context_rank_audit",
            dimension="judgment_standard",
            claim="用户要求回答附带证据。",
            supporting_signals=[f"profile_signals:{signal_id}"],
            confidence=0.9,
            privacy_level="local",
            last_verified_at="2026-07-23T00:00:00",
        )
    )
    revision_id = str(store.get_profile_assertion_revisions(assertion_id)[-1]["revision_id"])
    from core.access_policy import AccessNarrowing, PrincipalEnvelope

    read_principal = PrincipalEnvelope(
        principal_id="mcp:codex:context-rank-audit",
        agent="codex",
        host_kind="codex",
        capability_id="context-rank-audit",
        capabilities=frozenset({"memory_read"}),
    )
    read_narrowing = AccessNarrowing(session_id="context-rank-audit-session")
    _profile, access = store.build_authorized_user_cognitive_profile_v2(
        principal=read_principal,
        narrowing=read_narrowing,
        purpose="context_search_profile",
        consumer="context_search",
    )
    mapping = {assertion_id: revision_id}
    expected_delta = {
        "kind": "rank_score_delta",
        "target": "context_search_persona_candidates",
        "query_id": "context-search:rank-audit",
        "baseline_ranking": [
            {"candidate_id": "wiki:a", "page_path": "a.md", "rank": 1},
            {"candidate_id": "wiki:b", "page_path": "b.md", "rank": 2},
        ],
        "persona_enabled_ranking": [
            {"candidate_id": "wiki:b", "page_path": "b.md", "rank": 1},
            {"candidate_id": "wiki:a", "page_path": "a.md", "rank": 2},
        ],
        "changed_candidates": [
            {
                "candidate_id": "wiki:a",
                "baseline_rank": 1,
                "persona_enabled_rank": 2,
                "matched_assertion_ids": [],
            },
            {
                "candidate_id": "wiki:b",
                "baseline_rank": 2,
                "persona_enabled_rank": 1,
                "matched_assertion_ids": [assertion_id],
            },
        ],
        "eligible_candidate_ids": ["wiki:a", "wiki:b"],
        "matched_assertion_revisions": mapping,
    }
    store.record_profile_usage(
        ProfileUsageLog(
            consumer="context_search",
            profile_fields_used=[assertion_id],
            read_purpose="context_search_profile",
            read_authorization_token=str(access["read_authorization_token"]),
            target_receipt=compare_profile_effect(
                owner="context_search",
                target_type="ranking",
                target_id="context_search_persona_candidates",
                matched_assertion_revisions=mapping,
                baseline_output=expected_delta["baseline_ranking"],
                persona_enabled_output=expected_delta["persona_enabled_ranking"],
                expected_delta=expected_delta,
                receipt_id="context-rank-audit-target",
            ),
            outcome="search_weight_adjusted",
        ),
        principal=read_principal,
        narrowing=read_narrowing,
    )
    store.close()

    payload = _module().audit_persona_runtime_effectiveness(db_path)
    assert payload["rank_receipt_without_rank_delta"] == 0
    assert payload["filtered_candidate_counted_as_effect"] == 0
    assert payload["usage_contains_unmatched_assertion"] == 0
    assert payload["cross_query_profile_evidence_leak"] == 0
    assert payload["usage_recorded_before_final_render"] == 0
    assert payload["usage_without_read_authorization_token"] == 0
    assert payload["usage_purpose_acl_mismatch"] == 0

    with sqlite3.connect(db_path) as conn:
        drifted = dict(expected_delta)
        drifted["eligible_candidate_ids"] = ["wiki:a"]
        conn.execute(
            "UPDATE profile_usage_log SET expected_delta=?",
            (json.dumps(drifted, separators=(",", ":")),),
        )
    filtered_drift = _module().audit_persona_runtime_effectiveness(db_path)
    assert filtered_drift["filtered_candidate_counted_as_effect"] == 1
