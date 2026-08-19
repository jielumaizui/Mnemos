"""Typed, ACL-first retrieval over canonical cognitive state.

The canonical state store remains the owner.  This module only hydrates bodies
that ``CognitiveStateStore.authorized_current_revisions`` has admitted for the
authenticated principal and explicit read purpose.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognition_episode_contract import COGNITION_EPISODE_FIELDS
from core.cognitive.state_contract import CognitiveStateRevision
from core.cognitive.state_contract_schema import COGNITIVE_OBJECT_TYPES
from core.cognitive.state_store import CognitiveStateStore
from core.db_utils import render_sql
from core.cognitive.access_control import (
    authorize_cognitive_access,
    validate_cognitive_access_envelope,
)


@dataclass(frozen=True)
class CognitiveSearchHit:
    hit_id: str
    channel: str
    object_type: str
    object_id: str
    revision_id: str
    title: str
    snippet: str
    score: float
    confidence: float
    matched_field: str
    matched_terms: tuple[str, ...]
    source_revision_id: str
    source_span_ids: tuple[str, ...]
    acl_decision: str
    scope_type: str
    scope_id: str
    supersedes_revision_id: str
    is_current: bool = True


@dataclass(frozen=True)
class _SearchableField:
    path: str
    text: str
    confidence: float
    source_span_ids: tuple[str, ...]
    revision_id: str = ""
    source_revision_id: str = ""


COGNITIVE_SEARCH_PURPOSES = {
    object_type: (
        "belief_read"
        if object_type == "belief_revision"
        else (
            "prediction_read"
            if object_type == "prediction_record"
            else (
                "calibration_internal"
                if object_type == "calibration_record"
                else "cognitive_state_read"
            )
        )
    )
    for object_type in COGNITIVE_OBJECT_TYPES
}


class CognitiveSearch:
    """Search typed cognition without depending on a Wiki projection.

    Every configured channel performs header-only authorization before any
    searchable body is hydrated.  Channels recall independently and are fused
    only after their authorized candidate sets have been ranked.
    """

    PURPOSE = "cognitive_state_read"
    GRAPH_PURPOSE = "cognitive_graph_read"
    EVIDENCE_PURPOSE = "evidence_graph_read"

    def __init__(
        self,
        *,
        state_db: Path | str,
        cognitive_graph_db: Path | str | None = None,
        evidence_graph_db: Path | str | None = None,
    ):
        self.state_db = Path(state_db)
        self.cognitive_graph_db = (
            Path(cognitive_graph_db) if cognitive_graph_db is not None else None
        )
        self.evidence_graph_db = Path(evidence_graph_db) if evidence_graph_db is not None else None

    def search(
        self,
        query: str,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        limit: int = 10,
    ) -> tuple[list[CognitiveSearchHit], dict[str, Any]]:
        normalized_query = str(query or "").strip()
        requested_limit = max(1, int(limit))
        if not normalized_query:
            report = _empty_report("query_required")
            report["channels"] = {}
            return [], report

        terms = _query_terms(normalized_query)
        oversample = max(20, requested_limit * 4)
        channel_hits: dict[str, list[CognitiveSearchHit]] = {}
        channel_reports: dict[str, dict[str, Any]] = {}

        state_hits, state_report = self._search_state(
            normalized_query,
            terms=terms,
            principal=principal,
            narrowing=narrowing,
        )
        channel_hits["cognitive_state"] = state_hits
        channel_reports["cognitive_state"] = state_report

        if self.cognitive_graph_db is not None:
            graph_hits, graph_report = self._search_cognitive_graph(
                normalized_query,
                terms=terms,
                principal=principal,
                narrowing=narrowing,
            )
            channel_hits["cognitive_graph"] = graph_hits
            channel_reports["cognitive_graph"] = graph_report

        if self.evidence_graph_db is not None:
            evidence_hits, evidence_report = self._search_evidence_graph(
                normalized_query,
                terms=terms,
                principal=principal,
                narrowing=narrowing,
            )
            channel_hits["evidence_graph"] = evidence_hits
            channel_reports["evidence_graph"] = evidence_report

        fused: list[CognitiveSearchHit] = []
        for channel, hits in channel_hits.items():
            ranked = sorted(
                hits,
                key=lambda hit: (-hit.score, hit.object_id, hit.matched_field),
            )[:oversample]
            channel_reports[channel]["returned_count"] = len(ranked)
            for rank, hit in enumerate(ranked, start=1):
                reciprocal_rank = 1.0 / rank
                fused.append(
                    replace(
                        hit,
                        score=min(1.0, hit.score * 0.85 + reciprocal_rank * 0.15),
                    )
                )

        fused.sort(
            key=lambda hit: (
                -hit.score,
                -hit.confidence,
                hit.channel,
                hit.object_id,
                hit.matched_field,
            )
        )
        report = _combine_channel_reports(channel_reports)
        report["returned_count"] = min(len(fused), requested_limit)
        return fused[:requested_limit], report

    def authorize_identity(
        self,
        *,
        channel: str,
        object_type: str,
        object_id: str,
        revision_id: str,
        source_revision_id: str,
        source_span_ids: Sequence[str],
        matched_field: str,
        acl_decision: str,
        is_current: bool,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
    ) -> tuple[bool, str]:
        """Reauthorize a typed result immediately before application exposure."""

        if acl_decision != "authorized":
            return False, "reported_acl_not_authorized"
        if channel == "cognitive_state":
            if not self.state_db.is_file():
                return False, "state_store_unavailable"
            try:
                revisions, report = CognitiveStateStore(self.state_db).authorized_current_revisions(
                    principal=principal,
                    narrowing=narrowing,
                    purpose=COGNITIVE_SEARCH_PURPOSES.get(object_type, self.PURPOSE),
                    object_type=object_type,
                    object_id=object_id,
                )
            except (FileNotFoundError, OSError, RuntimeError, ValueError, sqlite3.Error):
                return False, "state_store_invalid"
            for revision in revisions:
                if (
                    revision.revision_id == revision_id
                    and revision.source_revision_id == source_revision_id
                    and is_current
                ):
                    field = next(
                        (
                            candidate
                            for candidate in _searchable_fields(revision)
                            if candidate.path == matched_field
                        ),
                        None,
                    )
                    if field is None or tuple(source_span_ids) != field.source_span_ids:
                        return False, "source_trace_mismatch"
                    return True, "authorized"
            denied = report.get("denied_by_reason")
            if isinstance(denied, Mapping) and denied:
                return False, str(next(iter(denied)))
            return False, "not_current"

        if channel == "cognitive_graph":
            table = {
                "cognitive_relation": ("cognitive_relations", "id", "stale = 0"),
                "canonical_node": ("canonical_nodes", "canonical_id", "1 = 1"),
            }.get(object_type)
            if table is None:
                return False, "object_type_mismatch"
            row, access, reason = _authorized_identity_row(
                self.cognitive_graph_db,
                table=table[0],
                id_column=table[1],
                object_id=object_id,
                extra_where=table[2],
                principal=principal,
                narrowing=narrowing,
                purpose=self.GRAPH_PURPOSE,
            )
            if row is None or not access:
                return False, reason
            node_fields, _trace_report = self._authorized_graph_node_fields(
                principal=principal,
                narrowing=narrowing,
            )
            fields = (
                _graph_relation_fields(row, node_fields)
                if object_type == "cognitive_relation"
                else _canonical_node_fields(row, node_fields)
            )
            current = next((field for field in fields if field.path == matched_field), None)
            if current is None:
                return False, "source_trace_unavailable"
            if (
                revision_id != current.revision_id
                or source_revision_id != current.source_revision_id
                or tuple(source_span_ids) != current.source_span_ids
            ):
                return False, "identity_mismatch"
            return True, "authorized"

        if channel == "evidence_graph":
            table = (
                ("evidence_edges", "id", "1 = 1")
                if object_type == "evidence_edge"
                else ("evidence_nodes", "id", "1 = 1")
            )
            row, access, reason = _authorized_identity_row(
                self.evidence_graph_db,
                table=table[0],
                id_column=table[1],
                object_id=object_id,
                extra_where=table[2],
                principal=principal,
                narrowing=narrowing,
                purpose=self.EVIDENCE_PURPOSE,
            )
            if row is None or not access:
                return False, reason
            if table[0] == "evidence_nodes" and str(row["node_type"]) != object_type:
                return False, "object_type_mismatch"
            current_revision, current_source, current_spans = _evidence_row_provenance(
                row,
                object_type=object_type,
                object_id=object_id,
                matched_field=matched_field,
            )
            if (
                current_revision != revision_id
                or current_source != source_revision_id
                or current_spans != tuple(source_span_ids)
            ):
                return False, "identity_mismatch"
            return True, "authorized"

        return False, "channel_unsupported"

    def _search_state(
        self,
        query: str,
        *,
        terms: tuple[str, ...],
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
    ) -> tuple[list[CognitiveSearchHit], dict[str, Any]]:
        if not self.state_db.is_file():
            return [], _empty_report("state_store_unavailable")
        if not terms:
            return [], _empty_report("query_terms_required")

        try:
            store = CognitiveStateStore(self.state_db)
            revisions, access = store.authorized_current_revisions_by_purpose(
                principal=principal,
                narrowing=narrowing,
                purposes_by_type=COGNITIVE_SEARCH_PURPOSES,
            )
        except (FileNotFoundError, OSError, RuntimeError, ValueError, sqlite3.Error):
            return [], _empty_report("state_store_invalid")

        hits: list[CognitiveSearchHit] = []
        for revision in revisions:
            best = _best_field(revision, terms, query)
            if best is None:
                continue
            field, score, matched_terms = best
            hits.append(
                CognitiveSearchHit(
                    hit_id=f"cognitive_state:{revision.revision_id}:{field.path}",
                    channel="cognitive_state",
                    object_type=revision.object_type,
                    object_id=revision.object_id,
                    revision_id=revision.revision_id,
                    title=f"{revision.object_type}:{revision.object_id}",
                    snippet=_snippet_around_match(field.text, matched_terms),
                    score=score,
                    confidence=field.confidence,
                    matched_field=field.path,
                    matched_terms=matched_terms,
                    source_revision_id=revision.source_revision_id,
                    source_span_ids=field.source_span_ids,
                    acl_decision="authorized",
                    scope_type=revision.scope_type,
                    scope_id=revision.scope_id,
                    supersedes_revision_id=revision.supersedes_revision_id,
                )
            )

        hits.sort(key=lambda hit: (-hit.score, hit.revision_id, hit.matched_field))
        report = dict(access)
        report["matched_count"] = len(hits)
        report["returned_count"] = len(hits)
        return hits, report

    def _search_cognitive_graph(
        self,
        query: str,
        *,
        terms: tuple[str, ...],
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
    ) -> tuple[list[CognitiveSearchHit], dict[str, Any]]:
        db_path = self.cognitive_graph_db
        if db_path is None or not db_path.is_file():
            return [], _empty_report("cognitive_graph_unavailable")
        if not terms:
            return [], _empty_report("query_terms_required")
        node_fields, trace_report = self._authorized_graph_node_fields(
            principal=principal,
            narrowing=narrowing,
        )
        try:
            with _read_only_connection(db_path) as conn:
                relation_rows, relation_acls, relation_report = _authorized_rows(
                    conn,
                    table="cognitive_relations",
                    id_column="id",
                    header_where="stale = 0",
                    order_by="updated_at DESC, id",
                    principal=principal,
                    narrowing=narrowing,
                    purpose=self.GRAPH_PURPOSE,
                )
                node_rows, node_acls, node_report = _authorized_rows(
                    conn,
                    table="canonical_nodes",
                    id_column="canonical_id",
                    header_where="1 = 1",
                    order_by="updated_at DESC, canonical_id",
                    principal=principal,
                    narrowing=narrowing,
                    purpose=self.GRAPH_PURPOSE,
                )
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            return [], _empty_report("cognitive_graph_invalid")

        hits: list[CognitiveSearchHit] = []
        for row in relation_rows:
            relation_id = str(row["id"])
            fields = _graph_relation_fields(row, node_fields)
            best = _best_searchable_field(fields, terms, query)
            if best is None:
                continue
            field, score, matched_terms = best
            access = relation_acls[relation_id]
            scope = access["scope"]
            hits.append(
                CognitiveSearchHit(
                    hit_id=f"cognitive_graph:relation:{relation_id}:{field.path}",
                    channel="cognitive_graph",
                    object_type="cognitive_relation",
                    object_id=relation_id,
                    revision_id=field.revision_id,
                    title=f"{row['source']} —{row['relation_type']}→ {row['target']}",
                    snippet=_snippet_around_match(field.text, matched_terms),
                    score=(score * (1.0 if str(row["relation_type"]) == "contains" else 0.98)),
                    confidence=_confidence(row["confidence"], 0.5),
                    matched_field=field.path,
                    matched_terms=matched_terms,
                    source_revision_id=field.source_revision_id,
                    source_span_ids=field.source_span_ids,
                    acl_decision="authorized",
                    scope_type=str(scope["scope_type"]),
                    scope_id=str(scope["scope_id"]),
                    supersedes_revision_id="",
                )
            )

        for row in node_rows:
            canonical_id = str(row["canonical_id"])
            fields = _canonical_node_fields(row, node_fields)
            best = _best_searchable_field(fields, terms, query)
            if best is None:
                continue
            field, score, matched_terms = best
            access = node_acls[canonical_id]
            scope = access["scope"]
            hits.append(
                CognitiveSearchHit(
                    hit_id=f"cognitive_graph:canonical:{canonical_id}:{field.path}",
                    channel="cognitive_graph",
                    object_type="canonical_node",
                    object_id=canonical_id,
                    revision_id=field.revision_id,
                    title=str(row["canonical_name"]),
                    snippet=_snippet_around_match(field.text, matched_terms),
                    score=score,
                    confidence=0.8,
                    matched_field=field.path,
                    matched_terms=matched_terms,
                    source_revision_id=field.source_revision_id,
                    source_span_ids=field.source_span_ids,
                    acl_decision="authorized",
                    scope_type=str(scope["scope_type"]),
                    scope_id=str(scope["scope_id"]),
                    supersedes_revision_id="",
                )
            )

        report = _merge_reports(relation_report, node_report)
        report["matched_count"] = len(hits)
        report["source_trace_candidate_count"] = int(trace_report.get("candidate_count", 0))
        report["source_trace_authorized_count"] = int(trace_report.get("authorized_count", 0))
        return hits, report

    def _authorized_graph_node_fields(
        self,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
    ) -> tuple[dict[str, _SearchableField], dict[str, Any]]:
        """Build semantic graph-node text only from authorized current episodes."""

        if not self.state_db.is_file():
            return {}, _empty_report("state_store_unavailable")
        try:
            revisions, report = CognitiveStateStore(self.state_db).authorized_current_revisions(
                principal=principal,
                narrowing=narrowing,
                purpose=self.GRAPH_PURPOSE,
                object_type="cognition_episode",
            )
            from core.cognitive.cognition_episode_dispatch import _episode_projection_manifest

            fields: dict[str, _SearchableField] = {}
            for revision in revisions:
                manifest = _episode_projection_manifest(revision)
                for raw_node in manifest["nodes"]:
                    node = dict(raw_node)
                    content = str(node.get("content") or "").strip()
                    if not content:
                        continue
                    metadata = dict(node.get("metadata") or {})
                    projection_revision = str(metadata.get("projection_revision_id") or "").strip()
                    source_revision = str(metadata.get("source_revision_id") or "").strip()
                    spans = _exact_revision_span_ids(
                        source_revision,
                        _metadata_span_ids(metadata),
                    )
                    if (
                        projection_revision != revision.revision_id
                        or not source_revision
                        or not spans
                    ):
                        continue
                    fields[str(node["id"])] = _SearchableField(
                        path=str(metadata.get("field_path") or "node.content"),
                        text=content,
                        confidence=0.9,
                        source_span_ids=spans,
                        revision_id=projection_revision,
                        source_revision_id=source_revision,
                    )
            return fields, report
        except (FileNotFoundError, OSError, RuntimeError, ValueError, sqlite3.Error):
            return {}, _empty_report("state_store_invalid")

    def _search_evidence_graph(
        self,
        query: str,
        *,
        terms: tuple[str, ...],
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
    ) -> tuple[list[CognitiveSearchHit], dict[str, Any]]:
        db_path = self.evidence_graph_db
        if db_path is None or not db_path.is_file():
            return [], _empty_report("evidence_graph_unavailable")
        if not terms:
            return [], _empty_report("query_terms_required")
        try:
            with _read_only_connection(db_path) as conn:
                node_rows, node_acls, node_report = _authorized_rows(
                    conn,
                    table="evidence_nodes",
                    id_column="id",
                    header_where="1 = 1",
                    order_by="created_at DESC, id",
                    principal=principal,
                    narrowing=narrowing,
                    purpose=self.EVIDENCE_PURPOSE,
                )
                edge_rows, edge_acls, edge_report = _authorized_rows(
                    conn,
                    table="evidence_edges",
                    id_column="id",
                    header_where="1 = 1",
                    order_by="created_at DESC, id",
                    principal=principal,
                    narrowing=narrowing,
                    purpose=self.EVIDENCE_PURPOSE,
                )
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            return [], _empty_report("evidence_graph_invalid")

        hits: list[CognitiveSearchHit] = []
        for row in node_rows:
            node_id = str(row["id"])
            metadata = _json_mapping(row["metadata"])
            revision_id, source_revision_id, spans = _evidence_row_provenance(
                row,
                object_type=str(row["node_type"]),
                object_id=node_id,
                matched_field="",
            )
            if not revision_id or not source_revision_id or not spans:
                continue
            fields = [
                _SearchableField("node.title", str(row["title"] or ""), 1.0, spans),
                _SearchableField("node.source_path", str(row["source_path"] or ""), 0.9, spans),
                _SearchableField("node.content", str(row["content"] or ""), 0.85, spans),
            ]
            _append_mapping_scalars(fields, "node.metadata", metadata, 0.7, spans)
            fields = [
                replace(
                    field,
                    revision_id=revision_id,
                    source_revision_id=source_revision_id,
                )
                for field in fields
            ]
            best = _best_searchable_field(tuple(fields), terms, query)
            if best is None:
                continue
            field, score, matched_terms = best
            access = node_acls[node_id]
            scope = access["scope"]
            hits.append(
                CognitiveSearchHit(
                    hit_id=f"evidence_graph:node:{node_id}:{field.path}",
                    channel="evidence_graph",
                    object_type=str(row["node_type"]),
                    object_id=node_id,
                    revision_id=revision_id,
                    title=str(row["title"] or node_id),
                    snippet=_snippet_around_match(field.text, matched_terms),
                    score=score,
                    confidence=_confidence(metadata.get("confidence"), field.confidence),
                    matched_field=field.path,
                    matched_terms=matched_terms,
                    source_revision_id=source_revision_id,
                    source_span_ids=field.source_span_ids,
                    acl_decision="authorized",
                    scope_type=str(scope["scope_type"]),
                    scope_id=str(scope["scope_id"]),
                    supersedes_revision_id=str(metadata.get("supersedes_revision_id") or ""),
                )
            )

        for row in edge_rows:
            edge_id = str(row["id"])
            evidence = _json_list(row["evidence"])
            revision_id, source_revision_id, source_span_ids = _evidence_row_provenance(
                row,
                object_type="evidence_edge",
                object_id=edge_id,
                matched_field="",
            )
            if not revision_id or not source_revision_id or not source_span_ids:
                continue
            fields = [
                _SearchableField("edge.source_id", str(row["source_id"]), 0.9, source_span_ids),
                _SearchableField("edge.target_id", str(row["target_id"]), 0.9, source_span_ids),
                _SearchableField(
                    "edge.relation_type", str(row["relation_type"]), 1.0, source_span_ids
                ),
            ]
            for index, value in enumerate(evidence):
                if not isinstance(value, Mapping):
                    continue
                item_spans = (
                    _exact_revision_span_ids(
                        source_revision_id,
                        _metadata_span_ids(value),
                    )
                    or source_span_ids
                )
                for key in ("quote", "description"):
                    text = str(value.get(key) or "").strip()
                    if text:
                        fields.append(
                            _SearchableField(
                                f"edge.evidence[{index}].{key}",
                                text,
                                0.85,
                                item_spans,
                            )
                        )
            fields = [
                replace(
                    field,
                    revision_id=revision_id,
                    source_revision_id=source_revision_id,
                )
                for field in fields
            ]
            best = _best_searchable_field(tuple(fields), terms, query)
            if best is None:
                continue
            field, score, matched_terms = best
            access = edge_acls[edge_id]
            scope = access["scope"]
            hits.append(
                CognitiveSearchHit(
                    hit_id=f"evidence_graph:edge:{edge_id}:{field.path}",
                    channel="evidence_graph",
                    object_type="evidence_edge",
                    object_id=edge_id,
                    revision_id=revision_id,
                    title=f"{row['source_id']} —{row['relation_type']}→ {row['target_id']}",
                    snippet=_snippet_around_match(field.text, matched_terms),
                    score=score * 0.96,
                    confidence=_confidence(row["confidence"], 1.0),
                    matched_field=field.path,
                    matched_terms=matched_terms,
                    source_revision_id=source_revision_id,
                    source_span_ids=field.source_span_ids,
                    acl_decision="authorized",
                    scope_type=str(scope["scope_type"]),
                    scope_id=str(scope["scope_id"]),
                    supersedes_revision_id="",
                )
            )

        report = _merge_reports(node_report, edge_report)
        report["matched_count"] = len(hits)
        return hits, report


def _graph_relation_fields(
    row: sqlite3.Row,
    node_fields: Mapping[str, _SearchableField],
) -> tuple[_SearchableField, ...]:
    """Expose relation topology only when an authorized semantic endpoint exists."""

    fields: list[_SearchableField] = []
    traces: list[_SearchableField] = []
    for side in ("source", "target"):
        trace = node_fields.get(str(row[side]))
        if trace is None:
            continue
        traces.append(trace)
        fields.append(
            replace(
                trace,
                path=f"relation.{side}.content",
                confidence=min(1.0, trace.confidence),
            )
        )
    if not traces:
        return ()
    provenance = traces[0]
    fields.extend(
        (
            replace(
                provenance,
                path="relation.relation_type",
                text=str(row["relation_type"]),
                confidence=1.0,
            ),
            replace(
                provenance,
                path="relation.source_layer",
                text=str(row["source_layer"] or ""),
                confidence=0.8,
            ),
            replace(
                provenance,
                path="relation.target_layer",
                text=str(row["target_layer"] or ""),
                confidence=0.8,
            ),
        )
    )
    return tuple(fields)


def _canonical_node_fields(
    row: sqlite3.Row,
    node_fields: Mapping[str, _SearchableField],
) -> tuple[_SearchableField, ...]:
    """Bind a canonical node to exact episode provenance through source IDs."""

    traces = [
        node_fields[source_id]
        for source_id in (str(value) for value in _json_list(row["source_ids"]))
        if source_id in node_fields
    ]
    if not traces:
        return ()
    provenance = traces[0]
    fields = [
        replace(
            provenance,
            path="canonical_node.name",
            text=str(row["canonical_name"]),
            confidence=1.0,
        )
    ]
    fields.extend(
        replace(
            provenance,
            path=f"canonical_node.aliases[{index}]",
            text=str(value),
            confidence=0.9,
        )
        for index, value in enumerate(_json_list(row["aliases"]))
    )
    return tuple(fields)


_SEARCH_TABLE_IDS = {
    "cognitive_relations": "id",
    "canonical_nodes": "canonical_id",
    "evidence_nodes": "id",
    "evidence_edges": "id",
}


def _evidence_row_provenance(
    row: sqlite3.Row,
    *,
    object_type: str,
    object_id: str,
    matched_field: str,
) -> tuple[str, str, tuple[str, ...]]:
    del object_type, object_id, matched_field
    candidates: list[Mapping[str, Any]] = []
    if "metadata" in row.keys():
        metadata = _json_mapping(row["metadata"])
        if metadata:
            candidates.append(metadata)
    if "evidence" in row.keys():
        candidates.extend(
            value
            for value in _json_list(row["evidence"])
            if isinstance(value, Mapping)
            and value.get("schema_version") == "mnemos.cognition_source_provenance.v1"
        )
    for candidate in candidates:
        projection_revision = str(candidate.get("projection_revision_id") or "").strip()
        source_revision = str(
            candidate.get("source_revision_id")
            or candidate.get("source_event_id")
            or candidate.get("raw_revision_id")
            or ""
        ).strip()
        spans = _exact_revision_span_ids(
            source_revision,
            _metadata_span_ids(candidate),
        )
        if projection_revision and source_revision and spans:
            return projection_revision, source_revision, spans
    return "", "", ()


def _authorized_identity_row(
    db_path: Path | None,
    *,
    table: str,
    id_column: str,
    object_id: str,
    extra_where: str,
    principal: PrincipalEnvelope | None,
    narrowing: AccessNarrowing | None,
    purpose: str,
) -> tuple[sqlite3.Row | None, dict[str, Any], str]:
    if db_path is None or not db_path.is_file():
        return None, {}, "channel_unavailable"
    if _SEARCH_TABLE_IDS.get(table) != id_column:
        return None, {}, "object_type_mismatch"
    try:
        with _read_only_connection(db_path) as connection:
            header = connection.execute(
                f"SELECT {id_column}, access_control FROM {table} "  # nosec B608 - fixed internal identifiers
                f"WHERE {id_column} = ? AND {extra_where}",
                (object_id,),
            ).fetchone()
            if header is None:
                return None, {}, "not_found"
            reason, access = _authorize_acl_header(
                header["access_control"],
                principal=principal,
                narrowing=narrowing,
                purpose=purpose,
            )
            if reason != "authorized":
                return None, {}, reason
            row = connection.execute(
                f"SELECT * FROM {table} WHERE {id_column} = ?",  # nosec B608 - fixed internal identifiers
                (object_id,),
            ).fetchone()
        return row, access, "authorized" if row is not None else "not_found"
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        return None, {}, "channel_invalid"


def _read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=5,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _authorized_rows(
    connection: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    header_where: str,
    order_by: str,
    principal: PrincipalEnvelope | None,
    narrowing: AccessNarrowing | None,
    purpose: str,
) -> tuple[list[sqlite3.Row], dict[str, dict[str, Any]], dict[str, Any]]:
    """Authorize every header before hydrating any row from a known table."""

    if _SEARCH_TABLE_IDS.get(table) != id_column:
        raise ValueError("unsupported cognitive search table")
    if principal is None:
        return [], {}, _empty_report("principal_required")
    if not str(purpose or "").strip():
        return [], {}, _empty_report("purpose_required")
    headers = connection.execute(
        f"SELECT {id_column}, access_control FROM {table} "  # nosec B608 - fixed internal identifiers
        f"WHERE {header_where} AND access_control <> '' "
        f"AND json_valid(access_control) ORDER BY {order_by}"
    ).fetchall()
    allowed_ids: list[str] = []
    normalized_acls: dict[str, dict[str, Any]] = {}
    denied: dict[str, int] = {}
    for header in headers:
        object_id = str(header[id_column])
        reason, access = _authorize_acl_header(
            header["access_control"],
            principal=principal,
            narrowing=narrowing,
            purpose=purpose,
        )
        if reason == "authorized":
            allowed_ids.append(object_id)
            normalized_acls[object_id] = access
        else:
            denied[reason] = denied.get(reason, 0) + 1

    rows_by_id: dict[str, sqlite3.Row] = {}
    for offset in range(0, len(allowed_ids), 500):
        batch = allowed_ids[offset : offset + 500]
        body_query = render_sql(
            "SELECT * FROM {table} WHERE {id_column} IN ({placeholders})",
            identifiers={"table": table, "id_column": id_column},
            placeholder_counts={"placeholders": len(batch)},
        )
        for row in connection.execute(
            body_query,
            tuple(batch),
        ).fetchall():
            rows_by_id[str(row[id_column])] = row

    rows = [rows_by_id[object_id] for object_id in allowed_ids if object_id in rows_by_id]
    return (
        rows,
        normalized_acls,
        {
            "candidate_count": len(headers),
            "authorized_count": len(rows),
            "denied_by_reason": denied,
            "matched_count": 0,
            "returned_count": 0,
        },
    )


def _authorize_acl_header(
    raw_access_control: Any,
    *,
    principal: PrincipalEnvelope | None,
    narrowing: AccessNarrowing | None,
    purpose: str,
) -> tuple[str, dict[str, Any]]:
    try:
        decoded = json.loads(str(raw_access_control or ""))
        access = validate_cognitive_access_envelope(decoded)
    except (TypeError, ValueError, json.JSONDecodeError):
        return "acl_unknown", {}
    decision = authorize_cognitive_access(
        access,
        principal=principal,
        narrowing=narrowing,
        purpose=purpose,
    )
    return decision.reason, access if decision.allowed else {}


def _empty_report(reason: str) -> dict[str, Any]:
    return {
        "candidate_count": 0,
        "authorized_count": 0,
        "denied_by_reason": {reason: 1},
        "matched_count": 0,
        "returned_count": 0,
    }


def _merge_reports(*reports: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "candidate_count": 0,
        "authorized_count": 0,
        "denied_by_reason": {},
        "matched_count": 0,
        "returned_count": 0,
    }
    denied: dict[str, int] = {}
    for report in reports:
        for field_name in (
            "candidate_count",
            "authorized_count",
            "matched_count",
            "returned_count",
        ):
            merged[field_name] += int(report.get(field_name, 0) or 0)
        raw_denied = report.get("denied_by_reason")
        if isinstance(raw_denied, Mapping):
            for reason, count in raw_denied.items():
                denied[str(reason)] = denied.get(str(reason), 0) + int(count or 0)
    merged["denied_by_reason"] = denied
    return merged


def _combine_channel_reports(
    channel_reports: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    combined = _merge_reports(*channel_reports.values())
    combined["channels"] = {channel: dict(report) for channel, report in channel_reports.items()}
    return combined


def _json_mapping(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _json_list(value: Any) -> list[Any]:
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(decoded, Sequence) or isinstance(decoded, (str, bytes)):
        return []
    return list(decoded)


def _metadata_span_ids(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    explicit = metadata.get("source_span_ids")
    if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
        normalized = tuple(
            dict.fromkeys(str(value).strip() for value in explicit if str(value).strip())
        )
        if normalized:
            return normalized
    revision_id = str(
        metadata.get("source_revision_id")
        or metadata.get("source_event_id")
        or metadata.get("raw_revision_id")
        or ""
    ).strip()
    start = metadata.get("span_start")
    end = metadata.get("span_end")
    if revision_id and start is not None and end is not None:
        return (f"{revision_id}#{start}:{end}",)
    return ()


def _best_field(
    revision: CognitiveStateRevision,
    terms: tuple[str, ...],
    query: str,
) -> tuple[_SearchableField, float, tuple[str, ...]] | None:
    return _best_searchable_field(_searchable_fields(revision), terms, query)


def _best_searchable_field(
    fields: Sequence[_SearchableField],
    terms: tuple[str, ...],
    query: str,
) -> tuple[_SearchableField, float, tuple[str, ...]] | None:
    ranked: list[tuple[float, int, int, _SearchableField, tuple[str, ...]]] = []
    object_matched_terms: set[str] = set()
    for order, field in enumerate(fields):
        score, matched = _field_score(field, terms, query)
        if score <= 0.0:
            continue
        object_matched_terms.update(matched)
        ranked.append(
            (
                score,
                _field_trace_priority(field.path),
                -order,
                field,
                matched,
            )
        )
    if not ranked:
        return None
    score, _, _, field, matched = max(
        ranked,
        key=lambda item: (item[0], item[1], item[2]),
    )
    selected_coverage = _weighted_term_coverage(matched, terms)
    object_coverage = _weighted_term_coverage(
        tuple(term for term in terms if term in object_matched_terms),
        terms,
    )
    cross_field_bonus = max(0.0, object_coverage - selected_coverage) * 0.25
    return field, min(score + cross_field_bonus, 1.0), matched


def _searchable_fields(revision: CognitiveStateRevision) -> tuple[_SearchableField, ...]:
    payload = revision.payload
    fields: list[_SearchableField] = []
    fallback_refs = _exact_revision_span_ids(
        revision.source_revision_id,
        revision.evidence_refs,
    )
    if revision.object_type == "belief_revision":
        belief_spans = _exact_revision_span_ids(
            revision.source_revision_id,
            revision.evidence_refs,
        )
        if not belief_spans:
            return ()
        semantic_belief = {
            key: payload.get(key)
            for key in (
                "claim",
                "claim_kind",
                "stance",
                "supporting_evidence",
                "opposing_evidence",
                "uncertainty",
                "valid_from",
                "valid_until",
                "invalidation_conditions",
            )
        }
        _append_mapping_scalars(
            fields,
            "belief_revision",
            semantic_belief,
            _confidence(payload.get("confidence"), 0.8),
            belief_spans,
        )
        return tuple(fields)

    claims = payload.get("claims")
    if isinstance(claims, Sequence) and not isinstance(claims, (str, bytes)):
        for index, claim in enumerate(claims):
            if not isinstance(claim, Mapping):
                continue
            confidence = _confidence(claim.get("confidence"), 0.8)
            spans = _source_span_ids(claim.get("evidence"))
            _append_scalar(
                fields, f"claims[{index}].claim_text", claim.get("claim_text"), confidence, spans
            )
            _append_scalar(
                fields, f"claims[{index}].claim_type", claim.get("claim_type"), confidence, spans
            )
            _append_mapping_scalars(
                fields, f"claims[{index}].scope", claim.get("scope"), confidence, spans
            )
            _append_mapping_scalars(
                fields,
                f"claims[{index}].relation_to_existing",
                claim.get("relation_to_existing"),
                confidence,
                spans,
            )
            _append_scalar(
                fields,
                f"claims[{index}].recommended_action",
                claim.get("recommended_action"),
                confidence,
                spans,
            )

    episode = {name: payload.get(name) for name in COGNITION_EPISODE_FIELDS if name in payload}
    for field_name, entries in episode.items():
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                continue
            status = str(entry.get("status") or "")
            key = "value" if status == "known" else "reason"
            _append_scalar(
                fields,
                f"cognition_episode.{field_name}[{index}].{key}",
                entry.get(key),
                0.9 if status == "known" else 0.55,
                _source_span_ids(entry.get("evidence_refs")),
            )

    behavior = payload.get("user_behavior_intent")
    behavior_spans = (
        _source_span_ids(behavior.get("intent_evidence")) if isinstance(behavior, Mapping) else ()
    )
    _append_mapping_scalars(
        fields,
        "user_behavior_intent",
        behavior,
        0.75,
        behavior_spans,
        excluded={"intent_evidence"},
    )

    if not fields:
        _append_mapping_scalars(
            fields,
            revision.object_type,
            payload,
            _confidence(payload.get("confidence"), 0.7),
            (),
            excluded={"access_control", "source_authority_catalog", "artifact_catalog"},
        )
    searchable: list[_SearchableField] = []
    for field in fields:
        spans = _exact_revision_span_ids(
            revision.source_revision_id,
            field.source_span_ids or fallback_refs,
        )
        if spans:
            searchable.append(replace(field, source_span_ids=spans))
    return tuple(searchable)


def _append_mapping_scalars(
    target: list[_SearchableField],
    prefix: str,
    value: Any,
    confidence: float,
    spans: tuple[str, ...],
    *,
    excluded: set[str] | None = None,
) -> None:
    if not isinstance(value, Mapping):
        return
    excluded = excluded or set()
    for key, nested in value.items():
        if str(key) in excluded:
            continue
        path = f"{prefix}.{key}"
        if isinstance(nested, Mapping):
            _append_mapping_scalars(target, path, nested, confidence, spans, excluded=excluded)
        elif isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
            for index, item in enumerate(nested):
                if isinstance(item, Mapping):
                    _append_mapping_scalars(
                        target,
                        f"{path}[{index}]",
                        item,
                        confidence,
                        spans,
                        excluded=excluded,
                    )
                else:
                    _append_scalar(target, f"{path}[{index}]", item, confidence, spans)
        else:
            _append_scalar(target, path, nested, confidence, spans)


def _append_scalar(
    target: list[_SearchableField],
    path: str,
    value: Any,
    confidence: float,
    spans: tuple[str, ...],
) -> None:
    if value in (None, "") or isinstance(value, bool):
        return
    text = str(value).strip()
    if text:
        target.append(_SearchableField(path, text, confidence, spans))


def _field_score(
    field: _SearchableField,
    terms: tuple[str, ...],
    query: str,
) -> tuple[float, tuple[str, ...]]:
    text = field.text.casefold()
    normalized_query = query.casefold()
    matched = tuple(term for term in terms if term in text)
    if not matched:
        return 0.0, ()
    coverage = _weighted_term_coverage(matched, terms)
    phrase_bonus = 0.35 if normalized_query in text else 0.0
    field_boost = _field_semantic_boost(field.path)
    longest = max(len(term) for term in matched)
    length_bonus = min(longest / max(4, len(normalized_query)), 1.0) * 0.2
    score = min(0.25 + coverage * 0.35 + phrase_bonus + field_boost + length_bonus, 1.0)
    return score, matched


def _weighted_term_coverage(
    matched_terms: Sequence[str],
    query_terms: Sequence[str],
) -> float:
    """Prefer distinctive query terms without discarding multi-field coverage."""

    denominator = sum(max(2, len(term)) for term in query_terms)
    if denominator <= 0:
        return 0.0
    matched = set(matched_terms)
    numerator = sum(max(2, len(term)) for term in query_terms if term in matched)
    return min(numerator / denominator, 1.0)


def _field_semantic_boost(path: str) -> float:
    """Keep human-readable assertions ahead of identifiers and topology labels."""

    if path.endswith((".claim_text", ".claim", ".value", ".content", ".quote", ".description")):
        return 0.15
    if path.endswith((".title", ".name")) or ".aliases[" in path:
        return 0.10
    if ".metadata." in path:
        return 0.06
    return 0.05


def _field_trace_priority(path: str) -> int:
    """Break equal relevance in favour of the most explainable source field."""

    if path.endswith((".claim_text", ".claim", ".value", ".content", ".quote", ".description")):
        return 3
    if path.endswith((".title", ".name")) or ".aliases[" in path:
        return 2
    return 1


def _snippet_around_match(text: str, matched_terms: tuple[str, ...], radius: int = 100) -> str:
    if not text:
        return ""
    lowered = text.casefold()
    offsets = [lowered.find(term) for term in matched_terms if lowered.find(term) >= 0]
    offset = min(offsets) if offsets else 0
    start = max(0, offset - radius)
    end = min(len(text), offset + max((len(term) for term in matched_terms), default=0) + radius)
    snippet = text[start:end].strip()
    return ("…" if start else "") + snippet + ("…" if end < len(text) else "")


def _source_span_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    spans: list[str] = []
    for evidence in value:
        if not isinstance(evidence, Mapping):
            continue
        revision_id = str(evidence.get("source_event_id") or "").strip()
        start = evidence.get("authority_span_start")
        end = evidence.get("authority_span_end")
        if revision_id and start is not None and end is not None:
            span = f"{revision_id}#{start}:{end}"
            if span not in spans:
                spans.append(span)
    return tuple(spans)


def _exact_revision_span_ids(
    source_revision_id: str,
    values: Sequence[Any],
) -> tuple[str, ...]:
    """Return only exact non-empty spans owned by ``source_revision_id``."""

    revision_id = str(source_revision_id or "").strip()
    if not revision_id:
        return ()
    pattern = re.compile(re.escape(revision_id) + r"#([0-9]+):([0-9]+)")
    spans: list[str] = []
    for value in values:
        span_id = str(value or "").strip()
        match = pattern.fullmatch(span_id)
        if match is None or int(match.group(2)) <= int(match.group(1)):
            continue
        if span_id not in spans:
            spans.append(span_id)
    return tuple(spans)


def _confidence(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(parsed, 1.0))


def _query_terms(query: str) -> tuple[str, ...]:
    tokens = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9_\-]{2,}", query.casefold())
    terms: list[str] = []
    for token in tokens:
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            terms.append(token)
            if len(token) > 2:
                terms.extend(token[index : index + 2] for index in range(len(token) - 1))
            if len(token) > 4:
                terms.extend(token[index : index + 3] for index in range(len(token) - 2))
        else:
            terms.append(token)
    return tuple(dict.fromkeys(term for term in terms if term))
