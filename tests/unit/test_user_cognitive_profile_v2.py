from __future__ import annotations

import json
from pathlib import Path


def _profile_principal():
    from core.access_policy import PrincipalEnvelope

    return PrincipalEnvelope(
        principal_id="mcp:codex:profile-test",
        agent="codex",
        host_kind="codex",
        capability_id="profile-test",
        capabilities=frozenset({"memory_read"}),
    )


def _profile_narrowing():
    from core.access_policy import AccessNarrowing

    return AccessNarrowing(session_id="profile-session")


def _seed_profile_assertion(tmp_path: Path):
    from core.cognitive.access_control import make_cognitive_access_envelope
    from core.persona.psyche import ProfileAssertion, ProfileSignal, SignalStore

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    signal_id = store.record_profile_signal(
        ProfileSignal(
            source_event_id="session:test-1",
            signal_type="explicit_correction",
            dimension="judgment_standard",
            value="用户要求先给证据和验证命令",
            evidence="user said: 先测试再说",
            confidence=0.9,
            privacy_level="local",
            observed_at="2026-07-05T10:00:00",
            access_control=make_cognitive_access_envelope(
                owner_principal_id="source-agent:codex",
                owner_agent="codex",
                scope_type="session",
                scope_id="profile-session",
                session_id="profile-session",
                purposes=(
                    "persona_preflight_read",
                    "context_search_profile",
                    "persona_summary_read",
                    "persona_behavior_prompt",
                    "persona_usage_metrics",
                ),
                consent_provenance_refs=("session:test-1",),
                sensitivity="sensitive",
                retention_policy="persona_retention",
                source_acl_lineage=("sha256:profile-test-source",),
                visibility="agent",
            ),
        )
    )
    assertion_id = store.upsert_profile_assertion(
        ProfileAssertion(
            assertion_id="pa_judgment_standard_test",
            dimension="judgment_standard",
            claim="用户判断质量时要求证据、验证命令和风险边界。",
            supporting_signals=[f"profile_signals:{signal_id}"],
            contradicting_signals=[],
            confidence=0.9,
            privacy_level="local",
            last_verified_at="2026-07-05T10:00:00",
        )
    )
    return store, assertion_id


def _usage_receipt(
    store,
    assertion_ids,
    *,
    consumer: str,
    target_type: str = "prompt",
    before="baseline",
    after="persona-enabled",
    receipt_id: str = "",
):
    from core.persona.profile_effect import compare_profile_effect

    revisions = {
        assertion_id: str(store.get_profile_assertion_revisions(assertion_id)[-1]["revision_id"])
        for assertion_id in assertion_ids
    }
    return compare_profile_effect(
        owner=consumer,
        target_type=target_type,
        target_id=f"{consumer}_target",
        matched_assertion_revisions=revisions,
        baseline_output=before,
        persona_enabled_output=after,
        expected_delta={"kind": f"{target_type}_delta"},
        receipt_id=receipt_id,
    )


def _usage_read_token(store, *, consumer: str, purpose: str) -> str:
    _profile, access = store.build_authorized_user_cognitive_profile_v2(
        principal=_profile_principal(),
        narrowing=_profile_narrowing(),
        purpose=purpose,
        consumer=consumer,
    )
    return str(access["read_authorization_token"])


def test_profile_signal_assertion_usage_contract(tmp_path: Path) -> None:
    from core.persona.psyche import ProfileUsageLog

    store, assertion_id = _seed_profile_assertion(tmp_path)

    profile, access = store.build_authorized_user_cognitive_profile_v2(
        principal=_profile_principal(),
        narrowing=_profile_narrowing(),
        purpose="persona_preflight_read",
        consumer="preflight_builder",
    )
    assert access["authorized_count"] == 1
    assert profile["schema_version"] == "mnemos.user_cognitive_profile.v2"
    assert profile["confidence"] == 0.9
    assert profile["judgment_standards"][0]["assertion_id"] == assertion_id
    assert profile["judgment_standards"][0]["privacy_level"] == "local"
    assert profile["evidence_refs"] == ["profile_signals:1"]

    store.record_profile_usage(
        ProfileUsageLog(
            consumer="preflight_builder",
            profile_fields_used=[assertion_id],
            read_purpose="persona_preflight_read",
            read_authorization_token=str(access["read_authorization_token"]),
            target_receipt=_usage_receipt(
                store,
                [assertion_id],
                consumer="preflight_builder",
            ),
            outcome="persona_section_augmented",
            user_feedback="accepted",
        ),
        principal=_profile_principal(),
        narrowing=_profile_narrowing(),
    )
    metrics = store.get_authorized_profile_usage_metrics(
        days=7,
        principal=_profile_principal(),
        narrowing=_profile_narrowing(),
        purpose="persona_usage_metrics",
    )
    assert metrics["by_consumer"]["preflight_builder"] == 1
    assert metrics["action_changed_count"] == 1
    assert metrics["feedback"]["accepted"] == 1
    conn = store._pool.get_conn()
    revision_ids, scope_snapshot = conn.execute(
        "SELECT profile_revision_ids, scope_snapshot FROM profile_usage_log"
    ).fetchone()
    assert json.loads(revision_ids) == [
        store.get_profile_assertion_revisions(assertion_id)[-1]["revision_id"]
    ]
    assert json.loads(scope_snapshot)["scope"]["session_id"] == "profile-session"


def test_profile_usage_rejects_caller_forged_revision_or_scope(tmp_path: Path) -> None:
    import pytest

    from core.persona.psyche import ProfileUsageLog

    store, assertion_id = _seed_profile_assertion(tmp_path)
    with pytest.raises(ValueError, match="consumer/purpose contract"):
        store.record_profile_usage(
            ProfileUsageLog(
                consumer="context_search",
                profile_fields_used=[assertion_id],
                target_receipt=_usage_receipt(
                    store,
                    [assertion_id],
                    consumer="context_search",
                    target_type="ranking",
                    receipt_id="missing-purpose",
                ),
                outcome="missing-purpose",
            ),
            principal=_profile_principal(),
            narrowing=_profile_narrowing(),
        )
    with pytest.raises(ValueError, match="revision ids"):
        store.record_profile_usage(
            ProfileUsageLog(
                consumer="context_search",
                profile_fields_used=[assertion_id],
                read_purpose="context_search_profile",
                read_authorization_token=_usage_read_token(
                    store,
                    consumer="context_search",
                    purpose="context_search_profile",
                ),
                profile_revision_ids=["par_forged"],
                target_receipt=_usage_receipt(
                    store,
                    [assertion_id],
                    consumer="context_search",
                    target_type="ranking",
                    receipt_id="forged-revision",
                ),
                outcome="forged",
            ),
            principal=_profile_principal(),
            narrowing=_profile_narrowing(),
        )
    with pytest.raises(ValueError, match="scope snapshot"):
        store.record_profile_usage(
            ProfileUsageLog(
                consumer="context_search",
                profile_fields_used=[assertion_id],
                read_purpose="context_search_profile",
                read_authorization_token=_usage_read_token(
                    store,
                    consumer="context_search",
                    purpose="context_search_profile",
                ),
                scope_snapshot={"scope": {"session_id": "forged"}},
                target_receipt=_usage_receipt(
                    store,
                    [assertion_id],
                    consumer="context_search",
                    target_type="ranking",
                    receipt_id="forged-scope",
                ),
                outcome="forged",
            ),
            principal=_profile_principal(),
            narrowing=_profile_narrowing(),
        )


def test_profile_assertion_corrections_are_append_only_and_idempotent(tmp_path: Path) -> None:
    from core.persona.psyche import ProfileAssertion

    store, assertion_id = _seed_profile_assertion(tmp_path)
    original = store.get_profile_assertion_revisions(assertion_id)
    assert len(original) == 1

    store.upsert_profile_assertion(
        ProfileAssertion(
            assertion_id=assertion_id,
            dimension="judgment_standard",
            claim="用户判断质量时要求证据、验证命令、风险边界和回归测试。",
            supporting_signals=["profile_signals:1"],
            confidence=0.95,
            privacy_level="local",
            last_verified_at="2026-07-06T10:00:00",
            expected_revision_id=original[-1]["revision_id"],
        )
    )
    corrected = store.get_profile_assertion_revisions(assertion_id)

    assert [row["revision_number"] for row in corrected] == [1, 2]
    assert corrected[1]["supersedes_revision_id"] == corrected[0]["revision_id"]
    assert corrected[0]["claim"] != corrected[1]["claim"]

    store.upsert_profile_assertion(
        ProfileAssertion(
            assertion_id=assertion_id,
            dimension="judgment_standard",
            claim="用户判断质量时要求证据、验证命令、风险边界和回归测试。",
            supporting_signals=["profile_signals:1"],
            confidence=0.95,
            privacy_level="local",
            last_verified_at="2026-07-07T10:00:00",
            expected_revision_id=corrected[-1]["revision_id"],
        )
    )

    assert len(store.get_profile_assertion_revisions(assertion_id)) == 2


def test_persona_summary_requires_principal_before_opening_legacy_profile(monkeypatch) -> None:
    from core.application.persona import PersonaApplicationService

    class FakePreferenceAnalyzer:
        def __init__(self):
            raise AssertionError("legacy profile bytes must not be opened")

    monkeypatch.setattr("core.persona.pythia.PreferenceAnalyzer", FakePreferenceAnalyzer)
    result = PersonaApplicationService().persona_summary()

    assert result == {
        "success": False,
        "code": "principal_required",
        "profile": {},
        "user_cognitive_profile_v2": {
            "schema_version": "mnemos.user_cognitive_profile.v2",
            "status": "restricted",
            "profile_assertions": [],
        },
    }


def test_persona_summary_returns_only_authorized_v2_without_legacy_profile(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from core.application.persona import PersonaApplicationService

    store, _assertion_id = _seed_profile_assertion(tmp_path)

    class FakePreferenceAnalyzer:
        def __init__(self):
            raise AssertionError("legacy profile bytes must not be opened")

    monkeypatch.setattr("core.persona.pythia.PreferenceAnalyzer", FakePreferenceAnalyzer)
    monkeypatch.setattr("core.persona.psyche.get_signal_store", lambda: store)
    result = PersonaApplicationService().persona_summary(
        principal=_profile_principal(),
        narrowing=_profile_narrowing(),
    )

    assert result["success"] is True
    assert result["profile"] == {}
    profile_v2 = result["user_cognitive_profile_v2"]
    assert profile_v2["schema_version"] == "mnemos.user_cognitive_profile.v2"
    assert profile_v2["judgment_standards"]


def test_persona_behavior_prompt_usage_binds_the_actual_read_purpose(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import sqlite3

    from core.application.persona import PersonaApplicationService

    store, _assertion_id = _seed_profile_assertion(tmp_path)
    monkeypatch.setattr("core.persona.psyche.get_signal_store", lambda: store)

    result = PersonaApplicationService().persona_behavior_prompt(
        principal=_profile_principal(),
        narrowing=_profile_narrowing(),
    )

    assert result["success"] is True
    with sqlite3.connect(tmp_path / "user_signals.db") as conn:
        row = conn.execute("""
            SELECT consumer, read_purpose, matched_assertion_revisions,
                   expected_delta, baseline_hash, persona_enabled_hash
            FROM profile_usage_log
            WHERE consumer='persona_behavior_prompt'
            """).fetchone()
    assert row[:2] == ("persona_behavior_prompt", "persona_behavior_prompt")
    assert json.loads(row[3])["rendered_assertion_revisions"] == json.loads(row[2])
    assert row[4] != row[5]


def test_persona_update_requires_principal_before_collecting_signals(monkeypatch) -> None:
    from core.application.persona import PersonaApplicationService

    class ForbiddenCollector:
        def __init__(self):
            raise AssertionError("persona signal stores must not open without a principal")

    monkeypatch.setattr("core.persona.daimon.SignalCollector", ForbiddenCollector)
    result = PersonaApplicationService().persona_update()

    assert result["success"] is False
    assert result["code"] == "principal_required"
    assert result["profile"] == {}
    assert result["user_cognitive_profile_v2"]["status"] == "restricted"


def test_persona_update_returns_authorized_v2_without_legacy_profile(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from core.application.persona import PersonaApplicationService

    store, _assertion_id = _seed_profile_assertion(tmp_path)

    class FakeCollector:
        def collect_all(self):
            return {"collected": 1}

    class FakePreferenceAnalyzer:
        def analyze(self, *, days: int):
            assert days == 30

    monkeypatch.setattr("core.persona.daimon.SignalCollector", FakeCollector)
    monkeypatch.setattr("core.persona.pythia.PreferenceAnalyzer", FakePreferenceAnalyzer)
    monkeypatch.setattr("core.persona.psyche.get_signal_store", lambda: store)

    result = PersonaApplicationService().persona_update(
        principal=_profile_principal(),
        narrowing=_profile_narrowing(),
    )

    assert result["success"] is True
    assert result["signals_collected"] == {"collected": 1}
    assert result["profile"] == {}
    assert result["user_cognitive_profile_v2"]["judgment_standards"]


def test_profile_assertion_denial_happens_before_body_deserialization(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from core.access_policy import AccessNarrowing, PrincipalEnvelope

    store, _assertion_id = _seed_profile_assertion(tmp_path)

    def body_reader_must_not_run(_row):
        raise AssertionError("denied profile claim was deserialized")

    monkeypatch.setattr(
        store._cognitive_profiles,
        "_assertion_from_row",
        body_reader_must_not_run,
    )
    profile, access = store.build_authorized_user_cognitive_profile_v2(
        principal=PrincipalEnvelope(
            principal_id="mcp:codex:wrong-session",
            agent="codex",
            host_kind="codex",
            capability_id="wrong-session",
            capabilities=frozenset({"memory_read"}),
        ),
        narrowing=AccessNarrowing(session_id="another-session"),
        purpose="persona_preflight_read",
    )

    assert profile["profile_assertions"] == []
    assert access["denied_by_reason"]["session_scope_mismatch"] == 1


def test_preflight_builder_consumes_profile_v2(monkeypatch, tmp_path: Path) -> None:
    from core.access_policy import AccessNarrowing, PrincipalEnvelope
    from integrations import preflight_builder

    store, assertion_id = _seed_profile_assertion(tmp_path)

    class FakeConfig:
        def get(self, key, default=None):
            values = {
                "persona.enabled": True,
                "persona.strategy_injection_enabled": True,
                "persona.strategy_token_limit": 300,
            }
            return values.get(key, default)

    monkeypatch.setattr("integrations.preflight_builder.get_config", lambda: FakeConfig())
    monkeypatch.setattr(
        "integrations.preflight_builder._load_contextual_persona_profiles",
        lambda _principal, _narrowing: ({}, {}),
    )
    monkeypatch.setattr("core.persona.delphi.get_behavior_prompt", lambda _agent: "base")
    monkeypatch.setattr("core.persona.psyche.get_signal_store", lambda: store)

    section = preflight_builder.build_persona_section(
        "codex",
        working_dir=str(tmp_path),
        principal=PrincipalEnvelope(
            principal_id="mcp:codex:profile-test",
            agent="codex",
            host_kind="codex",
            capability_id="profile-test",
            capabilities=frozenset({"memory_read"}),
        ),
        narrowing=AccessNarrowing(session_id="profile-session"),
    )

    assert "User Cognitive Profile v2" in section
    assert "用户判断质量时要求证据" in section
    metrics = store.get_authorized_profile_usage_metrics(
        days=7,
        principal=_profile_principal(),
        narrowing=_profile_narrowing(),
        purpose="persona_usage_metrics",
    )
    assert metrics["by_consumer"]["preflight_builder"] == 1
    assert (
        assertion_id
        in store.get_profile_assertions(
            principal=_profile_principal(),
            narrowing=_profile_narrowing(),
            purpose="persona_preflight_read",
        )[0]["assertion_id"]
    )


def test_context_search_records_profile_v2_only_after_authorization(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from core.access_policy import AccessNarrowing, PrincipalEnvelope
    from core.app.context_search import ContextAwareSearch

    store, assertion_id = _seed_profile_assertion(tmp_path)
    monkeypatch.setattr("core.persona.psyche.get_signal_store", lambda: store)
    monkeypatch.setattr(
        "core.app.context_search.get_config",
        lambda: type("Cfg", (), {"database_dir": tmp_path})(),
    )

    searcher = ContextAwareSearch(wiki_base=str(tmp_path))
    context_principal = PrincipalEnvelope(
        principal_id="mcp:codex:context-search-test",
        agent="codex",
        host_kind="codex",
        capability_id="context-search-test",
        capabilities=frozenset({"memory_read"}),
    )
    context_narrowing = AccessNarrowing(session_id="profile-session")
    weights = searcher._get_profile_weights(
        context_principal,
        context_narrowing,
    )

    assert weights["persona_assertions"]
    assert (
        "context_search"
        not in store.get_authorized_profile_usage_metrics(
            days=7,
            principal=_profile_principal(),
            narrowing=_profile_narrowing(),
            purpose="persona_usage_metrics",
        )["by_consumer"]
    )
    assert searcher._profile_usage_evidence is not None
    searcher._active_profile_query_id = "context-search:test-authorized"
    searcher._profile_usage_evidence["query_id"] = searcher._active_profile_query_id
    searcher._profile_usage_evidence["matched_assertion_ids"].add(assertion_id)
    searcher._profile_usage_evidence["rank_delta"] = [
        {
            "candidate_id": "wiki:b",
            "baseline_rank": 2,
            "persona_enabled_rank": 1,
            "matched_assertion_ids": [assertion_id],
        }
    ]
    searcher._profile_usage_evidence["eligible_candidate_ids"] = ["wiki:a", "wiki:b"]
    searcher._record_authorized_profile_usage(
        principal=context_principal,
        narrowing=context_narrowing,
        baseline_output=[
            {"candidate_id": "wiki:a", "page_path": "a", "rank": 1},
            {"candidate_id": "wiki:b", "page_path": "b", "rank": 2},
        ],
        persona_enabled_output=[
            {"candidate_id": "wiki:b", "page_path": "b", "rank": 1},
            {"candidate_id": "wiki:a", "page_path": "a", "rank": 2},
        ],
    )
    assert (
        store.get_authorized_profile_usage_metrics(
            days=7,
            principal=_profile_principal(),
            narrowing=_profile_narrowing(),
            purpose="persona_usage_metrics",
        )["by_consumer"]["context_search"]
        == 1
    )


def test_expired_or_conflicted_profile_assertions_are_not_authorized(tmp_path: Path) -> None:
    from core.persona.psyche import ProfileAssertion

    store, assertion_id = _seed_profile_assertion(tmp_path)
    conn = store._pool.get_conn()
    conn.execute(
        "UPDATE profile_signals SET expires_at=? WHERE id=1",
        ("2000-01-01T00:00:00",),
    )
    conn.commit()

    profile, access = store.build_authorized_user_cognitive_profile_v2(
        principal=_profile_principal(),
        narrowing=_profile_narrowing(),
        purpose="context_search_profile",
    )

    assert profile["profile_assertions"] == []
    assert access["denied_by_reason"]["assertion_evidence_expired"] == 1

    conn = store._pool.get_conn()
    conn.execute(
        "UPDATE profile_signals SET expires_at=NULL WHERE id=1",
    )
    conn.commit()
    current_revision_id = conn.execute(
        "SELECT revision_id FROM profile_assertion_heads WHERE assertion_id=?",
        (assertion_id,),
    ).fetchone()[0]
    store.upsert_profile_assertion(
        ProfileAssertion(
            assertion_id=assertion_id,
            dimension="reasoning_preference",
            claim="prefer counterexamples",
            supporting_signals=["profile_signals:1"],
            contradicting_signals=["profile_signals:1"],
            confidence=0.8,
            expected_revision_id=current_revision_id,
        )
    )
    profile, access = store.build_authorized_user_cognitive_profile_v2(
        principal=_profile_principal(),
        narrowing=_profile_narrowing(),
        purpose="context_search_profile",
    )

    assert profile["profile_assertions"] == []
    assert access["denied_by_reason"]["assertion_conflicted"] == 1


def test_corrupt_profile_projection_is_never_consumed(tmp_path: Path) -> None:
    store, assertion_id = _seed_profile_assertion(tmp_path)
    try:
        conn = store._pool.get_conn()
        conn.execute(
            "UPDATE profile_assertions SET claim=? WHERE assertion_id=?",
            ("forged current claim", assertion_id),
        )
        conn.commit()

        profile, access = store.build_authorized_user_cognitive_profile_v2(
            principal=_profile_principal(),
            narrowing=_profile_narrowing(),
            purpose="context_search_profile",
        )

        assert profile["profile_assertions"] == []
        assert access["denied_by_reason"]["assertion_projection_head_mismatch"] == 1
    finally:
        store.close()


def test_distill_prompt_does_not_open_profile_without_server_principal(
    monkeypatch, tmp_path: Path
) -> None:
    from core.hephaestus.prompt_builder import ContextAssembler, DistillTask, Session

    store, _assertion_id = _seed_profile_assertion(tmp_path)
    monkeypatch.setattr("core.persona.psyche.get_signal_store", lambda: store)

    context = ContextAssembler(tmp_path).assemble(
        DistillTask(
            task_type="extract",
            session=Session(
                id="s1",
                agent_name="codex",
                messages=[{"role": "user", "content": "请记住先测试再提交"}],
            ),
        )
    )

    assert context["cognitive_profile_context"].endswith("- none")
    assert store.get_profile_usage_metrics(days=7)["total_usages"] == 0


def test_downstream_consumers_do_not_open_profile_without_server_principal(
    monkeypatch, tmp_path: Path
) -> None:
    from core.hephaestus.cognitive_value_gate import CognitiveValueGate
    from core.kia.ixion import CognitiveDecisionFlywheel
    from core.ops.auto_healing import _record_profile_usage_for_auto_heal

    store, _assertion_id = _seed_profile_assertion(tmp_path)
    monkeypatch.setattr("core.persona.psyche.get_signal_store", lambda: store)

    CognitiveValueGate().evaluate("这是一个可复用方法，包含验证命令和证据。")
    _record_profile_usage_for_auto_heal({"status": "ok", "issues": [{"issue_id": "x"}]})
    CognitiveDecisionFlywheel._record_profile_usage_for_flywheel({"gaps": [object()]})

    consumers = store.get_profile_usage_metrics(days=7)["by_consumer"]
    assert consumers == {}
