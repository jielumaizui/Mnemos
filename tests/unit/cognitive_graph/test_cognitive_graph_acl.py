from __future__ import annotations

import pytest

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive_graph.store import CognitiveGraphStore
from tests.cognitive_decision_fixtures import canonical_material_action_scope


@pytest.fixture(autouse=True)
def _canonical_material_actions(tmp_path):
    """Exercise graph mutations through real canonical DecisionTrace permits."""

    with canonical_material_action_scope(tmp_path):
        yield


def _principal(session_id: str = "graph-session") -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="mcp:codex:graph-acl",
        agent="codex",
        host_kind="codex",
        capability_id="graph-acl",
        capabilities=frozenset({"memory_read"}),
    )


def _access(session_id: str = "graph-session") -> dict:
    return make_cognitive_access_envelope(
        owner_principal_id="source-agent:codex",
        owner_agent="codex",
        scope_type="session",
        scope_id=session_id,
        session_id=session_id,
        purposes=("cognitive_graph_read",),
        consent_provenance_refs=(f"session:{session_id}",),
        sensitivity="sensitive",
        retention_policy="source-retention",
        source_acl_lineage=(f"sha256:graph-source:{session_id}",),
        visibility="agent",
    )


def test_graph_relation_authorizes_headers_before_hydrating_body(tmp_path, monkeypatch) -> None:
    store = CognitiveGraphStore(str(tmp_path / "cognitive_graph.db"))
    relation = store.add_relation(
        "session://graph-session",
        "kg://SensitivePreference",
        "derived_from",
        access_control=_access(),
    )

    def body_reader_must_not_run(_row):
        raise AssertionError("denied graph relation body was hydrated")

    monkeypatch.setattr(store, "_row_to_relation", body_reader_must_not_run)
    denied, summary = store.authorized_get_relation(
        relation.id,
        principal=_principal(),
        narrowing=AccessNarrowing(session_id="other-session"),
    )

    assert denied is None
    assert summary == {"session_scope_mismatch": 1}


def test_graph_authorized_relation_and_canonical_node_reads(tmp_path) -> None:
    store = CognitiveGraphStore(str(tmp_path / "cognitive_graph.db"))
    relation = store.add_relation(
        "session://graph-session",
        "kg://SensitivePreference",
        "derived_from",
        access_control=_access(),
    )

    fetched, summary = store.authorized_get_relation(
        relation.id,
        principal=_principal(),
        narrowing=AccessNarrowing(session_id="graph-session"),
    )
    nodes, node_summary = store.authorized_find_canonical_nodes(
        source_id="kg://SensitivePreference",
        principal=_principal(),
        narrowing=AccessNarrowing(session_id="graph-session"),
    )

    assert fetched is not None
    assert fetched.target == "kg://SensitivePreference"
    assert summary == {"authorized": 1}
    assert [node.canonical_name for node in nodes] == ["SensitivePreference"]
    assert node_summary["authorized"] == 1


def test_graph_relation_acl_filter_refills_past_denied_top_rows(tmp_path) -> None:
    store = CognitiveGraphStore(str(tmp_path / "cognitive_graph.db"))
    authorized = store.add_relation(
        "session://graph-session/authorized",
        "kg://AuthorizedTail",
        "derived_from",
        access_control=_access("graph-session"),
    )
    for index in range(12):
        store.add_relation(
            f"session://denied-{index}",
            f"kg://Denied-{index}",
            "derived_from",
            access_control=_access(f"denied-session-{index}"),
        )

    relations, summary = store.authorized_get_relations(
        limit=1,
        principal=_principal(),
        narrowing=AccessNarrowing(session_id="graph-session"),
    )

    assert [relation.id for relation in relations] == [authorized.id]
    assert summary == {"session_scope_mismatch": 12, "authorized": 1}


def test_graph_canonical_node_acl_filter_refills_past_denied_top_rows(tmp_path) -> None:
    store = CognitiveGraphStore(str(tmp_path / "cognitive_graph.db"))
    for index in range(12):
        store.add_relation(
            f"session://denied-{index}",
            f"kg://Denied-{index}",
            "derived_from",
            access_control=_access(f"denied-session-{index}"),
        )
    store.add_relation(
        "session://graph-session/authorized",
        "kg://AuthorizedTail",
        "derived_from",
        access_control=_access("graph-session"),
    )

    nodes, summary = store.authorized_find_canonical_nodes(
        limit=1,
        principal=_principal(),
        narrowing=AccessNarrowing(session_id="graph-session"),
    )

    assert len(nodes) == 1
    assert nodes[0].access_control["scope"]["session_id"] == "graph-session"
    assert summary["authorized"] >= 1
    assert summary["session_scope_mismatch"] >= 12


def test_graph_missing_or_incompatible_acl_fails_closed(tmp_path) -> None:
    store = CognitiveGraphStore(str(tmp_path / "cognitive_graph.db"))
    legacy = store.add_relation("session://legacy", "kg://Legacy", "derived_from")
    restricted, summary = store.authorized_get_relation(
        legacy.id,
        principal=_principal(),
        narrowing=AccessNarrowing(session_id="graph-session"),
    )

    assert restricted is None
    assert summary == {"acl_scope_unresolved": 1}

    relation = store.add_relation(
        "session://graph-session",
        "kg://MixedAcl",
        "derived_from",
        access_control=_access("graph-session"),
    )
    store.add_relation(
        "session://graph-session",
        "kg://MixedAcl",
        "derived_from",
        access_control=_access("another-session"),
    )
    merged, merged_summary = store.authorized_get_relation(
        relation.id,
        principal=_principal(),
        narrowing=AccessNarrowing(session_id="graph-session"),
    )

    assert merged is None
    assert merged_summary == {"acl_scope_unresolved": 1}


def test_graph_unresolved_acl_cannot_verify_a_scoped_delete(tmp_path) -> None:
    store = CognitiveGraphStore(str(tmp_path / "cognitive_graph.db"))
    store.add_relation("session://legacy", "kg://Legacy", "derived_from")

    result = store.delete_subject_scope(
        request_id="graph-delete-legacy",
        scope_kind="session",
        scope_value="graph-session",
    )

    assert result["verified"] is False
    assert result["unresolved_legacy_count"] >= 3


def test_graph_subject_delete_removes_acl_matched_objects_and_blocks_relation_restore(
    tmp_path,
) -> None:
    store = CognitiveGraphStore(str(tmp_path / "cognitive_graph.db"))
    relation = store.add_relation(
        "session://graph-session",
        "kg://SensitivePreference",
        "derived_from",
        access_control=_access(),
    )
    outbox = store.add_sync_outbox(
        "reflection.completed",
        {"private": "must be deleted"},
        access_control=_access(),
    )

    result = store.delete_subject_scope(
        request_id="graph-delete-test",
        scope_kind="session",
        scope_value="graph-session",
    )

    assert result["status"] == "applied"
    assert result["target_count"] >= 4  # relation, two canonical nodes, and outbox
    assert result["verified"] is True
    assert store.get_relation(relation.id) is None
    assert store.get_outbox_item(outbox.id) is None
    with pytest.raises(PermissionError, match="subject-deleted"):
        store.add_relation(
            "session://graph-session",
            "kg://SensitivePreference",
            "derived_from",
            access_control=_access(),
        )

    retry = store.delete_subject_scope(
        request_id="graph-delete-test-retry",
        scope_kind="session",
        scope_value="graph-session",
    )
    assert retry["status"] == "existing"
