"""Independent, read-only audit for committed cognition-episode projections.

This module deliberately re-derives IDs, manifests, hashes, event envelopes,
and traversal expectations from the immutable state revision.  It must not
call the production dispatcher or its projection helpers: a producer defect
must not be able to certify itself by sharing the same implementation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from core.cognitive.cognition_episode_projection_audit_support import (
    dedupe as _dedupe,
    edge as _edge,
    evidence_identity as _evidence_identity,
    node_type as _node_type,
    relation as _relation,
    source_provenance as _source_provenance,
    source_span_provenance as _source_span_provenance,
    span_identity as _span_identity,
)
from core.frontmatter import fm_get, parse_frontmatter
from core.utils import read_text_value

SCHEMA_VERSION = "mnemos.cognitive_event_dispatch_audit.v1"
EVENT_TYPE = "cognition_episode_committed"
EVENT_SCHEMA_VERSION = "mnemos.cognition_episode_committed.v1"
COMMAND_TYPE = "project_cognition_episode"
CONSUMERS = ("wiki", "knowledge_graph", "cognitive_graph")
_STATE_DB = "producer_consumer_ledger.db"
_COGNITION_FIELDS = (
    "situation",
    "goal",
    "desired_state",
    "facts",
    "assumptions",
    "hypotheses",
    "causal_links",
    "alternatives",
    "tradeoffs",
    "decision",
    "rationale",
    "actions",
    "outcomes",
    "root_cause",
    "correction",
    "supersedes",
    "uncertainty",
    "invalidation_conditions",
    "scope",
)
_ALLOWED_EVENT_KEYS = frozenset(
    {
        "schema_version",
        "event_id",
        "episode_id",
        "episode_revision_id",
        "episode_payload_hash",
        "entry_ids",
        "claim_ids",
        "belief_ids",
        "decision_ids",
        "prediction_ids",
        "action_ids",
        "outcome_ids",
        "observation_ids",
        "source_span_ids",
        "evidence_manifest_hash",
        "graph_manifest_hash",
        "access_control_hash",
        "projection_identity_hash",
    }
)
_ACL_FIELDS = {
    "schema_version",
    "owner",
    "scope",
    "purposes",
    "consent",
    "sensitivity",
    "retention_policy",
    "redaction_policy",
    "source_acl_lineage",
    "visibility",
    "declassification",
}
_FIXTURE_OMISSION_REASON = "synthetic_fixture_source_not_in_canonical_raw"
_FIXTURE_OMISSION_OUTCOME = "synthetic demo object retired without projection"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _audit_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _stable_id(prefix: str, value: Any) -> str:
    return prefix + "-" + _audit_hash(value).split(":", 1)[1][:32]


def _valid_acl_envelope(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != _ACL_FIELDS:
        return False
    owner = value.get("owner")
    scope = value.get("scope")
    consent = value.get("consent")
    if (
        value.get("schema_version") != "mnemos.cognitive_access.v1"
        or not isinstance(owner, Mapping)
        or set(owner) != {"principal_id", "agent"}
        or not all(str(owner.get(key) or "").strip() for key in owner)
        or not isinstance(scope, Mapping)
        or set(scope) != {"scope_type", "scope_id", "project", "session_id", "resolution"}
        or not str(scope.get("scope_type") or "").strip()
        or not str(scope.get("scope_id") or "").strip()
        or not isinstance(consent, Mapping)
        or set(consent) != {"status", "provenance_refs"}
    ):
        return False
    for key in ("purposes", "source_acl_lineage"):
        values = value.get(key)
        if not isinstance(values, list) or values != sorted(set(str(item) for item in values)):
            return False
    refs = consent.get("provenance_refs")
    return isinstance(refs, list) and refs == sorted(set(str(item) for item in refs))


def _acl_hash(value: Any) -> str:
    if not _valid_acl_envelope(value):
        raise ValueError("cognition episode access-control envelope is invalid")
    return _audit_hash(dict(value))


def _expected_graph_acl(source: Mapping[str, Any]) -> dict[str, Any]:
    """Independently derive the single-source CognitiveGraph read envelope."""

    source_hash = _acl_hash(source)
    lineage = sorted({source_hash, *(str(item) for item in source["source_acl_lineage"])})
    compatible = "cognitive_graph_read" in set(source["purposes"])
    if compatible:
        return {
            "schema_version": "mnemos.cognitive_access.v1",
            "owner": dict(source["owner"]),
            "scope": dict(source["scope"]),
            "purposes": ["cognitive_graph_read"],
            "consent": {
                "status": str(source["consent"]["status"]),
                "provenance_refs": list(source["consent"]["provenance_refs"]),
            },
            "sensitivity": ("restricted" if source["sensitivity"] == "restricted" else "sensitive"),
            "retention_policy": "cognitive_graph_retention",
            "redaction_policy": str(source["redaction_policy"]),
            "source_acl_lineage": lineage,
            "visibility": str(source["visibility"]),
            "declassification": {"state": "not_requested"},
        }
    return {
        "schema_version": "mnemos.cognitive_access.v1",
        "owner": dict(source["owner"]),
        "scope": {
            "scope_type": str(source["scope"]["scope_type"]),
            "scope_id": str(source["scope"]["scope_id"]),
            "project": "",
            "session_id": "",
            "resolution": "restricted_unknown",
        },
        "purposes": ["cognitive_graph_read"],
        "consent": {"status": "restricted_unknown", "provenance_refs": []},
        "sensitivity": "restricted",
        "retention_policy": "cognitive_graph_retention",
        "redaction_policy": str(source["redaction_policy"]),
        "source_acl_lineage": lineage,
        "visibility": "restricted",
        "declassification": {"state": "not_requested"},
    }


def _audit_projection_plan(revision: Any) -> dict[str, Any]:
    """Re-derive the complete projection without importing producer code."""

    if revision.object_type != "cognition_episode":
        raise ValueError("audit revision is not a cognition episode")
    payload = dict(revision.payload)
    episode_id = revision.object_id
    access_control = dict(payload["access_control"])
    access_hash = _acl_hash(access_control)
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
    relations: list[dict[str, Any]] = []
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
    for raw_span in payload.get("source_spans", []):
        span = dict(raw_span)
        raw_span_id = _stable_id("rawspan", span)
        source_spans[_span_identity(span)] = raw_span_id
        raw_span_ids.append(raw_span_id)
        nodes.append(
            {
                "id": raw_span_id,
                "node_type": "raw_revision_span",
                "title": f"Raw span {span['revision_id']}",
                "source_path": (
                    f"mnemos://raw/{span['revision_id']}"
                    f"#{span['span_start']}:{span['span_end']}"
                ),
                "content": "",
                "metadata": {
                    **span,
                    **_source_span_provenance(revision, span),
                },
            }
        )

    entry_ids: list[str] = []
    evidenced_entry_ids: list[str] = []
    observation_ids: list[str] = []
    for field_name in _COGNITION_FIELDS:
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
                        "payload_hash": _audit_hash(omission_payload),
                    }
                )
                continue
            entry_id = str(entry.get("entry_id") or "")
            if not entry_id:
                raise ValueError("known cognition episode entry lacks entry_id")
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
            relations.append(_relation(episode_id, entry_id, "contains", "episode", entry_type))
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
                        "payload_hash": _audit_hash(omission_payload),
                    }
                )
                continue
            evidenced_entry_ids.append(entry_id)
            for evidence_index, raw_evidence in enumerate(evidence_refs):
                evidence = dict(raw_evidence)
                matched_raw_span_id = source_spans.get(_evidence_identity(evidence))
                if matched_raw_span_id is None:
                    raise ValueError("episode evidence does not bind an exact Raw span")
                observation_id = _stable_id(
                    "observation",
                    {
                        "entry_id": entry_id,
                        "evidence_index": evidence_index,
                        "source_event_id": evidence.get("source_event_id"),
                        "source_authority_id": evidence.get("source_authority_id"),
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
                            "source_authority_id": str(evidence.get("source_authority_id") or ""),
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
                        _edge(
                            revision,
                            entry_id,
                            observation_id,
                            "derived_from",
                            entry_provenance,
                            observation_provenance,
                            quote=str(evidence.get("quote") or ""),
                        ),
                        _edge(
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
                                    if _span_identity(dict(span)) == _evidence_identity(evidence)
                                ),
                            ),
                            quote=str(evidence.get("quote") or ""),
                        ),
                    )
                )
                relations.extend(
                    (
                        _relation(
                            entry_id,
                            observation_id,
                            "derived_from",
                            entry_type,
                            "observation",
                        ),
                        _relation(
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
            relations.append(
                _relation(
                    decision_id,
                    basis_id,
                    "based_on",
                    "decision",
                    "belief" if basis_id in ids_by_type["belief"] else "claim",
                )
            )
    for prediction_id in ids_by_type["prediction"]:
        for decision_id in ids_by_type["decision"]:
            relations.append(
                _relation(
                    prediction_id,
                    decision_id,
                    "predicted_from",
                    "prediction",
                    "decision",
                )
            )
    for action_id in ids_by_type["action"]:
        for decision_id in ids_by_type["decision"]:
            relations.append(_relation(action_id, decision_id, "implements", "action", "decision"))
        for prediction_id in ids_by_type["prediction"]:
            relations.append(
                _relation(action_id, prediction_id, "based_on", "action", "prediction")
            )
    for outcome_id in ids_by_type["outcome"]:
        for target_id, target_type in (
            *((value, "action") for value in ids_by_type["action"]),
            *((value, "prediction") for value in ids_by_type["prediction"]),
        ):
            relations.append(_relation(outcome_id, target_id, "measures", "outcome", target_type))

    node_metadata = {
        str(node["id"]): dict(node.get("metadata") or {})
        for node in nodes
    }
    for relation in relations:
        evidence_edges.append(
            _edge(
                revision,
                str(relation["source"]),
                str(relation["target"]),
                str(relation["relation_type"]),
                node_metadata.get(str(relation["source"]), {}),
                node_metadata.get(str(relation["target"]), {}),
            )
        )
    nodes = _dedupe(nodes, ("id",))
    evidence_edges = _dedupe(
        evidence_edges,
        ("source_id", "target_id", "relation_type"),
    )
    relations = _dedupe(relations, ("source", "target", "relation_type"))
    omissions = _dedupe(omissions, ("omission_id",))
    evidence_hash = _audit_hash(
        {
            "revision_id": revision.revision_id,
            "nodes": nodes,
            "edges": evidence_edges,
            "omissions": omissions,
            "access_control_hash": access_hash,
        }
    )
    graph_hash = _audit_hash(
        {
            "revision_id": revision.revision_id,
            "relations": relations,
            "access_control_hash": access_hash,
        }
    )
    return {
        "episode_id": episode_id,
        "entry_ids": sorted(set(entry_ids)),
        "evidenced_entry_ids": sorted(set(evidenced_entry_ids)),
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
        "graph_relations": relations,
        "omissions": omissions,
        "evidence_manifest_hash": evidence_hash,
        "graph_manifest_hash": graph_hash,
        "access_control": access_control,
        "access_control_hash": access_hash,
    }


def _audit_event_payload(revision: Any, plan: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_id": revision.source_event_id,
        "episode_id": revision.object_id,
        "episode_revision_id": revision.revision_id,
        "episode_payload_hash": revision.payload_hash,
        "entry_ids": list(plan["entry_ids"]),
        "claim_ids": list(plan["claim_ids"]),
        "belief_ids": list(plan["belief_ids"]),
        "decision_ids": list(plan["decision_ids"]),
        "prediction_ids": list(plan["prediction_ids"]),
        "action_ids": list(plan["action_ids"]),
        "outcome_ids": list(plan["outcome_ids"]),
        "observation_ids": list(plan["observation_ids"]),
        "source_span_ids": list(plan["source_span_ids"]),
        "evidence_manifest_hash": str(plan["evidence_manifest_hash"]),
        "graph_manifest_hash": str(plan["graph_manifest_hash"]),
        "access_control_hash": str(plan["access_control_hash"]),
    }
    payload["projection_identity_hash"] = _audit_hash(payload)
    return payload


def _audit_trace_id(revision_id: str) -> str:
    return _stable_id("cogdispatch", {"revision_id": revision_id, "event": EVENT_TYPE})


def _audit_effect_id(command_id: str, consumer: str) -> str:
    target = {
        "wiki": "wiki",
        "knowledge_graph": "evidence_graph",
        "cognitive_graph": "cognitive_graph",
    }[consumer]
    return _stable_id("cogprojection", {"command_id": command_id, "target": target})


def _audit_before_hash(revision_id: str, consumer: str) -> str:
    if consumer == "wiki":
        value = {"revision_id": revision_id, "wiki_projection": "unprojected"}
    elif consumer == "knowledge_graph":
        value = {"revision_id": revision_id, "projection_state": "unprojected"}
    else:
        value = {"revision_id": revision_id, "graph_projection": "unprojected"}
    return _audit_hash(value)


def _audit_command_id(revision_id: str, consumer: str, payload: Mapping[str, Any]) -> str:
    payload_hash = _audit_hash(payload)
    return _stable_id(
        "cogcmd",
        {
            "revision_id": revision_id,
            "consumer_id": consumer,
            "command_type": COMMAND_TYPE,
            "payload_hash": payload_hash,
        },
    )


def _event_fingerprint(source: str, payload: Mapping[str, Any]) -> str:
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    material = "\x1f".join((EVENT_TYPE, source, payload_json)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve(strict=True)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _static_findings(repo_root: Path) -> list[str]:
    required = {
        "core/mnemos_bus.py": (EVENT_TYPE, "lease_owner", "lease_expires_at"),
        "core/cognitive/cognition_episode_dispatch.py": (
            'CONSUMERS = ("wiki", "knowledge_graph", "cognitive_graph")',
            "HandlerOutcome.retry",
            "HandlerOutcome.dead",
            '"projection_revision_id"',
        ),
        "core/hephaestus/distillation_engine.py": ("publish_cognition_episode_revision",),
        "core/cognitive/state_effect_receipts.py": (
            "cognition episode commands require a specialized projection receipt",
        ),
        "mnemos_daemon.py": ("_register_cognition_episode_dispatch",),
    }
    findings: list[str] = []
    for relative, fragments in required.items():
        path = repo_root / relative
        content = read_text_value(path) if path.is_file() else ""
        for fragment in fragments:
            if fragment not in content:
                findings.append(f"missing_contract:{relative}:{fragment}")
    forbidden = {
        "core/hephaestus/distillation_engine.py": ("def _sync_knowledge_graph_update",),
        "core/hephaestus_worker.py": ("_emit_knowledge_distilled",),
        "core/hephaestus/wiki_builder.py": ("_emit_knowledge_distilled",),
    }
    for relative, fragments in forbidden.items():
        path = repo_root / relative
        content = read_text_value(path) if path.is_file() else ""
        for fragment in fragments:
            if fragment in content:
                findings.append(f"forbidden_duplicate_owner:{relative}:{fragment}")
    return findings


def _revision_from_row(row: sqlite3.Row) -> Any:
    payload = json.loads(str(row["payload_json"]))
    if _audit_hash(payload) != str(row["payload_hash"]):
        raise ValueError("cognition episode revision payload hash mismatch")
    return SimpleNamespace(
        revision_id=str(row["revision_id"]),
        object_type=str(row["object_type"]),
        object_id=str(row["object_id"]),
        source_event_id=str(row["source_event_id"]),
        source_revision_id=str(row["source_revision_id"]),
        evidence_refs=tuple(json.loads(str(row["evidence_refs"]))),
        payload=payload,
        payload_hash=str(row["payload_hash"]),
        created_at=str(row["created_at"]),
    )


def _valid_revision_omission(
    revision: Any,
    receipts_by_consumer: Mapping[str, Mapping[str, Any]],
    quarantine: Mapping[str, Any] | None,
) -> bool:
    if (
        quarantine is None
        or str(quarantine.get("source_key") or "") != revision.revision_id
        or str(quarantine.get("reason_code") or "") != _FIXTURE_OMISSION_REASON
        or str(quarantine.get("payload_hash") or "") != revision.payload_hash
        or not str(quarantine.get("quarantine_id") or "")
        or set(receipts_by_consumer) != set(CONSUMERS)
    ):
        return False
    quarantine_ref = f"cognitive-quarantine:{quarantine['quarantine_id']}"
    revision_ref = f"cognition-revision:{revision.revision_id}"
    target_effect_id = f"retired-demo-fixture:{revision.revision_id}"
    for receipt in receipts_by_consumer.values():
        try:
            evidence_refs = set(json.loads(str(receipt.get("evidence_refs") or "[]")))
            metadata = json.loads(str(receipt.get("consumption_metadata") or "{}"))
        except (json.JSONDecodeError, TypeError):
            return False
        if (
            str(receipt.get("status") or "") != "intentional_skip"
            or str(receipt.get("target_effect_id") or "") != target_effect_id
            or str(receipt.get("consumption_outcome") or "") != _FIXTURE_OMISSION_OUTCOME
            or str(metadata.get("terminal_reason_code") or "") != _FIXTURE_OMISSION_REASON
            or not {quarantine_ref, revision_ref} <= evidence_refs
        ):
            return False
    return True


def _load_events(event_db_path: Path) -> tuple[dict[str, list[dict]], dict[str, dict], list[dict]]:
    by_revision: dict[str, list[dict]] = {}
    claims: dict[str, dict] = {}
    handler_receipts: list[dict] = []
    if not event_db_path.is_file():
        return by_revision, claims, handler_receipts
    with _connect_read_only(event_db_path) as conn:
        tables = _tables(conn)
        for table, location in (("events", "events"), ("dead_letters", "dead_letters")):
            if table not in tables:
                continue
            for row in conn.execute(
                f"SELECT trace_id, timestamp, source, payload_json, status "  # nosec B608
                f"FROM {table} WHERE event_type=?",
                (EVENT_TYPE,),
            ).fetchall():
                try:
                    payload = json.loads(str(row["payload_json"]))
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                item = {
                    "trace_id": str(row["trace_id"]),
                    "timestamp": str(row["timestamp"]),
                    "source": str(row["source"]),
                    "status": str(row["status"]),
                    "location": location,
                    "payload": payload,
                }
                by_revision.setdefault(str(payload.get("episode_revision_id") or ""), []).append(
                    item
                )
        if "event_trace_claims" in tables:
            claims = {
                str(row["trace_id"]): dict(row)
                for row in conn.execute(
                    """SELECT trace_id, event_type, source, payload_fingerprint
                       FROM event_trace_claims WHERE event_type=?""",
                    (EVENT_TYPE,),
                ).fetchall()
            }
        if "handler_receipts" in tables:
            handler_receipts = [
                dict(row)
                for row in conn.execute(
                    """SELECT trace_id, consumer, disposition, output_json
                       FROM handler_receipts WHERE event_type=? ORDER BY id""",
                    (EVENT_TYPE,),
                ).fetchall()
            ]
    return by_revision, claims, handler_receipts


def _parse_json_mapping(value: Any) -> Mapping[str, Any] | None:
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _wiki_effect_matches(
    *,
    wiki_projection_db_path: Path,
    wiki_dir: Path,
    revision: Any,
    effect_id: str,
    before_hash: str,
    receipt: Mapping[str, Any],
) -> bool:
    db_path = wiki_projection_db_path
    if not db_path.is_file() or not wiki_dir.is_dir():
        return False
    with _connect_read_only(db_path) as conn:
        if not {
            "cognition_episode_projection_effects",
            "wiki_pages",
            "wiki_mutations",
        } <= _tables(conn):
            return False
        effect = conn.execute(
            "SELECT * FROM cognition_episode_projection_effects WHERE effect_id=?",
            (effect_id,),
        ).fetchone()
        if effect is None:
            return False
        try:
            pages = json.loads(str(effect["projection_json"]))
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(pages, list) or not pages:
            return False
        valid = str(effect["projection_json"]) == _canonical_json(pages)
        valid = valid and len(pages) == int(effect["page_count"])
        for raw_page in pages:
            if not isinstance(raw_page, Mapping):
                valid = False
                continue
            page = dict(raw_page)
            candidate = (wiki_dir / str(page.get("path") or "")).resolve(strict=False)
            try:
                candidate.relative_to(wiki_dir.resolve(strict=False))
            except ValueError:
                valid = False
                continue
            if not candidate.is_file():
                valid = False
                continue
            content = read_text_value(candidate)
            frontmatter, _body = parse_frontmatter(content)
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            lifecycle = conn.execute(
                """SELECT page.page_id, page.current_revision, page.current_path,
                          page.lifecycle_state, mutation.mutation_id,
                          mutation.page_path, mutation.content_sha256,
                          mutation.tombstone
                   FROM wiki_pages AS page
                   JOIN wiki_mutations AS mutation
                     ON mutation.page_id=page.page_id
                    AND mutation.page_revision=page.current_revision
                   WHERE mutation.mutation_id=?""",
                (str(page.get("mutation_id") or ""),),
            ).fetchone()
            if lifecycle is None or any(
                (
                    str(fm_get(frontmatter, "cognition_episode_revision_id") or "")
                    != revision.revision_id,
                    str(lifecycle["page_id"]) != str(page.get("page_id") or ""),
                    str(lifecycle["current_revision"]) != str(page.get("page_revision") or ""),
                    Path(str(lifecycle["current_path"])).resolve(strict=False) != candidate,
                    str(lifecycle["lifecycle_state"]) != "active",
                    Path(str(lifecycle["page_path"])).resolve(strict=False) != candidate,
                    str(lifecycle["content_sha256"]) != content_hash,
                    "sha256:" + content_hash != str(page.get("content_sha256") or ""),
                    bool(lifecycle["tombstone"]),
                )
            ):
                valid = False
        after_hash = _audit_hash({"revision_id": revision.revision_id, "pages": pages})
        return valid and all(
            (
                str(effect["revision_id"]) == revision.revision_id,
                str(effect["manifest_hash"]) == after_hash,
                str(effect["before_hash"]) == before_hash,
                str(effect["after_hash"]) == after_hash,
                str(receipt["before_hash"]) == before_hash,
                str(receipt["after_hash"]) == after_hash,
            )
        )


def _reachable_from_raw(
    edges: Sequence[tuple[str, str]],
    raw_ids: Sequence[str],
) -> set[str]:
    reverse: dict[str, set[str]] = {}
    for source, target in edges:
        reverse.setdefault(target, set()).add(source)
    reached = set(raw_ids)
    pending = list(raw_ids)
    while pending:
        target = pending.pop()
        for source in reverse.get(target, set()):
            if source not in reached:
                reached.add(source)
                pending.append(source)
    return reached


def _evidence_effect_matches(
    *,
    database_dir: Path,
    revision: Any,
    plan: Mapping[str, Any],
    effect_id: str,
    before_hash: str,
    receipt: Mapping[str, Any],
) -> tuple[bool, bool, bool]:
    db_path = database_dir / "evidence_graph.db"
    if not db_path.is_file():
        return False, False, False
    with _connect_read_only(db_path) as conn:
        if not {
            "evidence_nodes",
            "evidence_edges",
            "cognition_episode_projection_effects",
            "cognition_episode_projection_omissions",
        } <= _tables(conn):
            return False, False, False
        effect = conn.execute(
            "SELECT * FROM cognition_episode_projection_effects WHERE effect_id=?",
            (effect_id,),
        ).fetchone()
        expected_after = str(plan["evidence_manifest_hash"])
        if effect is None or any(
            (
                str(effect["revision_id"]) != revision.revision_id,
                str(effect["manifest_hash"]) != expected_after,
                str(effect["before_hash"]) != before_hash,
                str(effect["after_hash"]) != expected_after,
                int(effect["node_count"]) != len(plan["nodes"]),
                int(effect["edge_count"]) != len(plan["evidence_edges"]),
                int(effect["omission_count"]) != len(plan["omissions"]),
                str(effect["access_control_hash"]) != str(plan["access_control_hash"]),
                str(receipt["before_hash"]) != before_hash,
                str(receipt["after_hash"]) != expected_after,
            )
        ):
            return False, False, False
        valid = True
        for node in plan["nodes"]:
            row = conn.execute(
                """SELECT node_type, metadata, access_control
                   FROM evidence_nodes WHERE id=?""",
                (str(node["id"]),),
            ).fetchone()
            metadata = _parse_json_mapping(row["metadata"]) if row is not None else None
            acl = _parse_json_mapping(row["access_control"]) if row is not None else None
            if (
                row is None
                or str(row["node_type"]) != str(node["node_type"])
                or metadata != node["metadata"]
                or acl != plan["access_control"]
            ):
                valid = False
        actual_edges: list[tuple[str, str]] = []
        for edge in plan["evidence_edges"]:
            rows = conn.execute(
                """SELECT confidence, evidence, access_control
                   FROM evidence_edges
                   WHERE source_id=? AND target_id=? AND relation_type=?""",
                (
                    str(edge["source_id"]),
                    str(edge["target_id"]),
                    str(edge["relation_type"]),
                ),
            ).fetchall()
            if len(rows) != 1:
                valid = False
                continue
            row = rows[0]
            try:
                evidence = json.loads(str(row["evidence"]))
                acl = json.loads(str(row["access_control"]))
            except (json.JSONDecodeError, TypeError):
                valid = False
                continue
            if (
                float(row["confidence"]) != float(edge["confidence"])
                or evidence != edge["evidence"]
                or acl != plan["access_control"]
            ):
                valid = False
            actual_edges.append((str(edge["source_id"]), str(edge["target_id"])))
        omission_gap = False
        actual_omissions = conn.execute(
            """SELECT omission_id, field_name, entry_index, disposition,
                      reason_code, payload_hash
               FROM cognition_episode_projection_omissions
               WHERE revision_id=? ORDER BY omission_id""",
            (revision.revision_id,),
        ).fetchall()
        expected_omissions = sorted(plan["omissions"], key=lambda item: item["omission_id"])
        if len(actual_omissions) != len(expected_omissions):
            omission_gap = True
        else:
            for row, expected in zip(actual_omissions, expected_omissions):
                if tuple(row) != (
                    str(expected["omission_id"]),
                    str(expected["field_name"]),
                    int(expected["entry_index"]),
                    "omitted",
                    str(expected["reason_code"]),
                    str(expected["payload_hash"]),
                ):
                    omission_gap = True
        reached = _reachable_from_raw(actual_edges, plan["source_span_ids"])
        required_reachable = set(plan["observation_ids"]) | set(plan["evidenced_entry_ids"])
        lineage_gap = bool(required_reachable - reached)
        return valid and not omission_gap and not lineage_gap, omission_gap, lineage_gap


def _cognitive_graph_effect_matches(
    *,
    cognitive_graph_db_path: Path,
    revision: Any,
    plan: Mapping[str, Any],
    effect_id: str,
    before_hash: str,
    receipt: Mapping[str, Any],
) -> tuple[bool, bool]:
    db_path = cognitive_graph_db_path
    if not db_path.is_file():
        return False, False
    with _connect_read_only(db_path) as conn:
        if not {
            "cognitive_relations",
            "cognition_episode_projection_effects",
        } <= _tables(conn):
            return False, False
        effect = conn.execute(
            "SELECT * FROM cognition_episode_projection_effects WHERE effect_id=?",
            (effect_id,),
        ).fetchone()
        expected_after = str(plan["graph_manifest_hash"])
        if effect is None or any(
            (
                str(effect["revision_id"]) != revision.revision_id,
                str(effect["manifest_hash"]) != expected_after,
                str(effect["before_hash"]) != before_hash,
                str(effect["after_hash"]) != expected_after,
                int(effect["relation_count"]) != len(plan["graph_relations"]),
                str(effect["access_control_hash"]) != str(plan["access_control_hash"]),
                str(receipt["before_hash"]) != before_hash,
                str(receipt["after_hash"]) != expected_after,
            )
        ):
            return False, False
        valid = True
        expected_relation_acl = _expected_graph_acl(plan["access_control"])
        actual_edges: list[tuple[str, str]] = []
        for relation in plan["graph_relations"]:
            rows = conn.execute(
                """SELECT strength, confidence, source_layer, target_layer,
                          access_control
                   FROM cognitive_relations
                   WHERE source=? AND target=? AND relation_type=? AND stale=0""",
                (
                    str(relation["source"]),
                    str(relation["target"]),
                    str(relation["relation_type"]),
                ),
            ).fetchall()
            if len(rows) != 1:
                valid = False
                continue
            row = rows[0]
            try:
                acl = json.loads(str(row["access_control"]))
            except (json.JSONDecodeError, TypeError):
                valid = False
                continue
            if any(
                (
                    float(row["strength"]) != float(relation["strength"]),
                    float(row["confidence"]) != float(relation["confidence"]),
                    str(row["source_layer"]) != str(relation["source_layer"]),
                    str(row["target_layer"]) != str(relation["target_layer"]),
                    acl != expected_relation_acl,
                )
            ):
                valid = False
            actual_edges.append((str(relation["source"]), str(relation["target"])))
        reached = _reachable_from_raw(actual_edges, plan["source_span_ids"])
        required_reachable = set(plan["observation_ids"]) | set(plan["evidenced_entry_ids"])
        lineage_gap = bool(required_reachable - reached)
        return valid and not lineage_gap, lineage_gap


def _target_effect_matches(
    *,
    database_dir: Path,
    cognitive_graph_db_path: Path,
    wiki_projection_db_path: Path,
    wiki_dir: Path,
    revision: Any,
    plan: Mapping[str, Any],
    command: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> tuple[bool, bool, bool, bool]:
    consumer = str(command["consumer_id"])
    effect_id = _audit_effect_id(str(command["command_id"]), consumer)
    before_hash = _audit_before_hash(revision.revision_id, consumer)
    if str(receipt["target_effect_id"]) != effect_id:
        return False, False, False, False
    if consumer == "wiki":
        return (
            _wiki_effect_matches(
                wiki_projection_db_path=wiki_projection_db_path,
                wiki_dir=wiki_dir,
                revision=revision,
                effect_id=effect_id,
                before_hash=before_hash,
                receipt=receipt,
            ),
            False,
            False,
            False,
        )
    if consumer == "knowledge_graph":
        matched, omission_gap, lineage_gap = _evidence_effect_matches(
            database_dir=database_dir,
            revision=revision,
            plan=plan,
            effect_id=effect_id,
            before_hash=before_hash,
            receipt=receipt,
        )
        return matched, omission_gap, False, lineage_gap
    matched, lineage_gap = _cognitive_graph_effect_matches(
        cognitive_graph_db_path=cognitive_graph_db_path,
        revision=revision,
        plan=plan,
        effect_id=effect_id,
        before_hash=before_hash,
        receipt=receipt,
    )
    return matched, False, not matched, lineage_gap


def _required_runtime_schema(
    database_dir: Path,
    event_db_path: Path,
    cognitive_graph_db_path: Path,
    wiki_projection_db_path: Path,
) -> list[str]:
    required_by_path = {
        database_dir
        / _STATE_DB: {
            "cognitive_state_revisions",
            "cognitive_state_outbox",
            "cognitive_state_effect_receipts",
            "cognitive_data_consumptions",
            "cognitive_state_migration_quarantine",
        },
        database_dir
        / "evidence_graph.db": {
            "evidence_nodes",
            "evidence_edges",
            "cognition_episode_projection_effects",
            "cognition_episode_projection_omissions",
        },
        cognitive_graph_db_path: {
            "cognitive_relations",
            "cognition_episode_projection_effects",
        },
        wiki_projection_db_path: {
            "wiki_pages",
            "wiki_mutations",
            "cognition_episode_projection_effects",
        },
        event_db_path: {
            "events",
            "dead_letters",
            "event_trace_claims",
            "handler_receipts",
        },
    }
    findings: list[str] = []
    for path, required in required_by_path.items():
        if not path.is_file():
            findings.append(f"runtime_database_missing:{path.name}")
            continue
        try:
            with _connect_read_only(path) as conn:
                missing = required - _tables(conn)
                findings.extend(
                    f"runtime_table_missing:{path.name}:{table}" for table in sorted(missing)
                )
                if path == event_db_path and "handler_receipts" not in missing:
                    index = conn.execute("""SELECT sql FROM sqlite_master
                           WHERE type='index'
                             AND name='uq_cognition_episode_terminal_handler_receipt'""").fetchone()
                    normalized = "".join(str(index[0] or "").lower().split()) if index else ""
                    if (
                        "createuniqueindex" not in normalized
                        or "event_type='cognition_episode_committed'" not in normalized
                        or "dispositionin('ack','noop')" not in normalized
                    ):
                        findings.append(
                            "runtime_index_missing:events.db:"
                            "uq_cognition_episode_terminal_handler_receipt"
                        )
        except (OSError, sqlite3.Error) as exc:
            findings.append(f"runtime_schema_error:{path.name}:{type(exc).__name__}:{exc}")
    return findings


def build_report(
    *,
    database_dir: Path,
    event_db_path: Path,
    wiki_dir: Path,
    cognitive_graph_db_path: Path | None = None,
    wiki_projection_db_path: Path | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Independently audit every cognition-episode revision and durable effect."""

    repo_root = repo_root or Path(__file__).resolve().parents[2]
    database_dir = Path(database_dir)
    event_db_path = Path(event_db_path)
    wiki_dir = Path(wiki_dir)
    cognitive_graph_db_path = Path(cognitive_graph_db_path or database_dir / "cognitive_graph.db")
    wiki_projection_db_path = Path(wiki_projection_db_path or database_dir / "wiki_projection.db")
    state_db = database_dir / _STATE_DB
    gaps = {
        "static_contract_gap": 0,
        "schema_gap": 0,
        "consumer_command_gap": 0,
        "command_payload_gap": 0,
        "receipt_gap": 0,
        "target_effect_gap": 0,
        "event_missing_gap": 0,
        "event_duplicate_gap": 0,
        "event_terminal_gap": 0,
        "event_for_omitted_revision_gap": 0,
        "event_payload_gap": 0,
        "handler_terminal_gap": 0,
        "false_ack_gap": 0,
        "omission_gap": 0,
        "relation_gap": 0,
        "lineage_gap": 0,
        "integrity_gap": 0,
    }
    findings = _static_findings(repo_root)
    gaps["static_contract_gap"] = len(findings)
    schema_findings = _required_runtime_schema(
        database_dir,
        event_db_path,
        cognitive_graph_db_path,
        wiki_projection_db_path,
    )
    findings.extend(schema_findings)
    gaps["schema_gap"] = len(schema_findings)
    runtime = {
        "initialized": not schema_findings,
        "episode_count": 0,
        "projection_command_count": 0,
        "committed_effect_receipt_count": 0,
        "intentional_omission_revision_count": 0,
        "intentional_omission_receipt_count": 0,
        "event_count": 0,
    }
    if schema_findings:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "runtime": runtime,
            "gaps": gaps,
            "findings": findings,
        }

    events_by_revision, claims, handler_receipts = _load_events(event_db_path)
    runtime["event_count"] = sum(len(values) for values in events_by_revision.values())
    receipts_by_revision_consumer: dict[tuple[str, str], dict] = {}
    try:
        with _connect_read_only(state_db) as conn:
            revisions = [
                _revision_from_row(row)
                for row in conn.execute("""SELECT * FROM cognitive_state_revisions
                       WHERE object_type='cognition_episode'
                       ORDER BY created_at, revision_id""").fetchall()
            ]
            commands = [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM cognitive_state_outbox
                       WHERE command_type=? ORDER BY revision_id, consumer_id""",
                    (COMMAND_TYPE,),
                ).fetchall()
            ]
            receipts = [
                dict(row)
                for row in conn.execute(
                    """SELECT r.*, c.outcome AS consumption_outcome,
                              c.status AS consumption_status,
                              c.target_effect_id AS consumption_target_effect_id,
                              c.before_hash AS consumption_before_hash,
                              c.after_hash AS consumption_after_hash,
                              c.metadata AS consumption_metadata
                       FROM cognitive_state_effect_receipts AS r
                       JOIN cognitive_state_outbox AS o ON o.command_id=r.command_id
                       JOIN cognitive_data_consumptions AS c
                         ON c.consumption_id=r.consumption_id
                       WHERE o.command_type=?""",
                    (COMMAND_TYPE,),
                ).fetchall()
            ]
            quarantines = {
                str(row["source_key"]): dict(row)
                for row in conn.execute("""SELECT * FROM cognitive_state_migration_quarantine
                       WHERE source_table='cognitive_state_revisions'""").fetchall()
            }
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            gaps["integrity_gap"] += 1
        for path in (
            database_dir / "evidence_graph.db",
            cognitive_graph_db_path,
            wiki_projection_db_path,
            event_db_path,
        ):
            with _connect_read_only(path) as conn:
                gaps["integrity_gap"] += int(
                    str(conn.execute("PRAGMA integrity_check").fetchone()[0]) != "ok"
                )

        runtime["episode_count"] = len(revisions)
        runtime["projection_command_count"] = len(commands)
        runtime["committed_effect_receipt_count"] = sum(
            str(receipt["status"]) == "committed" for receipt in receipts
        )
        commands_by_revision: dict[str, list[dict]] = {}
        for command in commands:
            commands_by_revision.setdefault(str(command["revision_id"]), []).append(command)
        receipt_by_command = {str(row["command_id"]): row for row in receipts}
        for receipt in receipts:
            receipts_by_revision_consumer[
                (str(receipt["revision_id"]), str(receipt["consumer_id"]))
            ] = receipt

        known_revision_ids = {revision.revision_id for revision in revisions}
        gaps["event_payload_gap"] += sum(
            len(values)
            for revision_id, values in events_by_revision.items()
            if not revision_id or revision_id not in known_revision_ids
        )
        gaps["consumer_command_gap"] += sum(
            len(values)
            for revision_id, values in commands_by_revision.items()
            if revision_id not in known_revision_ids
        )

        handlers_by_trace: dict[str, list[dict]] = {}
        for handler in handler_receipts:
            handlers_by_trace.setdefault(str(handler["trace_id"]), []).append(handler)

        for revision in revisions:
            plan = _audit_projection_plan(revision)
            expected_payload = _audit_event_payload(revision, plan)
            expected_trace = _audit_trace_id(revision.revision_id)
            episode_commands = commands_by_revision.get(revision.revision_id, [])
            by_consumer: dict[str, list[dict]] = {}
            for command in episode_commands:
                by_consumer.setdefault(str(command["consumer_id"]), []).append(command)
            gaps["consumer_command_gap"] += sum(
                len(by_consumer.get(consumer, [])) != 1 for consumer in CONSUMERS
            )
            gaps["consumer_command_gap"] += sum(
                consumer not in CONSUMERS for consumer in by_consumer
            )
            receipts_for_consumers: dict[str, dict] = {}
            for consumer in CONSUMERS:
                values = by_consumer.get(consumer, [])
                if len(values) != 1:
                    continue
                command = values[0]
                expected_command_payload = {
                    "primary_revision_id": revision.revision_id,
                    "object_type": "cognition_episode",
                    "object_id": revision.object_id,
                }
                try:
                    command_payload = json.loads(str(command["payload_json"]))
                except (json.JSONDecodeError, TypeError):
                    command_payload = None
                expected_command_id = _audit_command_id(
                    revision.revision_id,
                    consumer,
                    expected_command_payload,
                )
                if any(
                    (
                        command_payload != expected_command_payload,
                        str(command["payload_hash"]) != _audit_hash(expected_command_payload),
                        str(command["command_id"]) != expected_command_id,
                        str(command["event_id"]) != revision.source_event_id,
                    )
                ):
                    gaps["command_payload_gap"] += 1
                candidate = receipt_by_command.get(str(command["command_id"]))
                if candidate is not None:
                    receipts_for_consumers[consumer] = candidate

            episode_events = events_by_revision.get(revision.revision_id, [])
            if _valid_revision_omission(
                revision,
                receipts_for_consumers,
                quarantines.get(revision.revision_id),
            ):
                runtime["intentional_omission_revision_count"] += 1
                runtime["intentional_omission_receipt_count"] += len(receipts_for_consumers)
                gaps["event_for_omitted_revision_gap"] += len(episode_events)
                continue

            if any(
                str(receipt.get("status") or "") == "intentional_skip"
                for receipt in receipts_for_consumers.values()
            ):
                gaps["omission_gap"] += 1
            for consumer in CONSUMERS:
                values = by_consumer.get(consumer, [])
                if len(values) != 1:
                    continue
                command = values[0]
                candidate_receipt = receipt_by_command.get(str(command["command_id"]))
                if candidate_receipt is None or str(candidate_receipt["status"]) != "committed":
                    gaps["receipt_gap"] += 1
                    continue
                receipt = candidate_receipt
                if any(
                    (
                        str(receipt["revision_id"]) != revision.revision_id,
                        str(receipt["event_id"]) != revision.source_event_id,
                        str(receipt["consumer_id"]) != consumer,
                        str(receipt["consumption_status"]) != "committed",
                        str(receipt["consumption_target_effect_id"])
                        != str(receipt["target_effect_id"]),
                        str(receipt["consumption_before_hash"]) != str(receipt["before_hash"]),
                        str(receipt["consumption_after_hash"]) != str(receipt["after_hash"]),
                    )
                ):
                    gaps["receipt_gap"] += 1
                    continue
                matched, omission_gap, relation_gap, lineage_gap = _target_effect_matches(
                    database_dir=database_dir,
                    cognitive_graph_db_path=cognitive_graph_db_path,
                    wiki_projection_db_path=wiki_projection_db_path,
                    wiki_dir=wiki_dir,
                    revision=revision,
                    plan=plan,
                    command=command,
                    receipt=receipt,
                )
                gaps["target_effect_gap"] += int(not matched)
                gaps["omission_gap"] += int(omission_gap)
                gaps["relation_gap"] += int(relation_gap)
                gaps["lineage_gap"] += int(lineage_gap)

            gaps["event_missing_gap"] += int(len(episode_events) == 0)
            gaps["event_duplicate_gap"] += max(0, len(episode_events) - 1)
            for event in episode_events:
                payload = event["payload"]
                claim = claims.get(event["trace_id"])
                if any(
                    (
                        set(payload) != _ALLOWED_EVENT_KEYS,
                        payload != expected_payload,
                        event["trace_id"] != expected_trace,
                        event["source"] != "cognitive_state_store",
                        event["timestamp"] != revision.created_at,
                        claim is None,
                        claim is not None and str(claim["source"]) != "cognitive_state_store",
                        claim is not None
                        and str(claim["payload_fingerprint"])
                        != _event_fingerprint("cognitive_state_store", expected_payload),
                    )
                ):
                    gaps["event_payload_gap"] += 1
                gaps["event_terminal_gap"] += int(
                    event["location"] != "events" or event["status"] != "done"
                )

            terminal_by_consumer: dict[str, list[dict]] = {}
            for handler in handlers_by_trace.get(expected_trace, []):
                if str(handler["disposition"]) in {"ack", "noop"}:
                    terminal_by_consumer.setdefault(str(handler["consumer"]), []).append(handler)
            for consumer in CONSUMERS:
                terminal = terminal_by_consumer.get(consumer, [])
                gaps["handler_terminal_gap"] += int(len(terminal) != 1)
                for handler in terminal:
                    target_receipt = receipts_by_revision_consumer.get(
                        (revision.revision_id, consumer)
                    )
                    output = _parse_json_mapping(handler["output_json"])
                    if (
                        target_receipt is None
                        or str(target_receipt.get("status") or "") != "committed"
                        or output is None
                        or str(output.get("effect_id") or "")
                        != str(target_receipt.get("target_effect_id") or "")
                        or str(output.get("before_hash") or "")
                        != str(target_receipt.get("before_hash") or "")
                        or str(output.get("after_hash") or "")
                        != str(target_receipt.get("after_hash") or "")
                    ):
                        gaps["false_ack_gap"] += 1
            gaps["handler_terminal_gap"] += sum(
                len(values)
                for consumer, values in terminal_by_consumer.items()
                if consumer not in CONSUMERS
            )
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        gaps["schema_gap"] += 1
        findings.append(f"runtime_audit_error:{type(exc).__name__}:{exc}")

    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not any(gaps.values()),
        "runtime": runtime,
        "gaps": gaps,
        "findings": findings,
    }
