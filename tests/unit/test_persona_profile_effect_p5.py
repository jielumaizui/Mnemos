from __future__ import annotations

from dataclasses import replace
import json
import sqlite3

import pytest


def _profile_access():
    from core.cognitive.access_control import make_cognitive_access_envelope

    return make_cognitive_access_envelope(
        owner_principal_id="mcp:codex:profile-effect",
        owner_agent="codex",
        scope_type="session",
        scope_id="profile-effect-session",
        session_id="profile-effect-session",
        project="mnemos",
        purposes=(
            "persona_preflight_read",
            "persona_behavior_prompt",
            "context_search_profile",
            "persona_usage_metrics",
        ),
        consent_provenance_refs=("session:profile-effect",),
        sensitivity="sensitive",
        retention_policy="persona_retention",
        source_acl_lineage=("sha256:profile-effect-source",),
        visibility="agent",
    )


def _profile_principal(
    *,
    principal_id="mcp:codex:profile-effect",
    allowed_projects=frozenset({"mnemos"}),
):
    from core.access_policy import PrincipalEnvelope

    return PrincipalEnvelope(
        principal_id=principal_id,
        agent="codex",
        host_kind="codex",
        capability_id="profile-effect",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=allowed_projects,
    )


def _profile_narrowing(*, project="mnemos"):
    from core.access_policy import AccessNarrowing

    return AccessNarrowing(
        session_id="profile-effect-session",
        project=project,
    )


def _seed_assertion(store, assertion_id: str, claim: str) -> str:
    from core.persona.psyche import ProfileAssertion, ProfileSignal

    signal_id = store.record_profile_signal(
        ProfileSignal(
            source_event_id=f"session:{assertion_id}",
            signal_type="explicit_preference",
            dimension="judgment_standard",
            value=claim,
            evidence=f"explicit user evidence for {assertion_id}",
            confidence=0.9,
            privacy_level="local",
            observed_at="2026-07-23T00:00:00",
            access_control=_profile_access(),
        )
    )
    store.upsert_profile_assertion(
        ProfileAssertion(
            assertion_id=assertion_id,
            dimension="judgment_standard",
            claim=claim,
            supporting_signals=[f"profile_signals:{signal_id}"],
            confidence=0.9,
            privacy_level="local",
            last_verified_at="2026-07-23T00:00:00",
        )
    )
    return str(store.get_profile_assertion_revisions(assertion_id)[-1]["revision_id"])


def _effect(
    assertion_revisions,
    *,
    before="base prompt",
    after="base prompt\nuse exact evidence",
    target_status="committed",
    receipt_id="profile-target:test",
):
    from core.persona.profile_effect import compare_profile_effect

    return compare_profile_effect(
        owner="preflight_builder",
        target_type="prompt",
        target_id="persona_section",
        matched_assertion_revisions=assertion_revisions,
        baseline_output=before,
        persona_enabled_output=after,
        expected_delta={"kind": "prompt_append"},
        target_status=target_status,
        receipt_id=receipt_id,
        request_id="request:test",
        decision_id="decision:test",
        created_at="2026-07-23T00:00:01",
    )


def _profile_read_token(
    store,
    *,
    consumer="preflight_builder",
    purpose="persona_preflight_read",
):
    _profile, access = store.build_authorized_user_cognitive_profile_v2(
        principal=_profile_principal(),
        narrowing=_profile_narrowing(),
        purpose=purpose,
        consumer=consumer,
    )
    return str(access["read_authorization_token"])


def _record_profile_usage(store, usage):
    return store.record_profile_usage(
        usage,
        principal=_profile_principal(),
        narrowing=_profile_narrowing(),
    )


def test_profile_usage_caller_cannot_supply_action_changed() -> None:
    from core.persona.psyche import ProfileUsageLog

    with pytest.raises(TypeError):
        ProfileUsageLog(
            consumer="preflight_builder",
            profile_fields_used=["pa_one"],
            read_purpose="persona_preflight_read",
            target_receipt=None,
            action_changed=True,
        )


def test_profile_usage_requires_sealed_read_authorization(tmp_path) -> None:
    from core.persona.psyche import ProfileUsageLog, SignalStore

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    revision_id = _seed_assertion(store, "pa_read_token", "用户要求证据。")

    with pytest.raises(ValueError, match="read authorization token"):
        _record_profile_usage(
            store,
            ProfileUsageLog(
                consumer="preflight_builder",
                profile_fields_used=["pa_read_token"],
                read_purpose="persona_preflight_read",
                target_receipt=_effect({"pa_read_token": revision_id}),
            ),
        )


def test_profile_usage_read_token_binds_consumer_purpose_and_expiry(tmp_path) -> None:
    from core.persona.psyche import ProfileUsageLog, SignalStore

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    revision_id = _seed_assertion(store, "pa_bound_token", "用户要求证据。")
    context_token = _profile_read_token(
        store,
        consumer="context_search",
        purpose="context_search_profile",
    )

    with pytest.raises(ValueError, match="consumer/purpose"):
        _record_profile_usage(
            store,
            ProfileUsageLog(
                consumer="preflight_builder",
                profile_fields_used=["pa_bound_token"],
                read_purpose="persona_preflight_read",
                read_authorization_token=context_token,
                target_receipt=_effect({"pa_bound_token": revision_id}),
            ),
        )

    expired_token = _profile_read_token(store)
    with sqlite3.connect(tmp_path / "user_signals.db") as conn:
        conn.execute(
            "UPDATE profile_read_authorizations "
            "SET expires_at='2000-01-01T00:00:00+00:00' WHERE token_id=?",
            (expired_token,),
        )
    with pytest.raises(ValueError, match="expired"):
        _record_profile_usage(
            store,
            ProfileUsageLog(
                consumer="preflight_builder",
                profile_fields_used=["pa_bound_token"],
                read_purpose="persona_preflight_read",
                read_authorization_token=expired_token,
                target_receipt=_effect(
                    {"pa_bound_token": revision_id},
                    receipt_id="profile-target:expired-token",
                ),
            ),
        )


@pytest.mark.parametrize(
    ("principal", "narrowing"),
    [
        (
            _profile_principal(principal_id="mcp:codex:another-user"),
            _profile_narrowing(),
        ),
        (
            _profile_principal(allowed_projects=frozenset({"other-project"})),
            _profile_narrowing(project="other-project"),
        ),
    ],
)
def test_profile_usage_read_token_binds_the_resolved_principal_and_project(
    tmp_path,
    principal,
    narrowing,
) -> None:
    from core.persona.psyche import ProfileUsageLog, SignalStore

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    revision_id = _seed_assertion(store, "pa_principal_bound", "用户要求证据。")
    token = _profile_read_token(store)

    with pytest.raises(ValueError, match="principal/scope"):
        store.record_profile_usage(
            ProfileUsageLog(
                consumer="preflight_builder",
                profile_fields_used=["pa_principal_bound"],
                read_purpose="persona_preflight_read",
                read_authorization_token=token,
                target_receipt=_effect({"pa_principal_bound": revision_id}),
            ),
            principal=principal,
            narrowing=narrowing,
        )


def test_profile_read_token_rejects_incompatible_authorized_acl_contexts(
    tmp_path,
) -> None:
    from core.cognitive.access_control import make_cognitive_access_envelope
    from core.persona.psyche import ProfileAssertion, ProfileSignal, SignalStore

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    _seed_assertion(store, "pa_agent_visible", "用户要求证据。")
    private_access = make_cognitive_access_envelope(
        owner_principal_id="mcp:codex:profile-effect",
        owner_agent="codex",
        scope_type="session",
        scope_id="profile-effect-session",
        session_id="profile-effect-session",
        project="mnemos",
        purposes=("persona_preflight_read", "persona_usage_metrics"),
        consent_provenance_refs=("session:profile-effect-private",),
        sensitivity="sensitive",
        retention_policy="persona_retention",
        source_acl_lineage=("sha256:profile-effect-private",),
        visibility="private",
    )
    signal_id = store.record_profile_signal(
        ProfileSignal(
            source_event_id="session:profile-effect-private",
            signal_type="explicit_preference",
            dimension="judgment_standard",
            value="用户要求私有证据。",
            access_control=private_access,
        )
    )
    store.upsert_profile_assertion(
        ProfileAssertion(
            assertion_id="pa_private",
            dimension="judgment_standard",
            claim="用户要求私有证据。",
            supporting_signals=[f"profile_signals:{signal_id}"],
        )
    )

    with pytest.raises(ValueError, match="compatible resolved ACL"):
        store.build_authorized_user_cognitive_profile_v2(
            principal=_profile_principal(),
            narrowing=_profile_narrowing(),
            purpose="persona_preflight_read",
            consumer="preflight_builder",
        )


def test_profile_usage_rejects_duplicate_fields_even_with_valid_read_token(
    tmp_path,
) -> None:
    from core.persona.psyche import ProfileUsageLog, SignalStore

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    revision_id = _seed_assertion(store, "pa_duplicate_field", "用户要求证据。")
    token = _profile_read_token(store)

    with pytest.raises(ValueError, match="duplicate profile usage fields"):
        _record_profile_usage(
            store,
            ProfileUsageLog(
                consumer="preflight_builder",
                profile_fields_used=["pa_duplicate_field", "pa_duplicate_field"],
                read_purpose="persona_preflight_read",
                read_authorization_token=token,
                target_receipt=_effect({"pa_duplicate_field": revision_id}),
            ),
        )


def test_equal_outputs_can_never_be_marked_changed(tmp_path) -> None:
    from core.persona.psyche import ProfileUsageLog, SignalStore
    from core.persona.profile_effect import validate_profile_target_effect_receipt

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    revision_id = _seed_assertion(store, "pa_equal", "用户要求证据。")
    receipt = _effect(
        {"pa_equal": revision_id},
        before={"prompt": "same"},
        after={"prompt": "same"},
    )

    usage_id = _record_profile_usage(
        store,
        ProfileUsageLog(
            consumer="preflight_builder",
            profile_fields_used=["pa_equal"],
            read_purpose="persona_preflight_read",
            read_authorization_token=_profile_read_token(store),
            target_receipt=receipt,
        ),
    )

    store.close()
    with sqlite3.connect(tmp_path / "user_signals.db") as conn:
        row = conn.execute(
            """
            SELECT action_changed, baseline_hash, persona_enabled_hash,
                   terminal_status, target_receipt
            FROM profile_usage_log WHERE id=?
            """,
            (usage_id,),
        ).fetchone()
    assert tuple(row[:4]) == (
        0,
        receipt.baseline_hash,
        receipt.persona_enabled_hash,
        "no_effect",
    )
    assert receipt.baseline_hash == receipt.persona_enabled_hash
    assert json.loads(str(row[4]))["action_changed"] is False
    with pytest.raises(ValueError, match="comparator-derived"):
        validate_profile_target_effect_receipt(replace(receipt, action_changed=True))


def test_changed_output_has_exact_revision_delta_and_reciprocal_receipt(tmp_path) -> None:
    from core.persona.psyche import ProfileUsageLog, SignalStore

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    revision_id = _seed_assertion(store, "pa_changed", "用户要求证据。")
    receipt = _effect({"pa_changed": revision_id})

    usage_id = _record_profile_usage(
        store,
        ProfileUsageLog(
            consumer="preflight_builder",
            profile_fields_used=["pa_changed"],
            read_purpose="persona_preflight_read",
            read_authorization_token=_profile_read_token(store),
            target_receipt=receipt,
            outcome="persona_section_augmented",
        ),
    )

    store.close()
    with sqlite3.connect(tmp_path / "user_signals.db") as conn:
        row = conn.execute(
            """
            SELECT matched_assertion_revisions, action_changed, expected_delta,
                   actual_target_delta, target_receipt_hash, terminal_status
            FROM profile_usage_log WHERE id=?
            """,
            (usage_id,),
        ).fetchone()
    assert json.loads(str(row[0])) == {"pa_changed": revision_id}
    assert row[1] == 1
    assert json.loads(str(row[2])) == {"kind": "prompt_append"}
    assert json.loads(str(row[3])) == {
        "after_hash": receipt.persona_enabled_hash,
        "before_hash": receipt.baseline_hash,
        "changed": True,
        "target_id": "persona_section",
        "target_type": "prompt",
    }
    assert row[4] == receipt.receipt_hash
    assert row[5] == "committed"


def _preflight_profile(assertions):
    return {
        "confidence": 0.9,
        "profile_assertions": assertions,
        "judgment_standards": assertions,
    }


def test_preflight_formatter_returns_only_six_exact_emitted_revisions() -> None:
    from integrations.preflight_builder import (
        _format_cognitive_profile_v2_section_with_matches,
    )

    assertions = [
        {
            "assertion_id": f"pa_{index}",
            "current_revision_id": f"par_{index}",
            "claim": f"必须执行第 {index} 条证据规则",
            "confidence": 0.9,
        }
        for index in range(10)
    ]

    section, matches = _format_cognitive_profile_v2_section_with_matches(
        _preflight_profile(assertions)
    )

    assert len([line for line in section.splitlines() if line.startswith("- 判断标准:")]) == 6
    assert matches == {f"pa_{index}": f"par_{index}" for index in range(6)}
    assert "第 6 条" not in section


def test_preflight_formatter_skips_empty_and_duplicate_claims() -> None:
    from integrations.preflight_builder import (
        _format_cognitive_profile_v2_section_with_matches,
    )

    assertions = [
        {
            "assertion_id": "pa_empty",
            "current_revision_id": "par_empty",
            "claim": "   ",
            "confidence": 0.9,
        },
        {
            "assertion_id": "pa_first",
            "current_revision_id": "par_first",
            "claim": "回答必须附带证据",
            "confidence": 0.9,
        },
        {
            "assertion_id": "pa_duplicate",
            "current_revision_id": "par_duplicate",
            "claim": "回答必须附带证据",
            "confidence": 0.8,
        },
    ]

    section, matches = _format_cognitive_profile_v2_section_with_matches(
        _preflight_profile(assertions)
    )

    assert section.count("回答必须附带证据") == 1
    assert matches == {"pa_first": "par_first"}


def test_preflight_formatter_rejects_claim_revision_projection_drift() -> None:
    from integrations.preflight_builder import (
        _format_cognitive_profile_v2_section_with_matches,
    )

    profile = _preflight_profile(
        [
            {
                "assertion_id": "pa_drift",
                "current_revision_id": "par_drift",
                "claim": "canonical claim",
                "confidence": 0.9,
            }
        ]
    )
    profile["judgment_standards"] = [
        {
            "assertion_id": "pa_drift",
            "current_revision_id": "par_drift",
            "claim": "different bucket claim",
            "confidence": 0.9,
        }
    ]

    with pytest.raises(ValueError, match="claim/revision projection drift"):
        _format_cognitive_profile_v2_section_with_matches(profile)


def test_preflight_formatter_receipt_matches_token_budget_emission() -> None:
    from integrations.preflight_builder import (
        _format_cognitive_profile_v2_section_with_matches,
        _persona_token_estimate,
    )

    assertions = [
        {
            "assertion_id": "pa_short",
            "current_revision_id": "par_short",
            "claim": "回答必须附带证据",
            "confidence": 0.9,
        },
        {
            "assertion_id": "pa_long",
            "current_revision_id": "par_long",
            "claim": "回答必须逐项附带完整证据、验证命令、退出码和失败边界说明",
            "confidence": 0.9,
        },
    ]
    first_only, _ = _format_cognitive_profile_v2_section_with_matches(
        _preflight_profile(assertions[:1])
    )

    section, matches = _format_cognitive_profile_v2_section_with_matches(
        _preflight_profile(assertions),
        token_limit=_persona_token_estimate(first_only),
    )

    assert section == first_only
    assert matches == {"pa_short": "par_short"}


def test_preflight_usage_receipt_binds_emitted_revisions_and_prompt_hashes(
    monkeypatch,
    tmp_path,
) -> None:
    from core.persona.psyche import SignalStore
    from integrations.preflight_builder import _record_profile_v2_usage

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    revision_id = _seed_assertion(store, "pa_emitted", "用户要求证据。")
    monkeypatch.setattr("core.persona.psyche.get_signal_store", lambda: store)

    _record_profile_v2_usage(
        "preflight_builder",
        {"pa_emitted": revision_id},
        principal=_profile_principal(),
        narrowing=_profile_narrowing(),
        baseline_output="base",
        persona_enabled_output="base\nprofile",
        expected_delta={
            "kind": "prompt_append",
            "section": "user_cognitive_profile_v2",
        },
        outcome="persona_section_augmented",
        read_authorization_token=_profile_read_token(store),
    )
    store.close()

    with sqlite3.connect(tmp_path / "user_signals.db") as conn:
        row = conn.execute("""
            SELECT matched_assertion_revisions, expected_delta,
                   baseline_hash, persona_enabled_hash, action_changed
            FROM profile_usage_log
            """).fetchone()
    assert json.loads(row[0]) == {"pa_emitted": revision_id}
    assert json.loads(row[1])["emitted_assertion_revisions"] == {"pa_emitted": revision_id}
    assert row[2] != row[3]
    assert row[4] == 1


def test_usage_rejects_unrelated_assertion_and_stale_revision(tmp_path) -> None:
    from core.persona.psyche import ProfileUsageLog, SignalStore

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    revision_a = _seed_assertion(store, "pa_a", "用户要求证据。")
    _seed_assertion(store, "pa_b", "用户要求先测试。")
    receipt = _effect({"pa_a": revision_a})

    with pytest.raises(ValueError, match="exact matched assertions"):
        _record_profile_usage(
            store,
            ProfileUsageLog(
                consumer="preflight_builder",
                profile_fields_used=["pa_a", "pa_b"],
                read_purpose="persona_preflight_read",
                read_authorization_token=_profile_read_token(store),
                target_receipt=receipt,
            ),
        )

    stale = _effect({"pa_a": "forged-revision"}, receipt_id="profile-target:stale")
    with pytest.raises(ValueError, match="current projection"):
        _record_profile_usage(
            store,
            ProfileUsageLog(
                consumer="preflight_builder",
                profile_fields_used=["pa_a"],
                read_purpose="persona_preflight_read",
                read_authorization_token=_profile_read_token(store),
                target_receipt=stale,
            ),
        )


def test_duplicate_receipt_is_idempotent_but_conflict_fails(tmp_path) -> None:
    from core.persona.psyche import ProfileUsageLog, SignalStore

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    revision_id = _seed_assertion(store, "pa_duplicate", "用户要求证据。")
    usage = ProfileUsageLog(
        consumer="preflight_builder",
        profile_fields_used=["pa_duplicate"],
        read_purpose="persona_preflight_read",
        read_authorization_token=_profile_read_token(store),
        target_receipt=_effect({"pa_duplicate": revision_id}),
        outcome="persona_section_augmented",
    )

    first = _record_profile_usage(store, usage)
    second = _record_profile_usage(store, usage)
    assert second == first
    with pytest.raises(ValueError, match="idempotency conflict"):
        _record_profile_usage(store, replace(usage, outcome="different"))


def test_profile_read_token_cannot_authorize_a_different_usage_command(tmp_path) -> None:
    from core.persona.psyche import ProfileUsageLog, SignalStore

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    revision_id = _seed_assertion(store, "pa_single_use", "用户要求证据。")
    token = _profile_read_token(store)
    first = ProfileUsageLog(
        consumer="preflight_builder",
        profile_fields_used=["pa_single_use"],
        read_purpose="persona_preflight_read",
        read_authorization_token=token,
        target_receipt=_effect(
            {"pa_single_use": revision_id},
            receipt_id="profile-target:single-use:first",
        ),
    )
    second = ProfileUsageLog(
        consumer="preflight_builder",
        profile_fields_used=["pa_single_use"],
        read_purpose="persona_preflight_read",
        read_authorization_token=token,
        target_receipt=_effect(
            {"pa_single_use": revision_id},
            receipt_id="profile-target:single-use:second",
        ),
    )

    _record_profile_usage(store, first)
    with pytest.raises(ValueError, match="already consumed"):
        _record_profile_usage(store, second)


def test_usage_outbox_survives_crash_and_replays_once(tmp_path) -> None:
    from core.persona.psyche import ProfileUsageLog, SignalStore

    db_path = tmp_path / "user_signals.db"
    store = SignalStore(initialize_schema=True, db_path=db_path)
    revision_id = _seed_assertion(store, "pa_replay", "用户要求证据。")
    usage = ProfileUsageLog(
        consumer="preflight_builder",
        profile_fields_used=["pa_replay"],
        read_purpose="persona_preflight_read",
        read_authorization_token=_profile_read_token(store),
        target_receipt=_effect(
            {"pa_replay": revision_id},
            receipt_id="profile-target:replay",
        ),
        outcome="persona_section_augmented",
    )

    def crash_after_outbox(phase: str) -> None:
        if phase == "after_usage_outbox_commit":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        store._cognitive_profiles.record_usage(
            usage,
            principal=_profile_principal(),
            narrowing=_profile_narrowing(),
            _failpoint=crash_after_outbox,
        )
    store.close()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT status FROM profile_usage_outbox").fetchone() == ("pending",)
        assert conn.execute("SELECT COUNT(*) FROM profile_usage_log").fetchone()[0] == 0
        conn.execute(
            "UPDATE profile_read_authorizations " "SET expires_at='2000-01-01T00:00:00+00:00'"
        )
        conn.commit()

    restarted = SignalStore(db_path=db_path)
    replayed = restarted.replay_profile_usage_outbox()
    assert len(replayed) == 1
    assert restarted.replay_profile_usage_outbox() == ()
    restarted.close()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT status, usage_id, attempts FROM profile_usage_outbox"
        ).fetchone() == ("committed", 1, 1)
        assert conn.execute("SELECT COUNT(*) FROM profile_usage_log").fetchone()[0] == 1


def test_usage_receipt_and_outbox_commit_atomically(tmp_path) -> None:
    from core.persona.psyche import ProfileUsageLog, SignalStore

    db_path = tmp_path / "user_signals.db"
    store = SignalStore(initialize_schema=True, db_path=db_path)
    revision_id = _seed_assertion(store, "pa_atomic", "用户要求证据。")
    usage = ProfileUsageLog(
        consumer="preflight_builder",
        profile_fields_used=["pa_atomic"],
        read_purpose="persona_preflight_read",
        read_authorization_token=_profile_read_token(store),
        target_receipt=_effect(
            {"pa_atomic": revision_id},
            receipt_id="profile-target:atomic",
        ),
    )

    def crash_before_commit(phase: str) -> None:
        if phase == "after_usage_receipt_before_commit":
            raise RuntimeError("simulated receipt commit crash")

    with pytest.raises(RuntimeError, match="receipt commit crash"):
        store._cognitive_profiles.record_usage(
            usage,
            principal=_profile_principal(),
            narrowing=_profile_narrowing(),
            _failpoint=crash_before_commit,
        )
    store.close()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT status FROM profile_usage_outbox").fetchone() == ("pending",)
        assert conn.execute("SELECT COUNT(*) FROM profile_usage_log").fetchone()[0] == 0


def test_failed_target_receipt_cannot_claim_an_effect(tmp_path) -> None:
    from core.persona.psyche import ProfileUsageLog, SignalStore

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    revision_id = _seed_assertion(store, "pa_failed", "用户要求证据。")
    receipt = _effect(
        {"pa_failed": revision_id},
        before="base",
        after="would have changed",
        target_status="failed",
        receipt_id="profile-target:failed",
    )

    usage_id = _record_profile_usage(
        store,
        ProfileUsageLog(
            consumer="preflight_builder",
            profile_fields_used=["pa_failed"],
            read_purpose="persona_preflight_read",
            read_authorization_token=_profile_read_token(store),
            target_receipt=receipt,
            outcome="target_write_failed",
        ),
    )
    store.close()
    with sqlite3.connect(tmp_path / "user_signals.db") as conn:
        row = conn.execute(
            """
            SELECT action_changed, terminal_status, baseline_hash,
                   persona_enabled_hash
            FROM profile_usage_log WHERE id=?
            """,
            (usage_id,),
        ).fetchone()
    assert tuple(row[:2]) == (0, "failed")
    assert row[2] == row[3]


def test_context_search_match_evidence_excludes_unrelated_assertion(tmp_path) -> None:
    from core.app.context_search import ContextAwareSearch

    searcher = ContextAwareSearch(wiki_base=str(tmp_path))
    score, matched = searcher._compute_persona_score_with_matches(
        {
            "title": "证据验证闭环",
            "content": "回答必须给出证据和验证命令。",
            "frontmatter": {},
        },
        {
            "persona_assertions": [
                {
                    "assertion_id": "pa_evidence",
                    "claim": "回答必须包含证据和验证命令",
                    "confidence": 0.9,
                },
                {
                    "assertion_id": "pa_unrelated",
                    "claim": "用户喜欢旅行摄影和爵士音乐",
                    "confidence": 0.99,
                },
            ]
        },
    )

    assert score > 0.0
    assert matched == ["pa_evidence"]


def test_context_search_rank_effect_ignores_match_when_order_is_unchanged(tmp_path) -> None:
    from core.app.context_search import ContextAwareSearch

    searcher = ContextAwareSearch(wiki_base=str(tmp_path))
    baseline, enabled, matched, changed = searcher._build_profile_rank_effect(
        [
            {
                "candidate_id": "wiki:a",
                "page_path": "a.md",
                "baseline_score": 0.9,
                "persona_enabled_score": 0.95,
                "matched_assertion_ids": ["pa_evidence"],
            },
            {
                "candidate_id": "wiki:b",
                "page_path": "b.md",
                "baseline_score": 0.8,
                "persona_enabled_score": 0.8,
                "matched_assertion_ids": [],
            },
        ],
        limit=2,
    )

    assert baseline == enabled
    assert matched == set()
    assert changed == []


def test_context_search_rank_effect_excludes_filtered_and_topk_outside_matches(
    tmp_path,
) -> None:
    from core.app.context_search import ContextAwareSearch

    searcher = ContextAwareSearch(wiki_base=str(tmp_path))
    baseline, enabled, matched, changed = searcher._build_profile_rank_effect(
        [
            {
                "candidate_id": "wiki:a",
                "page_path": "a.md",
                "baseline_score": 0.9,
                "persona_enabled_score": 0.9,
                "matched_assertion_ids": [],
            },
            {
                "candidate_id": "wiki:outside",
                "page_path": "outside.md",
                "baseline_score": 0.1,
                "persona_enabled_score": 0.2,
                "matched_assertion_ids": ["pa_outside"],
            },
        ],
        limit=1,
    )

    assert baseline == enabled
    assert matched == set()
    assert changed == []


def test_context_search_rank_effect_binds_only_changed_candidate_assertions(
    tmp_path,
) -> None:
    from core.app.context_search import ContextAwareSearch

    searcher = ContextAwareSearch(wiki_base=str(tmp_path))
    baseline, enabled, matched, changed = searcher._build_profile_rank_effect(
        [
            {
                "candidate_id": "wiki:a",
                "page_path": "a.md",
                "baseline_score": 0.9,
                "persona_enabled_score": 0.9,
                "matched_assertion_ids": [],
            },
            {
                "candidate_id": "wiki:b",
                "page_path": "b.md",
                "baseline_score": 0.8,
                "persona_enabled_score": 0.95,
                "matched_assertion_ids": ["pa_rank"],
            },
            {
                "candidate_id": "wiki:c",
                "page_path": "c.md",
                "baseline_score": 0.7,
                "persona_enabled_score": 0.75,
                "matched_assertion_ids": ["pa_unselected"],
            },
        ],
        limit=2,
    )

    assert [row["candidate_id"] for row in baseline] == ["wiki:a", "wiki:b"]
    assert [row["candidate_id"] for row in enabled] == ["wiki:b", "wiki:a"]
    assert matched == {"pa_rank"}
    assert {row["candidate_id"] for row in changed} == {"wiki:a", "wiki:b"}


def test_context_search_rank_effect_uses_stable_identity_for_ties(tmp_path) -> None:
    from core.app.context_search import ContextAwareSearch

    searcher = ContextAwareSearch(wiki_base=str(tmp_path))
    baseline, enabled, matched, changed = searcher._build_profile_rank_effect(
        [
            {
                "candidate_id": "wiki:b",
                "page_path": "b.md",
                "baseline_score": 0.8,
                "persona_enabled_score": 0.8,
                "matched_assertion_ids": [],
            },
            {
                "candidate_id": "wiki:a",
                "page_path": "a.md",
                "baseline_score": 0.8,
                "persona_enabled_score": 0.8,
                "matched_assertion_ids": [],
            },
        ],
        limit=2,
    )

    assert [row["candidate_id"] for row in baseline] == ["wiki:a", "wiki:b"]
    assert enabled == baseline
    assert matched == set()
    assert changed == []


def test_context_search_clears_profile_evidence_at_each_query_start(
    monkeypatch,
    tmp_path,
) -> None:
    from core.access_policy import PrincipalEnvelope
    from core.app.context_search import ContextAwareSearch

    class Config:
        @staticmethod
        def get(_key, default=None):
            return default

    monkeypatch.setattr("core.app.context_search.get_config", lambda: Config())
    searcher = ContextAwareSearch(wiki_base=str(tmp_path))
    searcher._profile_usage_evidence = {
        "authorized_revisions": {"pa_stale": "par_stale"},
        "matched_assertion_ids": {"pa_stale"},
    }

    assert (
        searcher.search(
            "second query",
            principal=PrincipalEnvelope(
                principal_id="mcp:codex:rank-reset",
                agent="codex",
                host_kind="codex",
                capability_id="rank-reset",
                capabilities=frozenset({"memory_read"}),
            ),
        )
        == []
    )
    assert searcher._profile_usage_evidence is None


def test_context_search_records_only_real_topk_reorder_and_does_not_leak(
    monkeypatch,
    tmp_path,
) -> None:
    from core.access_policy import AccessNarrowing, PrincipalEnvelope
    from core.app.context_search import ContextAwareSearch
    from core.persona.psyche import SignalStore

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    assertion_id = "pa_context_rank"
    revision_id = _seed_assertion(
        store,
        assertion_id,
        "回答必须包含证据和验证命令",
    )
    store.close()

    class Config:
        database_dir = tmp_path
        wiki_dir = tmp_path

        @staticmethod
        def get(key, default=None):
            return False if key == "embedding.enabled" else default

    monkeypatch.setattr("core.app.context_search.get_config", lambda: Config())
    frontmatter = (
        "---\n"
        "scope: agent\n"
        "source_agent: codex\n"
        "acl_schema_version: 1\n"
        "acl_metadata_complete: true\n"
        "acl_reconciliation_status: proven\n"
        "---\n"
    )
    (tmp_path / "a.md").write_text(
        frontmatter + "# 共同检索词 A\n共同检索词普通候选。\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        frontmatter + "# 共同检索词 B\n共同检索词，回答必须包含证据和验证命令。\n",
        encoding="utf-8",
    )
    searcher = ContextAwareSearch(wiki_base=str(tmp_path), database_dir=tmp_path)
    monkeypatch.setattr(searcher, "_compute_relevance", lambda *_args: 0.8)
    monkeypatch.setattr(searcher, "_compute_confidence", lambda *_args: 0.9)
    monkeypatch.setattr(searcher, "_compute_continuity", lambda *_args: 0.3)
    monkeypatch.setattr(searcher, "_compute_freshness", lambda *_args: 0.8)
    monkeypatch.setattr(searcher, "_compute_context_boost", lambda *_args: 1.0)
    monkeypatch.setattr(searcher, "_get_freshness_checker", lambda: None)
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:context-rank",
        agent="codex",
        host_kind="codex",
        capability_id="context-rank",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )
    narrowing = AccessNarrowing(
        session_id="profile-effect-session",
        project="mnemos",
    )

    results = searcher.search(
        "共同检索词",
        limit=2,
        allow_embedding=False,
        principal=principal,
        narrowing=narrowing,
    )

    assert [result.page_path for result in results] == ["b.md", "a.md"]
    assert searcher._profile_usage_evidence is None
    with sqlite3.connect(tmp_path / "user_signals.db") as conn:
        row = conn.execute("""
            SELECT matched_assertion_revisions, expected_delta, action_changed
            FROM profile_usage_log WHERE consumer='context_search'
            """).fetchone()
    assert json.loads(row[0]) == {assertion_id: revision_id}
    delta = json.loads(row[1])
    assert delta["query_id"].startswith("context-search:")
    assert {item["candidate_id"].split("|")[2] for item in delta["changed_candidates"]} == {
        "a.md",
        "b.md",
    }
    assert row[2] == 1

    monkeypatch.setattr(
        searcher,
        "_compute_relevance",
        lambda _query, candidate: (0.1 if str(candidate.get("path") or "") == "b.md" else 0.8),
    )
    filtered_results = searcher.search(
        "共同检索词",
        limit=2,
        allow_embedding=False,
        principal=principal,
        narrowing=narrowing,
    )
    assert [result.page_path for result in filtered_results] == ["a.md"]
    assert searcher._profile_usage_evidence is None
    with sqlite3.connect(tmp_path / "user_signals.db") as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM profile_usage_log " "WHERE consumer='context_search'"
            ).fetchone()[0]
            == 1
        )

    monkeypatch.setattr(searcher, "_recall_from_files", lambda _query: [])
    monkeypatch.setattr(searcher, "_recall_from_kg", lambda _query: [])
    assert (
        searcher.search(
            "没有任何页面包含的第二次查询",
            limit=2,
            allow_embedding=False,
            principal=principal,
            narrowing=narrowing,
        )
        == []
    )
    assert searcher._profile_usage_evidence is None
    with sqlite3.connect(tmp_path / "user_signals.db") as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM profile_usage_log " "WHERE consumer='context_search'"
            ).fetchone()[0]
            == 1
        )


def test_effectful_prompt_callers_do_not_swallow_usage_write_failure(
    monkeypatch,
) -> None:
    from core.application.persona import PersonaApplicationService
    from integrations.preflight_builder import _record_profile_v2_usage

    class FailingStore:
        @staticmethod
        def record_profile_usage(_usage, **_kwargs):
            raise sqlite3.OperationalError("usage database locked")

    monkeypatch.setattr(
        "core.persona.psyche.get_signal_store",
        lambda: FailingStore(),
    )
    revisions = {"pa_required": "par_required"}

    with pytest.raises(sqlite3.OperationalError, match="database locked"):
        _record_profile_v2_usage(
            "preflight_builder",
            revisions,
            principal=_profile_principal(),
            narrowing=_profile_narrowing(),
            baseline_output="base",
            persona_enabled_output="base\nprofile",
            expected_delta={"kind": "prompt_append"},
            outcome="persona_section_augmented",
            read_authorization_token="profile-read:test",
        )
    with pytest.raises(sqlite3.OperationalError, match="database locked"):
        PersonaApplicationService()._record_profile_usage(
            principal=_profile_principal(),
            narrowing=_profile_narrowing(),
            consumer="persona_behavior_prompt",
            matched_assertion_revisions=revisions,
            baseline_output=[],
            persona_enabled_output=["profile"],
            outcome="prompt_returned",
            read_authorization_token="profile-read:test",
        )


def test_context_search_does_not_return_persona_ranking_without_usage_receipt(
    monkeypatch,
    tmp_path,
) -> None:
    from core.app.context_search import ContextAwareSearch

    class FailingStore:
        def __init__(self, **_kwargs):
            pass

        @staticmethod
        def record_profile_usage(_usage, **_kwargs):
            raise sqlite3.OperationalError("usage database locked")

        @staticmethod
        def close():
            pass

    monkeypatch.setattr("core.persona.psyche.SignalStore", FailingStore)
    searcher = ContextAwareSearch(
        wiki_base=str(tmp_path / "wiki"),
        database_dir=tmp_path,
    )
    searcher._active_profile_query_id = "context-search:locked-usage"
    searcher._profile_usage_evidence = {
        "authorized_revisions": {"pa_required": "par_required"},
        "matched_assertion_ids": {"pa_required"},
        "query_id": searcher._active_profile_query_id,
        "rank_delta": [
            {
                "candidate_id": "wiki:b",
                "baseline_rank": 2,
                "persona_enabled_rank": 1,
                "matched_assertion_ids": ["pa_required"],
            }
        ],
        "eligible_candidate_ids": ["wiki:a", "wiki:b"],
    }

    with pytest.raises(sqlite3.OperationalError, match="database locked"):
        searcher._record_authorized_profile_usage(
            principal=_profile_principal(),
            narrowing=_profile_narrowing(),
            baseline_output=[
                {"candidate_id": "wiki:a", "page_path": "a.md", "rank": 1},
                {"candidate_id": "wiki:b", "page_path": "b.md", "rank": 2},
            ],
            persona_enabled_output=[
                {"candidate_id": "wiki:b", "page_path": "b.md", "rank": 1},
                {"candidate_id": "wiki:a", "page_path": "a.md", "rank": 2},
            ],
        )


def test_distillation_prompt_has_no_private_principal_bypass(
    monkeypatch,
    tmp_path,
) -> None:
    from core.hephaestus.prompt_builder import ContextAssembler

    monkeypatch.setattr(
        "core.persona.psyche.get_signal_store",
        lambda: (_ for _ in ()).throw(AssertionError("profile store must stay closed")),
    )
    assembler = ContextAssembler(tmp_path)

    assert assembler._build_cognitive_profile_context().endswith("- none")
    with pytest.raises(TypeError, match="unexpected keyword argument 'principal'"):
        assembler._build_cognitive_profile_context(principal=object())


@pytest.mark.parametrize(
    ("mutation_sql", "mutation_args"),
    [
        (
            "UPDATE profile_signals SET expires_at=? WHERE id=1",
            ("2000-01-01T00:00:00",),
        ),
        (
            "UPDATE profile_assertions SET contradicting_signals=? "
            "WHERE assertion_id='pa_ineligible'",
            ('["profile_signals:1"]',),
        ),
    ],
)
def test_expired_or_conflicted_assertion_cannot_record_effect(
    tmp_path,
    mutation_sql,
    mutation_args,
) -> None:
    from core.persona.psyche import ProfileUsageLog, SignalStore

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    revision_id = _seed_assertion(
        store,
        "pa_ineligible",
        "用户要求证据。",
    )
    read_authorization_token = _profile_read_token(store)
    conn = store._pool.get_conn()
    conn.execute(mutation_sql, mutation_args)
    conn.commit()

    with pytest.raises(ValueError, match="not eligible"):
        _record_profile_usage(
            store,
            ProfileUsageLog(
                consumer="preflight_builder",
                profile_fields_used=["pa_ineligible"],
                read_purpose="persona_preflight_read",
                read_authorization_token=read_authorization_token,
                target_receipt=_effect(
                    {"pa_ineligible": revision_id},
                    receipt_id=f"profile-target:{mutation_sql[:8]}",
                ),
            ),
        )
