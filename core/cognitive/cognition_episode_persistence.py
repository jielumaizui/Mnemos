"""Atomic persistence seam for admitted distillation cognition episodes."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import sqlite3
from typing import Any, Mapping

from core.cognition_episode_contract import (
    COGNITION_EPISODE_FIELDS,
    COGNITION_EPISODE_SCHEMA_VERSION,
)
from core.cognitive.state_contract import (
    CognitiveStateRevision,
    LocalConsumerCommand,
    now_utc,
    sha256_json,
)
from core.cognitive.state_store import CognitiveStateStore
from core.hephaestus.distillation_contract import validate_admitted_extraction_root
from core.ops.cognitive_data_contract import CognitiveDataEvent

COGNITION_EPISODE_CONSUMERS = ("wiki", "knowledge_graph", "cognitive_graph")


def _stable_id(prefix: str, value: Any) -> str:
    return prefix + "-" + sha256_json(value).split(":", 1)[1][:32]


@dataclass(frozen=True)
class CognitionEpisodeCommitReceipt:
    status: str
    event_id: str
    revision_id: str
    object_id: str
    outbox_ids: tuple[str, ...]
    consumer_ids: tuple[str, ...]
    transaction_hash: str
    payload_hash: str
    redaction_counts: tuple[tuple[str, int], ...]


def validate_cognition_episode_route_binding(result: Any, database_dir: Any) -> str:
    """Return an error when a route tries to reuse an unrelated/stale revision."""

    revision_id = str(result.cognition_episode_revision_id or "")
    if not revision_id:
        return "action routing requires a committed canonical cognition episode revision"
    store = CognitiveStateStore(database_dir)
    try:
        revision = store.revision(revision_id)
    except (FileNotFoundError, OSError, RuntimeError, ValueError, sqlite3.Error):
        return "canonical cognition episode store is unavailable or invalid"
    if revision is None or revision.object_type != "cognition_episode":
        return "action routing cognition episode revision is not committed canonically"

    expected_payload = {
        "input_spec_hash": result.input_spec.input_spec_hash,
        "extraction_output_hash": result.extraction_output_hash,
        "source_agent": result.input_spec.source_agent,
        "source_session_id": result.input_spec.source_session_id,
        "source_event_ids": list(result.input_spec.source_event_ids),
        "raw_completeness": result.input_spec.raw_completeness,
        "cognition_context_hash": result.input_spec.cognition_context.context_hash,
    }
    if (
        revision.admission_state != "active"
        or revision.scope_type != "session"
        or revision.scope_id != result.input_spec.source_session_id
        or revision.source_content_hash != result.extraction_output_hash
        or any(revision.payload.get(key) != value for key, value in expected_payload.items())
    ):
        return (
            "canonical cognition episode revision is not bound to this "
            "admitted distillation root"
        )
    try:
        current = store.current_revision("cognition_episode", revision.object_id)
    except (FileNotFoundError, OSError, RuntimeError, ValueError, sqlite3.Error):
        return "canonical cognition episode current-state projection is unavailable or invalid"
    if current is None or current.revision_id != revision_id:
        return (
            "action routing cognition episode revision is no longer the "
            "canonical current revision"
        )
    return ""


def _episode_entries(episode: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    canonical: dict[str, list[dict[str, Any]]] = {}
    for field_name in COGNITION_EPISODE_FIELDS:
        values = episode.get(field_name)
        if not isinstance(values, list):
            raise ValueError(f"cognition_episode.{field_name} must be a list")
        entries: list[dict[str, Any]] = []
        for value in values:
            if not isinstance(value, Mapping):
                raise ValueError(f"cognition_episode.{field_name} entry must be an object")
            entry = deepcopy(dict(value))
            entry["entry_id"] = _stable_id(
                "cogentry",
                {"field": field_name, "entry": entry},
            )
            entries.append(entry)
        canonical[field_name] = entries
    return canonical


def _evidence_refs(episode: Mapping[str, list[dict[str, Any]]]) -> tuple[str, ...]:
    refs: list[str] = []
    for field_name in COGNITION_EPISODE_FIELDS:
        for entry in episode[field_name]:
            evidence = entry.get("evidence_refs")
            if not isinstance(evidence, list):
                continue
            for item in evidence:
                if not isinstance(item, Mapping):
                    continue
                for key in ("source_event_id", "source_authority_id", "artifact_ref_id"):
                    value = str(item.get(key) or "")
                    if value and value not in refs:
                        refs.append(value)
    if not refs:
        raise ValueError("cognition episode has no canonical evidence references")
    return tuple(refs)


def _canonical_payload(result: Any) -> dict[str, Any]:
    structured = result.structured_output
    if not isinstance(structured, Mapping):
        raise ValueError("structured_output is required")
    episode = structured.get("cognition_episode")
    if not isinstance(episode, Mapping):
        raise ValueError("cognition_episode is required")
    input_spec = result.input_spec
    context = getattr(input_spec, "cognition_context", None)
    if context is None:
        raise ValueError("cognition extraction context is required")
    semantic = _episode_entries(episode)
    claims = structured.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ValueError("claims are required")
    canonical_claims = deepcopy(claims)
    behavior_intent = structured.get("user_behavior_intent")
    if not isinstance(behavior_intent, Mapping):
        raise ValueError("user_behavior_intent is required")
    return {
        "schema_version": COGNITION_EPISODE_SCHEMA_VERSION,
        "cognition_context_hash": context.context_hash,
        "input_spec_hash": input_spec.input_spec_hash,
        "extraction_output_hash": str(result.extraction_output_hash),
        "source_agent": input_spec.source_agent,
        "source_session_id": input_spec.source_session_id,
        "source_event_ids": list(input_spec.source_event_ids),
        "raw_completeness": input_spec.raw_completeness,
        "loss_contract": context.loss_contract,
        "source_spans": [dict(value) for value in context.source_spans],
        "artifact_catalog_hash": context.artifact_catalog_hash,
        "source_authority_catalog_hash": context.source_authority_catalog_hash,
        "source_authority_catalog": input_spec.source_authority_catalog.canonical_payload(),
        "artifact_catalog": input_spec.artifact_catalog.canonical_payload(),
        "acl": context.acl,
        "access_control": context.access_control,
        "purpose": context.purpose,
        "retention_policy": context.retention_policy,
        "claims": canonical_claims,
        "claim_catalog_hash": sha256_json(canonical_claims),
        "user_behavior_intent": deepcopy(dict(behavior_intent)),
        **semantic,
    }


def _same_semantic_revision(
    current: CognitiveStateRevision | None,
    probe: CognitiveStateRevision,
) -> bool:
    return bool(
        current is not None
        and current.payload_hash == probe.payload_hash
        and current.evidence_hash == probe.evidence_hash
        and current.source_event_id == probe.source_event_id
        and current.source_revision_id == probe.source_revision_id
        and current.source_content_hash == probe.source_content_hash
        and current.scope_type == probe.scope_type
        and current.scope_id == probe.scope_id
    )


def commit_cognition_episode(result: Any, config: Any) -> CognitionEpisodeCommitReceipt:
    """Commit one admitted episode plus envelope/outbox in one SQLite transaction.

    The function never initializes or reconciles schema.  A missing or stale
    canonical store fails before any Wiki/action sink can run.
    """

    admission = validate_admitted_extraction_root(
        input_spec=result.input_spec,
        structured_output=result.structured_output,
        extraction_contract_valid=result.extraction_contract_valid,
        extraction_output=result.extraction_output,
        extraction_output_hash=str(result.extraction_output_hash),
        extraction_judgment=str(result.extraction_judgment or result.judgment),
    )
    if not admission.valid or admission.is_skip:
        raise ValueError("cognition episode root admission failed: " + admission.error_text)

    payload = _canonical_payload(result)
    episode = {field: payload[field] for field in COGNITION_EPISODE_FIELDS}
    evidence_refs = _evidence_refs(episode)
    input_spec = result.input_spec
    object_id = _stable_id(
        "cogepisode",
        {
            "source_agent": input_spec.source_agent,
            "source_session_id": input_spec.source_session_id,
        },
    )
    event_id = _stable_id(
        "cogevent",
        {
            "object_id": object_id,
            "input_spec_hash": input_spec.input_spec_hash,
            "extraction_output_hash": result.extraction_output_hash,
        },
    )
    source_revision_id = (
        input_spec.source_event_ids[0]
        if len(input_spec.source_event_ids) == 1
        else "distill-input:" + input_spec.input_spec_hash.removeprefix("sha256:")
    )
    store = CognitiveStateStore(config)
    current = store.current_revision("cognition_episode", object_id)
    probe = CognitiveStateRevision.create(
        object_type="cognition_episode",
        object_id=object_id,
        source_event_id=event_id,
        source_revision_id=source_revision_id,
        source_content_hash=str(result.extraction_output_hash),
        scope_type="session",
        scope_id=input_spec.source_session_id,
        evidence_refs=evidence_refs,
        payload=payload,
        created_at=now_utc(),
    )
    if _same_semantic_revision(current, probe):
        assert current is not None
        revision = current
    else:
        revision = CognitiveStateRevision.create(
            object_type="cognition_episode",
            object_id=object_id,
            source_event_id=event_id,
            source_revision_id=source_revision_id,
            source_content_hash=str(result.extraction_output_hash),
            scope_type="session",
            scope_id=input_spec.source_session_id,
            evidence_refs=evidence_refs,
            payload=payload,
            supersedes_revision_id=current.revision_id if current is not None else "",
            created_at=now_utc(),
        )

    event = CognitiveDataEvent(
        event_id=event_id,
        source_id=input_spec.source_agent,
        asset_id=input_spec.input_spec_hash,
        source_kind="distillation_extraction",
        source_uri="mnemos://distillation/" + object_id,
        content_hash=str(result.extraction_output_hash),
        canonical_subject=f"cognition_episode:{object_id}",
        data_type="cognition_episode",
        producer="cognitive_state_store",
        intended_consumers=COGNITION_EPISODE_CONSUMERS,
        privacy_level="private",
        confidence=1.0,
        evidence_refs=evidence_refs,
        dedupe_key=f"cognitive-state:{event_id}",
        created_at=revision.created_at,
        retention_policy=input_spec.cognition_context.retention_policy,
        metadata={
            "revision_ids": [revision.revision_id],
            "contract_version": COGNITION_EPISODE_SCHEMA_VERSION,
        },
    )
    commands = tuple(
        LocalConsumerCommand.create(
            revision_id=revision.revision_id,
            consumer_id=consumer_id,
            command_type="project_cognition_episode",
            payload={
                "primary_revision_id": revision.revision_id,
                "object_type": "cognition_episode",
                "object_id": object_id,
            },
            created_at=revision.created_at,
        )
        for consumer_id in COGNITION_EPISODE_CONSUMERS
    )
    committed = store.unit_of_work().commit(
        revisions=(revision,),
        event=event,
        commands=commands,
    )
    receipt = CognitionEpisodeCommitReceipt(
        status=committed.status,
        event_id=committed.event_id,
        revision_id=revision.revision_id,
        object_id=object_id,
        outbox_ids=committed.outbox_ids,
        consumer_ids=COGNITION_EPISODE_CONSUMERS,
        transaction_hash=committed.transaction_hash,
        payload_hash=revision.payload_hash,
        redaction_counts=revision.redaction_counts,
    )
    result.cognition_episode_revision_id = revision.revision_id
    result.cognition_episode_receipt = receipt
    return receipt
