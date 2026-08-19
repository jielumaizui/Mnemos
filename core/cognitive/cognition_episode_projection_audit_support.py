"""Independent projection-shape helpers for cognition dispatch auditing."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

_FIELD_NODE_TYPES = {
    "assumptions": "belief",
    "hypotheses": "prediction",
    "decision": "decision",
    "actions": "action",
    "outcomes": "outcome",
}


def node_type(field_name: str) -> str:
    return _FIELD_NODE_TYPES.get(field_name, "claim")


def span_identity(span: Mapping[str, Any]) -> tuple[Any, ...]:
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


def evidence_identity(evidence: Mapping[str, Any]) -> tuple[Any, ...]:
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


def dedupe(values: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> list[dict]:
    result: list[dict] = []
    seen: set[tuple[str, ...]] = set()
    for raw_value in values:
        value = dict(raw_value)
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
        raise ValueError("audit source evidence does not contain an exact span")
    return f"{source_revision_id}#{start}:{end}"


def source_provenance(revision: Any, evidence_refs: Sequence[Mapping[str, Any]]) -> dict:
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


def source_span_provenance(revision: Any, span: Mapping[str, Any]) -> dict:
    source_revision_id = str(span.get("revision_id") or "").strip()
    start = int(span.get("span_start", -1))
    end = int(span.get("span_end", -1))
    if not source_revision_id or start < 0 or end <= start:
        raise ValueError("audit Raw source span is incomplete")
    return {
        "projection_revision_id": revision.revision_id,
        "source_revision_id": source_revision_id,
        "source_revision_ids": [source_revision_id],
        "source_span_ids": [f"{source_revision_id}#{start}:{end}"],
    }


def _edge_provenance(
    revision: Any,
    *metadata_values: Mapping[str, Any],
    quote: str = "",
) -> dict:
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
    return {
        "schema_version": "mnemos.cognition_source_provenance.v1",
        "projection_revision_id": revision.revision_id,
        "source_revision_id": (
            source_revisions[0] if len(source_revisions) == 1 else revision.source_revision_id
        ),
        "source_revision_ids": source_revisions,
        "source_span_ids": source_spans,
        "quote": str(quote),
    }


def edge(
    revision: Any,
    source: str,
    target: str,
    relation: str,
    *metadata_values: Mapping[str, Any],
    quote: str = "",
) -> dict[str, Any]:
    return {
        "source_id": source,
        "target_id": target,
        "relation_type": relation,
        "confidence": 1.0,
        "evidence": [
            "deterministic projection from committed cognition episode IDs",
            _edge_provenance(revision, *metadata_values, quote=quote),
        ],
    }


def relation(
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


__all__ = [
    "dedupe",
    "edge",
    "evidence_identity",
    "node_type",
    "relation",
    "source_provenance",
    "source_span_provenance",
    "span_identity",
]
