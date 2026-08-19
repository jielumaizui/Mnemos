"""Single durable dispatch owner for committed cognition-episode projections.

The canonical episode remains in ``CognitiveStateStore``.  This module publishes
one content-free, versioned EventBus envelope and gives each existing local
outbox consumer a deterministic projection.  Target effects are independently
observable; a crash between target commit and state receipt is repaired by
replaying only the still-pending consumer command.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from core.cognition_episode_contract import COGNITION_EPISODE_FIELDS
from core.cognitive.access_control import cognitive_access_hash
from core.cognitive.cognition_episode_projection_receipt import (
    CognitionEpisodeProjectionProof,
    projection_before_hash,
    projection_effect_id,
)
from core.cognitive.cognition_episode_projection_schema import (
    initialize_fresh_projection_targets,
    validate_cognition_episode_projection_schema,
)
from core.cognitive.state_contract import CognitiveStateRevision, sha256_json
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive_graph.store import CognitiveGraphStore
from core.cognitive_graph.updater import CognitiveGraphUpdater
from core.evidence.evidence_graph import EvidenceGraph
from core.frontmatter import fm_get, parse_frontmatter
from core.mnemos_bus import Event, EventBus, HandlerOutcome
from core.utils import read_text_value
from core.wiki_projection_lifecycle import (
    WikiProjectionLedger,
    resolve_wiki_projection_db_path,
)

EVENT_TYPE = "cognition_episode_committed"
EVENT_SCHEMA_VERSION = "mnemos.cognition_episode_committed.v1"
COMMAND_TYPE = "project_cognition_episode"
CONSUMERS = ("wiki", "knowledge_graph", "cognitive_graph")

_FIELD_NODE_TYPES = {
    "assumptions": "belief",
    "hypotheses": "prediction",
    "decision": "decision",
    "actions": "action",
    "outcomes": "outcome",
}


def _stable_id(prefix: str, value: Any) -> str:
    return prefix + "-" + str(sha256_json(value)).split(":", 1)[1][:32]


def _node_type(field_name: str) -> str:
    return _FIELD_NODE_TYPES.get(field_name, "claim")


def _source_span_identity(span: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(span.get("source_authority_id") or ""),
        str(span.get("revision_id") or ""),
        str(span.get("role") or ""),
        int(span.get("span_start", -1)),
        int(span.get("span_end", -1)),
        str(span.get("span_status") or ""),
        str(span.get("content_sha256") or ""),
        str(span.get("source_revision_sha256") or ""),
    )


def _evidence_span_identity(evidence: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(evidence.get("source_authority_id") or ""),
        str(evidence.get("source_event_id") or ""),
        str(evidence.get("authority_role") or ""),
        int(evidence.get("authority_span_start", -1)),
        int(evidence.get("authority_span_end", -1)),
        str(evidence.get("authority_span_status") or ""),
        str(evidence.get("authority_content_sha256") or ""),
        str(evidence.get("authority_source_revision_sha256") or ""),
    )


def _dedupe_dicts(values: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> list[dict]:
    seen: set[tuple[str, ...]] = set()
    result: list[dict] = []
    for raw in values:
        value = dict(raw)
        identity = tuple(str(value[key]) for key in keys)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(value)
    return sorted(result, key=lambda item: tuple(str(item[key]) for key in keys))


def _span_id_from_evidence(evidence: Mapping[str, Any]) -> str:
    source_revision_id = str(evidence.get("source_event_id") or "").strip()
    start = int(evidence.get("authority_span_start", -1))
    end = int(evidence.get("authority_span_end", -1))
    if not source_revision_id or start < 0 or end <= start:
        raise ValueError("cognition episode provenance requires an exact source span")
    return f"{source_revision_id}#{start}:{end}"


def _source_provenance(
    revision: CognitiveStateRevision,
    evidence_refs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    refs = [dict(value) for value in evidence_refs]
    source_revisions = tuple(
        dict.fromkeys(str(value.get("source_event_id") or "").strip() for value in refs)
    )
    source_revisions = tuple(value for value in source_revisions if value)
    span_ids = tuple(dict.fromkeys(_span_id_from_evidence(value) for value in refs))
    return {
        "projection_revision_id": revision.revision_id,
        "source_revision_id": (
            source_revisions[0] if len(source_revisions) == 1 else revision.source_revision_id
        ),
        "source_revision_ids": list(source_revisions),
        "source_span_ids": list(span_ids),
    }


def _source_span_provenance(
    revision: CognitiveStateRevision,
    span: Mapping[str, Any],
) -> dict[str, Any]:
    source_revision_id = str(span.get("revision_id") or "").strip()
    start = int(span.get("span_start", -1))
    end = int(span.get("span_end", -1))
    if not source_revision_id or start < 0 or end <= start:
        raise ValueError("cognition episode Raw span provenance is incomplete")
    return {
        "projection_revision_id": revision.revision_id,
        "source_revision_id": source_revision_id,
        "source_revision_ids": [source_revision_id],
        "source_span_ids": [f"{source_revision_id}#{start}:{end}"],
    }


def _edge_provenance(
    revision: CognitiveStateRevision,
    *metadata_values: Mapping[str, Any],
    quote: str = "",
) -> dict[str, Any]:
    source_revisions: list[str] = []
    source_spans: list[str] = []
    for metadata in metadata_values:
        for value in metadata.get("source_revision_ids") or ():
            normalized = str(value).strip()
            if normalized and normalized not in source_revisions:
                source_revisions.append(normalized)
        for value in metadata.get("source_span_ids") or ():
            normalized = str(value).strip()
            if normalized and normalized not in source_spans:
                source_spans.append(normalized)
    source_revision_id = (
        source_revisions[0] if len(source_revisions) == 1 else revision.source_revision_id
    )
    return {
        "schema_version": "mnemos.cognition_source_provenance.v1",
        "projection_revision_id": revision.revision_id,
        "source_revision_id": source_revision_id,
        "source_revision_ids": source_revisions,
        "source_span_ids": source_spans,
        "quote": str(quote),
    }


def _episode_projection_manifest(revision: CognitiveStateRevision) -> dict[str, Any]:
    if revision.object_type != "cognition_episode":
        raise ValueError("cognition episode dispatch requires a cognition_episode revision")
    payload = dict(revision.payload)
    episode_id = revision.object_id
    access_control = dict(payload["access_control"])
    nodes: list[dict[str, Any]] = [
        {
            "id": episode_id,
            "node_type": "episode",
            "title": f"Cognition episode {episode_id}",
            "source_path": "",
            "content": "",
            "metadata": {
                "revision_id": revision.revision_id,
                "payload_hash": revision.payload_hash,
                "projection_revision_id": revision.revision_id,
                "source_revision_id": revision.source_revision_id,
                "source_revision_ids": [revision.source_revision_id],
                "source_span_ids": list(revision.evidence_refs),
            },
        }
    ]
    evidence_edges: list[dict[str, Any]] = []
    graph_relations: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    ids_by_type: dict[str, list[str]] = {
        "claim": [],
        "belief": [],
        "decision": [],
        "prediction": [],
        "action": [],
        "outcome": [],
    }

    source_spans: dict[tuple[Any, ...], str] = {}
    raw_span_ids: list[str] = []
    for span in payload.get("source_spans", []):
        source_span = dict(span)
        raw_span_id = _stable_id("rawspan", source_span)
        source_spans[_source_span_identity(source_span)] = raw_span_id
        raw_span_ids.append(raw_span_id)
        nodes.append(
            {
                "id": raw_span_id,
                "node_type": "raw_revision_span",
                "title": f"Raw span {source_span['revision_id']}",
                "source_path": (
                    f"mnemos://raw/{source_span['revision_id']}"
                    f"#{source_span['span_start']}:{source_span['span_end']}"
                ),
                "content": "",
                "metadata": {
                    **source_span,
                    **_source_span_provenance(revision, source_span),
                },
            }
        )

    observation_ids: list[str] = []
    entry_ids: list[str] = []
    for field_name in COGNITION_EPISODE_FIELDS:
        raw_entries = payload.get(field_name)
        if not isinstance(raw_entries, (list, tuple)):
            raise ValueError(f"cognition episode field {field_name} is not a sequence")
        for entry_index, raw_entry in enumerate(raw_entries):
            entry = dict(raw_entry)
            status = str(entry.get("status") or "")
            if status != "known":
                omission_payload = {
                    "revision_id": revision.revision_id,
                    "field_name": field_name,
                    "entry_index": entry_index,
                    "status": status,
                    "reason": str(entry.get("reason") or ""),
                }
                omissions.append(
                    {
                        "omission_id": _stable_id("cogomit", omission_payload),
                        "field_name": field_name,
                        "entry_index": entry_index,
                        "reason_code": f"entry_status_{status or 'missing'}",
                        "payload_hash": sha256_json(omission_payload),
                    }
                )
                continue
            entry_id = str(entry.get("entry_id") or "")
            if not entry_id:
                raise ValueError("known cognition episode entry lacks committed entry_id")
            entry_type = _node_type(field_name)
            evidence_refs = entry.get("evidence_refs")
            normalized_evidence = (
                [dict(value) for value in evidence_refs]
                if isinstance(evidence_refs, (list, tuple))
                else []
            )
            entry_provenance = (
                _source_provenance(revision, normalized_evidence)
                if normalized_evidence
                else {
                    "projection_revision_id": revision.revision_id,
                    "source_revision_id": revision.source_revision_id,
                    "source_revision_ids": [revision.source_revision_id],
                    "source_span_ids": list(revision.evidence_refs),
                }
            )
            entry_ids.append(entry_id)
            ids_by_type[entry_type].append(entry_id)
            nodes.append(
                {
                    "id": entry_id,
                    "node_type": entry_type,
                    "title": f"{field_name}: {entry_id}",
                    "source_path": "",
                    "content": str(entry.get("value") or ""),
                    "metadata": {
                        "field_name": field_name,
                        "claim_ids": list(entry.get("claim_ids") or ()),
                        "revision_id": revision.revision_id,
                        "field_path": f"cognition_episode.{field_name}[{entry_index}].value",
                        **entry_provenance,
                    },
                }
            )
            graph_relations.append(
                _graph_relation(
                    episode_id,
                    entry_id,
                    "contains",
                    "episode",
                    entry_type,
                )
            )
            if not isinstance(evidence_refs, (list, tuple)) or not evidence_refs:
                omission_payload = {
                    "revision_id": revision.revision_id,
                    "field_name": field_name,
                    "entry_index": entry_index,
                    "entry_id": entry_id,
                }
                omissions.append(
                    {
                        "omission_id": _stable_id("cogomit", omission_payload),
                        "field_name": field_name,
                        "entry_index": entry_index,
                        "reason_code": "known_entry_without_evidence",
                        "payload_hash": sha256_json(omission_payload),
                    }
                )
                continue
            for evidence_index, raw_evidence in enumerate(evidence_refs):
                evidence = dict(raw_evidence)
                authority_id = str(evidence.get("source_authority_id") or "")
                matched_raw_span_id = source_spans.get(_evidence_span_identity(evidence))
                if matched_raw_span_id is None:
                    raise ValueError("cognition episode evidence lacks an exact committed Raw span")
                observation_id = _stable_id(
                    "observation",
                    {
                        "entry_id": entry_id,
                        "evidence_index": evidence_index,
                        "source_event_id": evidence.get("source_event_id"),
                        "source_authority_id": authority_id,
                        "quote": evidence.get("quote"),
                    },
                )
                observation_ids.append(observation_id)
                observation_provenance = _source_provenance(revision, (evidence,))
                nodes.append(
                    {
                        "id": observation_id,
                        "node_type": "observation",
                        "title": f"Observation for {entry_id}",
                        "source_path": "",
                        "content": str(evidence.get("quote") or ""),
                        "metadata": {
                            "source_event_id": str(evidence.get("source_event_id") or ""),
                            "source_authority_id": authority_id,
                            "entry_id": entry_id,
                            "field_path": (
                                f"cognition_episode.{field_name}[{entry_index}]"
                                f".evidence_refs[{evidence_index}].quote"
                            ),
                            **observation_provenance,
                        },
                    }
                )
                evidence_edges.extend(
                    (
                        _evidence_edge(
                            revision,
                            entry_id,
                            observation_id,
                            "derived_from",
                            entry_provenance,
                            observation_provenance,
                            quote=str(evidence.get("quote") or ""),
                        ),
                        _evidence_edge(
                            revision,
                            observation_id,
                            matched_raw_span_id,
                            "observed_in",
                            observation_provenance,
                            _source_span_provenance(
                                revision,
                                next(
                                    span
                                    for span in payload.get("source_spans", [])
                                    if _source_span_identity(dict(span))
                                    == _evidence_span_identity(evidence)
                                ),
                            ),
                            quote=str(evidence.get("quote") or ""),
                        ),
                    )
                )
                graph_relations.extend(
                    (
                        _graph_relation(
                            entry_id,
                            observation_id,
                            "derived_from",
                            entry_type,
                            "observation",
                        ),
                        _graph_relation(
                            observation_id,
                            matched_raw_span_id,
                            "observed_in",
                            "observation",
                            "raw_revision_span",
                        ),
                    )
                )

    claims_and_beliefs = [*ids_by_type["claim"], *ids_by_type["belief"]]
    for decision_id in ids_by_type["decision"]:
        for basis_id in claims_and_beliefs:
            graph_relations.append(
                _graph_relation(
                    decision_id,
                    basis_id,
                    "based_on",
                    "decision",
                    "belief" if basis_id in ids_by_type["belief"] else "claim",
                )
            )
    for prediction_id in ids_by_type["prediction"]:
        for decision_id in ids_by_type["decision"]:
            graph_relations.append(
                _graph_relation(
                    prediction_id,
                    decision_id,
                    "predicted_from",
                    "prediction",
                    "decision",
                )
            )
    for action_id in ids_by_type["action"]:
        for decision_id in ids_by_type["decision"]:
            graph_relations.append(
                _graph_relation(
                    action_id,
                    decision_id,
                    "implements",
                    "action",
                    "decision",
                )
            )
        for prediction_id in ids_by_type["prediction"]:
            graph_relations.append(
                _graph_relation(
                    action_id,
                    prediction_id,
                    "based_on",
                    "action",
                    "prediction",
                )
            )
    for outcome_id in ids_by_type["outcome"]:
        for target_id, target_type in (
            *((value, "action") for value in ids_by_type["action"]),
            *((value, "prediction") for value in ids_by_type["prediction"]),
        ):
            graph_relations.append(
                _graph_relation(
                    outcome_id,
                    target_id,
                    "measures",
                    "outcome",
                    target_type,
                )
            )

    # EvidenceGraph freezes the same derived → evidence direction as the
    # cognitive graph.  Semantic edges are copied from the deterministic plan,
    # never inferred again by a consumer.
    node_metadata = {
        str(node["id"]): dict(node.get("metadata") or {})
        for node in nodes
    }
    for relation in graph_relations:
        evidence_edges.append(
            _evidence_edge(
                revision,
                str(relation["source"]),
                str(relation["target"]),
                str(relation["relation_type"]),
                node_metadata.get(str(relation["source"]), {}),
                node_metadata.get(str(relation["target"]), {}),
            )
        )

    nodes = _dedupe_dicts(nodes, ("id",))
    evidence_edges = _dedupe_dicts(evidence_edges, ("source_id", "target_id", "relation_type"))
    graph_relations = _dedupe_dicts(graph_relations, ("source", "target", "relation_type"))
    omissions = _dedupe_dicts(omissions, ("omission_id",))
    evidence_manifest_hash = sha256_json(
        {
            "revision_id": revision.revision_id,
            "nodes": nodes,
            "edges": evidence_edges,
            "omissions": omissions,
            "access_control_hash": cognitive_access_hash(access_control),
        }
    )
    graph_manifest_hash = sha256_json(
        {
            "revision_id": revision.revision_id,
            "relations": graph_relations,
            "access_control_hash": cognitive_access_hash(access_control),
        }
    )
    return {
        "episode_id": episode_id,
        "entry_ids": sorted(set(entry_ids)),
        "claim_ids": sorted(set(ids_by_type["claim"])),
        "belief_ids": sorted(set(ids_by_type["belief"])),
        "decision_ids": sorted(set(ids_by_type["decision"])),
        "prediction_ids": sorted(set(ids_by_type["prediction"])),
        "action_ids": sorted(set(ids_by_type["action"])),
        "outcome_ids": sorted(set(ids_by_type["outcome"])),
        "observation_ids": sorted(set(observation_ids)),
        "source_span_ids": sorted(set(raw_span_ids)),
        "nodes": nodes,
        "evidence_edges": evidence_edges,
        "graph_relations": graph_relations,
        "omissions": omissions,
        "evidence_manifest_hash": evidence_manifest_hash,
        "graph_manifest_hash": graph_manifest_hash,
        "access_control": access_control,
    }


def _evidence_edge(
    revision: CognitiveStateRevision,
    source_id: str,
    target_id: str,
    relation_type: str,
    *metadata_values: Mapping[str, Any],
    quote: str = "",
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "target_id": target_id,
        "relation_type": relation_type,
        "confidence": 1.0,
        "evidence": [
            "deterministic projection from committed cognition episode IDs",
            _edge_provenance(revision, *metadata_values, quote=quote),
        ],
    }


def _graph_relation(
    source: str,
    target: str,
    relation_type: str,
    source_layer: str,
    target_layer: str,
) -> dict[str, Any]:
    return {
        "source": source,
        "target": target,
        "relation_type": relation_type,
        "strength": 1.0,
        "confidence": 1.0,
        "source_layer": source_layer,
        "target_layer": target_layer,
    }


def _event_payload(revision: CognitiveStateRevision, manifest: Mapping[str, Any]) -> dict:
    payload = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_id": revision.source_event_id,
        "episode_id": revision.object_id,
        "episode_revision_id": revision.revision_id,
        "episode_payload_hash": revision.payload_hash,
        "entry_ids": list(manifest["entry_ids"]),
        "claim_ids": list(manifest["claim_ids"]),
        "belief_ids": list(manifest["belief_ids"]),
        "decision_ids": list(manifest["decision_ids"]),
        "prediction_ids": list(manifest["prediction_ids"]),
        "action_ids": list(manifest["action_ids"]),
        "outcome_ids": list(manifest["outcome_ids"]),
        "observation_ids": list(manifest["observation_ids"]),
        "source_span_ids": list(manifest["source_span_ids"]),
        "evidence_manifest_hash": str(manifest["evidence_manifest_hash"]),
        "graph_manifest_hash": str(manifest["graph_manifest_hash"]),
        "access_control_hash": cognitive_access_hash(manifest["access_control"]),
    }
    payload["projection_identity_hash"] = sha256_json(payload)
    return payload


def _trace_id(revision_id: str) -> str:
    return _stable_id("cogdispatch", {"revision_id": revision_id, "event": EVENT_TYPE})


@dataclass(frozen=True)
class _TargetEffect:
    effect_id: str
    before_hash: str
    after_hash: str
    metadata: Mapping[str, Any]
    proof: CognitionEpisodeProjectionProof


class CognitionEpisodeDispatchOwner:
    """Publish and consume one durable event per committed episode revision."""

    def __init__(
        self,
        *,
        config: Any,
        event_bus: EventBus,
        cognitive_graph_store: CognitiveGraphStore | None = None,
        evidence_graph: EvidenceGraph | None = None,
        fail_after_relation: int = 0,
    ):
        self.config = config
        self.event_bus = event_bus
        self.database_dir = Path(config.database_dir)
        self.wiki_dir = Path(config.wiki_dir)
        graph_path = Path(
            getattr(config, "cognitive_graph_db_path", None)
            or self.database_dir / "cognitive_graph.db"
        )
        evidence_path = self.database_dir / "evidence_graph.db"
        wiki_projection_path = resolve_wiki_projection_db_path(config)
        self.wiki_projection_db_path = wiki_projection_path
        target_was_absent = {
            "evidence_graph": not evidence_path.is_file(),
            "cognitive_graph": not graph_path.is_file(),
            "wiki": not wiki_projection_path.is_file(),
        }
        self.state = CognitiveStateStore(config)
        self.cognitive_graph = cognitive_graph_store or CognitiveGraphStore(
            str(graph_path), ownership_config=config
        )
        if Path(self.cognitive_graph.db_path).resolve(strict=False) != graph_path.resolve(
            strict=False
        ):
            raise ValueError("cognition episode cognitive graph target differs from config")
        if evidence_graph is None:
            self.evidence_graph = EvidenceGraph(str(evidence_path))
        else:
            # Keep the independently verified configured target present even
            # when a test seam injects a failing projector implementation.
            if not evidence_path.is_file():
                EvidenceGraph(str(evidence_path))
            self.evidence_graph = evidence_graph
        if target_was_absent["wiki"]:
            WikiProjectionLedger(wiki_projection_path)
        initialize_fresh_projection_targets(
            evidence_db_path=evidence_path,
            cognitive_graph_db_path=graph_path,
            wiki_projection_db_path=wiki_projection_path,
            target_was_absent=target_was_absent,
        )
        validate_cognition_episode_projection_schema(
            evidence_db_path=evidence_path,
            cognitive_graph_db_path=graph_path,
            wiki_projection_db_path=wiki_projection_path,
        )
        self.graph_updater = CognitiveGraphUpdater(store=self.cognitive_graph)
        self.fail_after_relation = max(0, int(fail_after_relation))
        self._subscribed = False

    def subscribe(self) -> None:
        if self._subscribed:
            return
        self.event_bus.subscribe(EVENT_TYPE, self._on_wiki, consumer_id="wiki")
        self.event_bus.subscribe(
            EVENT_TYPE,
            self._on_knowledge_graph,
            consumer_id="knowledge_graph",
        )
        self.event_bus.subscribe(
            EVENT_TYPE,
            self._on_cognitive_graph,
            consumer_id="cognitive_graph",
        )
        self._subscribed = True

    def publish_pending(self, *, limit: int = 100) -> dict[str, Any]:
        revision_ids: list[str] = []
        for command in self.state.pending_commands():
            if command["command_type"] != COMMAND_TYPE:
                continue
            revision_id = str(command["revision_id"])
            if revision_id not in revision_ids:
                revision_ids.append(revision_id)
            if len(revision_ids) >= max(1, int(limit)):
                break
        events = [self.publish_revision(revision_id) for revision_id in revision_ids]
        return {"published": len(events), "events": events}

    def publish_revision(self, revision_id: str) -> dict[str, str]:
        return publish_cognition_episode_revision(
            config=self.config,
            event_bus=self.event_bus,
            revision_id=revision_id,
        )

    def reconcile_revision(self, revision_id: str) -> dict[str, str]:
        """Re-observe and repair target effects without rewriting EventBus history."""

        revision = self.state.revision(str(revision_id))
        if revision is None:
            raise LookupError(f"unknown cognition episode revision: {revision_id}")
        event = _event_for_revision(revision)
        outcomes = {
            "wiki": self._on_wiki(event),
            "knowledge_graph": self._on_knowledge_graph(event),
            "cognitive_graph": self._on_cognitive_graph(event),
        }
        failures = {
            consumer: outcome.reason
            for consumer, outcome in outcomes.items()
            if outcome.disposition not in {"ack", "noop"}
        }
        if failures:
            raise RuntimeError(
                "cognition episode target reconciliation failed: "
                + ", ".join(f"{consumer}={reason}" for consumer, reason in sorted(failures.items()))
            )
        return {consumer: outcome.disposition for consumer, outcome in outcomes.items()}

    def _context(self, event: Event, consumer: str) -> tuple:
        if event.event_type != EVENT_TYPE:
            raise ValueError("cognition episode dispatch event type mismatch")
        revision_id = str(event.payload.get("episode_revision_id") or "")
        revision = self.state.revision(revision_id)
        if revision is None:
            raise LookupError("cognition episode dispatch revision is missing")
        manifest = _episode_projection_manifest(revision)
        if dict(event.payload) != _event_payload(revision, manifest):
            raise ValueError("cognition episode dispatch payload drift")
        if event.trace_id != _trace_id(revision_id):
            raise ValueError("cognition episode dispatch trace identity mismatch")
        commands = [
            command
            for command in self.state.commands_for_revision(revision_id)
            if command["command_type"] == COMMAND_TYPE and command["consumer_id"] == consumer
        ]
        if len(commands) != 1:
            raise ValueError("cognition episode consumer command cardinality mismatch")
        command = commands[0]
        existing = self.state.effect_receipt(str(command["command_id"]))
        return revision, manifest, command, existing

    def _dispatch(
        self,
        event: Event,
        consumer: str,
        projector: Callable[
            [Event, CognitiveStateRevision, Mapping[str, Any], Mapping[str, Any]], _TargetEffect
        ],
    ) -> HandlerOutcome:
        try:
            revision, manifest, command, existing = self._context(event, consumer)
            effect = projector(event, revision, manifest, command)
            receipt = self.state.record_cognition_episode_projection_receipt(
                str(command["command_id"]),
                proof=effect.proof,
            )
            outcome = HandlerOutcome.noop if existing is not None else HandlerOutcome.ack
            return outcome(
                consumer,
                effect_id=effect.effect_id,
                effect_receipt_id=receipt.receipt_id,
                before_hash=effect.before_hash,
                after_hash=effect.after_hash,
                revision_id=revision.revision_id,
                **dict(effect.metadata),
            )
        except (LookupError, PermissionError, TypeError, ValueError) as exc:
            return HandlerOutcome.dead(consumer, str(exc))
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            return HandlerOutcome.retry(consumer, str(exc))

    def _on_wiki(self, event: Event) -> HandlerOutcome:
        return self._dispatch(event, "wiki", self._project_wiki)

    def _on_knowledge_graph(self, event: Event) -> HandlerOutcome:
        return self._dispatch(event, "knowledge_graph", self._project_evidence_graph)

    def _on_cognitive_graph(self, event: Event) -> HandlerOutcome:
        return self._dispatch(event, "cognitive_graph", self._project_cognitive_graph)

    def _project_wiki(
        self,
        _event: Event,
        revision: CognitiveStateRevision,
        _manifest: Mapping[str, Any],
        command: Mapping[str, Any],
    ) -> _TargetEffect:
        effect_id = projection_effect_id(str(command["command_id"]), "wiki")
        before_hash = projection_before_hash(revision.revision_id, "wiki")
        with sqlite3.connect(str(self.wiki_projection_db_path), timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            try:
                pages: list[dict[str, str]] = []
                if self.wiki_dir.is_dir():
                    for page in sorted(self.wiki_dir.rglob("*.md")):
                        content = read_text_value(page)
                        frontmatter, _body = parse_frontmatter(content)
                        if (
                            str(fm_get(frontmatter, "cognition_episode_revision_id") or "")
                            != revision.revision_id
                        ):
                            continue
                        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
                        lifecycle = conn.execute(
                            """SELECT page.page_id, page.current_revision,
                                      mutation.mutation_id, mutation.content_sha256,
                                      mutation.page_path, mutation.tombstone
                               FROM wiki_pages AS page
                               JOIN wiki_mutations AS mutation
                                 ON mutation.page_id=page.page_id
                                AND mutation.page_revision=page.current_revision
                               WHERE page.current_path=?
                                 AND page.lifecycle_state='active'""",
                            (str(page.resolve(strict=True)),),
                        ).fetchone()
                        if lifecycle is None or any(
                            (
                                str(lifecycle["content_sha256"]) != content_sha256,
                                Path(str(lifecycle["page_path"])).resolve(strict=False)
                                != page.resolve(strict=True),
                                bool(lifecycle["tombstone"]),
                            )
                        ):
                            raise RuntimeError(
                                "committed cognition episode Wiki page lacks an exact lifecycle mutation"
                            )
                        pages.append(
                            {
                                "path": str(page.relative_to(self.wiki_dir)),
                                "content_sha256": "sha256:" + content_sha256,
                                "page_id": str(lifecycle["page_id"]),
                                "page_revision": str(lifecycle["current_revision"]),
                                "mutation_id": str(lifecycle["mutation_id"]),
                            }
                        )
                if not pages:
                    raise RuntimeError("committed cognition episode has no bound Wiki projection")
                after_hash = sha256_json({"revision_id": revision.revision_id, "pages": pages})
                projection_json = json.dumps(
                    pages,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                existing = conn.execute(
                    "SELECT * FROM cognition_episode_projection_effects WHERE effect_id=?",
                    (effect_id,),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """INSERT INTO cognition_episode_projection_effects
                           (effect_id, revision_id, manifest_hash, before_hash,
                            after_hash, page_count, projection_json, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            effect_id,
                            revision.revision_id,
                            after_hash,
                            before_hash,
                            after_hash,
                            len(pages),
                            projection_json,
                            str(command["created_at"]),
                        ),
                    )
                elif any(
                    (
                        str(existing["revision_id"]) != revision.revision_id,
                        str(existing["manifest_hash"]) != after_hash,
                        str(existing["before_hash"]) != before_hash,
                        str(existing["after_hash"]) != after_hash,
                        int(existing["page_count"]) != len(pages),
                        str(existing["projection_json"]) != projection_json,
                    )
                ):
                    raise RuntimeError("cognition episode Wiki target effect conflict")
                conn.commit()
            except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error):
                conn.rollback()
                raise
        return _TargetEffect(
            effect_id=effect_id,
            before_hash=before_hash,
            after_hash=after_hash,
            metadata={"page_count": len(pages)},
            proof=CognitionEpisodeProjectionProof(
                consumer_id="wiki",
                revision_id=revision.revision_id,
                effect_id=effect_id,
                before_hash=before_hash,
                after_hash=after_hash,
            ),
        )

    def _project_evidence_graph(
        self,
        _event: Event,
        revision: CognitiveStateRevision,
        manifest: Mapping[str, Any],
        command: Mapping[str, Any],
    ) -> _TargetEffect:
        effect_id = projection_effect_id(str(command["command_id"]), "knowledge_graph")
        effect = self.evidence_graph.project_cognition_episode(
            effect_id=effect_id,
            revision_id=revision.revision_id,
            manifest_hash=str(manifest["evidence_manifest_hash"]),
            nodes=manifest["nodes"],
            edges=manifest["evidence_edges"],
            omissions=manifest["omissions"],
            access_control=manifest["access_control"],
            created_at=str(command["created_at"]),
        )
        after_hash = str(effect["after_hash"])
        return _TargetEffect(
            effect_id=effect_id,
            before_hash=str(effect["before_hash"]),
            after_hash=after_hash,
            metadata={
                "node_count": int(effect["node_count"]),
                "edge_count": int(effect["edge_count"]),
                "omission_count": int(effect["omission_count"]),
            },
            proof=CognitionEpisodeProjectionProof(
                consumer_id="knowledge_graph",
                revision_id=revision.revision_id,
                effect_id=effect_id,
                before_hash=str(effect["before_hash"]),
                after_hash=after_hash,
            ),
        )

    def _project_cognitive_graph(
        self,
        event: Event,
        revision: CognitiveStateRevision,
        manifest: Mapping[str, Any],
        command: Mapping[str, Any],
    ) -> _TargetEffect:
        relations = list(manifest["graph_relations"])
        self.graph_updater.project_committed_episode_relations(
            event,
            relations,
            access_control=manifest["access_control"],
            fail_after=self.fail_after_relation,
        )
        effect_id = projection_effect_id(str(command["command_id"]), "cognitive_graph")
        before_hash = projection_before_hash(revision.revision_id, "cognitive_graph")
        after_hash = str(manifest["graph_manifest_hash"])
        with sqlite3.connect(str(self.cognitive_graph.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            missing = [
                relation
                for relation in relations
                if conn.execute(
                    """SELECT 1 FROM cognitive_relations
                       WHERE source=? AND target=? AND relation_type=? AND stale=0""",
                    (
                        relation["source"],
                        relation["target"],
                        relation["relation_type"],
                    ),
                ).fetchone()
                is None
            ]
            if missing:
                raise RuntimeError("cognition episode cognitive graph projection is incomplete")
            existing = conn.execute(
                "SELECT * FROM cognition_episode_projection_effects WHERE effect_id=?",
                (effect_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO cognition_episode_projection_effects
                       (effect_id, revision_id, manifest_hash, before_hash, after_hash,
                        relation_count, access_control_hash, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        effect_id,
                        revision.revision_id,
                        str(manifest["graph_manifest_hash"]),
                        before_hash,
                        after_hash,
                        len(relations),
                        cognitive_access_hash(manifest["access_control"]),
                        str(command["created_at"]),
                    ),
                )
                conn.commit()
                existing = conn.execute(
                    "SELECT * FROM cognition_episode_projection_effects WHERE effect_id=?",
                    (effect_id,),
                ).fetchone()
            if (
                existing is None
                or str(existing["revision_id"]) != revision.revision_id
                or str(existing["manifest_hash"]) != str(manifest["graph_manifest_hash"])
                or int(existing["relation_count"]) != len(relations)
            ):
                raise RuntimeError("cognition episode cognitive graph effect conflict")
        return _TargetEffect(
            effect_id=effect_id,
            before_hash=before_hash,
            after_hash=after_hash,
            metadata={"relation_count": len(relations)},
            proof=CognitionEpisodeProjectionProof(
                consumer_id="cognitive_graph",
                revision_id=revision.revision_id,
                effect_id=effect_id,
                before_hash=before_hash,
                after_hash=after_hash,
            ),
        )


def publish_pending_cognition_episodes(
    *,
    config: Any,
    event_bus: EventBus,
    limit: int = 100,
) -> dict[str, Any]:
    """Register canonical consumers and publish currently pending revisions."""

    owner = CognitionEpisodeDispatchOwner(config=config, event_bus=event_bus)
    owner.subscribe()
    return owner.publish_pending(limit=limit)


def publish_cognition_episode_revision(
    *,
    config: Any,
    event_bus: EventBus,
    revision_id: str,
) -> dict[str, str]:
    """Publish one immutable ID-only event without constructing any consumer."""

    revision = CognitiveStateStore(config).revision(str(revision_id))
    if revision is None:
        raise LookupError(f"unknown cognition episode revision: {revision_id}")
    event = _event_for_revision(revision)
    published = event_bus.publish(event)
    return {"revision_id": revision.revision_id, "trace_id": published}


def _event_for_revision(revision: CognitiveStateRevision) -> Event:
    manifest = _episode_projection_manifest(revision)
    return Event(
        event_type=EVENT_TYPE,
        source="cognitive_state_store",
        payload=_event_payload(revision, manifest),
        trace_id=_trace_id(revision.revision_id),
        timestamp=revision.created_at,
        subject_provenance=dict(manifest["access_control"]),
    )
