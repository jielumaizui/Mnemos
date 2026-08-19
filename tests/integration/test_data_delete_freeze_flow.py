import sqlite3

import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.privacy.data_ownership import DataOwnershipManager, DeletionProof
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.state_contract import CognitiveStateRevision, LocalConsumerCommand, sha256_json
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.models import Dimension, Observation, ObservationType, SourceType
from core.cognitive.observation_store import ObservationStore
from core.ops.cognitive_data_contract import CognitiveDataEvent
from core.telemetry.prompt_call_log import ModelCallLedger, metered_provider_usage
from core.sync_framework.raw_event_store import RawEventStore
from core.sync_framework.sync_engine import SyncEngine
from core.reflection.models import ReflectionRecord, ReflectionTrigger
from core.reflection.reflection_store import ReflectionStore
from core.cognitive_graph.store import (
    COGNITIVE_RELATION_ACTION,
    COGNITIVE_RELATION_EXECUTOR,
    COGNITIVE_RELATION_OWNER,
    CognitiveGraphStore,
    cognitive_relation_material_action_binding,
)
from core.wiki_projection_lifecycle import WikiProjectionLedger
from core.embeddings.cache import EmbeddingCache
from core.persona.cognitive_profile import ProfileAssertion, ProfileSignal, ProfileUsageLog
from core.persona.psyche import SignalStore
from tests.cognitive_decision_fixtures import material_action_authorization


class FakeConfig:
    def __init__(self, root: Path):
        self.mnemos_dir = root
        self.data_dir = root
        self.database_dir = root / "db"
        self.database_dir.mkdir(parents=True)
        self._vaults = {"mnemos": root / "mnemos_vault", "raw": root / "raw_vault"}
        for vault in self._vaults.values():
            vault.mkdir()

    def vault_dir(self, name: str) -> Path:
        return self._vaults[name]

    def get(self, _key: str, default=None):
        if _key == "llm.provider_prices":
            return {"test": {"model": {"input": 0.1, "output": 0.2}}}
        return default


def _delete_snapshot(manager: DataOwnershipManager, scope: str) -> str:
    return manager.create_delete_snapshot(scope, retention_days=1).snapshot_id


def test_delete_dry_run_requires_freeze_snapshot_and_confirmation(tmp_path):
    manager = DataOwnershipManager(FakeConfig(tmp_path))
    request = manager.delete("session:test", dry_run=True)

    assert request.status == "dry_run_planned"
    assert request.requires_freeze is True
    assert request.requires_snapshot is True
    assert request.requires_confirmation is True


def test_delete_apply_requires_freeze_before_proof(tmp_path):
    config = FakeConfig(tmp_path)
    manager = DataOwnershipManager(config)
    with pytest.raises(PermissionError):
        manager.delete(
            "session:test",
            dry_run=False,
            apply=True,
            confirm=True,
            snapshot_ref="snap:test",
        )

    ledger = ModelCallLedger.for_config(config)
    run_id = ledger.start_run("opaque-deletion-run", subject_scope=("session", "test"))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="distill_extract",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.mark_dispatched()
    usage = metered_provider_usage(
        {"prompt_tokens": 1, "completion_tokens": 0},
        request_id="usage-delete-1",
        output_required=True,
    )
    assert usage is not None
    reservation.settle(usage=usage)

    manager.freeze("session:test")
    snapshot_ref = _delete_snapshot(manager, "session:test")
    proof = manager.delete(
        "session:test",
        dry_run=False,
        apply=True,
        confirm=True,
        snapshot_ref=snapshot_ref,
    )

    assert isinstance(proof, DeletionProof)
    assert proof.status == "verified"
    assert proof.affected_domains == ("model_call_ledger",)
    assert proof.verification_results["model_call_ledger"] == {
        "status": "applied",
        "matched_run_count": 1,
        "deleted_entry_count": 1,
        "deleted_run_count": 1,
    }
    assert ledger.run_summary(run_id)["exists"] is False
    assert b"private prompt must not persist" not in ledger.db_path.read_bytes()
    assert proof.validate() == []


def test_delete_apply_rejects_an_unverifiable_snapshot_literal_before_mutation(tmp_path):
    config = FakeConfig(tmp_path)
    manager = DataOwnershipManager(config)
    manager.freeze("session:invalid-snapshot")

    with pytest.raises(PermissionError, match="valid retained snapshot"):
        manager.delete(
            "session:invalid-snapshot",
            dry_run=False,
            apply=True,
            confirm=True,
            snapshot_ref="snapshot:invented-literal",
        )

    with sqlite3.connect(manager.db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM data_ownership_requests WHERE request_type='delete_proof'"
            ).fetchone()[0]
            == 0
        )


def _cognitive_revision_and_event() -> tuple[CognitiveStateRevision, CognitiveDataEvent]:
    access_control = make_cognitive_access_envelope(
        owner_principal_id="test:data-owner",
        owner_agent="data-owner-test",
        scope_type="project",
        scope_id="mnemos",
        project="mnemos",
        purposes=("cognitive_state_read",),
        consent_provenance_refs=(sha256_json({"consent": "test"}),),
        sensitivity="sensitive",
        retention_policy="cognitive_state",
        source_acl_lineage=(sha256_json({"source": "test"}),),
    )
    content_hash = sha256_json({"source": "data-delete-test"})
    revision = CognitiveStateRevision.create(
        object_type="cognitive_update_receipt",
        object_id="delete-test-state",
        source_event_id="cde-data-delete-state",
        source_revision_id="raw-data-delete-state",
        source_content_hash=content_hash,
        scope_type="project",
        scope_id="mnemos",
        evidence_refs=("raw-event:data-delete#0:1",),
        payload={
            "input_refs": ["raw-event:data-delete#0:1"],
            "attribution": {"action": "test"},
            "target_command_ref": "command:test",
            "before_hash": sha256_json({"before": "test"}),
            "after_hash": sha256_json({"after": "test"}),
            "effect_receipt_ref": "pending",
            "access_control": access_control,
        },
    )
    event = CognitiveDataEvent(
        event_id=revision.source_event_id,
        source_id="raw-data-delete-state",
        asset_id="asset-data-delete-state",
        source_kind="test",
        source_uri="test://data-delete/state",
        content_hash=content_hash,
        canonical_subject="data-delete-test-state",
        data_type="cognitive_update_receipt",
        producer="test",
        intended_consumers=("wiki",),
        privacy_level="private",
        confidence=1.0,
        evidence_refs=revision.evidence_refs,
        dedupe_key="data-delete-test-state",
        created_at="2026-07-16T00:00:00+00:00",
    )
    return revision, event


def test_delete_plans_cognitive_tombstone_before_partial_physical_deletion(tmp_path):
    config = FakeConfig(tmp_path)
    state_path = config.database_dir / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_path)
    store = CognitiveStateStore(config)
    revision, event = _cognitive_revision_and_event()
    command = LocalConsumerCommand.create(
        revision_id=revision.revision_id,
        consumer_id="wiki",
        command_type="project_cognition_update_receipt",
        payload={"projection": "wiki"},
    )
    store.unit_of_work().commit(revisions=(revision,), event=event, commands=(command,))

    manager = DataOwnershipManager(config)
    manager.freeze("project:mnemos")
    snapshot_ref = _delete_snapshot(manager, "project:mnemos")
    proof = manager.delete(
        "project:mnemos",
        dry_run=False,
        apply=True,
        confirm=True,
        snapshot_ref=snapshot_ref,
    )

    assert isinstance(proof, DeletionProof)
    assert proof.status == "partially_deleted"
    assert store.current_revision(revision.object_type, revision.object_id) is None
    cognitive_state = proof.verification_results["cognitive_state"]
    assert cognitive_state["status"] == "pending_consumer_receipts"
    assert cognitive_state["target_count"] == 1
    assert cognitive_state["required_consumers"] == ["wiki"]
    tombstone = next(
        item
        for item in store.pending_commands()
        if item["command_type"] == "tombstone_cognitive_state"
    )
    from core.cognitive.tombstone_consumer_coordinator import (
        apply_receipt_only_cognitive_tombstones,
    )

    checkpoint = apply_receipt_only_cognitive_tombstones(
        store,
        request_id=str(tombstone["payload"]["request_id"]),
    )
    assert checkpoint["status"] == "unsupported_consumers"
    assert checkpoint["unsupported_consumers"] == ("wiki",)
    assert checkpoint["verified"] is False
    assert store.effect_receipt(str(tombstone["command_id"])) is None


def test_delete_retry_cannot_escape_pending_cognitive_tombstone_receipts(tmp_path):
    config = FakeConfig(tmp_path)
    state_path = config.database_dir / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_path)
    store = CognitiveStateStore(config)
    revision, event = _cognitive_revision_and_event()
    command = LocalConsumerCommand.create(
        revision_id=revision.revision_id,
        consumer_id="wiki",
        command_type="project_cognition_update_receipt",
        payload={"projection": "wiki"},
    )
    store.unit_of_work().commit(revisions=(revision,), event=event, commands=(command,))

    manager = DataOwnershipManager(config)
    manager.freeze("project:mnemos")
    snapshot_ref = _delete_snapshot(manager, "project:mnemos")
    first = manager.delete(
        "project:mnemos",
        dry_run=False,
        apply=True,
        confirm=True,
        snapshot_ref=snapshot_ref,
    )
    retry = manager.delete(
        "project:mnemos",
        dry_run=False,
        apply=True,
        confirm=True,
        snapshot_ref=snapshot_ref,
    )

    assert first.status == "partially_deleted"
    assert retry.status == "partially_deleted"
    assert retry.verification_results["cognitive_state"]["status"] == ("pending_consumer_receipts")
    assert retry.verification_results["observation"]["status"] == ("pending_checkpoint")
    assert retry.verification_results["remaining_unimplemented_domains"] == (
        "cognitive_state",
        "observation",
    )


def test_delete_retry_becomes_verified_after_cognitive_tombstone_receipts_commit(
    tmp_path,
):
    config = FakeConfig(tmp_path)
    state_path = config.database_dir / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_path)
    store = CognitiveStateStore(config)
    revision, event = _cognitive_revision_and_event()
    command = LocalConsumerCommand.create(
        revision_id=revision.revision_id,
        consumer_id="wiki",
        command_type="project_cognition_update_receipt",
        payload={"projection": "wiki"},
    )
    store.unit_of_work().commit(revisions=(revision,), event=event, commands=(command,))

    manager = DataOwnershipManager(config)
    manager.freeze("project:mnemos")
    snapshot_ref = _delete_snapshot(manager, "project:mnemos")
    first = manager.delete(
        "project:mnemos",
        dry_run=False,
        apply=True,
        confirm=True,
        snapshot_ref=snapshot_ref,
    )
    tombstone_commands = [
        item
        for item in store.pending_commands()
        if item["command_type"] == "tombstone_cognitive_state"
    ]
    assert len(tombstone_commands) == 1
    tombstone = tombstone_commands[0]
    payload = tombstone["payload"]
    store.record_effect_receipt(
        tombstone["command_id"],
        status="committed",
        target_effect_id=f"tombstone:wiki:{payload['request_id']}",
        before_hash=payload["before_hash"],
        after_hash=payload["tombstone_hash"],
        evidence_refs=(
            f"tombstone-command:{tombstone['command_id']}",
            f"tombstone-oracle:wiki:{payload['tombstone_hash']}",
        ),
    )

    retry = manager.delete(
        "project:mnemos",
        dry_run=False,
        apply=True,
        confirm=True,
        snapshot_ref=snapshot_ref,
    )

    assert first.status == "partially_deleted"
    assert retry.status == "verified", retry.verification_results
    assert retry.verification_results["cognitive_state"]["status"] == "verified"
    assert retry.verification_results["remaining_unimplemented_domains"] == ()


def test_cognitive_state_writes_are_blocked_after_matching_freeze(tmp_path):
    config = FakeConfig(tmp_path)
    state_path = config.database_dir / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_path)
    manager = DataOwnershipManager(config)
    manager.freeze("project:mnemos")
    revision, event = _cognitive_revision_and_event()
    command = LocalConsumerCommand.create(
        revision_id=revision.revision_id,
        consumer_id="wiki",
        command_type="project_cognition_update_receipt",
        payload={"projection": "wiki"},
    )

    with pytest.raises(PermissionError, match="frozen"):
        CognitiveStateStore(config).unit_of_work().commit(
            revisions=(revision,), event=event, commands=(command,)
        )


def test_observation_writes_are_blocked_after_matching_freeze(tmp_path):
    config = FakeConfig(tmp_path)
    manager = DataOwnershipManager(config)
    manager.freeze("session:frozen-observation-session")
    access_control = make_cognitive_access_envelope(
        owner_principal_id="test:data-owner",
        owner_agent="data-owner-test",
        scope_type="observation",
        scope_id="frozen-observation",
        session_id="frozen-observation-session",
        project="mnemos",
        purposes=("observation_read",),
        consent_provenance_refs=(sha256_json({"consent": "observation"}),),
        sensitivity="sensitive",
        retention_policy="observation_retention",
        source_acl_lineage=(sha256_json({"source": "observation"}),),
    )

    with pytest.raises(PermissionError, match="frozen"):
        ObservationStore(
            str(config.database_dir / "observations.db"),
            ownership_config=config,
        ).save(
            Observation(
                id="frozen-observation",
                dimension=Dimension.ATTENTION,
                observation_type=ObservationType.FREQUENCY,
                value={"private": "must not resurrect"},
                source_type=SourceType.RAW,
                source_id="raw-frozen-observation",
                access_control=access_control,
            )
        )


def test_reflection_writes_are_blocked_after_matching_freeze(tmp_path):
    config = FakeConfig(tmp_path)
    manager = DataOwnershipManager(config)
    manager.freeze("session:frozen-reflection-session")
    access_control = make_cognitive_access_envelope(
        owner_principal_id="test:data-owner",
        owner_agent="data-owner-test",
        scope_type="reflection",
        scope_id="frozen-reflection",
        session_id="frozen-reflection-session",
        project="mnemos",
        purposes=("reflection_read",),
        consent_provenance_refs=(sha256_json({"consent": "reflection"}),),
        sensitivity="sensitive",
        retention_policy="reflection_retention",
        source_acl_lineage=(sha256_json({"source": "reflection"}),),
    )

    with pytest.raises(PermissionError, match="frozen"):
        ReflectionStore(
            str(config.database_dir / "reflections.db"),
            ownership_config=config,
        ).save_record(
            ReflectionRecord(
                id="frozen-reflection",
                created_at=datetime.now(),
                trigger=ReflectionTrigger.MANUAL,
                user_query="must not resurrect",
                access_control=access_control,
            )
        )


def test_cognitive_graph_writes_are_blocked_after_matching_freeze(tmp_path):
    config = FakeConfig(tmp_path)
    manager = DataOwnershipManager(config)
    manager.freeze("session:frozen-graph-session")
    access_control = make_cognitive_access_envelope(
        owner_principal_id="test:data-owner",
        owner_agent="data-owner-test",
        scope_type="session",
        scope_id="frozen-graph-session",
        session_id="frozen-graph-session",
        project="mnemos",
        purposes=("cognitive_graph_read",),
        consent_provenance_refs=(sha256_json({"consent": "graph"}),),
        sensitivity="sensitive",
        retention_policy="cognitive_graph_retention",
        source_acl_lineage=(sha256_json({"source": "graph"}),),
    )

    with pytest.raises(PermissionError, match="frozen"):
        CognitiveGraphStore(
            str(config.database_dir / "cognitive_graph.db"),
            ownership_config=config,
        ).add_relation(
            "session://frozen-graph-session",
            "kg://must-not-resurrect",
            "derived_from",
            access_control=access_control,
        )


def test_retired_scoring_writer_cannot_bypass_matching_freeze(tmp_path, monkeypatch):
    from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

    config = FakeConfig(tmp_path)
    monkeypatch.setattr("core.config.get_config", lambda: config)
    manager = DataOwnershipManager(config)
    manager.freeze("session:frozen-scoring-session")
    access_control = make_cognitive_access_envelope(
        owner_principal_id="test:data-owner",
        owner_agent="data-owner-test",
        scope_type="session",
        scope_id="frozen-scoring-session",
        session_id="frozen-scoring-session",
        project="mnemos",
        purposes=("score_training",),
        consent_provenance_refs=(sha256_json({"consent": "scoring"}),),
        sensitivity="sensitive",
        retention_policy="scoring_retention",
        source_acl_lineage=(sha256_json({"source": "scoring"}),),
    )

    with pytest.raises(PermissionError, match="training_admission_receipt_required"):
        AdaptiveScorerV2.enqueue_training_sample(
            session_id="frozen-scoring-write",
            dimension="kg",
            features={"private": "must not resurrect"},
            expected_score=1.0,
            source="freeze-test",
            db_path=str(config.database_dir / "mnemos.db"),
            subject_provenance=access_control,
        )
    assert not (config.database_dir / "mnemos.db").exists()


def test_action_ledger_writes_are_blocked_atomically_after_matching_freeze(tmp_path, monkeypatch):
    from core.system_contracts import ActionLedger, make_action_record

    config = FakeConfig(tmp_path)
    monkeypatch.setattr("core.config.get_config", lambda: config)
    manager = DataOwnershipManager(config)
    manager.freeze("session:frozen-action-session")
    access_control = make_cognitive_access_envelope(
        owner_principal_id="test:data-owner",
        owner_agent="data-owner-test",
        scope_type="session",
        scope_id="frozen-action-session",
        session_id="frozen-action-session",
        project="mnemos",
        purposes=("data_delete",),
        consent_provenance_refs=(sha256_json({"consent": "action"}),),
        sensitivity="sensitive",
        retention_policy="action_retention",
        source_acl_lineage=(sha256_json({"source": "action"}),),
    )
    ledger = ActionLedger(
        config.database_dir / "action_ledger.db",
        initialize=True,
    )

    with pytest.raises(PermissionError, match="frozen"):
        ledger.record(
            make_action_record(
                actor="freeze-test",
                action_type="data_delete",
                target="must-not-resurrect",
                evidence_refs=("sha256:" + "a" * 64,),
                rollback_ref="manual",
                subject_provenance=access_control,
            )
        )

    with sqlite3.connect(config.database_dir / "action_ledger.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM action_ledger").fetchone()[0] == 0


def test_event_bus_writes_are_blocked_atomically_after_matching_freeze(tmp_path, monkeypatch):
    from core.mnemos_bus import Event, EventBus, HandlerOutcome

    config = FakeConfig(tmp_path)
    monkeypatch.setattr("core.config.get_config", lambda: config)
    manager = DataOwnershipManager(config)
    manager.freeze("session:frozen-event-session")
    access_control = make_cognitive_access_envelope(
        owner_principal_id="test:data-owner",
        owner_agent="data-owner-test",
        scope_type="session",
        scope_id="frozen-event-session",
        session_id="frozen-event-session",
        project="mnemos",
        purposes=("event_dispatch",),
        consent_provenance_refs=(sha256_json({"consent": "event"}),),
        sensitivity="sensitive",
        retention_policy="event_retention",
        source_acl_lineage=(sha256_json({"source": "event"}),),
    )
    bus = EventBus(config=config)
    bus.subscribe(
        "freeze-test-event",
        lambda _event: HandlerOutcome.ack("freeze-test-event"),
        consumer_id="freeze-test-event",
    )
    try:
        with pytest.raises(PermissionError, match="frozen"):
            bus.publish(
                Event(
                    event_type="freeze-test-event",
                    source="freeze-test",
                    payload={"private": "must not resurrect"},
                    trace_id="frozen-event-trace",
                    subject_provenance=access_control,
                )
            )
    finally:
        bus.close()

    with sqlite3.connect(config.mnemos_dir / "events.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM event_trace_claims").fetchone()[0] == 0


def test_delete_apply_redacts_matching_canonical_raw_before_proof(tmp_path):
    config = FakeConfig(tmp_path)
    raw_path = config.database_dir / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=config)
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="ownership-delete-session",
            turn_number=1,
            user_content="raw body that must be deleted",
            assistant_content="raw reply that must be deleted",
            metadata={"project": "mnemos"},
            completeness={"visible_text": "full"},
        )
        event_id = store.get_logical_event_id(revision_id)
    finally:
        store.close()

    # The model-call owner remains independently initialized so the existing
    # partial-proof contract can run alongside the new canonical Raw adapter.
    ModelCallLedger.for_config(config)
    manager = DataOwnershipManager(config)
    manager.freeze("session:ownership-delete-session")
    snapshot_ref = _delete_snapshot(manager, "session:ownership-delete-session")
    proof = manager.delete(
        "session:ownership-delete-session",
        dry_run=False,
        apply=True,
        confirm=True,
        snapshot_ref=snapshot_ref,
    )

    assert proof.status == "verified"
    assert proof.affected_domains == ("model_call_ledger", "raw")
    assert proof.verification_results["raw"]["status"] == "applied"
    assert proof.verification_results["raw"]["target_count"] == 1

    reopened = RawEventStore(db_path=raw_path, config=config)
    try:
        assert reopened.get_turn(revision_id) is None
        assert reopened.get_turn(event_id) is None
        assert reopened.list_current_headers(session_id="ownership-delete-session") == []
    finally:
        reopened.close()


def test_delete_apply_removes_exact_agent_source_metadata_but_keeps_unmapped_audit_pending(
    tmp_path,
):
    config = FakeConfig(tmp_path)
    sync_path = config.database_dir / "sync_log.db"
    engine = SyncEngine(backend=Mock(), db_path=str(sync_path), config=config)
    engine.close()
    with sqlite3.connect(sync_path) as conn:
        conn.execute(
            """
            INSERT INTO sync_log (
                agent_name, session_id, turn_number, content_hash, status
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("codex", "ownership-source-session", 0, "source-hash", "synced"),
        )
        conn.execute(
            """
            INSERT INTO user_signals (
                timestamp, agent, session_id, turn_number, content_length,
                has_code, has_tools, user_questions
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-07-16T00:00:00+00:00", "codex", "ownership-source-session", 0, 1, 0, 0, 0),
        )
        # Current sync_audit rows do not carry a session key.  The deletion
        # owner must not broaden a session request just to turn green.
        conn.execute(
            "INSERT INTO sync_audit (source, audit_type, created_at) VALUES (?, ?, ?)",
            ("codex", "l1_scan", 1.0),
        )

    ModelCallLedger.for_config(config)
    manager = DataOwnershipManager(config)
    manager.freeze("session:ownership-source-session")
    snapshot_ref = _delete_snapshot(manager, "session:ownership-source-session")
    proof = manager.delete(
        "session:ownership-source-session",
        dry_run=False,
        apply=True,
        confirm=True,
        snapshot_ref=snapshot_ref,
    )

    result = proof.verification_results["agent_source_metadata"]
    assert proof.status == "partially_deleted"
    assert "agent_source_metadata" in proof.affected_domains
    assert result == {
        "status": "applied",
        "target_count": 2,
        "receipt_count": 1,
        "sync_log_deleted": 1,
        "user_signals_deleted": 1,
        "sync_audit_deleted": 0,
        "after_count": 0,
        "unresolved_sync_audit_count": 1,
        "verified": False,
    }
    with sqlite3.connect(sync_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sync_log WHERE session_id=?",
                ("ownership-source-session",),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM user_signals WHERE session_id=?",
                ("ownership-source-session",),
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM sync_audit").fetchone()[0] == 1


def test_delete_apply_verifies_raw_consumer_access_log_after_oracle(tmp_path):
    config = FakeConfig(tmp_path)
    raw_path = config.database_dir / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=config)
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="ownership-access-log-session",
            turn_number=1,
            user_content="raw body with an access trace",
            assistant_content="raw reply",
            metadata={"project": "mnemos"},
            completeness={"visible_text": "full"},
        )
        store.record_access(
            revision_id,
            "search",
            query="subject-derived search trace",
            consumer="context_search",
        )
    finally:
        store.close()

    ModelCallLedger.for_config(config)
    manager = DataOwnershipManager(config)
    manager.freeze("session:ownership-access-log-session")
    snapshot_ref = _delete_snapshot(manager, "session:ownership-access-log-session")
    proof = manager.delete(
        "session:ownership-access-log-session",
        dry_run=False,
        apply=True,
        confirm=True,
        snapshot_ref=snapshot_ref,
    )

    assert proof.status == "verified"
    assert proof.affected_domains == (
        "consumer_access_log",
        "model_call_ledger",
        "raw",
    )
    assert proof.verification_results["consumer_access_log"] == {
        "status": "applied",
        "target_count": 1,
        "after_count": 0,
        "verified": True,
        "owner": "raw_event_store",
    }
    with sqlite3.connect(raw_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_access_log").fetchone()[0] == 0


def test_delete_apply_globally_flushes_unattributable_embedding_cache(tmp_path):
    config = FakeConfig(tmp_path)
    cache = EmbeddingCache(
        db_path=config.database_dir / "embedding_cache.db",
        model_version="test-model",
    )
    cache.set("embedding derived from deleted subject", [1.0])
    cache.set("embedding derived from another subject", [2.0])
    ModelCallLedger.for_config(config)

    manager = DataOwnershipManager(config)
    manager.freeze("session:embedding-cache-delete")
    snapshot_ref = _delete_snapshot(manager, "session:embedding-cache-delete")
    proof = manager.delete(
        "session:embedding-cache-delete",
        dry_run=False,
        apply=True,
        confirm=True,
        snapshot_ref=snapshot_ref,
    )

    assert proof.status == "verified"
    assert proof.affected_domains == ("embedding_cache", "model_call_ledger")
    assert proof.verification_results["embedding_cache"] == {
        "status": "applied",
        "target_count": 2,
        "receipt_count": 1,
        "deleted_entry_count": 2,
        "after_entry_count": 0,
        "verified": True,
        "mode": "global_unattributable_cache_flush",
    }
    assert cache.get_stats()["total_entries"] == 0


def test_delete_apply_removes_acl_matched_persona_derivatives(tmp_path):
    config = FakeConfig(tmp_path)
    persona_store = SignalStore(initialize_schema=True, db_path=config.database_dir / "user_signals.db")
    try:
        access_control = make_cognitive_access_envelope(
            owner_principal_id="mcp:codex:persona",
            owner_agent="codex",
            scope_type="session",
            scope_id="persona-delete-session",
            session_id="persona-delete-session",
            project="mnemos",
            purposes=(
                "persona_preflight_read",
                "persona_summary_read",
                "persona_usage_metrics",
            ),
            consent_provenance_refs=("raw:persona-delete",),
            sensitivity="sensitive",
            retention_policy="persona_retention",
            source_acl_lineage=("sha256:" + "e" * 64,),
            visibility="private",
        )
        signal_id = persona_store.record_profile_signal(
            ProfileSignal(
                source_event_id="raw-persona-delete",
                signal_type="preference",
                dimension="detail",
                value="persona content that must be deleted",
                access_control=access_control,
            )
        )
        assertion_id = persona_store.upsert_profile_assertion(
            ProfileAssertion(
                assertion_id="persona-delete-assertion",
                dimension="detail",
                claim="persona assertion that must be deleted",
                supporting_signals=[f"profile_signals:{signal_id}"],
            )
        )
        from core.persona.profile_effect import compare_profile_effect

        revision_id = str(
            persona_store.get_profile_assertion_revisions(assertion_id)[-1]["revision_id"]
        )
        read_principal = PrincipalEnvelope(
            principal_id="mcp:codex:persona",
            agent="codex",
            host_kind="codex",
            capability_id="persona-delete",
            capabilities=frozenset({"memory_read"}),
            allowed_projects=frozenset({"mnemos"}),
        )
        read_narrowing = AccessNarrowing(
            session_id="persona-delete-session",
            project="mnemos",
        )
        _profile, read_access = persona_store.build_authorized_user_cognitive_profile_v2(
            principal=read_principal,
            narrowing=read_narrowing,
            purpose="persona_preflight_read",
            consumer="preflight_builder",
        )
        persona_store.record_profile_usage(
            ProfileUsageLog(
                consumer="preflight_builder",
                profile_fields_used=[assertion_id],
                read_purpose="persona_preflight_read",
                read_authorization_token=str(read_access["read_authorization_token"]),
                target_receipt=compare_profile_effect(
                    owner="preflight_builder",
                    target_type="test_target",
                    target_id="data_delete_profile_usage",
                    matched_assertion_revisions={assertion_id: revision_id},
                    baseline_output="before",
                    persona_enabled_output="after",
                    expected_delta={"kind": "test_delta"},
                    receipt_id="data-delete-profile-target",
                ),
                outcome="persona use that must be deleted",
            ),
            principal=read_principal,
            narrowing=read_narrowing,
        )
    finally:
        persona_store.close()

    ModelCallLedger.for_config(config)
    manager = DataOwnershipManager(config)
    manager.freeze("session:persona-delete-session")
    snapshot_ref = _delete_snapshot(manager, "session:persona-delete-session")
    proof = manager.delete(
        "session:persona-delete-session",
        dry_run=False,
        apply=True,
        confirm=True,
        snapshot_ref=snapshot_ref,
    )

    assert proof.status == "verified"
    assert proof.affected_domains == ("model_call_ledger", "persona")
    assert proof.verification_results["persona"] == {
        "status": "applied",
        "target_count": 5,
        "receipt_count": 5,
        "profile_signals_deleted": 1,
        "profile_assertions_deleted": 1,
        "profile_usage_logs_deleted": 1,
        "profile_read_authorizations_deleted": 1,
        "profile_usage_outboxes_deleted": 1,
        "unresolved_legacy_count": 0,
        "unmapped_legacy_persona_count": 0,
        "verified": True,
    }


def test_delete_apply_removes_acl_matched_reflections_with_typed_receipts(tmp_path):
    config = FakeConfig(tmp_path)
    reflection_path = config.database_dir / "reflections.db"
    access_control = make_cognitive_access_envelope(
        owner_principal_id="mcp:codex:reflection",
        owner_agent="codex",
        scope_type="reflection",
        scope_id="reflection-delete-1",
        session_id="reflection-delete-session",
        project="mnemos",
        purposes=(
            "reflection_read",
            "reflection_feedback",
            "reflection_prompt",
            "reflection_experience_read",
            "reflection_export",
        ),
        consent_provenance_refs=("raw:reflection-delete",),
        sensitivity="sensitive",
        retention_policy="reflection_retention",
        source_acl_lineage=("sha256:" + "b" * 64,),
        visibility="private",
    )
    record = ReflectionRecord(
        id="reflection-delete-1",
        created_at=datetime.now(),
        trigger=ReflectionTrigger.MANUAL,
        user_query="reflection body that must be deleted",
        access_control=access_control,
    )
    ReflectionStore(str(reflection_path)).save_record(record)

    ModelCallLedger.for_config(config)
    manager = DataOwnershipManager(config)
    manager.freeze("session:reflection-delete-session")
    snapshot_ref = _delete_snapshot(manager, "session:reflection-delete-session")
    proof = manager.delete(
        "session:reflection-delete-session",
        dry_run=False,
        apply=True,
        confirm=True,
        snapshot_ref=snapshot_ref,
    )

    assert proof.status == "verified"
    assert proof.affected_domains == ("model_call_ledger", "reflection")
    assert proof.verification_results["reflection"] == {
        "status": "applied",
        "target_count": 1,
        "receipt_count": 1,
        "verified": True,
        "legacy_unscoped_layer5_count": 0,
        "unresolved_legacy_record_count": 0,
        "unresolved_legacy_shift_count": 0,
    }
    assert ReflectionStore(str(reflection_path)).get_by_id("reflection-delete-1") is None


def test_delete_apply_removes_acl_matched_cognitive_graph_objects(
    tmp_path,
):
    config = FakeConfig(tmp_path)
    graph_path = config.database_dir / "cognitive_graph.db"
    access_control = make_cognitive_access_envelope(
        owner_principal_id="mcp:codex:graph",
        owner_agent="codex",
        scope_type="session",
        scope_id="graph-delete-session",
        session_id="graph-delete-session",
        project="mnemos",
        purposes=("cognitive_graph_read",),
        consent_provenance_refs=("raw:graph-delete",),
        sensitivity="sensitive",
        retention_policy="cognitive_graph_retention",
        source_acl_lineage=("sha256:" + "c" * 64,),
        visibility="agent",
    )
    graph = CognitiveGraphStore(str(graph_path))
    relation_binding = cognitive_relation_material_action_binding(
        source="session://graph-delete-session",
        target="kg://PrivateGraphFact",
        relation_type="derived_from",
        access_control=access_control,
    )
    relation_action = material_action_authorization(
        config.database_dir,
        action_type=COGNITIVE_RELATION_ACTION,
        owner=COGNITIVE_RELATION_OWNER,
        executor=COGNITIVE_RELATION_EXECUTOR,
        target_ref=relation_binding["target_ref"],
        input_hash=relation_binding["input_hash"],
    )
    relation = graph.add_relation(
        "session://graph-delete-session",
        "kg://PrivateGraphFact",
        "derived_from",
        access_control=access_control,
        material_action=relation_action,
    )

    ModelCallLedger.for_config(config)
    manager = DataOwnershipManager(config)
    manager.freeze("session:graph-delete-session")
    snapshot_ref = _delete_snapshot(manager, "session:graph-delete-session")
    proof = manager.delete(
        "session:graph-delete-session",
        dry_run=False,
        apply=True,
        confirm=True,
        snapshot_ref=snapshot_ref,
    )

    assert proof.status == "verified"
    assert proof.affected_domains == ("cognitive_graph", "model_call_ledger")
    assert proof.verification_results["cognitive_graph"]["status"] == "applied"
    assert proof.verification_results["cognitive_graph"]["target_count"] >= 3
    assert graph.get_relation(relation.id) is None


def test_delete_apply_removes_lifecycle_owned_wiki_page_without_false_verification(
    tmp_path, monkeypatch
):
    config = FakeConfig(tmp_path)
    monkeypatch.setattr("core.trust.config.get_config", lambda: config)
    page = config.vault_dir("mnemos") / "00-Inbox" / "subject-page.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "\n".join(
            (
                "---",
                "scope: private",
                "source_agent: codex",
                "session_id: wiki-delete-session",
                "project: mnemos",
                "acl_schema_version: 1",
                "acl_metadata_complete: true",
                "acl_reconciliation_status: server_principal",
                "---",
                "derived Wiki body that must be removed",
            )
        ),
        encoding="utf-8",
    )
    projection = WikiProjectionLedger(config.database_dir / "wiki_projection.db")
    projection.record_mutation(page, mutation_type="create")

    ModelCallLedger.for_config(config)

    class ConfigBoundBus:
        projection_db_path = config.database_dir / "wiki_projection.db"

        @staticmethod
        def publish(event):
            return event.trace_id

    manager = DataOwnershipManager(config, event_bus=ConfigBoundBus())
    manager.freeze("session:wiki-delete-session")
    snapshot_ref = _delete_snapshot(manager, "session:wiki-delete-session")
    proof = manager.delete(
        "session:wiki-delete-session",
        dry_run=False,
        apply=True,
        confirm=True,
        snapshot_ref=snapshot_ref,
    )

    assert proof.status == "partially_deleted"
    assert proof.affected_domains == ("model_call_ledger", "wiki")
    assert proof.verification_results["wiki"]["status"] == "applied"
    assert proof.verification_results["wiki"]["verified"] is False
    assert proof.verification_results["wiki"]["pending_required_consumer_count"] == 6
    assert page.exists() is False
    with sqlite3.connect(config.database_dir / "producer_consumer_ledger.db") as conn:
        assert conn.execute("SELECT status FROM cognitive_state_effect_receipts").fetchall() == [
            ("committed",)
        ]
