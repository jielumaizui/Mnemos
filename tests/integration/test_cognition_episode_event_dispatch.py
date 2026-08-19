from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


class _RuntimeConfig:
    def __init__(self, root: Path):
        self.data_dir = root / "data"
        self.database_dir = self.data_dir / "databases"
        self.mnemos_dir = root / "runtime"
        self.wiki_dir = root / "wiki"
        self.cognitive_graph_db_path = self.database_dir / "cognitive_graph.db"
        self._values = {
            "event_bus.max_retries": 3,
            "event_bus.retry_base_seconds": 0,
            "event_bus.retry_max_seconds": 0,
            "event_bus.dispatch_workers": 1,
            "event_bus.handler_timeout_seconds": 0,
        }

    def get(self, key: str, default=None):
        return self._values.get(key, default)


def _committed_full_episode(config: _RuntimeConfig, *, holdout_order: bool = False):
    from core.cognitive.cognition_episode_persistence import commit_cognition_episode
    from core.cognitive.cognition_episode_projection_schema import (
        initialize_wiki_projection_schema,
    )
    from core.cognitive.state_schema import initialize_cognitive_state_schema
    from core.wiki_projection_lifecycle import WikiProjectionLedger
    from tests.unit.cognitive.test_cognition_episode_contract import (
        _input_spec,
        _resolve,
        _root,
        _write_result,
    )

    spec = _input_spec()
    root = _resolve(_root(spec, include_episode=True), spec)
    episode = root["structured_output"]["cognition_episode"]
    known = deepcopy(episode["facts"][0])
    values = {
        "assumptions": "连接峰值将在发布窗口继续上升。",
        "hypotheses": "提高连接上限后，连接耗尽告警应显著下降。",
        "alternatives": "方案一提高连接上限；方案二只缩短空闲超时。",
        "tradeoffs": "提高上限增加资源占用，缩短超时增加重连频率。",
        "outcomes": "发布后连接耗尽告警下降，重连率保持稳定。",
    }
    for field_name, value in values.items():
        entry = deepcopy(known)
        entry["value"] = value
        episode[field_name] = [entry]
    if holdout_order:
        second_fact = deepcopy(known)
        second_fact["value"] = "连接复用率低于预期，峰值期间出现额外连接放大。"

        def anticipated_entry_id(entry):
            material = json.dumps(
                {"field": "facts", "entry": entry},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return "cogentry-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

        episode["facts"] = sorted(
            [known, second_fact],
            key=anticipated_entry_id,
            reverse=True,
        )

    result = _write_result(spec, root)
    config.database_dir.mkdir(parents=True)
    config.wiki_dir.mkdir(parents=True)
    initialize_cognitive_state_schema(config.database_dir / "producer_consumer_ledger.db")
    receipt = commit_cognition_episode(result, config)
    page = config.wiki_dir / "redis-decision.md"
    page.write_text(
        "---\n" f"认知事件修订ID: {receipt.revision_id}\n" "---\n\n" "# Redis 连接池决策\n",
        encoding="utf-8",
    )
    wiki_projection_db = config.database_dir / "wiki_projection.db"
    WikiProjectionLedger(wiki_projection_db).record_mutation(
        page,
        mutation_type="create",
        page_id="test-page-" + receipt.revision_id,
    )
    initialize_wiki_projection_schema(wiki_projection_db)
    return result, receipt


def _wait_for_terminal_event(bus, trace_id: str, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with sqlite3.connect(bus._db_path) as conn:
            row = conn.execute("SELECT status FROM events WHERE trace_id=?", (trace_id,)).fetchone()
            if row and row[0] == "done":
                return str(row[0])
        time.sleep(0.01)
    return "timeout"


def _wait_for_handler_disposition(
    db_path: Path,
    trace_id: str,
    consumer: str,
    disposition: str,
    timeout: float = 5.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """SELECT 1 FROM handler_receipts
                   WHERE trace_id=? AND consumer=? AND disposition=?""",
                (trace_id, consumer, disposition),
            ).fetchone()
            if row:
                return True
        time.sleep(0.01)
    return False


def test_committed_episode_dispatches_stable_ids_to_real_graph_effects(tmp_path):
    from core.cognitive.cognition_episode_dispatch import (
        CognitionEpisodeDispatchOwner,
    )
    from core.cognitive.state_store import CognitiveStateStore
    from core.mnemos_bus import EventBus

    config = _RuntimeConfig(tmp_path)
    _result, receipt = _committed_full_episode(config)
    bus = EventBus(config=config)
    owner = CognitionEpisodeDispatchOwner(config=config, event_bus=bus)
    owner.subscribe()
    bus.start_dispatch()
    try:
        published = owner.publish_pending()
        assert published["published"] == 1
        assert published["events"][0]["revision_id"] == receipt.revision_id
        assert _wait_for_terminal_event(bus, published["events"][0]["trace_id"]) == "done"
    finally:
        bus.stop_dispatch()
        bus.close()

    state = CognitiveStateStore(config)
    assert state.pending_commands() == []
    receipts = state.effect_receipts_for_revision(receipt.revision_id)
    assert {item["consumer_id"] for item in receipts} == {
        "wiki",
        "knowledge_graph",
        "cognitive_graph",
    }
    assert all(item["status"] == "committed" for item in receipts)
    assert all(item["before_hash"].startswith("sha256:") for item in receipts)
    assert all(item["after_hash"].startswith("sha256:") for item in receipts)

    with sqlite3.connect(config.mnemos_dir / "events.db") as conn:
        event_row = conn.execute(
            "SELECT payload_json FROM events WHERE trace_id=?",
            (published["events"][0]["trace_id"],),
        ).fetchone()
        assert event_row is not None
        payload = event_row[0]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM events WHERE trace_id=?",
                (published["events"][0]["trace_id"],),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM event_trace_claims WHERE trace_id=?",
                (published["events"][0]["trace_id"],),
            ).fetchone()[0]
            == 1
        )
    assert "cognition_episode_committed.v1" in payload
    assert "Redis 连接池" not in payload
    assert "wiki_pages" not in payload

    with sqlite3.connect(config.cognitive_graph_db_path) as conn:
        relation_types = {
            row[0] for row in conn.execute("SELECT DISTINCT relation_type FROM cognitive_relations")
        }
    assert {
        "derived_from",
        "based_on",
        "predicted_from",
        "implements",
        "measures",
    } <= relation_types

    with sqlite3.connect(config.database_dir / "evidence_graph.db") as conn:
        node_types = {
            row[0] for row in conn.execute("SELECT DISTINCT node_type FROM evidence_nodes")
        }
        omission_gap = conn.execute(
            "SELECT COUNT(*) FROM cognition_episode_projection_omissions "
            "WHERE revision_id=? AND disposition!='omitted'",
            (receipt.revision_id,),
        ).fetchone()[0]
    assert {
        "raw_revision_span",
        "observation",
        "claim",
        "belief",
        "decision",
        "prediction",
        "action",
        "outcome",
        "episode",
    } <= node_types
    assert omission_gap == 0


def test_real_episode_dispatch_is_recallable_from_graph_channels_with_exact_provenance(
    tmp_path,
):
    from core.access_policy import AccessNarrowing, PrincipalEnvelope
    from core.cognitive.cognition_episode_dispatch import CognitionEpisodeDispatchOwner
    from core.cognitive.search import CognitiveSearch
    from core.cognitive.state_store import CognitiveStateStore
    from core.mnemos_bus import EventBus

    config = _RuntimeConfig(tmp_path)
    _result, receipt = _committed_full_episode(config)
    bus = EventBus(config=config)
    owner = CognitionEpisodeDispatchOwner(config=config, event_bus=bus)
    owner.subscribe()
    bus.start_dispatch()
    try:
        published = owner.publish_pending()
        assert published["published"] == 1
        assert _wait_for_terminal_event(bus, published["events"][0]["trace_id"]) == "done"
    finally:
        bus.stop_dispatch()
        bus.close()

    revision = CognitiveStateStore(config).revision(receipt.revision_id)
    assert revision is not None
    access = revision.payload["access_control"]
    scope = access["scope"]
    principal = PrincipalEnvelope(
        principal_id=str(access["owner"]["principal_id"]),
        agent=str(access["owner"]["agent"]),
        host_kind="integration",
        capability_id="real-cognition-graph-search",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset(
            {str(scope["project"])} if str(scope["project"]) else set()
        ),
    )
    narrowing = AccessNarrowing(
        session_id=str(scope["session_id"]),
        project=str(scope["project"]),
    )
    search = CognitiveSearch(
        state_db=config.database_dir / "producer_consumer_ledger.db",
        cognitive_graph_db=config.cognitive_graph_db_path,
        evidence_graph_db=config.database_dir / "evidence_graph.db",
    )

    hits, report = search.search(
        "提高连接上限后 连接耗尽告警显著下降",
        principal=principal,
        narrowing=narrowing,
        limit=10,
    )

    graph_hit = next(hit for hit in hits if hit.channel == "cognitive_graph")
    evidence_hit = next(hit for hit in hits if hit.channel == "evidence_graph")
    for hit in (graph_hit, evidence_hit):
        assert hit.revision_id == revision.revision_id
        assert hit.source_revision_id == revision.source_revision_id
        assert hit.source_span_ids
        assert hit.source_span_ids[0].startswith(revision.source_revision_id + "#")
        assert hit.acl_decision == "authorized"
        assert search.authorize_identity(
            channel=hit.channel,
            object_type=hit.object_type,
            object_id=hit.object_id,
            revision_id=hit.revision_id,
            source_revision_id=hit.source_revision_id,
            source_span_ids=hit.source_span_ids,
            matched_field=hit.matched_field,
            acl_decision=hit.acl_decision,
            is_current=hit.is_current,
            principal=principal,
            narrowing=narrowing,
        ) == (True, "authorized")
    assert graph_hit.matched_field in {
        "relation.source.content",
        "relation.target.content",
    }
    assert evidence_hit.matched_field in {
        "node.content",
        "edge.evidence[1].quote",
    }
    assert report["channels"]["cognitive_graph"]["source_trace_authorized_count"] == 1


def test_cognition_episode_projection_cannot_self_sign_a_generic_receipt(tmp_path):
    from core.cognitive.state_store import CognitiveStateStore

    config = _RuntimeConfig(tmp_path)
    _result, receipt = _committed_full_episode(config)
    state = CognitiveStateStore(config)
    command = next(
        item
        for item in state.commands_for_revision(receipt.revision_id)
        if item["consumer_id"] == "cognitive_graph"
    )

    with pytest.raises(PermissionError, match="specialized projection receipt"):
        state.record_effect_receipt(
            command["command_id"],
            status="committed",
            target_effect_id="forged-effect",
            before_hash="sha256:" + "0" * 64,
            after_hash="sha256:" + "1" * 64,
            evidence_refs=("forged-target-oracle",),
            outcome="forged",
        )


def test_wiki_projection_proof_cannot_select_an_unconfigured_forged_root(tmp_path):
    from core.cognitive.cognition_episode_projection_receipt import (
        CognitionEpisodeProjectionProof,
        projection_before_hash,
        projection_effect_id,
    )
    from core.cognitive.state_contract import sha256_json
    from core.cognitive.state_store import CognitiveStateStore

    config = _RuntimeConfig(tmp_path)
    _result, receipt = _committed_full_episode(config)
    state = CognitiveStateStore(config)
    command = next(
        item
        for item in state.commands_for_revision(receipt.revision_id)
        if item["consumer_id"] == "wiki"
    )
    forged_root = tmp_path / "forged-wiki"
    forged_root.mkdir()
    forged_page = forged_root / "forged.md"
    forged_page.write_text(
        "---\n" f"认知事件修订ID: {receipt.revision_id}\n" "---\n\n# Forged\n",
        encoding="utf-8",
    )
    content_hash = "sha256:" + hashlib.sha256(forged_page.read_bytes()).hexdigest()
    after_hash = sha256_json(
        {
            "revision_id": receipt.revision_id,
            "pages": [{"path": "forged.md", "content_sha256": content_hash}],
        }
    )
    proof = CognitionEpisodeProjectionProof(
        consumer_id="wiki",
        revision_id=receipt.revision_id,
        effect_id=projection_effect_id(str(command["command_id"]), "wiki"),
        before_hash=projection_before_hash(receipt.revision_id, "wiki"),
        after_hash=after_hash,
    )

    with pytest.raises(RuntimeError, match="configured Wiki projection"):
        state.record_cognition_episode_projection_receipt(
            str(command["command_id"]),
            proof=proof,
        )


def test_relation_failure_is_not_acked_and_restart_replays_only_missing_effects(tmp_path):
    from core.cognitive.cognition_episode_dispatch import CognitionEpisodeDispatchOwner
    from core.cognitive.state_store import CognitiveStateStore
    from core.mnemos_bus import EventBus

    config = _RuntimeConfig(tmp_path)
    config._values["event_bus.retry_base_seconds"] = 60
    config._values["event_bus.retry_max_seconds"] = 60
    _result, receipt = _committed_full_episode(config)

    first_bus = EventBus(config=config)
    first_owner = CognitionEpisodeDispatchOwner(
        config=config,
        event_bus=first_bus,
        fail_after_relation=3,
    )
    first_owner.subscribe()
    first_bus.start_dispatch()
    published = first_owner.publish_pending()
    trace_id = published["events"][0]["trace_id"]
    assert _wait_for_handler_disposition(
        first_bus._db_path,
        trace_id,
        "cognitive_graph",
        "retry",
    )
    first_bus.stop_dispatch()
    first_bus.close()

    state = CognitiveStateStore(config)
    episode_receipts = state.effect_receipts_for_revision(receipt.revision_id)
    assert {item["consumer_id"] for item in episode_receipts} == {
        "wiki",
        "knowledge_graph",
    }
    with sqlite3.connect(config.cognitive_graph_db_path) as conn:
        partial_relations = {
            row[0]: tuple(row)
            for row in conn.execute(
                "SELECT id, source, target, relation_type, created_at, updated_at "
                "FROM cognitive_relations ORDER BY id"
            )
        }
        assert len(partial_relations) == 3
        assert (
            conn.execute("SELECT COUNT(*) FROM cognition_episode_projection_effects").fetchone()[0]
            == 0
        )

    with sqlite3.connect(config.mnemos_dir / "events.db") as conn:
        conn.execute(
            "UPDATE events SET next_attempt_at='' WHERE trace_id=?",
            (trace_id,),
        )
        conn.commit()

    restarted_bus = EventBus(config=config)
    restarted_owner = CognitionEpisodeDispatchOwner(
        config=config,
        event_bus=restarted_bus,
    )
    restarted_owner.subscribe()
    restarted_bus.start_dispatch()
    try:
        assert _wait_for_terminal_event(restarted_bus, trace_id) == "done"
    finally:
        restarted_bus.stop_dispatch()
        restarted_bus.close()

    final_receipts = state.effect_receipts_for_revision(receipt.revision_id)
    assert {item["consumer_id"] for item in final_receipts} == {
        "wiki",
        "knowledge_graph",
        "cognitive_graph",
    }
    with sqlite3.connect(config.cognitive_graph_db_path) as conn:
        final_relations = {
            row[0]: tuple(row)
            for row in conn.execute(
                "SELECT id, source, target, relation_type, created_at, updated_at "
                "FROM cognitive_relations ORDER BY id"
            )
        }
        assert len(final_relations) > len(partial_relations)
        assert {
            relation_id: final_relations[relation_id] for relation_id in partial_relations
        } == partial_relations
        assert (
            conn.execute("SELECT COUNT(*) FROM cognition_episode_projection_effects").fetchone()[0]
            == 1
        )
    with sqlite3.connect(config.mnemos_dir / "events.db") as conn:
        dispositions = [
            row[0]
            for row in conn.execute(
                """SELECT disposition FROM handler_receipts
                   WHERE trace_id=? AND consumer='cognitive_graph'
                   ORDER BY id""",
                (trace_id,),
            )
        ]
    assert dispositions == ["retry", "ack"]


def test_internal_projection_failure_is_retry_not_false_ack(tmp_path):
    from core.cognitive.cognition_episode_dispatch import CognitionEpisodeDispatchOwner
    from core.cognitive.state_store import CognitiveStateStore
    from core.mnemos_bus import EventBus

    class FailingEvidenceGraph:
        def project_cognition_episode(self, **_kwargs):
            raise RuntimeError("injected evidence projection failure")

    config = _RuntimeConfig(tmp_path)
    config._values["event_bus.retry_base_seconds"] = 60
    config._values["event_bus.retry_max_seconds"] = 60
    _result, receipt = _committed_full_episode(config)
    bus = EventBus(config=config)
    owner = CognitionEpisodeDispatchOwner(
        config=config,
        event_bus=bus,
        evidence_graph=FailingEvidenceGraph(),
    )
    owner.subscribe()
    bus.start_dispatch()
    try:
        trace_id = owner.publish_pending()["events"][0]["trace_id"]
        assert _wait_for_handler_disposition(
            bus._db_path,
            trace_id,
            "knowledge_graph",
            "retry",
        )
    finally:
        bus.stop_dispatch()
        bus.close()

    receipts = CognitiveStateStore(config).effect_receipts_for_revision(receipt.revision_id)
    assert "knowledge_graph" not in {item["consumer_id"] for item in receipts}
    with sqlite3.connect(config.mnemos_dir / "events.db") as conn:
        assert (
            conn.execute(
                """SELECT COUNT(*) FROM handler_receipts
               WHERE trace_id=? AND consumer='knowledge_graph' AND disposition IN ('ack', 'noop')""",
                (trace_id,),
            ).fetchone()[0]
            == 0
        )


def test_evidence_projection_conflict_fails_closed_without_overwriting_existing_row(tmp_path):
    from core.cognitive.cognition_episode_dispatch import _episode_projection_manifest
    from core.cognitive.cognition_episode_projection_schema import (
        initialize_evidence_projection_schema,
    )
    from core.cognitive.state_store import CognitiveStateStore
    from core.evidence.evidence_graph import EvidenceGraph

    config = _RuntimeConfig(tmp_path)
    _result, receipt = _committed_full_episode(config)
    revision = CognitiveStateStore(config).revision(receipt.revision_id)
    assert revision is not None
    manifest = _episode_projection_manifest(revision)
    graph = EvidenceGraph(str(config.database_dir / "evidence_graph.db"))
    initialize_evidence_projection_schema(graph.db_path)
    conflicting = manifest["nodes"][0]
    with sqlite3.connect(graph.db_path) as conn:
        conn.execute(
            """INSERT INTO evidence_nodes
               (id, node_type, title, source_path, content, metadata,
                access_control, created_at)
               VALUES (?, 'memory', 'pre-existing row', '', 'must survive', '{}', '{}',
                       '2026-07-01T00:00:00+00:00')""",
            (conflicting["id"],),
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="node identity conflict"):
        graph.project_cognition_episode(
            effect_id="effect-conflict",
            revision_id=revision.revision_id,
            manifest_hash=manifest["evidence_manifest_hash"],
            nodes=manifest["nodes"],
            edges=manifest["evidence_edges"],
            omissions=manifest["omissions"],
            access_control=manifest["access_control"],
            created_at="2026-07-20T00:00:00+00:00",
        )

    with sqlite3.connect(graph.db_path) as conn:
        row = conn.execute(
            "SELECT node_type, title, content FROM evidence_nodes WHERE id=?",
            (conflicting["id"],),
        ).fetchone()
        effect_count = conn.execute(
            "SELECT COUNT(*) FROM cognition_episode_projection_effects"
        ).fetchone()[0]
    assert row == ("memory", "pre-existing row", "must survive")
    assert effect_count == 0


def test_tampered_committed_episode_event_fails_closed_without_effect_receipts(tmp_path):
    from core.cognitive.cognition_episode_dispatch import CognitionEpisodeDispatchOwner
    from core.cognitive.state_store import CognitiveStateStore
    from core.mnemos_bus import EventBus

    config = _RuntimeConfig(tmp_path)
    _result, receipt = _committed_full_episode(config)
    bus = EventBus(config=config)
    owner = CognitionEpisodeDispatchOwner(config=config, event_bus=bus)
    owner.subscribe()
    published = owner.publish_pending()
    trace_id = published["events"][0]["trace_id"]
    event_db_path = bus._db_path
    bus.close()
    with sqlite3.connect(event_db_path) as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM events WHERE trace_id=?", (trace_id,)
            ).fetchone()[0]
        )
        payload["wiki_pages"] = ["forged.md"]
        conn.execute(
            "UPDATE events SET payload_json=? WHERE trace_id=?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), trace_id),
        )
        conn.commit()

    restarted_bus = EventBus(config=config)
    restarted_owner = CognitionEpisodeDispatchOwner(
        config=config,
        event_bus=restarted_bus,
    )
    restarted_owner.subscribe()
    restarted_bus.start_dispatch()
    try:
        for consumer in ("wiki", "knowledge_graph", "cognitive_graph"):
            assert _wait_for_handler_disposition(
                restarted_bus._db_path,
                trace_id,
                consumer,
                "dead",
            )
    finally:
        restarted_bus.stop_dispatch()
        restarted_bus.close()

    assert not CognitiveStateStore(config).effect_receipts_for_revision(receipt.revision_id)


def test_evidence_upstream_traversal_reaches_full_raw_to_outcome_lineage(tmp_path):
    from core.cognitive.cognition_episode_dispatch import CognitionEpisodeDispatchOwner
    from core.cognitive.state_store import CognitiveStateStore
    from core.evidence.evidence_graph import EvidenceGraph
    from core.mnemos_bus import EventBus

    config = _RuntimeConfig(tmp_path)
    _result, receipt = _committed_full_episode(config)
    bus = EventBus(config=config)
    owner = CognitionEpisodeDispatchOwner(config=config, event_bus=bus)
    owner.subscribe()
    bus.start_dispatch()
    try:
        trace_id = owner.publish_pending()["events"][0]["trace_id"]
        assert _wait_for_terminal_event(bus, trace_id) == "done"
    finally:
        bus.stop_dispatch()
        bus.close()

    revision = CognitiveStateStore(config).revision(receipt.revision_id)
    assert revision is not None
    outcome_id = revision.payload["outcomes"][0]["entry_id"]
    lineage = EvidenceGraph(str(config.database_dir / "evidence_graph.db")).get_lineage(
        outcome_id, direction="upstream", depth=20
    )

    node_types = {node["node_type"] for node in lineage["nodes"].values()}
    relation_types = {edge["relation_type"] for edge in lineage["edges"]}
    assert {
        "raw_revision_span",
        "observation",
        "claim",
        "belief",
        "decision",
        "prediction",
        "action",
        "outcome",
    } <= node_types
    assert {
        "observed_in",
        "derived_from",
        "based_on",
        "predicted_from",
        "implements",
        "measures",
    } <= relation_types


def test_manifest_binds_evidence_to_exact_span_when_authority_id_is_repeated(tmp_path):
    from core.cognitive.cognition_episode_dispatch import (
        _episode_projection_manifest,
        _stable_id,
    )
    from core.cognitive.state_store import CognitiveStateStore

    config = _RuntimeConfig(tmp_path)
    _result, receipt = _committed_full_episode(config)
    revision = CognitiveStateStore(config).revision(receipt.revision_id)
    assert revision is not None
    payload = deepcopy(dict(revision.payload))
    original_span = dict(payload["source_spans"][0])
    colliding_span = {
        **original_span,
        "span_start": int(original_span["span_start"]) + 1,
        "span_end": int(original_span["span_end"]) + 1,
        "content_sha256": "sha256:" + "f" * 64,
    }
    payload["source_spans"].append(colliding_span)
    synthetic_revision = SimpleNamespace(
        object_type=revision.object_type,
        object_id=revision.object_id,
        revision_id=revision.revision_id,
        source_revision_id=revision.source_revision_id,
        evidence_refs=revision.evidence_refs,
        payload=payload,
        payload_hash=revision.payload_hash,
    )

    manifest = _episode_projection_manifest(synthetic_revision)

    exact_raw_span_id = _stable_id("rawspan", original_span)
    colliding_raw_span_id = _stable_id("rawspan", colliding_span)
    observed_targets = {
        edge["target_id"]
        for edge in manifest["evidence_edges"]
        if edge["relation_type"] == "observed_in"
    }
    assert exact_raw_span_id in observed_targets
    assert colliding_raw_span_id not in observed_targets


def test_distillation_write_boundary_publishes_only_the_committed_episode_event(
    tmp_path, monkeypatch
):
    from core.hephaestus.distillation_engine import DistillationEngine
    from core.mnemos_bus import EventBus

    config = _RuntimeConfig(tmp_path)
    result, receipt = _committed_full_episode(config)
    page = config.wiki_dir / "redis-decision.md"
    bus = EventBus(config=config)
    engine = SimpleNamespace(
        _runtime_receipt_config=config,
        _event_bus=bus,
    )
    monkeypatch.setattr("core.mnemos_bus.publish_event", lambda *args, **kwargs: "ignored")
    monkeypatch.setattr(
        "core.hephaestus.distillation_engine.publish_wiki_page_updated",
        lambda *args, **kwargs: {},
    )
    try:
        DistillationEngine._emit_distill_events(
            engine,
            result,
            [(page, result.fragments[0])],
            [str(page)],
        )
    finally:
        bus.close()

    with sqlite3.connect(config.mnemos_dir / "events.db") as conn:
        rows = conn.execute("SELECT event_type, payload_json FROM events ORDER BY id").fetchall()
    cognition_rows = [row for row in rows if row[0] == "cognition_episode_committed"]
    assert len(cognition_rows) == 1
    payload = cognition_rows[0][1]
    assert receipt.revision_id in payload
    assert "wiki_pages" not in payload
    assert "Redis 连接池" not in payload


def test_real_distillation_engine_binds_all_events_to_explicit_runtime_config(
    tmp_path, monkeypatch
):
    from core.hephaestus.distillation_engine import DistillationEngine
    from core.mnemos_bus import EventBus

    config = _RuntimeConfig(tmp_path)
    result, receipt = _committed_full_episode(config)
    page = config.wiki_dir / "redis-decision.md"
    bus = EventBus(config=config)
    monkeypatch.setattr(
        "core.hephaestus.distillation_engine.get_link_probe_worker",
        lambda: None,
    )
    backend = SimpleNamespace(call=lambda *_args, **_kwargs: {}, caller=None)
    engine = DistillationEngine(
        wiki_base=str(config.wiki_dir),
        backend_factory=lambda: backend,
        receipt_config=config,
        event_bus=bus,
    )
    try:
        engine._emit_distill_events(
            result,
            [(page, result.fragments[0])],
            [str(page)],
        )
    finally:
        bus.close()

    with sqlite3.connect(config.mnemos_dir / "events.db") as conn:
        cognition_events = conn.execute(
            """SELECT COUNT(*) FROM events
               WHERE event_type='cognition_episode_committed'
                 AND json_extract(payload_json, '$.episode_revision_id')=?""",
            (receipt.revision_id,),
        ).fetchone()[0]
        foreign_paths = conn.execute(
            "SELECT COUNT(*) FROM pragma_database_list WHERE file NOT LIKE ?",
            (str(config.mnemos_dir) + "%",),
        ).fetchone()[0]
    with sqlite3.connect(config.database_dir / "wiki_projection.db") as conn:
        wiki_mutations = conn.execute(
            "SELECT COUNT(*) FROM wiki_mutations WHERE page_path=?",
            (str(page.resolve()),),
        ).fetchone()[0]
    assert cognition_events == 1
    assert foreign_paths == 0
    assert wiki_mutations >= 1


def test_two_event_bus_processes_cannot_consume_the_same_unexpired_lease(tmp_path):
    from core.mnemos_bus import Event, EventBus, HandlerOutcome

    config = _RuntimeConfig(tmp_path)
    calls: list[str] = []

    def handler(event):
        calls.append(event.trace_id)
        time.sleep(0.2)
        return HandlerOutcome.ack("lease-probe")

    first = EventBus(config=config)
    first.subscribe("session.start", handler, consumer_id="lease-probe")
    trace_id = first.publish(
        Event(
            event_type="session.start",
            source="lease-test",
            payload={"probe": True},
        )
    )
    second = EventBus(config=config)
    second.subscribe("session.start", handler, consumer_id="lease-probe")
    first.start_dispatch()
    second.start_dispatch()
    try:
        assert _wait_for_terminal_event(first, trace_id) == "done"
        time.sleep(0.3)
    finally:
        first.stop_dispatch()
        second.stop_dispatch()
        first.close()
        second.close()

    assert calls == [trace_id]


def test_cognition_episode_handler_is_not_replayed_while_a_timed_call_is_still_running(
    tmp_path,
):
    from core.event_outcome import HandlerOutcome
    from core.mnemos_bus import Event, EventBus

    config = _RuntimeConfig(tmp_path)
    config._values["event_bus.handler_timeout_seconds"] = 0.05
    config._values["event_bus.lease_seconds"] = 0.1
    calls: list[str] = []

    def slow_handler(event):
        calls.append(event.trace_id)
        time.sleep(0.2)
        return HandlerOutcome.ack("holdout-consumer", effect_id="holdout-effect")

    bus = EventBus(config=config)
    bus.subscribe(
        "cognition_episode_committed",
        slow_handler,
        consumer_id="holdout-consumer",
    )
    bus.start_dispatch()
    try:
        trace_id = bus.publish(
            Event(
                event_type="cognition_episode_committed",
                source="lease-holdout",
                payload={"episode_revision_id": "cogrev-timeout-holdout"},
            )
        )
        assert _wait_for_terminal_event(bus, trace_id) == "done"
        time.sleep(0.15)
        with sqlite3.connect(bus._db_path) as conn:
            row = conn.execute(
                "SELECT retry_count, lease_owner FROM events WHERE trace_id=?",
                (trace_id,),
            ).fetchone()
            terminal_receipts = conn.execute(
                """SELECT COUNT(*) FROM handler_receipts
                   WHERE trace_id=? AND consumer='holdout-consumer'
                     AND disposition IN ('ack','noop')""",
                (trace_id,),
            ).fetchone()[0]
    finally:
        bus.stop_dispatch()
        bus.close()

    assert row == (0, "")
    assert terminal_receipts == 1
    assert calls == [trace_id]


def test_independent_audit_closes_a_holdout_episode_with_nonsequential_entry_ids(
    tmp_path,
):
    from core.cognitive.cognition_episode_dispatch import CognitionEpisodeDispatchOwner
    from core.cognitive.cognition_episode_dispatch_audit import build_report
    from core.cognitive.state_store import CognitiveStateStore
    from core.mnemos_bus import EventBus

    config = _RuntimeConfig(tmp_path)
    _result, receipt = _committed_full_episode(config, holdout_order=True)
    revision = CognitiveStateStore(config).revision(receipt.revision_id)
    assert revision is not None
    fact_ids = [entry["entry_id"] for entry in revision.payload["facts"]]
    assert len(fact_ids) == 2
    assert fact_ids == sorted(fact_ids, reverse=True)

    bus = EventBus(config=config)
    owner = CognitionEpisodeDispatchOwner(config=config, event_bus=bus)
    owner.subscribe()
    bus.start_dispatch()
    try:
        trace_id = owner.publish_pending()["events"][0]["trace_id"]
        assert _wait_for_terminal_event(bus, trace_id) == "done"
    finally:
        bus.stop_dispatch()
        bus.close()

    report = build_report(
        database_dir=config.database_dir,
        event_db_path=config.mnemos_dir / "events.db",
        wiki_dir=config.wiki_dir,
        cognitive_graph_db_path=config.cognitive_graph_db_path,
        wiki_projection_db_path=config.database_dir / "wiki_projection.db",
    )
    assert report["ok"] is True
    assert report["runtime"]["episode_count"] == 1
