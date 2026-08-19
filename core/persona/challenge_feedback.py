"""Exact presentation-bound feedback for canonical Persona challenges."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
from typing import Any, Mapping, Sequence

from core.access_policy import PrincipalEnvelope
from core.cognitive.access_control import (
    make_cognitive_access_envelope,
    validate_cognitive_access_envelope,
)
from core.cognitive.feedback_attribution import FeedbackAttributionStore, UserReactionInput
from core.cognitive.state_contract import sha256_json
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.user_model_asset_store import UserCognitiveBlindspotStore
from core.cognitive.user_model_assets import CognitiveAuthorityEvidence
from core.evidence.source_authority import SourceAuthorityCatalog
from core.persona.challenge_queue import (
    PERSONA_CHALLENGE_COMMAND,
    PERSONA_CHALLENGE_CONSUMER,
)


_REACTION_KINDS = {
    "accepted": "accept",
    "ignored": "ignore",
    "rejected": "dismiss",
}
_OUTCOME_STATUS = {
    "validated": "confirmed",
    "invalidated": "dismissed",
}


def record_persona_challenge_feedback(
    *,
    database_dir: Path,
    delivery_id: str,
    presentation_receipt_hash: str,
    reaction: str,
    principal: PrincipalEnvelope,
    observed_at: str,
    outcome: str = "",
    outcome_evidence: Sequence[CognitiveAuthorityEvidence] = (),
    source_authority_catalog: SourceAuthorityCatalog | None = None,
) -> dict[str, Any]:
    """Record one exact reaction and optional independently evidenced asset outcome."""

    root = Path(database_dir).expanduser()
    normalized_delivery_id = str(delivery_id or "").strip()
    normalized_presentation_hash = str(presentation_receipt_hash or "").strip()
    normalized_reaction = str(reaction or "").strip().lower()
    normalized_outcome = str(outcome or "").strip().lower()
    normalized_observed_at = str(observed_at or "").strip()
    if (
        not normalized_delivery_id
        or not _is_sha256(normalized_presentation_hash)
        or normalized_reaction not in _REACTION_KINDS
        or not normalized_observed_at
        or normalized_outcome not in {"", *_OUTCOME_STATUS}
    ):
        raise ValueError("Persona challenge feedback input is invalid")
    if not isinstance(principal, PrincipalEnvelope):
        raise TypeError("Persona challenge feedback requires a typed principal")
    if normalized_outcome and (
        not outcome_evidence or source_authority_catalog is None
    ):
        raise ValueError("Persona challenge outcome requires exact authority evidence")
    binding = _presented_delivery_binding(
        root / "producer_consumer_ledger.db",
        delivery_id=normalized_delivery_id,
        presentation_receipt_hash=normalized_presentation_hash,
    )
    delivery = binding["delivery"]
    presentation = binding["presentation"]
    try:
        observed_timestamp = datetime.fromisoformat(
            normalized_observed_at.replace("Z", "+00:00")
        )
        presented_timestamp = datetime.fromisoformat(
            str(presentation["presented_at"]).replace("Z", "+00:00")
        )
        if observed_timestamp.tzinfo is None:
            observed_timestamp = observed_timestamp.replace(tzinfo=timezone.utc)
        if presented_timestamp.tzinfo is None:
            presented_timestamp = presented_timestamp.replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Persona challenge feedback timestamp is invalid") from exc
    if observed_timestamp < presented_timestamp:
        raise ValueError("Persona challenge feedback predates presentation")
    principal_ref = delivery["principal"]
    if (
        str(principal_ref["principal_id"]) != principal.principal_id
        or str(principal_ref["agent"]).lower() != principal.agent.lower()
    ):
        raise PermissionError("Persona challenge feedback principal mismatch")
    state = CognitiveStateStore(root / "producer_consumer_ledger.db")
    decision_ref = delivery["decision_trace"]
    decision = state.revision(str(decision_ref["revision_id"]))
    if (
        decision is None
        or decision.object_type != "decision_trace"
        or decision.object_id != str(decision_ref["decision_id"])
        or decision.payload_hash != str(decision_ref["content_hash"])
    ):
        raise ValueError("Persona challenge feedback DecisionTrace is unavailable")
    decision_access = validate_cognitive_access_envelope(
        decision.payload["access_control"],
        expected_scope_type=decision.scope_type,
        expected_scope_id=decision.scope_id,
    )
    source_snapshot = {
        "schema_version": "mnemos.persona_challenge_feedback_input.v1",
        "delivery_id": normalized_delivery_id,
        "delivery_hash": sha256_json(delivery),
        "presentation_receipt_hash": normalized_presentation_hash,
        "rendered_content_hash": str(presentation["rendered_content_hash"]),
        "reaction": normalized_reaction,
        "principal_id": principal.principal_id,
        "principal_agent": principal.agent,
        "observed_at": normalized_observed_at,
    }
    source_hash = sha256_json(source_snapshot)
    source_identity = source_hash.split(":", 1)[1][:32]
    access = make_cognitive_access_envelope(
        owner_principal_id=principal.principal_id,
        owner_agent=principal.agent,
        scope_type=decision.scope_type,
        scope_id=decision.scope_id,
        session_id=str(decision_access["scope"]["session_id"]),
        project=str(decision_access["scope"]["project"]),
        purposes=("cognitive_state_read", "cognitive_state_write"),
        consent_provenance_refs=(
            f"challenge-delivery:{normalized_delivery_id}",
            f"challenge-presentation:{normalized_presentation_hash}",
            f"feedback-input:{source_hash}",
        ),
        sensitivity=str(decision_access["sensitivity"]),
        retention_policy="feedback_attribution",
        source_acl_lineage=(
            *tuple(str(value) for value in decision_access["source_acl_lineage"]),
            source_hash,
        ),
        visibility="private",
    )
    reaction_input = UserReactionInput(
        source_event_id=f"persona-challenge-feedback-{source_identity}",
        source_revision_id=f"persona-challenge-feedback:{source_identity}",
        source_content_hash=source_hash,
        observed_at=normalized_observed_at,
        scope_type=decision.scope_type,
        scope_id=decision.scope_id,
        source_channel="delivery_feedback",
        subject_ref={"type": "persona_challenge", "id": normalized_delivery_id},
        kind=_REACTION_KINDS[normalized_reaction],
        evidence_refs=(
            f"challenge-delivery:{normalized_delivery_id}",
            f"challenge-presentation:{normalized_presentation_hash}",
            f"blindspot-revision:{delivery['asset_revision']['revision_id']}",
        ),
        evidence_content_hashes=(
            sha256_json(delivery),
            str(presentation["rendered_content_hash"]),
            str(delivery["asset_revision"]["content_hash"]),
        ),
        access_control=access,
        delivery_ref={
            "state": "available",
            "event_id": normalized_delivery_id,
            "event_payload_hash": sha256_json(delivery),
            "unavailable_reason": "",
        },
        display_ref={
            "state": "available",
            "display_id": normalized_presentation_hash,
            "content_hash": str(presentation["rendered_content_hash"]),
            "unavailable_reason": "",
        },
        decision_ref={
            "state": "available",
            "id": decision.object_id,
            "revision_id": decision.revision_id,
            "content_hash": decision.payload_hash,
            "unavailable_reason": "",
        },
        prediction_ref=_unavailable_entity_ref(),
        action_ref=_unavailable_entity_ref(),
        exposure_id=normalized_delivery_id,
        interface_id="persona-challenge-card",
    )
    owner = FeedbackAttributionStore(state, target_adapters={})
    reaction_receipt = owner.record_reaction(reaction_input, principal)
    terminal_receipts = [
        owner.process_command(command_id).to_dict()
        for command_id in reaction_receipt.command_ids
    ]
    asset_transition: dict[str, Any] = {}
    if normalized_outcome:
        assert source_authority_catalog is not None
        asset_transition = _apply_asset_outcome(
            root,
            delivery,
            next_status=_OUTCOME_STATUS[normalized_outcome],
            outcome=normalized_outcome,
            evidence=outcome_evidence,
            catalog=source_authority_catalog,
            reaction=normalized_reaction,
            observed_at=normalized_observed_at,
            presentation_receipt_hash=normalized_presentation_hash,
            feedback_event_id=reaction_receipt.event_id,
            reaction_revision_id=reaction_receipt.reaction_revision_id,
        )
    return {
        "success": True,
        "delivery_id": normalized_delivery_id,
        "presentation_receipt_hash": normalized_presentation_hash,
        "reaction": normalized_reaction,
        "outcome": normalized_outcome,
        "feedback_event_id": reaction_receipt.event_id,
        "reaction_id": reaction_receipt.reaction_id,
        "reaction_revision_id": reaction_receipt.reaction_revision_id,
        "attribution_id": reaction_receipt.attribution_id,
        "attribution_revision_id": reaction_receipt.attribution_revision_id,
        "terminal_receipts": terminal_receipts,
        "asset_transition": asset_transition,
    }


def _presented_delivery_binding(
    state_db: Path,
    *,
    delivery_id: str,
    presentation_receipt_hash: str,
) -> dict[str, Any]:
    if not state_db.is_file():
        raise ValueError("Persona challenge presentation is unavailable")
    matches: list[dict[str, Any]] = []
    with sqlite3.connect(f"file:{state_db.resolve(strict=True)}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            """
            SELECT consumption.outcome
            FROM cognitive_state_outbox AS command
            JOIN cognitive_state_effect_receipts AS receipt
              ON receipt.command_id=command.command_id
            JOIN cognitive_data_consumptions AS consumption
              ON consumption.consumption_id=receipt.consumption_id
            WHERE command.consumer_id=?
              AND command.command_type=?
              AND receipt.status='committed'
            """,
            (PERSONA_CHALLENGE_CONSUMER, PERSONA_CHALLENGE_COMMAND),
        ).fetchall()
    for row in rows:
        try:
            outcome = json.loads(str(row[0] or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        presentation = outcome.get("presentation_receipt")
        if (
            outcome.get("disposition") != "presented"
            or not isinstance(presentation, Mapping)
            or presentation.get("receipt_hash") != presentation_receipt_hash
            or delivery_id not in presentation.get("delivery_ids", ())
        ):
            continue
        for delivery in outcome.get("delivery_commands") or ():
            if isinstance(delivery, Mapping) and delivery.get("delivery_id") == delivery_id:
                matches.append(
                    {
                        "delivery": dict(delivery),
                        "presentation": dict(presentation),
                    }
                )
    if not matches:
        raise ValueError("Persona challenge presentation is unavailable")
    if len(matches) != 1:
        raise RuntimeError("Persona challenge delivery maps to multiple presentations")
    return matches[0]


def _apply_asset_outcome(
    root: Path,
    delivery: Mapping[str, Any],
    *,
    next_status: str,
    outcome: str,
    evidence: Sequence[CognitiveAuthorityEvidence],
    catalog: SourceAuthorityCatalog,
    reaction: str,
    observed_at: str,
    presentation_receipt_hash: str,
    feedback_event_id: str,
    reaction_revision_id: str,
) -> dict[str, Any]:
    asset_ref = delivery["asset_revision"]
    store = UserCognitiveBlindspotStore(root / "user_cognitive_blindspots.db")
    current = store.current_blindspot(str(asset_ref["asset_id"]))
    if current is None:
        raise ValueError("Persona challenge asset is unavailable")
    expected_revision_id = str(asset_ref["revision_id"])
    if current.revision_id == expected_revision_id:
        transitioned = store.transition_blindspot(
            current.asset_id,
            expected_revision_id=expected_revision_id,
            next_status=next_status,
            evidence=evidence,
            catalog=catalog,
            payload_updates={
                "challenge_count": current.challenge_count + 1,
                "last_challenged": observed_at,
                "user_reaction": reaction,
                "challenge_outcome": outcome,
                "challenge_delivery_id": str(delivery["delivery_id"]),
                "challenge_presentation_receipt_hash": presentation_receipt_hash,
                "challenge_feedback_event_id": feedback_event_id,
                "challenge_reaction_revision_id": reaction_revision_id,
            },
        )
        return {
            "status": transitioned.status,
            "asset_id": transitioned.asset_id,
            "revision_id": transitioned.revision_id,
            "supersedes_revision_id": transitioned.supersedes_revision_id,
        }
    if (
        current.supersedes_revision_id == expected_revision_id
        and current.status == next_status
    ):
        return {
            "status": current.status,
            "asset_id": current.asset_id,
            "revision_id": current.revision_id,
            "supersedes_revision_id": current.supersedes_revision_id,
        }
    raise ValueError("Persona challenge feedback asset revision is stale")


def _unavailable_entity_ref() -> dict[str, str]:
    return {
        "state": "unavailable",
        "id": "",
        "revision_id": "",
        "content_hash": "",
        "unavailable_reason": "not_applicable",
    }


def _is_sha256(value: str) -> bool:
    normalized = str(value or "")
    return (
        normalized.startswith("sha256:")
        and len(normalized) == 71
        and all(char in "0123456789abcdef" for char in normalized[7:])
    )
