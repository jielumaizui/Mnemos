from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

from core.cognitive.user_model_asset_store import (
    USER_COGNITIVE_BLINDSPOT_SPEC,
    UserCognitiveBlindspotStore,
    initialize_asset_store,
)
from core.cognitive.user_model_assets import (
    AssetScope,
    CognitiveAuthorityEvidence,
    UserCognitiveBlindspot,
)
from core.evidence.source_authority import SourceAuthorityCatalog
from core.persona.hamartia import (
    BlindSpotProfileManager,
    BlindspotHypothesis,
    CanonicalBlindspotChallenge,
)
from core.persona.pythia import PreferenceProfile


def _authority() -> tuple[SourceAuthorityCatalog, CognitiveAuthorityEvidence]:
    text = "我明确要求你在这个决策中提醒我检查遗漏的替代方案。"
    content_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    catalog = SourceAuthorityCatalog.from_messages(
        (
            {
                "role": "user",
                "content": text,
                "source_span": {
                    "revision_id": "raw-user-canonical-challenge",
                    "role": "user",
                    "span_start": 0,
                    "span_end": len(text),
                    "content_hash": content_hash,
                },
            },
        ),
        allowed_source_event_ids=("raw-user-canonical-challenge",),
    )
    entry = next(item for item in catalog.entries if item.authority.value == "explicit_user")
    return catalog, CognitiveAuthorityEvidence.from_catalog(
        catalog,
        source_authority_id=entry.source_authority_id,
        quote="我明确要求你在这个决策中提醒我检查遗漏的替代方案",
    )


def _asset_store(tmp_path: Path) -> UserCognitiveBlindspotStore:
    path = tmp_path / "user_cognitive_blindspots.db"
    initialize_asset_store(path, USER_COGNITIVE_BLINDSPOT_SPEC)
    return UserCognitiveBlindspotStore(path)


def _persist_asset(
    store: UserCognitiveBlindspotStore,
    *,
    expires_at: str = "2026-08-23T00:00:00+00:00",
    scope_type: str = "session",
    scope_id: str = "decision-session-1",
    principal_id: str = "mcp:codex:test",
    blindspot_type: str = "framing",
    description: str = "The current options share one premise.",
    admission_key: str = "canonical-challenge",
) -> UserCognitiveBlindspot:
    catalog, evidence = _authority()
    asset = UserCognitiveBlindspot.create(
        blindspot_type=blindspot_type,
        description=description,
        evidence_refs=(evidence.evidence_ref,),
        user_goal_ref="goal:choose-runtime",
        impact="May exclude a materially different runtime.",
        scope=AssetScope(
            scope_type=scope_type,
            scope_id=scope_id,
            purpose="decision_support",
            principal_id=principal_id,
        ),
        confidence=0.9,
        expires_at=expires_at,
        invalidation_condition="A later exact decision trace has independent premises.",
        authority_evidence_refs=(evidence.evidence_ref,),
        admission_command_id=f"blindspot-admission-{admission_key}",
        admission_command_hash="sha256:" + "a" * 64,
        admission_idempotency_key=f"blindspot-admission:{admission_key}",
        decision_context={
            "decision_id": "decision-canonical-challenge",
            "decision_trace_revision_id": "decision-canonical-challenge:r1",
            "decision_trace_hash": "sha256:" + "b" * 64,
            "session_id": "decision-session-1",
            "project_id": "mnemos",
            "persona_revision_id": "persona-v2:r1",
        },
    )
    assert store.persist(asset, evidence=(evidence,), catalog=catalog)
    return asset


def _manager(store: UserCognitiveBlindspotStore) -> BlindSpotProfileManager:
    signal_store = MagicMock()
    signal_store.get_latest_persona_version.return_value = None
    manager = BlindSpotProfileManager(store=signal_store, asset_store=store)
    manager.detector.detect = MagicMock(
        return_value=[
            BlindspotHypothesis(
                type="framing",
                description="Unpersisted assistant inference.",
                evidence=["assistant-only"],
                confidence=0.99,
            )
        ]
    )
    return manager


def _context() -> dict[str, str]:
    return {
        "session_id": "decision-session-1",
        "project_id": "mnemos",
        "principal_id": "mcp:codex:test",
        "decision_risk": "high",
        "decision_created_at": "2026-07-23T00:00:00+00:00",
    }


def test_shadow_only_hypothesis_cannot_be_returned_as_user_challenge(tmp_path: Path) -> None:
    manager = _manager(_asset_store(tmp_path))

    challenges = manager.analyze_and_update(
        _context(),
        [{"id": "one"}, {"id": "two"}],
        PreferenceProfile(),
    )

    assert challenges == []
    assert manager.last_challenge_disposition == "no_admitted_canonical_revision"
    manager.store.update_blindspot_profile.assert_not_called()


def test_admitted_current_asset_is_returned_with_exact_revision_and_content_hash(
    tmp_path: Path,
) -> None:
    store = _asset_store(tmp_path)
    asset = _persist_asset(store)
    manager = _manager(store)

    challenges = manager.analyze_and_update(
        _context(),
        [{"id": "one"}, {"id": "two"}],
        PreferenceProfile(),
    )

    assert len(challenges) == 1
    challenge = challenges[0]
    assert isinstance(challenge, CanonicalBlindspotChallenge)
    assert challenge.asset_id == asset.asset_id
    assert challenge.asset_revision_id == asset.revision_id
    assert challenge.asset_revision_hash.startswith("sha256:")
    assert challenge.challenge_content == asset.description
    assert challenge.challenge_content_hash.startswith("sha256:")
    assert challenge.source_kind == "canonical_admitted_blindspot"


def test_dismissed_or_expired_current_asset_cannot_be_challenged(tmp_path: Path) -> None:
    dismissed_store = _asset_store(tmp_path / "dismissed")
    dismissed = _persist_asset(dismissed_store)
    catalog, evidence = _authority()
    dismissed_store.transition_blindspot(
        dismissed.asset_id,
        expected_revision_id=dismissed.revision_id,
        next_status="dismissed",
        evidence=(evidence,),
        catalog=catalog,
    )
    assert _manager(dismissed_store).analyze_and_update(
        _context(), [{"id": "one"}, {"id": "two"}], PreferenceProfile()
    ) == []

    expired_store = _asset_store(tmp_path / "expired")
    _persist_asset(expired_store, expires_at="2026-07-22T00:00:00+00:00")
    assert _manager(expired_store).analyze_and_update(
        _context(), [{"id": "one"}, {"id": "two"}], PreferenceProfile()
    ) == []


def test_queue_persists_delivery_command_bound_to_asset_decision_and_content(
    tmp_path: Path,
) -> None:
    from core.cognitive.state_store import CognitiveStateStore
    from core.persona.challenge_queue import (
        PERSONA_CHALLENGE_CONSUMER,
        PERSONA_CHALLENGE_DELIVERY_SCHEMA_VERSION,
        PersonaChallengeQueueConsumer,
    )
    from tests.unit.test_persona_challenge_queue_p5 import (
        _config,
        _insert_persona_revision,
        _seal_decision,
    )

    _insert_persona_revision(
        tmp_path / "user_signals.db",
        revision_id="persona-revision:1:challenge",
        version=1,
    )
    asset_store = _asset_store(tmp_path)
    asset = _persist_asset(
        asset_store,
        scope_type="project",
        scope_id="mnemos",
        principal_id="mcp:codex:material-sink-test",
    )
    _seal_decision(tmp_path)
    state = CognitiveStateStore(_config(tmp_path))
    command = state.pending_commands(PERSONA_CHALLENGE_CONSUMER)[0]

    consumer = PersonaChallengeQueueConsumer(
        _config(tmp_path),
        manager_factory=lambda _store: _manager(asset_store),
    )
    result = consumer.run_once()

    assert result["status"] == "awaiting_presentation"
    assert result["reason"] == "delivery_pending_presentation"
    assert result["challenges"] == 1
    assert len(state.pending_commands(PERSONA_CHALLENGE_CONSUMER)) == 1
    presentation = consumer.record_presentation(
        command_id=command["command_id"],
        delivery_ids=result["delivery_ids"],
        host_agent="codex",
        rendered_content_hash=result["rendered_content_hash"],
    )
    receipt = state.effect_receipt(command["command_id"])
    assert receipt is not None
    outcome = json.loads(receipt["consumption_outcome"])
    delivery = outcome["delivery_commands"][0]
    assert delivery["schema_version"] == PERSONA_CHALLENGE_DELIVERY_SCHEMA_VERSION
    assert delivery["status"] == "pending_presentation"
    assert delivery["source_command_id"] == command["command_id"]
    assert delivery["decision_trace"]["revision_id"] == command["revision_id"]
    assert delivery["asset_revision"]["asset_id"] == asset.asset_id
    assert delivery["asset_revision"]["revision_id"] == asset.revision_id
    assert delivery["asset_revision"]["content_hash"].startswith("sha256:")
    assert delivery["challenge"]["content"] == asset.description
    assert delivery["challenge"]["content_hash"].startswith("sha256:")
    assert delivery["delivery_id"] in result["delivery_ids"]
    assert outcome["presentation_receipt"]["receipt_hash"] == presentation["receipt_hash"]
    from tests.unit.test_persona_challenge_queue_p5 import _audit

    audit = _audit(tmp_path / "producer_consumer_ledger.db")
    assert audit["ok"] is True
    assert audit["delivery_command_count"] == 1
    assert audit["presented_challenge_without_canonical_revision"] == 0
    assert audit["presented_challenge_from_shadow_only"] == 0
    assert audit["stale_or_revoked_challenge"] == 0


def test_queue_rejects_changed_challenge_content_hash(tmp_path: Path) -> None:
    from dataclasses import replace

    from core.persona.challenge_queue import PersonaChallengeQueueConsumer
    from tests.unit.test_persona_challenge_queue_p5 import (
        _config,
        _insert_persona_revision,
        _seal_decision,
    )

    _insert_persona_revision(
        tmp_path / "user_signals.db",
        revision_id="persona-revision:1:challenge",
        version=1,
    )
    asset_store = _asset_store(tmp_path)
    _persist_asset(
        asset_store,
        scope_type="project",
        scope_id="mnemos",
        principal_id="mcp:codex:material-sink-test",
    )
    _seal_decision(tmp_path)
    manager = _manager(asset_store)
    original = manager.analyze_and_update

    def changed(*args, **kwargs):
        challenges = original(*args, **kwargs)
        return [
            replace(
                challenges[0],
                challenge_content="Content changed after the canonical binding.",
            )
        ]

    manager.analyze_and_update = changed  # type: ignore[method-assign]
    result = PersonaChallengeQueueConsumer(
        _config(tmp_path),
        manager_factory=lambda _store: manager,
    ).run_once()

    assert result["status"] == "intentional_skip"
    assert result["reason"] == "challenge_content_or_revision_hash_changed"
    assert result["challenges"] == 0


def test_queue_rejects_asset_revision_that_became_stale_before_delivery(
    tmp_path: Path,
) -> None:
    from core.persona.challenge_queue import PersonaChallengeQueueConsumer
    from tests.unit.test_persona_challenge_queue_p5 import (
        _config,
        _insert_persona_revision,
        _seal_decision,
    )

    _insert_persona_revision(
        tmp_path / "user_signals.db",
        revision_id="persona-revision:1:challenge",
        version=1,
    )
    asset_store = _asset_store(tmp_path)
    asset = _persist_asset(
        asset_store,
        scope_type="project",
        scope_id="mnemos",
        principal_id="mcp:codex:material-sink-test",
    )
    _seal_decision(tmp_path)
    manager = _manager(asset_store)
    original = manager.analyze_and_update
    catalog, evidence = _authority()

    def stale(*args, **kwargs):
        challenges = original(*args, **kwargs)
        asset_store.transition_blindspot(
            asset.asset_id,
            expected_revision_id=asset.revision_id,
            next_status="dismissed",
            evidence=(evidence,),
            catalog=catalog,
        )
        return challenges

    manager.analyze_and_update = stale  # type: ignore[method-assign]
    result = PersonaChallengeQueueConsumer(
        _config(tmp_path),
        manager_factory=lambda _store: manager,
    ).run_once()

    assert result["status"] == "intentional_skip"
    assert result["reason"] == "stale_canonical_blindspot_revision"
    assert result["challenges"] == 0
