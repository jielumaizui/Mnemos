from __future__ import annotations

from pathlib import Path

import pytest

from core.access_policy import PrincipalEnvelope
from core.cognitive.state_store import CognitiveStateStore
from core.persona.challenge_feedback import record_persona_challenge_feedback
from core.persona.challenge_queue import (
    PERSONA_CHALLENGE_CONSUMER,
    PersonaChallengeQueueConsumer,
)
from tests.unit.test_persona_challenge_canonical_p5 import (
    _asset_store,
    _authority,
    _manager,
    _persist_asset,
)
from tests.unit.test_persona_challenge_queue_p5 import (
    _config,
    _insert_persona_revision,
    _seal_decision,
)


def _principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="mcp:codex:material-sink-test",
        agent="codex",
        host_kind="test",
        capability_id="material-sink-test",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def _pending_delivery(tmp_path: Path):
    _insert_persona_revision(
        tmp_path / "user_signals.db",
        revision_id="persona-revision:1:challenge",
        version=1,
    )
    assets = _asset_store(tmp_path)
    asset = _persist_asset(
        assets,
        scope_type="project",
        scope_id="mnemos",
        principal_id=_principal().principal_id,
    )
    _seal_decision(tmp_path)
    consumer = PersonaChallengeQueueConsumer(
        _config(tmp_path),
        manager_factory=lambda _store: _manager(assets),
    )
    result = consumer.run_once()
    command = CognitiveStateStore(_config(tmp_path)).pending_commands(
        PERSONA_CHALLENGE_CONSUMER
    )[0]
    return assets, asset, consumer, command, result


def test_apollon_session_text_never_infers_challenge_feedback(monkeypatch) -> None:
    import core.persona.hamartia as hamartia
    from integrations.apollon import _analyze_blindspot_feedback

    class _ForbiddenManager:
        def __init__(self):
            raise AssertionError("keyword feedback path must not instantiate Persona manager")

    monkeypatch.setattr(hamartia, "BlindSpotProfileManager", _ForbiddenManager)

    result = _analyze_blindspot_feedback(
        [
            {"role": "user", "content": "你说得对，今天的天气确实很好。"},
            {"role": "assistant", "content": "这是与 challenge 无关的普通对话。"},
        ]
    )

    assert result == {
        "status": "noop",
        "reason": "exact_delivery_feedback_required",
        "recorded": 0,
    }


def test_feedback_requires_exact_presentation_ack_and_delivery_id(tmp_path: Path) -> None:
    _assets, _asset, _consumer, _command, result = _pending_delivery(tmp_path)
    assert result["status"] == "awaiting_presentation"

    with pytest.raises(ValueError, match="presentation"):
        record_persona_challenge_feedback(
            database_dir=tmp_path,
            delivery_id=result["delivery_ids"][0],
            presentation_receipt_hash="sha256:" + "f" * 64,
            reaction="accepted",
            principal=_principal(),
            observed_at="2026-07-23T01:00:00+00:00",
        )


def test_accepted_reaction_is_idempotent_telemetry_not_validation(tmp_path: Path) -> None:
    assets, asset, consumer, command, result = _pending_delivery(tmp_path)
    presentation = consumer.record_presentation(
        command_id=command["command_id"],
        delivery_ids=result["delivery_ids"],
        host_agent="codex",
        rendered_content_hash=result["rendered_content_hash"],
    )
    replayed_presentation = consumer.record_presentation(
        command_id=command["command_id"],
        delivery_ids=result["delivery_ids"],
        host_agent="codex",
        rendered_content_hash=result["rendered_content_hash"],
    )
    assert replayed_presentation == presentation
    with pytest.raises(ValueError, match="immutable"):
        consumer.record_presentation(
            command_id=command["command_id"],
            delivery_ids=result["delivery_ids"],
            host_agent="codex",
            rendered_content_hash="sha256:" + "f" * 64,
        )

    first = record_persona_challenge_feedback(
        database_dir=tmp_path,
        delivery_id=result["delivery_ids"][0],
        presentation_receipt_hash=presentation["receipt_hash"],
        reaction="accepted",
        principal=_principal(),
        observed_at=presentation["presented_at"],
    )
    replay = record_persona_challenge_feedback(
        database_dir=tmp_path,
        delivery_id=result["delivery_ids"][0],
        presentation_receipt_hash=presentation["receipt_hash"],
        reaction="accepted",
        principal=_principal(),
        observed_at=presentation["presented_at"],
    )

    assert first["reaction_revision_id"] == replay["reaction_revision_id"]
    assert first["delivery_id"] == result["delivery_ids"][0]
    assert assets.current_blindspot(asset.asset_id).status == "suspected"
    reactions = CognitiveStateStore(_config(tmp_path)).current_revisions(
        object_type="user_reaction_event"
    )
    assert len(reactions) == 1
    assert reactions[0].payload["delivery_ref"]["event_id"] == result["delivery_ids"][0]
    from scripts.audit_persona_challenge_feedback import (
        audit_persona_challenge_feedback,
    )

    audit = audit_persona_challenge_feedback(
        tmp_path / "producer_consumer_ledger.db"
    )
    assert audit["ok"] is True
    assert audit["feedback_without_delivery_ref"] == 0
    assert audit["keyword_inferred_feedback"] == 0
    assert audit["accepted_as_validated"] == 0
    assert audit["swallowed_feedback_persistence_error"] == 0


@pytest.mark.parametrize(
    ("outcome", "reaction", "expected_status"),
    (
        ("validated", "accepted", "confirmed"),
        ("invalidated", "rejected", "dismissed"),
    ),
)
def test_explicit_outcome_transitions_exact_asset_revision(
    tmp_path: Path,
    outcome: str,
    reaction: str,
    expected_status: str,
) -> None:
    assets, asset, consumer, command, result = _pending_delivery(tmp_path)
    presentation = consumer.record_presentation(
        command_id=command["command_id"],
        delivery_ids=result["delivery_ids"],
        host_agent="codex",
        rendered_content_hash=result["rendered_content_hash"],
    )
    catalog, evidence = _authority()

    reaction_receipt = record_persona_challenge_feedback(
        database_dir=tmp_path,
        delivery_id=result["delivery_ids"][0],
        presentation_receipt_hash=presentation["receipt_hash"],
        reaction=reaction,
        principal=_principal(),
        observed_at=presentation["presented_at"],
    )
    receipt = record_persona_challenge_feedback(
        database_dir=tmp_path,
        delivery_id=result["delivery_ids"][0],
        presentation_receipt_hash=presentation["receipt_hash"],
        reaction=reaction,
        principal=_principal(),
        observed_at=presentation["presented_at"],
        outcome=outcome,
        outcome_evidence=(evidence,),
        source_authority_catalog=catalog,
    )

    current = assets.current_blindspot(asset.asset_id)
    assert current.status == expected_status
    assert current.supersedes_revision_id == asset.revision_id
    assert receipt["asset_transition"]["status"] == expected_status
    assert receipt["asset_transition"]["revision_id"] == current.revision_id
    assert receipt["reaction_revision_id"] == reaction_receipt["reaction_revision_id"]


def test_feedback_persistence_error_is_not_swallowed(tmp_path: Path, monkeypatch) -> None:
    _assets, _asset, consumer, command, result = _pending_delivery(tmp_path)
    presentation = consumer.record_presentation(
        command_id=command["command_id"],
        delivery_ids=result["delivery_ids"],
        host_agent="codex",
        rendered_content_hash=result["rendered_content_hash"],
    )
    catalog, evidence = _authority()

    def fail_transition(*_args, **_kwargs):
        raise OSError("durable asset write failed")

    monkeypatch.setattr(
        "core.cognitive.user_model_asset_store.UserCognitiveBlindspotStore.transition_blindspot",
        fail_transition,
    )

    with pytest.raises(OSError, match="durable asset write failed"):
        record_persona_challenge_feedback(
            database_dir=tmp_path,
            delivery_id=result["delivery_ids"][0],
            presentation_receipt_hash=presentation["receipt_hash"],
            reaction="accepted",
            principal=_principal(),
            observed_at=presentation["presented_at"],
            outcome="validated",
            outcome_evidence=(evidence,),
            source_authority_catalog=catalog,
        )


def test_multiple_same_session_challenges_require_exact_delivery_identity(
    tmp_path: Path,
) -> None:
    _insert_persona_revision(
        tmp_path / "user_signals.db",
        revision_id="persona-revision:1:challenge",
        version=1,
    )
    assets = _asset_store(tmp_path)
    _persist_asset(
        assets,
        scope_type="project",
        scope_id="mnemos",
        principal_id=_principal().principal_id,
        admission_key="framing",
    )
    _persist_asset(
        assets,
        scope_type="project",
        scope_id="mnemos",
        principal_id=_principal().principal_id,
        blindspot_type="option_coverage",
        description="The decision omits a materially different option.",
        admission_key="option-coverage",
    )
    _seal_decision(tmp_path)
    consumer = PersonaChallengeQueueConsumer(
        _config(tmp_path),
        manager_factory=lambda _store: _manager(assets),
    )
    result = consumer.run_once()
    state = CognitiveStateStore(_config(tmp_path))
    command = state.pending_commands(PERSONA_CHALLENGE_CONSUMER)[0]
    assert len(result["delivery_ids"]) == 2
    presentation = consumer.record_presentation(
        command_id=command["command_id"],
        delivery_ids=result["delivery_ids"],
        host_agent="codex",
        rendered_content_hash=result["rendered_content_hash"],
    )

    selected = result["delivery_ids"][0]
    receipt = record_persona_challenge_feedback(
        database_dir=tmp_path,
        delivery_id=selected,
        presentation_receipt_hash=presentation["receipt_hash"],
        reaction="accepted",
        principal=_principal(),
        observed_at=presentation["presented_at"],
    )

    assert receipt["delivery_id"] == selected
    reactions = state.current_revisions(object_type="user_reaction_event")
    assert len(reactions) == 1
    assert reactions[0].payload["subject_ref"] == {
        "type": "persona_challenge",
        "id": selected,
    }
    with pytest.raises(ValueError, match="presentation"):
        record_persona_challenge_feedback(
            database_dir=tmp_path,
            delivery_id="persona-challenge-delivery-" + "0" * 32,
            presentation_receipt_hash=presentation["receipt_hash"],
            reaction="accepted",
            principal=_principal(),
            observed_at=presentation["presented_at"],
        )
