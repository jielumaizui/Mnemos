from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.state_contract import CognitiveStateRevision, sha256_json
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateStore
from tests.unit.cognitive.test_state_store import _commands, _event, _revision


def _principal(principal_id: str = "test:state-store") -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id=principal_id,
        agent="state-store-test",
        host_kind="codex",
        capability_id="cognitive-search-test",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def _scoped_revision(
    suffix: str,
    *,
    claim_text: str,
    owner_principal_id: str,
) -> CognitiveStateRevision:
    event = _event(suffix, ("wiki", "cognitive_graph"))
    base = _revision(suffix, event.event_id, goal=claim_text)
    payload = deepcopy(dict(base.payload))
    old_access = payload["access_control"]
    payload["access_control"] = make_cognitive_access_envelope(
        owner_principal_id=owner_principal_id,
        owner_agent="state-store-test",
        scope_type="project",
        scope_id="mnemos",
        project="mnemos",
        session_id="",
        purposes=("cognitive_state_read",),
        consent_provenance_refs=tuple(old_access["consent"]["provenance_refs"]),
        sensitivity="sensitive",
        retention_policy="cognitive_state",
        source_acl_lineage=tuple(old_access["source_acl_lineage"]),
    )
    payload["cognition_context_hash"] = sha256_json(
        {
            "schema_version": "mnemos.cognition_extraction_context.v1",
            "source_agent": payload["source_agent"],
            "source_session_id": payload["source_session_id"],
            "source_event_ids": list(payload["source_event_ids"]),
            "raw_completeness": payload["raw_completeness"],
            "loss_contract": payload["loss_contract"],
            "source_spans": list(payload["source_spans"]),
            "artifact_catalog_hash": payload["artifact_catalog_hash"],
            "source_authority_catalog_hash": payload["source_authority_catalog_hash"],
            "acl": payload["acl"],
            "access_control": payload["access_control"],
            "purpose": payload["purpose"],
            "retention_policy": payload["retention_policy"],
        }
    )
    return CognitiveStateRevision.create(
        object_type=base.object_type,
        object_id=base.object_id,
        source_event_id=base.source_event_id,
        source_revision_id=base.source_revision_id,
        source_content_hash=base.source_content_hash,
        scope_type=base.scope_type,
        scope_id=base.scope_id,
        evidence_refs=base.evidence_refs,
        payload=payload,
        created_at=base.created_at,
    )


def _commit(store: CognitiveStateStore, revision: CognitiveStateRevision, suffix: str) -> None:
    store.unit_of_work().commit(
        revisions=(revision,),
        event=_event(suffix, ("wiki", "cognitive_graph")),
        commands=_commands(revision.revision_id),
    )


def test_typed_cognitive_search_finds_canonical_state_without_wiki(tmp_path):
    from core.cognitive.search import CognitiveSearch

    database_dir = tmp_path / ".kg"
    database_dir.mkdir()
    state_db = database_dir / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_db)
    store = CognitiveStateStore(state_db)
    revision = _scoped_revision(
        "authorized",
        claim_text="连接上限过低且缺少超时监控会导致连接池耗尽。",
        owner_principal_id="test:state-store",
    )
    _commit(store, revision, "authorized")
    for index in range(40):
        unrelated = _scoped_revision(
            f"unrelated-{index:02d}",
            claim_text=f"unrelated historical material {index}",
            owner_principal_id=f"denied:{index}",
        )
        _commit(store, unrelated, f"unrelated-{index:02d}")

    hits, report = CognitiveSearch(state_db=state_db).search(
        "连接池耗尽根因",
        principal=_principal(),
        narrowing=AccessNarrowing(project="mnemos"),
        limit=5,
    )

    assert hits
    hit = hits[0]
    assert hit.channel == "cognitive_state"
    assert hit.object_type == "cognition_episode"
    assert hit.revision_id == revision.revision_id
    assert hit.matched_field == "claims[0].claim_text"
    assert "连接上限过低" in hit.snippet
    assert hit.source_revision_id == "raw-revision-authorized"
    assert hit.source_span_ids == ("raw-revision-authorized#0:32",)
    assert hit.acl_decision == "authorized"
    assert hit.is_current is True
    assert report["candidate_count"] == 41
    assert report["authorized_count"] == 1
    _, unrelated_report = CognitiveSearch(state_db=state_db).search(
        "a query that matches none of these objects",
        principal=_principal(),
        narrowing=AccessNarrowing(project="mnemos"),
        limit=5,
    )
    assert unrelated_report["candidate_count"] == report["candidate_count"]
    assert unrelated_report["denied_by_reason"] == report["denied_by_reason"]


def test_typed_cognitive_search_filters_acl_before_top_k_and_refills(tmp_path):
    from core.cognitive.search import CognitiveSearch

    state_db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_db)
    store = CognitiveStateStore(state_db)
    for index in range(12):
        suffix = f"denied-{index:02d}"
        revision = _scoped_revision(
            suffix,
            claim_text=f"REFILL-SENTINEL denied exact candidate {index}",
            owner_principal_id=f"denied:{index}",
        )
        _commit(store, revision, suffix)
    authorized = _scoped_revision(
        "authorized-tail",
        claim_text="REFILL-SENTINEL authorized target behind denied candidates",
        owner_principal_id="test:state-store",
    )
    _commit(store, authorized, "authorized-tail")

    hits, report = CognitiveSearch(state_db=state_db).search(
        "REFILL-SENTINEL",
        principal=_principal(),
        narrowing=AccessNarrowing(project="mnemos"),
        limit=1,
    )

    assert [hit.revision_id for hit in hits] == [authorized.revision_id]
    assert report["candidate_count"] == 13
    assert report["authorized_count"] == 1
    assert report["denied_by_reason"] == {"owner_principal_mismatch": 12}


def test_state_searchable_fields_reject_spans_from_another_source_revision():
    from core.cognitive.search import _searchable_fields

    revision = _scoped_revision(
        "source-mismatch",
        claim_text="MISMATCH-SENTINEL must not become searchable",
        owner_principal_id="test:state-store",
    )
    mismatched = replace(
        revision,
        source_revision_id="raw-revision-different",
    )

    assert _searchable_fields(mismatched) == ()


def test_typed_cognitive_search_uses_type_specific_purpose_and_only_current_belief(
    tmp_path,
):
    from core.cognitive.search import CognitiveSearch
    from tests.unit.cognitive.test_belief_revision import (
        _belief_store,
        _command as _belief_command,
        _principal as _belief_principal,
    )

    store = _belief_store(tmp_path)
    principal = _belief_principal()
    first = store.revise(
        _belief_command(source_id="source:1", supporting=("evidence:support:1",)),
        principal=principal,
    )
    current = store.revise(
        _belief_command(
            source_id="source:2",
            opposing=("evidence:oppose:1",),
            expected_current_revision_id=first.revision_id,
        ),
        principal=principal,
    )

    hits, report = CognitiveSearch(state_db=store.state_store.db_path).search(
        "SQLite backups retention expiry",
        principal=principal,
        narrowing=AccessNarrowing(project="mnemos"),
        limit=5,
    )

    belief_hits = [hit for hit in hits if hit.object_type == "belief_revision"]
    assert len(belief_hits) == 1
    assert belief_hits[0].revision_id == current.revision_id
    assert belief_hits[0].supersedes_revision_id == first.revision_id
    assert belief_hits[0].matched_field == "belief_revision.claim"
    assert belief_hits[0].source_revision_id == "revision:source:2"
    assert belief_hits[0].source_span_ids == ("revision:source:2#0:52",)
    assert report["channels"]["cognitive_state"]["authorized_count"] == 1

    superseded_hits, _ = CognitiveSearch(state_db=store.state_store.db_path).search(
        first.revision_id,
        principal=principal,
        narrowing=AccessNarrowing(project="mnemos"),
        limit=5,
    )
    assert not [hit for hit in superseded_hits if hit.object_type == "belief_revision"]

    searcher = CognitiveSearch(state_db=store.state_store.db_path)
    exposed_hit = belief_hits[0]
    assert searcher.authorize_identity(
        channel=exposed_hit.channel,
        object_type=exposed_hit.object_type,
        object_id=exposed_hit.object_id,
        revision_id=exposed_hit.revision_id,
        source_revision_id=exposed_hit.source_revision_id,
        source_span_ids=exposed_hit.source_span_ids,
        matched_field=exposed_hit.matched_field,
        acl_decision=exposed_hit.acl_decision,
        is_current=exposed_hit.is_current,
        principal=principal,
        narrowing=AccessNarrowing(project="mnemos"),
    ) == (True, "authorized")

    store.revise(
        _belief_command(
            source_id="source:3",
            supporting=("evidence:support:3",),
            expected_current_revision_id=current.revision_id,
        ),
        principal=principal,
    )
    assert searcher.authorize_identity(
        channel=exposed_hit.channel,
        object_type=exposed_hit.object_type,
        object_id=exposed_hit.object_id,
        revision_id=exposed_hit.revision_id,
        source_revision_id=exposed_hit.source_revision_id,
        source_span_ids=exposed_hit.source_span_ids,
        matched_field=exposed_hit.matched_field,
        acl_decision=exposed_hit.acl_decision,
        is_current=exposed_hit.is_current,
        principal=principal,
        narrowing=AccessNarrowing(project="mnemos"),
    ) == (False, "not_current")


def test_evidence_search_rejects_span_from_another_source_revision(tmp_path):
    from core.cognitive.search import CognitiveSearch
    from core.evidence.evidence_graph import EvidenceGraph, EvidenceNode, EvidenceNodeType

    state_db = tmp_path / "producer_consumer_ledger.db"
    evidence_db = tmp_path / "evidence_graph.db"
    initialize_cognitive_state_schema(state_db)
    principal = PrincipalEnvelope(
        principal_id="search:owner",
        agent="codex",
        host_kind="codex",
        capability_id="cognitive-search-evidence-mismatch-test",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )
    access = make_cognitive_access_envelope(
        owner_principal_id="search:owner",
        owner_agent="codex",
        scope_type="project",
        scope_id="mnemos",
        project="mnemos",
        purposes=("evidence_graph_read",),
        consent_provenance_refs=("evidence:mismatch",),
        sensitivity="sensitive",
        retention_policy="cognitive_state",
        source_acl_lineage=("sha256:evidence-mismatch",),
        visibility="private",
    )
    EvidenceGraph(str(evidence_db)).ensure_node(
        EvidenceNode(
            id="evidence-mismatched-source-span",
            node_type=EvidenceNodeType.CLAIM,
            title="MISMATCH-EVIDENCE-SENTINEL",
            metadata={
                "projection_revision_id": "cogrev-mismatch",
                "source_revision_id": "raw-revision-a",
                "source_span_ids": ["raw-revision-b#0:16"],
            },
            access_control=access,
        )
    )

    hits, report = CognitiveSearch(
        state_db=state_db,
        evidence_graph_db=evidence_db,
    ).search(
        "MISMATCH-EVIDENCE-SENTINEL",
        principal=principal,
        narrowing=AccessNarrowing(project="mnemos"),
        limit=5,
    )

    assert not [hit for hit in hits if hit.channel == "evidence_graph"]
    assert report["channels"]["evidence_graph"]["authorized_count"] == 1
    assert report["channels"]["evidence_graph"]["matched_count"] == 0


def test_typed_cognitive_search_excludes_belief_without_exact_source_span(tmp_path):
    from core.cognitive.search import CognitiveSearch
    from tests.unit.cognitive.test_belief_revision import (
        _belief_store,
        _command as _belief_command,
        _principal as _belief_principal,
    )

    store = _belief_store(tmp_path)
    principal = _belief_principal()
    revision = store.revise(
        _belief_command(source_id="source:4", source_span_ids=()),
        principal=principal,
    )

    hits, _ = CognitiveSearch(state_db=store.state_store.db_path).search(
        "SQLite backups retention expiry",
        principal=principal,
        narrowing=AccessNarrowing(project="mnemos"),
        limit=5,
    )

    assert revision.revision_id
    assert not [hit for hit in hits if hit.object_type == "belief_revision"]


def test_typed_cognitive_search_excludes_legacy_graph_rows_without_source_trace(
    tmp_path,
):
    from core.cognitive.search import CognitiveSearch
    from core.cognitive_graph.store import CognitiveGraphStore
    from core.evidence.evidence_graph import EvidenceGraph, EvidenceNode, EvidenceNodeType

    state_db = tmp_path / "producer_consumer_ledger.db"
    graph_db = tmp_path / "cognitive_graph.db"
    evidence_db = tmp_path / "evidence_graph.db"
    initialize_cognitive_state_schema(state_db)
    principal = PrincipalEnvelope(
        principal_id="search:owner",
        agent="codex",
        host_kind="codex",
        capability_id="cognitive-search-graph-test",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )
    graph_access = make_cognitive_access_envelope(
        owner_principal_id="search:owner",
        owner_agent="codex",
        scope_type="project",
        scope_id="mnemos",
        project="mnemos",
        purposes=("cognitive_graph_read",),
        consent_provenance_refs=("graph:test",),
        sensitivity="sensitive",
        retention_policy="cognitive_graph_retention",
        source_acl_lineage=("sha256:graph-test",),
        visibility="private",
    )
    CognitiveGraphStore(
        str(graph_db),
        ownership_config=SimpleNamespace(database_dir=tmp_path),
    )
    with sqlite3.connect(graph_db) as conn:
        conn.execute(
            """INSERT INTO cognitive_relations
               (id, source, target, relation_type, strength, confidence,
                source_layer, target_layer, created_at, updated_at, stale,
                access_control)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (
                "graph-search-relation",
                "decision://GRAPH-ONLY-CAUSALITY",
                "claim://root-cause",
                "based_on",
                1.0,
                0.95,
                "decision",
                "claim",
                "2026-07-20T00:00:00+00:00",
                "2026-07-20T00:00:00+00:00",
                json.dumps(graph_access, sort_keys=True),
            ),
        )
        conn.commit()
    evidence_access = make_cognitive_access_envelope(
        owner_principal_id="search:owner",
        owner_agent="codex",
        scope_type="project",
        scope_id="mnemos",
        project="mnemos",
        purposes=("evidence_graph_read",),
        consent_provenance_refs=("evidence:test",),
        sensitivity="sensitive",
        retention_policy="cognitive_state",
        source_acl_lineage=("sha256:evidence-test",),
        visibility="private",
    )
    evidence = EvidenceGraph(str(evidence_db))
    evidence.ensure_node(
        EvidenceNode(
            id="evidence-search-node",
            node_type=EvidenceNodeType.CLAIM,
            title="GRAPH-ONLY-CAUSALITY root cause claim",
            content="The decision is causally based on the root-cause claim.",
            metadata={
                "projection_revision_id": "cogrev-graph-only",
                "source_event_id": "raw-graph-only",
                "span_start": 11,
                "span_end": 42,
            },
            access_control=evidence_access,
        )
    )
    with sqlite3.connect(graph_db) as conn:
        for index in range(40):
            conn.execute(
                """INSERT INTO cognitive_relations
                   (id, source, target, relation_type, strength, confidence,
                    source_layer, target_layer, created_at, updated_at, stale,
                    access_control)
                   VALUES (?, ?, ?, 'related_to', 1.0, 0.5,
                           'decision', 'claim', ?, ?, 0, '')""",
                (
                    f"unrelated-graph-{index:02d}",
                    f"decision://unrelated/{index}",
                    f"claim://unrelated/{index}",
                    "2026-07-20T00:00:00+00:00",
                    "2026-07-20T00:00:00+00:00",
                ),
            )
        conn.commit()
    with sqlite3.connect(evidence_db) as conn:
        for index in range(40):
            conn.execute(
                """INSERT INTO evidence_nodes
                   (id, node_type, title, source_path, content, metadata,
                    access_control, created_at)
                   VALUES (?, 'claim', ?, '', '', '{}', '', ?)""",
                (
                    f"unrelated-evidence-{index:02d}",
                    f"unrelated evidence {index}",
                    "2026-07-20T00:00:00+00:00",
                ),
            )
        conn.commit()

    hits, report = CognitiveSearch(
        state_db=state_db,
        cognitive_graph_db=graph_db,
        evidence_graph_db=evidence_db,
    ).search(
        "GRAPH-ONLY-CAUSALITY",
        principal=principal,
        narrowing=AccessNarrowing(project="mnemos"),
        limit=5,
    )

    assert {hit.channel for hit in hits} == {"evidence_graph"}
    evidence_hit = next(hit for hit in hits if hit.channel == "evidence_graph")
    assert evidence_hit.matched_field == "node.title"
    assert evidence_hit.revision_id == "cogrev-graph-only"
    assert evidence_hit.source_revision_id == "raw-graph-only"
    assert evidence_hit.source_span_ids == ("raw-graph-only#11:42",)
    assert report["channels"]["cognitive_graph"]["candidate_count"] == 1
    assert report["channels"]["evidence_graph"]["candidate_count"] == 1
    assert report["channels"]["cognitive_graph"]["authorized_count"] == 1
    assert report["channels"]["evidence_graph"]["authorized_count"] == 1
    assert report["channels"]["cognitive_graph"]["matched_count"] == 0
    assert report["channels"]["cognitive_graph"]["source_trace_authorized_count"] == 0

    searcher = CognitiveSearch(
        state_db=state_db,
        cognitive_graph_db=graph_db,
        evidence_graph_db=evidence_db,
    )
    assert searcher.authorize_identity(
        channel=evidence_hit.channel,
        object_type=evidence_hit.object_type,
        object_id=evidence_hit.object_id,
        revision_id=evidence_hit.revision_id,
        source_revision_id=evidence_hit.source_revision_id,
        source_span_ids=evidence_hit.source_span_ids,
        matched_field=evidence_hit.matched_field,
        acl_decision=evidence_hit.acl_decision,
        is_current=evidence_hit.is_current,
        principal=principal,
        narrowing=AccessNarrowing(project="mnemos"),
    ) == (True, "authorized")
