from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from core.cognition_episode_contract import (
    COGNITION_EPISODE_FIELDS,
    COGNITION_EPISODE_SCHEMA_VERSION,
)
from core.cognitive.state_contract import (
    CognitiveHeadPrecondition,
    CognitiveStateRevision,
    LocalConsumerCommand,
    canonical_json,
    sha256_json,
)
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateConflict, CognitiveStateStore
from core.migrations.registry import MigrationLedger, MigrationLedgerRecord
from core.ops.cognitive_data_contract import CognitiveDataEvent
from core.ops.cognitive_event_ledger import cognitive_data_snapshot_in_connection
from scripts.reconcile_cognitive_state_store import main as reconcile_state_main


def _event(suffix: str, consumers: tuple[str, ...]) -> CognitiveDataEvent:
    return CognitiveDataEvent(
        event_id=f"cde-state-{suffix}",
        source_id=f"raw-event-{suffix}",
        asset_id=f"raw-asset-{suffix}",
        source_kind="distill",
        source_uri=f"raw://session/{suffix}",
        content_hash=f"sha256:source-{suffix}",
        canonical_subject=f"episode:{suffix}",
        data_type="cognition_episode",
        producer="cognitive_state_store",
        intended_consumers=consumers,
        privacy_level="private",
        confidence=0.9,
        evidence_refs=(f"raw-event-{suffix}#0:32",),
        dedupe_key=f"episode:{suffix}:v1",
        created_at="2026-07-16T00:00:00+00:00",
    )


def _revision(
    suffix: str,
    event_id: str,
    *,
    object_id: str = "",
    supersedes_revision_id: str = "",
    correction_of_revision_id: str = "",
    goal: str = "Close the cognitive state transaction gap",
) -> CognitiveStateRevision:
    evidence_ref = f"raw-event-{suffix}#0:32"
    source_event_id = f"raw-revision-{suffix}"
    source_hash = f"sha256:source-{suffix}"
    authority_id = f"source-authority:{suffix}"
    source_span = {
        "source_authority_id": authority_id,
        "revision_id": source_event_id,
        "role": "user",
        "span_start": 0,
        "span_end": 32,
        "span_status": "exact",
        "content_sha256": source_hash,
        "source_revision_sha256": source_hash,
    }
    authority_entry = {
        "source_authority_id": authority_id,
        "source_authority": "explicit_user",
        "source_event_id": source_event_id,
        "role": "user",
        "purpose": "user_instruction",
        "content_sha256": source_hash,
        "span_start": 0,
        "span_end": 32,
        "span_status": "exact",
        "source_revision_sha256": source_hash,
        "artifact_ref_id": "",
        "allows_cognitive_update": True,
    }
    authority_catalog = {
        "schema_version": "mnemos.source_authority_catalog.v1",
        "entries": [authority_entry],
        "rejected_count": 0,
        "rejection_codes": [],
    }
    artifact_catalog = {
        "schema_version": "mnemos.artifact_catalog.v1",
        "entries": [],
        "rejected_count": 0,
        "rejection_codes": [],
    }
    artifact_catalog_hash = sha256_json(artifact_catalog)
    authority_catalog_hash = sha256_json(authority_catalog)
    access_control = make_cognitive_access_envelope(
        owner_principal_id="test:state-store",
        owner_agent="state-store-test",
        scope_type="project",
        scope_id="mnemos",
        project="mnemos",
        session_id=suffix,
        purposes=("cognitive_state_read",),
        consent_provenance_refs=(authority_catalog_hash,),
        sensitivity="sensitive",
        retention_policy="cognitive_state",
        source_acl_lineage=(authority_catalog_hash,),
    )
    context_hash = sha256_json(
        {
            "schema_version": "mnemos.cognition_extraction_context.v1",
            "source_agent": "state-store-test",
            "source_session_id": suffix,
            "source_event_ids": [source_event_id],
            "raw_completeness": "full",
            "loss_contract": "lossless-visible-v1",
            "source_spans": [source_span],
            "artifact_catalog_hash": artifact_catalog_hash,
            "source_authority_catalog_hash": authority_catalog_hash,
            "acl": "local_user",
            "access_control": access_control,
            "purpose": "cognition_distillation",
            "retention_policy": "cognitive_state",
        }
    )
    evidence = {
        "source_event_id": source_event_id,
        "source_authority_id": authority_id,
        "quote": f"evidence for {suffix}",
        "authority_role": "user",
        "authority_span_start": 0,
        "authority_span_end": 32,
        "authority_span_status": "exact",
        "authority_content_sha256": source_hash,
        "authority_source_revision_sha256": source_hash,
    }
    episode_fields = {
        field_name: [
            {
                "entry_id": f"entry-{suffix}-{field_name}",
                "status": "known",
                "value": goal if field_name == "goal" else f"{field_name} for {suffix}",
                "evidence_refs": [evidence],
                "claim_ids": [f"claim-{suffix}"],
            }
        ]
        for field_name in COGNITION_EPISODE_FIELDS
    }
    claims = [
        {
            "claim_id": f"claim-{suffix}",
            "claim_text": goal,
            "claim_type": "technical_fact",
            "scope": {
                "domain": "cognitive-state-test",
                "applies_to": ["mnemos"],
                "not_applies_to": [],
            },
            "evidence": [evidence],
            "relation_to_existing": {
                "type": "new",
                "target_pages": [],
                "delta_text": "",
                "reason": "test fixture",
            },
            "recommended_action": "create_page",
            "confidence": 0.9,
        }
    ]
    behavior_intent = {
        "content_source": "native_dialogue",
        "user_intent_signal": "sharing_information",
        "intent_hypothesis": goal,
        "intent_evidence": [evidence],
        "intent_verification_events": [],
        "intent_confidence": 0.9,
        "intent_status": "verified",
        "behavior_summary": goal,
    }
    return CognitiveStateRevision.create(
        object_type="cognition_episode",
        object_id=object_id or f"episode-{suffix}",
        source_event_id=event_id,
        source_revision_id=f"raw-revision-{suffix}",
        source_content_hash=f"sha256:source-{suffix}",
        scope_type="project",
        scope_id="mnemos",
        evidence_refs=(evidence_ref,),
        payload={
            "schema_version": COGNITION_EPISODE_SCHEMA_VERSION,
            "cognition_context_hash": context_hash,
            "input_spec_hash": f"sha256:input-{suffix}",
            "extraction_output_hash": f"sha256:source-{suffix}",
            "source_agent": "state-store-test",
            "source_session_id": suffix,
            "source_event_ids": [source_event_id],
            "raw_completeness": "full",
            "loss_contract": "lossless-visible-v1",
            "source_spans": [source_span],
            "artifact_catalog_hash": artifact_catalog_hash,
            "source_authority_catalog_hash": authority_catalog_hash,
            "source_authority_catalog": authority_catalog,
            "artifact_catalog": artifact_catalog,
            "acl": "local_user",
            "access_control": access_control,
            "purpose": "cognition_distillation",
            "retention_policy": "cognitive_state",
            "claims": claims,
            "claim_catalog_hash": sha256_json(claims),
            "user_behavior_intent": behavior_intent,
            **episode_fields,
        },
        supersedes_revision_id=supersedes_revision_id,
        correction_of_revision_id=correction_of_revision_id,
        created_at="2026-07-16T00:00:00+00:00",
    )


def _commands(revision_id: str) -> tuple[LocalConsumerCommand, ...]:
    return (
        LocalConsumerCommand.create(
            revision_id=revision_id,
            consumer_id="wiki",
            command_type="project_cognition_episode",
            payload={"projection": "wiki"},
        ),
        LocalConsumerCommand.create(
            revision_id=revision_id,
            consumer_id="cognitive_graph",
            command_type="project_cognition_episode",
            payload={"projection": "cognitive_graph"},
        ),
    )


def _generic_projection_commands(
    revision_id: str,
) -> tuple[LocalConsumerCommand, ...]:
    """Exercise the generic receipt ledger without the episode-specific oracle."""

    return (
        LocalConsumerCommand.create(
            revision_id=revision_id,
            consumer_id="wiki",
            command_type="project_test_state",
            payload={"projection": "wiki"},
        ),
        LocalConsumerCommand.create(
            revision_id=revision_id,
            consumer_id="cognitive_graph",
            command_type="project_test_state",
            payload={"projection": "cognitive_graph"},
        ),
    )


def _store(tmp_path: Path) -> CognitiveStateStore:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    return CognitiveStateStore(db_path)


def _counts(db_path: Path) -> tuple[int, int, int]:
    with sqlite3.connect(db_path) as conn:
        return (
            int(conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM cognitive_data_events").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM cognitive_state_outbox").fetchone()[0]),
        )


@pytest.mark.parametrize("drift", ["context_hash", "span", "evidence"])
def test_cognition_episode_revision_rejects_context_catalog_drift(drift: str) -> None:
    original = _revision("drift", "cde-state-drift")
    payload = deepcopy(dict(original.payload))
    if drift == "context_hash":
        payload["cognition_context_hash"] = "sha256:" + "f" * 64
    elif drift == "span":
        payload["source_spans"][0]["span_end"] = 31
    else:
        payload["facts"][0]["evidence_refs"][0]["authority_span_end"] = 31

    with pytest.raises(ValueError, match="context|span|evidence"):
        CognitiveStateRevision.create(
            object_type="cognition_episode",
            object_id="episode-drift",
            source_event_id="cde-state-drift",
            source_revision_id="raw-revision-drift",
            source_content_hash="sha256:source-drift",
            scope_type="project",
            scope_id="mnemos",
            evidence_refs=("raw-event-drift#0:32",),
            payload=payload,
        )


def test_cognition_episode_persistence_redacts_credentials_without_encryption() -> None:
    secret = "".join(("sk-", "test-", "abcdefghijklmnopqrstuvwxyz123456"))
    revision = _revision("privacy", "cde-state-privacy", goal=f"rotate {secret}")

    serialized = str(dict(revision.payload))
    assert secret not in serialized
    assert "[REDACTED:" in serialized
    assert revision.redaction_counts


def test_constructor_is_read_only_for_a_missing_database(tmp_path: Path) -> None:
    db_path = tmp_path / "missing" / "producer_consumer_ledger.db"

    store = CognitiveStateStore(db_path)

    assert store.db_path == db_path
    assert not db_path.exists()
    with pytest.raises(FileNotFoundError):
        store.integrity_report()
    assert not db_path.parent.exists()


def test_unit_of_work_commits_revision_envelope_and_outbox_together(tmp_path: Path) -> None:
    store = _store(tmp_path)
    event = _event("atomic", ("wiki", "cognitive_graph"))
    revision = _revision("atomic", event.event_id)

    receipt = store.unit_of_work().commit(
        revisions=(revision,),
        event=event,
        commands=_commands(revision.revision_id),
    )

    assert receipt.status == "committed"
    assert _counts(store.db_path) == (1, 1, 2)
    current = store.current_revision("cognition_episode", revision.object_id)
    assert current is not None
    assert current.revision_id == revision.revision_id
    assert current.payload_hash == revision.payload_hash
    report = store.integrity_report()
    assert report["canonical_state_owner_count"] == 1
    assert report["semantic_revision_without_envelope"] == 0
    assert report["envelope_without_semantic_revision"] == 0
    assert report["outbox_without_source_commit"] == 0
    assert report["multiple_current_revision"] == 0


def test_effect_receipt_closes_outbox_and_consumer_pair_atomically(tmp_path: Path) -> None:
    store = _store(tmp_path)
    event = _event("effects", ("wiki", "cognitive_graph"))
    revision = _revision("effects", event.event_id)
    commands = _generic_projection_commands(revision.revision_id)
    store.unit_of_work().commit(revisions=(revision,), event=event, commands=commands)

    first = store.record_effect_receipt(
        commands[0].command_id,
        status="committed",
        target_effect_id="wiki-page:episode-effects",
        before_hash="sha256:absent",
        after_hash="sha256:wiki-page-v1",
        evidence_refs=("wiki-journal:mutation-1",),
        outcome="projected",
        created_at="2026-07-16T00:01:00+00:00",
    )
    replay = store.record_effect_receipt(
        commands[0].command_id,
        status="committed",
        target_effect_id="wiki-page:episode-effects",
        before_hash="sha256:absent",
        after_hash="sha256:wiki-page-v1",
        evidence_refs=("wiki-journal:mutation-1",),
        outcome="projected",
    )

    assert replay == first
    assert [item["command_id"] for item in store.pending_commands()] == [commands[1].command_id]
    with sqlite3.connect(store.db_path) as conn:
        effect_row = conn.execute(
            "SELECT command_id, consumption_id FROM cognitive_state_effect_receipts"
        ).fetchone()
        consumption_row = conn.execute(
            "SELECT consumption_id, target_effect_id, action_changed "
            "FROM cognitive_data_consumptions"
        ).fetchone()
    assert effect_row == (commands[0].command_id, first.consumption_id)
    assert consumption_row == (
        first.consumption_id,
        "wiki-page:episode-effects",
        1,
    )

    store.record_effect_receipt(
        commands[1].command_id,
        status="intentional_skip",
        target_effect_id="cognitive-graph:episode-effects",
        evidence_refs=("skip-policy:not-applicable",),
        outcome="not applicable to this projection",
    )

    assert store.pending_commands() == []
    report = store.integrity_report()
    assert report["effect_receipt_without_command"] == 0
    assert report["effect_receipt_reciprocity_gap"] == 0
    assert report["aggregate_consumed_with_missing_consumer"] == 0
    assert report["partial_facade_commit"] == 0


def test_effect_receipt_rejects_orphan_or_conflicting_proof(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="outbox command does not exist"):
        store.record_effect_receipt(
            "missing-command",
            status="committed",
            target_effect_id="wiki-page:missing",
            before_hash="sha256:absent",
            after_hash="sha256:present",
            evidence_refs=("wiki-journal:missing",),
        )

    event = _event("effect-conflict", ("wiki", "cognitive_graph"))
    revision = _revision("effect-conflict", event.event_id)
    commands = _generic_projection_commands(revision.revision_id)
    command = commands[0]
    store.unit_of_work().commit(
        revisions=(revision,),
        event=event,
        commands=commands,
    )
    store.record_effect_receipt(
        command.command_id,
        status="committed",
        target_effect_id="wiki-page:effect-conflict",
        before_hash="sha256:absent",
        after_hash="sha256:v1",
        evidence_refs=("wiki-journal:effect-conflict",),
    )

    with pytest.raises(ValueError, match="terminal receipt conflict"):
        store.record_effect_receipt(
            command.command_id,
            status="committed",
            target_effect_id="wiki-page:effect-conflict",
            before_hash="sha256:v1",
            after_hash="sha256:v2",
            evidence_refs=("wiki-journal:conflicting-effect",),
        )


def test_subject_tombstone_hides_current_state_and_requires_typed_consumer_receipts(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    event = _event("tombstone", ("wiki", "cognitive_graph"))
    revision = _revision("tombstone", event.event_id)
    commands = _commands(revision.revision_id)
    store.unit_of_work().commit(revisions=(revision,), event=event, commands=commands)

    plan = store.plan_subject_tombstone(
        request_id="delete-cognitive-state",
        scope_kind="project",
        scope_value="mnemos",
        snapshot_ref="snapshot:trusted",
    )

    assert plan.status == "committed"
    assert plan.target_revision_ids == (revision.revision_id,)
    assert store.current_revision("cognition_episode", revision.object_id) is None
    assert store.current_revisions(object_type="cognition_episode") == ()
    assert store.revision(revision.revision_id) is None
    assert store.tombstone_status(plan.request_id)["verified"] is False

    pending = {
        item["consumer_id"]: item
        for item in store.pending_commands()
        if item["command_type"] == "tombstone_cognitive_state"
    }
    assert set(pending) == {"wiki", "cognitive_graph"}
    wiki_command = pending["wiki"]
    with pytest.raises(ValueError, match="tombstone receipt"):
        store.record_effect_receipt(
            wiki_command["command_id"],
            status="committed",
            target_effect_id="wiki:wrong-target",
            before_hash=plan.before_hash,
            after_hash=plan.tombstone_hash,
            evidence_refs=("tombstone-oracle:fake",),
        )

    for consumer_id, command in pending.items():
        store.record_effect_receipt(
            command["command_id"],
            status="committed",
            target_effect_id=f"tombstone:{consumer_id}:{plan.request_id}",
            before_hash=plan.before_hash,
            after_hash=plan.tombstone_hash,
            evidence_refs=(
                f"tombstone-command:{command['command_id']}",
                f"tombstone-oracle:{consumer_id}:{plan.tombstone_hash}",
            ),
        )

    status = store.tombstone_status(plan.request_id)
    assert status["status"] == "verified"
    assert status["verified"] is True
    assert status["required_consumers"] == ["cognitive_graph", "wiki"]
    assert store.rebuild_current_state()["projection_hash_matches"] is True


def test_subject_tombstone_replay_is_idempotent_and_cannot_retarget_request(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    event = _event("tombstone-replay", ("wiki", "cognitive_graph"))
    revision = _revision("tombstone-replay", event.event_id)
    store.unit_of_work().commit(
        revisions=(revision,), event=event, commands=_commands(revision.revision_id)
    )

    first = store.plan_subject_tombstone(
        request_id="delete-replay",
        scope_kind="project",
        scope_value="mnemos",
        snapshot_ref="snapshot:trusted",
    )
    replay = store.plan_subject_tombstone(
        request_id="delete-replay",
        scope_kind="project",
        scope_value="mnemos",
        snapshot_ref="snapshot:trusted",
    )

    assert replay.status == "existing"
    assert replay.control_revision_id == first.control_revision_id
    assert replay.command_ids == first.command_ids
    with pytest.raises(CognitiveStateConflict, match="different subject"):
        store.plan_subject_tombstone(
            request_id="delete-replay",
            scope_kind="project",
            scope_value="other-project",
            snapshot_ref="snapshot:trusted",
        )


def test_restart_recovers_pending_target_and_failure_is_not_false_consumed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    event = _event("partial-target", ("wiki", "cognitive_graph"))
    revision = _revision("partial-target", event.event_id)
    commands = _generic_projection_commands(revision.revision_id)
    store.unit_of_work().commit(revisions=(revision,), event=event, commands=commands)
    store.record_effect_receipt(
        commands[0].command_id,
        status="committed",
        target_effect_id="wiki-page:partial-target",
        before_hash="sha256:absent",
        after_hash="sha256:wiki-v1",
        evidence_refs=("wiki-journal:partial-target",),
    )

    restarted = CognitiveStateStore(store.db_path)
    assert [item["command_id"] for item in restarted.pending_commands()] == [commands[1].command_id]
    restarted.record_effect_receipt(
        commands[1].command_id,
        status="dead_letter",
        target_effect_id="cognitive-graph:partial-target",
        evidence_refs=("dead-letter:projection-failed",),
        outcome="projection failed after bounded retries",
    )

    assert restarted.pending_commands() == []
    with sqlite3.connect(store.db_path) as conn:
        snapshot = cognitive_data_snapshot_in_connection(conn)
    assert snapshot["events"][0]["aggregate_status"] == "terminal_with_failures"
    assert snapshot["counts"]["consumed_events"] == 0
    assert restarted.integrity_report()["partial_facade_commit"] == 0


def test_semantic_correction_appends_and_preserves_the_prior_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_event = _event("correction", ("wiki", "cognitive_graph"))
    first = _revision("correction", first_event.event_id, goal="original goal")
    store.unit_of_work().commit(
        revisions=(first,),
        event=first_event,
        commands=_commands(first.revision_id),
    )
    corrected_event = _event("correction-v2", ("wiki", "cognitive_graph"))
    corrected = _revision(
        "correction-v2",
        corrected_event.event_id,
        object_id=first.object_id,
        goal="corrected goal",
        supersedes_revision_id=first.revision_id,
        correction_of_revision_id=first.revision_id,
    )

    store.unit_of_work().commit(
        revisions=(corrected,),
        event=corrected_event,
        commands=_commands(corrected.revision_id),
    )

    assert store.current_revision("cognition_episode", first.object_id) == corrected
    assert store.revision(first.revision_id).payload["goal"][0]["value"] == "original goal"
    with sqlite3.connect(store.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE cognitive_state_revisions SET payload_json='{}' " "WHERE revision_id=?",
                (first.revision_id,),
            )


@pytest.mark.parametrize("stage", ["after_revision", "after_event", "after_outbox"])
def test_unit_of_work_rolls_back_every_failure_boundary(tmp_path: Path, stage: str) -> None:
    store = _store(tmp_path)
    event = _event(stage, ("wiki", "cognitive_graph"))
    revision = _revision(stage, event.event_id)

    def failpoint(current: str) -> None:
        if current == stage:
            raise sqlite3.OperationalError(f"injected:{stage}")

    with pytest.raises(sqlite3.OperationalError, match=f"injected:{stage}"):
        store.unit_of_work().commit(
            revisions=(revision,),
            event=event,
            commands=_commands(revision.revision_id),
            failpoint=failpoint,
        )

    assert _counts(store.db_path) == (0, 0, 0)


def test_exact_concurrent_replay_has_one_revision_head_and_one_outbox_set(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    event = _event("replay", ("wiki", "cognitive_graph"))
    revision = _revision("replay", event.event_id)

    def commit_once(index: int) -> str:
        local = CognitiveStateStore(store.db_path)
        replay_revision = replace(
            revision,
            created_at=f"2026-07-13T01:02:{index:02d}+00:00",
        )
        return (
            local.unit_of_work()
            .commit(
                revisions=(replay_revision,),
                event=event,
                commands=_commands(replay_revision.revision_id),
            )
            .status
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(commit_once, range(8)))

    assert set(statuses) <= {"committed", "existing"}
    assert "committed" in statuses
    assert _counts(store.db_path) == (1, 1, 2)
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_heads").fetchone()[0] == 1
        created_at = conn.execute("SELECT created_at FROM cognitive_state_revisions").fetchone()[0]
    assert created_at in {f"2026-07-13T01:02:{index:02d}+00:00" for index in range(8)}


def test_runtime_unit_of_work_rejects_historical_candidate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    event = _event("historical", ("wiki", "cognitive_graph"))
    revision = replace(
        _revision("historical", event.event_id),
        admission_state="historical_candidate",
    )

    with pytest.raises(ValueError, match="accepts active revisions only"):
        store.unit_of_work().commit(
            revisions=(revision,),
            event=event,
            commands=_commands(revision.revision_id),
        )

    assert _counts(store.db_path) == (0, 0, 0)


def test_rebuild_excludes_append_only_quarantined_retirement(tmp_path: Path) -> None:
    store = _store(tmp_path)
    event = _event("quarantined-retirement", ("wiki", "cognitive_graph"))
    revision = _revision("quarantined-retirement", event.event_id)
    store.unit_of_work().commit(
        revisions=(revision,),
        event=event,
        commands=_commands(revision.revision_id),
    )
    quarantine_payload = {
        "schema_version": "mnemos.cognitive_state_retirement.v1",
        "object_type": revision.object_type,
        "object_id": revision.object_id,
        "revision_id": revision.revision_id,
        "reason_code": "verified_test_retirement",
    }
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            INSERT INTO cognitive_state_migration_quarantine (
                quarantine_id, source_table, source_key, reason_code,
                field_manifest, payload_json, payload_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "quarantine-verified-retirement",
                "cognitive_state_revisions",
                revision.revision_id,
                "verified_test_retirement",
                canonical_json(sorted(quarantine_payload)),
                canonical_json(quarantine_payload),
                sha256_json(quarantine_payload),
                "2026-07-20T00:00:00+00:00",
            ),
        )
        conn.execute(
            "DELETE FROM cognitive_state_heads WHERE revision_id=?",
            (revision.revision_id,),
        )

    rebuilt = store.rebuild_current_state()

    assert rebuilt["heads"] == []
    assert rebuilt["projection_hash_matches"] is True
    assert store.integrity_report()["current_state_hash_mismatch"] == 0


def test_reconcile_cli_backfills_only_migration_verified_retirement_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(tmp_path)
    event = _event("legacy-retirement", ("wiki", "cognitive_graph"))
    revision = _revision("legacy-retirement", event.event_id)
    store.unit_of_work().commit(
        revisions=(revision,),
        event=event,
        commands=_commands(revision.revision_id),
    )
    evidence_db = tmp_path / "producer-consumer-pre-demo-retirement.db"
    with sqlite3.connect(store.db_path) as source, sqlite3.connect(evidence_db) as target:
        source.backup(target)
    evidence_sha = "sha256:" + hashlib.sha256(evidence_db.read_bytes()).hexdigest()
    plan_hash = "sha256:" + "7" * 64
    backup_ref = json.dumps(
        [
            {
                "kind": "sqlite",
                "source": str(store.db_path.resolve()),
                "existed": True,
                "path": str(evidence_db.resolve()),
                "sha256": evidence_sha,
                "integrity_check": "ok",
            }
        ]
    )
    ledger = MigrationLedger(tmp_path / "migrations.db")
    ledger.record(
        MigrationLedgerRecord(
            ledger_id="demo-retirement-applying",
            migration_id="database.demo_fixture_leak.v1",
            status="applying",
            plan_hash=plan_hash,
            from_version="fixture-telemetry-in-production",
            to_version="isolated-demo-fixture-v1",
            backup_ref=backup_ref,
            actor="test",
        )
    )
    ledger.record(
        MigrationLedgerRecord(
            ledger_id="demo-retirement-verified",
            migration_id="database.demo_fixture_leak.v1",
            status="verified",
            plan_hash=plan_hash,
            from_version="fixture-telemetry-in-production",
            to_version="isolated-demo-fixture-v1",
            backup_ref=backup_ref,
            actor="test",
            verification={"retired_episode_count": 1},
        )
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "DELETE FROM cognitive_state_heads WHERE revision_id=?",
            (revision.revision_id,),
        )

    assert (
        reconcile_state_main(
            [
                "--db-path",
                str(store.db_path),
                "--retirement-evidence-db",
                str(evidence_db),
                "--json",
            ]
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["retirement_reconciliation"]["candidate_count"] == 1
    with sqlite3.connect(store.db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM cognitive_state_migration_quarantine").fetchone()[0]
            == 0
        )

    monkeypatch.setattr(
        "scripts.reconcile_cognitive_state_store.runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    backup_dir = tmp_path / "backups"
    assert (
        reconcile_state_main(
            [
                "--db-path",
                str(store.db_path),
                "--retirement-evidence-db",
                str(evidence_db),
                "--backup-dir",
                str(backup_dir),
                "--json",
            ]
        )
        == 0
    )
    apply_plan_hash = json.loads(capsys.readouterr().out)["plan_hash"]
    assert (
        reconcile_state_main(
            [
                "--db-path",
                str(store.db_path),
                "--retirement-evidence-db",
                str(evidence_db),
                "--apply",
                "--backup-dir",
                str(backup_dir),
                "--expected-plan-hash",
                apply_plan_hash,
                "--json",
            ]
        )
        == 0
    )
    applied = json.loads(capsys.readouterr().out)
    assert applied["applied"] is True
    assert applied["action"] == "retirement_sidecars_inserted"
    assert applied["retirement_reconciliation"]["inserted_count"] == 1
    assert applied["state_integrity"]["current_state_hash_mismatch"] == 0
    with sqlite3.connect(store.db_path) as conn:
        assert (
            conn.execute(
                "SELECT reason_code FROM cognitive_state_migration_quarantine "
                "WHERE source_key=?",
                (revision.revision_id,),
            ).fetchone()[0]
            == "synthetic_fixture_source_not_in_canonical_raw"
        )


def test_only_one_competing_revision_can_supersede_the_current_head(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_event = _event("chain", ("wiki", "cognitive_graph"))
    first = _revision("chain", first_event.event_id)
    store.unit_of_work().commit(
        revisions=(first,),
        event=first_event,
        commands=_commands(first.revision_id),
    )

    def competing(label: str) -> str:
        event = _event(f"chain-{label}", ("wiki", "cognitive_graph"))
        revision = _revision(
            f"chain-{label}",
            event.event_id,
            object_id=first.object_id,
            goal=f"winning revision {label}",
            supersedes_revision_id=first.revision_id,
        )
        local = CognitiveStateStore(store.db_path)
        local.unit_of_work().commit(
            revisions=(revision,),
            event=event,
            commands=_commands(revision.revision_id),
        )
        return revision.revision_id

    winners: list[str] = []
    conflicts = 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(competing, label) for label in ("a", "b")]
        for future in futures:
            try:
                winners.append(future.result())
            except CognitiveStateConflict:
                conflicts += 1

    assert len(winners) == 1
    assert conflicts == 1
    assert store.current_revision("cognition_episode", first.object_id).revision_id == winners[0]
    assert store.integrity_report()["multiple_current_revision"] == 0


def test_unit_of_work_rejects_a_stale_consumed_head_before_any_write(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first_event = _event("observed-head-v1", ("wiki", "cognitive_graph"))
    first = _revision("observed-head-v1", first_event.event_id)
    store.unit_of_work().commit(
        revisions=(first,),
        event=first_event,
        commands=_commands(first.revision_id),
    )
    second_event = _event("observed-head-v2", ("wiki", "cognitive_graph"))
    second = _revision(
        "observed-head-v2",
        second_event.event_id,
        object_id=first.object_id,
        supersedes_revision_id=first.revision_id,
    )
    store.unit_of_work().commit(
        revisions=(second,),
        event=second_event,
        commands=_commands(second.revision_id),
    )
    attempted_event = _event("stale-snapshot", ("wiki", "cognitive_graph"))
    attempted = _revision("stale-snapshot", attempted_event.event_id)

    with pytest.raises(CognitiveStateConflict, match="rebuild the snapshot"):
        store.unit_of_work().commit(
            revisions=(attempted,),
            event=attempted_event,
            commands=_commands(attempted.revision_id),
            expected_heads=(
                CognitiveHeadPrecondition.create(
                    object_type=first.object_type,
                    object_id=first.object_id,
                    revision_id=first.revision_id,
                ),
            ),
        )

    assert _counts(store.db_path) == (2, 2, 4)


def test_current_state_rebuild_does_not_depend_on_mutable_head_projection(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    event = _event("rebuild", ("wiki", "cognitive_graph"))
    revision = _revision("rebuild", event.event_id)
    store.unit_of_work().commit(
        revisions=(revision,),
        event=event,
        commands=_commands(revision.revision_id),
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DELETE FROM cognitive_state_heads")

    rebuilt = store.rebuild_current_state()

    assert rebuilt["state_hash"] == store.revision_state_hash(
        ("cognition_episode", revision.object_id, revision.revision_id)
    )
    assert rebuilt["heads"] == [
        {
            "object_type": "cognition_episode",
            "object_id": revision.object_id,
            "revision_id": revision.revision_id,
        }
    ]
    assert rebuilt["projection_hash_matches"] is False
