from __future__ import annotations

import sqlite3


def test_direction_audit_fails_closed_for_uninitialized_runtime(tmp_path):
    from scripts.audit_evidence_graph_direction import build_report

    report = build_report(tmp_path / "evidence_graph.db")

    assert report["ok"] is False
    assert report["runtime"]["initialized"] is False
    assert report["gaps"]["schema_gap"] == 1
    assert report["gaps"]["direction_gap"] == 0


def test_direction_audit_rejects_legacy_evidence_to_derived_edge(tmp_path):
    from core.evidence.evidence_graph import (
        EvidenceEdge,
        EvidenceGraph,
        EvidenceNode,
        EvidenceNodeType,
        EvidenceRelationType,
    )
    from core.cognitive.cognition_episode_projection_schema import (
        initialize_evidence_projection_schema,
    )
    from scripts.audit_evidence_graph_direction import build_report

    db_path = tmp_path / "evidence_graph.db"
    graph = EvidenceGraph(str(db_path))
    initialize_evidence_projection_schema(db_path)
    graph.ensure_node(EvidenceNode(id="memory-1", node_type=EvidenceNodeType.MEMORY))
    graph.ensure_node(EvidenceNode(id="observation-1", node_type=EvidenceNodeType.OBSERVATION))
    graph.add_edge(
        EvidenceEdge(
            source_id="observation-1",
            target_id="memory-1",
            relation_type=EvidenceRelationType.OBSERVED_IN,
        )
    )
    assert build_report(db_path)["ok"] is True

    with sqlite3.connect(db_path) as conn:
        conn.execute("""UPDATE evidence_edges SET source_id='memory-1', target_id='observation-1'
               WHERE relation_type='observed_in'""")
        conn.commit()

    broken = build_report(db_path)
    assert broken["ok"] is False
    assert broken["gaps"]["direction_gap"] == 1
