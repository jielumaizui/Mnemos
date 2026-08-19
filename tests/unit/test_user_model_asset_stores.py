"""COG-016 independent stores, authority, and lifecycle contracts."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.cognitive.user_model_asset_store import (
    INTERACTION_PREFERENCE_SPEC,
    USER_COGNITIVE_BLINDSPOT_SPEC,
    InteractionPreferenceStore,
    UserCognitiveBlindspotStore,
    UserModelAssetStoreError,
    initialize_asset_store,
)
from core.cognitive.user_model_assets import (
    AssetScope,
    CognitiveAuthorityEvidence,
    InteractionPreference,
    UserCognitiveBlindspot,
)
from core.evidence.source_authority import SourceAuthorityCatalog


def _authority(
    text: str = "我明确只考虑了同一前提，并希望你长期给出实施级答案。",
) -> tuple[SourceAuthorityCatalog, CognitiveAuthorityEvidence]:
    content_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    catalog = SourceAuthorityCatalog.from_messages(
        (
            {
                "role": "user",
                "content": text,
                "source_span": {
                    "revision_id": "raw-event-user-1",
                    "role": "user",
                    "span_start": 0,
                    "span_end": len(text),
                    "content_hash": content_hash,
                },
            },
        ),
        allowed_source_event_ids=("raw-event-user-1",),
    )
    entry = next(item for item in catalog.entries if item.authority.value == "explicit_user")
    evidence = CognitiveAuthorityEvidence.from_catalog(
        catalog,
        source_authority_id=entry.source_authority_id,
        quote="我明确只考虑了同一前提",
    )
    return catalog, evidence


def _scope() -> AssetScope:
    return AssetScope(
        scope_type="session",
        scope_id="decision-session-1",
        purpose="decision_support",
        principal_id="mcp:codex:test",
    )


def _blindspot(evidence: CognitiveAuthorityEvidence) -> UserCognitiveBlindspot:
    return UserCognitiveBlindspot.create(
        blindspot_type="framing",
        description="The current options share one premise.",
        evidence_refs=(evidence.evidence_ref,),
        user_goal_ref="goal:choose-runtime",
        impact="May exclude a materially different runtime.",
        scope=_scope(),
        confidence=0.8,
        expires_at="2026-08-23T00:00:00+00:00",
        invalidation_condition="A later exact decision trace has independent premises.",
        authority_evidence_refs=(evidence.evidence_ref,),
        admission_command_id="blindspot-store-contract-1",
        admission_command_hash="sha256:" + "a" * 64,
        admission_idempotency_key="blindspot-store-contract:decision-1:framing",
        decision_context={
            "decision_id": "decision-store-contract-1",
            "decision_trace_revision_id": "decision-store-contract-1:r1",
            "decision_trace_hash": "sha256:" + "b" * 64,
            "session_id": "decision-session-1",
            "project_id": "mnemos",
            "persona_revision_id": "persona-v2:r1",
        },
    )


def _preference(evidence: CognitiveAuthorityEvidence) -> InteractionPreference:
    return InteractionPreference.create(
        dimension="response_depth",
        value="implementation_ready",
        evidence_refs=(evidence.evidence_ref,),
        scope=AssetScope(
            scope_type="user",
            scope_id="mcp:codex:test",
            purpose="interaction_adaptation",
            principal_id="mcp:codex:test",
        ),
        confidence=0.9,
        expires_at="2026-10-23T00:00:00+00:00",
        invalidation_condition="The user explicitly requests concise answers.",
        authority_evidence_refs=(evidence.evidence_ref,),
    )


def test_read_only_uninitialized_stores_create_no_paths(tmp_path):
    blindspot_path = tmp_path / "missing" / "user_cognitive_blindspots.db"
    preference_path = tmp_path / "missing" / "interaction_preferences.db"

    assert UserCognitiveBlindspotStore(blindspot_path).schema_status()["status"] == "uninitialized"
    assert InteractionPreferenceStore(preference_path).schema_status()["status"] == "uninitialized"
    assert not blindspot_path.parent.exists()


def test_runtime_writers_refuse_uninitialized_stores_without_creating_paths(tmp_path):
    catalog, evidence = _authority()
    blindspot_path = tmp_path / "missing" / "user_cognitive_blindspots.db"
    preference_path = tmp_path / "missing" / "interaction_preferences.db"

    with pytest.raises(UserModelAssetStoreError, match="explicit reconciliation"):
        UserCognitiveBlindspotStore(blindspot_path).persist(
            _blindspot(evidence), evidence=(evidence,), catalog=catalog
        )
    with pytest.raises(UserModelAssetStoreError, match="explicit reconciliation"):
        InteractionPreferenceStore(preference_path).persist(
            _preference(evidence), evidence=(evidence,), catalog=catalog
        )

    assert not blindspot_path.parent.exists()
    assert not preference_path.parent.exists()


def test_blindspot_and_preference_use_independent_stores_and_state_machines(tmp_path):
    from core.app.blindspot_asset_schema import initialize_blindspot_asset_schema

    catalog, evidence = _authority()
    knowledge_gap_path = tmp_path / "knowledge_gaps.db"
    blindspot_path = tmp_path / "blindspots.db"
    preference_path = tmp_path / "preferences.db"
    initialize_blindspot_asset_schema(knowledge_gap_path)
    initialize_asset_store(blindspot_path, USER_COGNITIVE_BLINDSPOT_SPEC)
    initialize_asset_store(preference_path, INTERACTION_PREFERENCE_SPEC)
    blindspot_store = UserCognitiveBlindspotStore(blindspot_path)
    preference_store = InteractionPreferenceStore(preference_path)
    blindspot = _blindspot(evidence)
    preference = _preference(evidence)

    knowledge_gap_bytes = knowledge_gap_path.read_bytes()
    assert blindspot_store.persist(blindspot, evidence=(evidence,), catalog=catalog)
    blindspot_bytes = blindspot_store.path.read_bytes()
    assert preference_store.persist(preference, evidence=(evidence,), catalog=catalog)

    assert knowledge_gap_path.read_bytes() == knowledge_gap_bytes
    assert blindspot_store.path.read_bytes() == blindspot_bytes
    assert blindspot_store.schema_status()["current_count"] == 1
    assert preference_store.schema_status()["current_count"] == 1
    blindspot_mtime = blindspot_store.path.stat().st_mtime_ns
    preference_mtime = preference_store.path.stat().st_mtime_ns
    assert blindspot_store.current_blindspots()[0].status == "suspected"
    assert preference_store.current_preferences()[0].status == "active"
    assert blindspot_store.path.stat().st_mtime_ns == blindspot_mtime
    assert preference_store.path.stat().st_mtime_ns == preference_mtime
    assert not (tmp_path / "blindspots.db-wal").exists()
    assert not (tmp_path / "preferences.db-wal").exists()

    confirmed = blindspot_store.transition_blindspot(
        blindspot.asset_id,
        expected_revision_id=blindspot.revision_id,
        next_status="confirmed",
        evidence=(evidence,),
        catalog=catalog,
    )
    invalidated = preference_store.transition_preference(
        preference.asset_id,
        expected_revision_id=preference.revision_id,
        next_status="invalidated",
        evidence=(evidence,),
        catalog=catalog,
    )

    assert confirmed.revision_id.endswith(":r2")
    assert confirmed.status == "confirmed"
    assert invalidated.revision_id.endswith(":r2")
    assert invalidated.status == "invalidated"
    with pytest.raises(UserModelAssetStoreError, match="invalid"):
        blindspot_store.transition_blindspot(
            blindspot.asset_id,
            expected_revision_id=confirmed.revision_id,
            next_status="confirmed",
            evidence=(evidence,),
            catalog=catalog,
        )


def test_low_authority_or_bare_string_cannot_create_cognitive_assets(tmp_path):
    text = "The assistant infers a stable preference."
    content_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    catalog = SourceAuthorityCatalog.from_messages(
        (
            {
                "role": "assistant",
                "content": text,
                "source_span": {
                    "revision_id": "raw-assistant-1",
                    "role": "assistant",
                    "span_start": 0,
                    "span_end": len(text),
                    "content_hash": content_hash,
                },
            },
        ),
        allowed_source_event_ids=("raw-assistant-1",),
    )
    entry = catalog.entries[0]
    with pytest.raises(ValueError, match="cannot authorize"):
        CognitiveAuthorityEvidence.from_catalog(
            catalog,
            source_authority_id=entry.source_authority_id,
            quote=text,
        )

    store = UserCognitiveBlindspotStore(tmp_path / "blindspots.db")
    with pytest.raises(TypeError, match="typed CognitiveAuthorityEvidence"):
        store.persist(  # type: ignore[arg-type]
            UserCognitiveBlindspot.create(
                blindspot_type="framing",
                description="inference",
                evidence_refs=("self-signed",),
                user_goal_ref="goal:1",
                impact="impact",
                scope=_scope(),
                confidence=0.5,
                expires_at="2026-08-23T00:00:00+00:00",
                invalidation_condition="invalidated",
            ),
            evidence=("self-signed",),
            catalog=catalog,
        )


def test_hamartia_admission_and_validation_use_canonical_store(tmp_path):
    from core.persona.hamartia import (
        BlindspotAdmission,
        BlindspotAdmissionCommand,
        BlindspotAdmissionService,
        BlindspotDecisionContext,
        BlindSpotProfileManager,
    )
    from core.persona.pythia import PreferenceProfile

    catalog, evidence = _authority()
    signal_store = MagicMock()
    signal_store.get_latest_persona_version.return_value = None
    asset_store_path = tmp_path / "blindspots.db"
    initialize_asset_store(asset_store_path, USER_COGNITIVE_BLINDSPOT_SPEC)
    asset_store = UserCognitiveBlindspotStore(asset_store_path)
    manager = BlindSpotProfileManager(store=signal_store, asset_store=asset_store)
    service = BlindspotAdmissionService(manager)
    admission = BlindspotAdmission(
        blindspot_type="framing",
        user_goal_ref="goal:choose-runtime",
        impact="May exclude a materially different runtime.",
        scope=_scope(),
        expires_at="2026-08-23T00:00:00+00:00",
        invalidation_condition="A later exact decision trace has independent premises.",
        evidence=(evidence,),
    )
    command = BlindspotAdmissionCommand(
        command_id="blindspot-admission-validation-1",
        idempotency_key="blindspot-admission-validation:decision-session-1:framing",
        decision_context=BlindspotDecisionContext(
            decision_id="decision-validation-1",
            decision_trace_revision_id="decision-validation-1:r1",
            decision_trace_hash="sha256:" + "2" * 64,
            session_id="decision-session-1",
            project_id="mnemos",
            persona_revision_id="persona-v2:r1",
        ),
        admission=admission,
        source_authority_catalog_hash=catalog.catalog_hash,
    )
    receipt = service.admit(
        command,
        session_context={
            "session_id": "decision-session-1",
            "project_id": "mnemos",
            "decision_id": "decision-validation-1",
            "task_type": "architecture",
            "decision_risk": "medium",
        },
        user_options=[
            {"premise": "one-frame", "time_horizon": "short", "keywords": ["same"]},
            {"premise": "one-frame", "time_horizon": "short", "keywords": ["same"]},
        ],
        persona=PreferenceProfile(),
        source_authority_catalog=catalog,
    )
    assert receipt.status == "committed"
    suspected = asset_store.current_blindspots()[0]
    assert suspected.status == "suspected"

    manager.record_challenge_outcome(
        "framing",
        "accepted",
        session_id="decision-session-1",
        asset_id=suspected.asset_id,
    )
    assert asset_store.current_blindspots()[0].status == "suspected"

    manager.record_challenge_outcome(
        "framing",
        "accepted",
        session_id="decision-session-1",
        asset_id=suspected.asset_id,
        outcome="validated",
        outcome_evidence=(evidence,),
        source_authority_catalog=catalog,
    )
    assert asset_store.current_blindspots()[0].status == "confirmed"


def test_canonical_blindspot_admission_command_is_idempotent_and_hash_bound(tmp_path):
    from core.persona.hamartia import (
        BlindspotAdmission,
        BlindspotAdmissionCommand,
        BlindspotAdmissionService,
        BlindspotDecisionContext,
        BlindSpotProfileManager,
    )
    from core.persona.pythia import PreferenceProfile

    catalog, evidence = _authority()
    signal_store = MagicMock()
    signal_store.get_latest_persona_version.return_value = None
    asset_store_path = tmp_path / "blindspots.db"
    initialize_asset_store(asset_store_path, USER_COGNITIVE_BLINDSPOT_SPEC)
    asset_store = UserCognitiveBlindspotStore(asset_store_path)
    service = BlindspotAdmissionService(
        BlindSpotProfileManager(store=signal_store, asset_store=asset_store)
    )
    admission = BlindspotAdmission(
        blindspot_type="framing",
        user_goal_ref="goal:choose-runtime",
        impact="May exclude a materially different runtime.",
        scope=_scope(),
        expires_at="2026-08-23T00:00:00+00:00",
        invalidation_condition="A later exact decision trace has independent premises.",
        evidence=(evidence,),
    )
    context = BlindspotDecisionContext(
        decision_id="decision-runtime-1",
        decision_trace_revision_id="decision-runtime-1:r1",
        decision_trace_hash="sha256:" + "1" * 64,
        session_id="decision-session-1",
        project_id="mnemos",
        persona_revision_id="persona-v2:r7",
    )
    command = BlindspotAdmissionCommand(
        command_id="blindspot-admission-1",
        idempotency_key="blindspot-admission:decision-runtime-1:framing",
        decision_context=context,
        admission=admission,
        source_authority_catalog_hash=catalog.catalog_hash,
    )
    kwargs = {
        "session_context": {
            "session_id": "decision-session-1",
            "project_id": "mnemos",
            "decision_id": "decision-runtime-1",
        },
        "user_options": [
            {"premise": "one-frame", "time_horizon": "short", "keywords": ["same"]},
            {"premise": "one-frame", "time_horizon": "short", "keywords": ["same"]},
        ],
        "persona": PreferenceProfile(),
        "source_authority_catalog": catalog,
    }

    created = service.admit(command, **kwargs)
    replayed = service.admit(command, **kwargs)

    assert created.status == "committed"
    assert replayed.status == "replayed"
    assert created.asset_id == replayed.asset_id
    current = asset_store.current_blindspots()
    assert len(current) == 1
    assert current[0].admission_command_id == command.command_id
    assert current[0].admission_command_hash == command.command_hash
    assert current[0].decision_context["decision_trace_revision_id"] == "decision-runtime-1:r1"

    conflicting = BlindspotAdmissionCommand(
        command_id="blindspot-admission-1-conflict",
        idempotency_key=command.idempotency_key,
        decision_context=context,
        admission=BlindspotAdmission(
            blindspot_type="framing",
            user_goal_ref="goal:changed",
            impact=admission.impact,
            scope=admission.scope,
            expires_at=admission.expires_at,
            invalidation_condition=admission.invalidation_condition,
            evidence=admission.evidence,
        ),
        source_authority_catalog_hash=catalog.catalog_hash,
    )
    with pytest.raises(UserModelAssetStoreError, match="idempotency key"):
        service.admit(conflicting, **kwargs)


def test_pythia_is_runtime_owner_for_interaction_preference(tmp_path):
    from core.persona.pythia import PreferenceAnalyzer

    catalog, evidence = _authority()
    preference_path = tmp_path / "preferences.db"
    initialize_asset_store(preference_path, INTERACTION_PREFERENCE_SPEC)
    preference_store = InteractionPreferenceStore(preference_path)
    analyzer = PreferenceAnalyzer(store=MagicMock(), interaction_preference_store=preference_store)
    preference = analyzer.record_interaction_preference(
        dimension="response_depth",
        value="implementation_ready",
        scope=AssetScope(
            scope_type="user",
            scope_id="user-1",
            purpose="interaction_adaptation",
        ),
        confidence=0.9,
        expires_at="2026-10-23T00:00:00+00:00",
        invalidation_condition="The user explicitly requests a concise response.",
        evidence=(evidence,),
        source_authority_catalog=catalog,
    )
    assert preference_store.current_preferences()[0].asset_id == preference.asset_id

    invalidated = analyzer.invalidate_interaction_preference(
        preference,
        evidence=(evidence,),
        source_authority_catalog=catalog,
    )
    assert invalidated.status == "invalidated"


def test_context_search_consumes_only_exact_principal_preference(tmp_path):
    from core.access_policy import AccessNarrowing, PrincipalEnvelope
    from core.application.intelligence import IntelligenceApplicationService

    catalog, evidence = _authority()
    path = tmp_path / "interaction_preferences.db"
    initialize_asset_store(path, INTERACTION_PREFERENCE_SPEC)
    store = InteractionPreferenceStore(path)
    preference = _preference(evidence)
    assert store.persist(preference, evidence=(evidence,), catalog=catalog)
    before_bytes = path.read_bytes()
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:test",
        agent="codex",
        host_kind="codex",
        capability_id="test",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset(),
    )
    with patch("core.config.get_config", return_value=SimpleNamespace(database_dir=tmp_path)):
        accepted, summary = IntelligenceApplicationService._active_interaction_preferences(
            principal=principal,
            narrowing=AccessNarrowing(),
        )
        rejected, _ = IntelligenceApplicationService._active_interaction_preferences(
            principal=PrincipalEnvelope(
                principal_id="mcp:other:test",
                agent="other",
                host_kind="codex",
                capability_id="test",
                capabilities=frozenset({"memory_read"}),
                allowed_projects=frozenset(),
            ),
            narrowing=AccessNarrowing(),
        )

    assert accepted[0]["asset_id"] == preference.asset_id
    assert summary["interaction_preference_authorized"] == 1
    assert rejected == []
    assert path.read_bytes() == before_bytes
