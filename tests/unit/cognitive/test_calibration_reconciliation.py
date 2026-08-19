"""Object-level CalibrationRecord provenance migration tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from core.cognitive.auto_calibration import CalibrationEngine, CrossSourceValidator
from core.cognitive.calibration_record import CalibrationRecordStore
from core.cognitive.calibration_reconcile_contracts import CalibrationReconciliationPaths
from core.cognitive.calibration_reconcile_executor import (
    apply_calibration_reconciliation,
)
from core.cognitive.calibration_reconcile_planner import (
    build_calibration_reconciliation_plan,
)
from core.cognitive.models import Dimension, Observation, ObservationType, SourceType
from core.cognitive.observation_store import ObservationStore
from core.cognitive.sources import ContentSource, SourceItem, UserIntent
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateStore
from core.sync_framework.raw_event_store import RawEventStore


class HistoricalCrossSource(CrossSourceValidator):
    spec_version = "historical-test-v1"


def _paths(tmp_path: Path) -> CalibrationReconciliationPaths:
    database_dir = tmp_path / "database"
    database_dir.mkdir()
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    initialize_cognitive_state_schema(database_dir / "producer_consumer_ledger.db")
    ObservationStore(str(database_dir / "observations.db"))
    raw_store = RawEventStore(db_path=database_dir / "raw_events.db")
    raw_store.close()
    return CalibrationReconciliationPaths(database_dir, wiki_dir)


def _raw_source(paths: CalibrationReconciliationPaths, text: str = "AI evidence"):
    raw_store = RawEventStore(db_path=paths.raw_path)
    try:
        revision_id = raw_store.upsert_turn(
            source_agent="codex",
            session_id="calibration-migration-test",
            turn_number=1,
            user_content=text,
            assistant_content="acknowledged",
            timestamp="2026-07-19T00:00:00+00:00",
            completeness={"visible_text": "full"},
        )
    finally:
        raw_store.close()
    with sqlite3.connect(paths.raw_path) as conn:
        raw_hash = str(
            conn.execute(
                "SELECT content_hash FROM raw_turn_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()[0]
        )
        event_id = str(
            conn.execute(
                "SELECT logical_event_id FROM raw_turn_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()[0]
        )
    source = SourceItem(
        source_type="raw",
        file_path=f"raw://{event_id}/{revision_id}",
        content=text,
        raw_revision_id=revision_id,
        raw_content_hash=raw_hash,
        source_content_hash=raw_hash,
        content_source=ContentSource.NATIVE_DIALOGUE,
        user_intent=UserIntent.SEEKING_JUDGMENT,
    )
    return source, revision_id


def _observation(source: SourceItem, revision_id: str) -> Observation:
    return Observation(
        id="migration-observation",
        dimension=Dimension.ATTENTION,
        observation_type=ObservationType.FREQUENCY,
        value={"concepts": {"ai": 2}, "dominant": "ai", "total_mentions": 2},
        confidence=0.6,
        source_type=SourceType.RAW,
        source_path=source.file_path,
        source_id=revision_id,
        evidence=["AI evidence"],
        content_source=ContentSource.NATIVE_DIALOGUE,
        user_intent_signal=UserIntent.SEEKING_JUDGMENT,
    )


def _seed_stale_record(paths: CalibrationReconciliationPaths):
    source, revision_id = _raw_source(paths)
    observation = _observation(source, revision_id)
    observation_store = ObservationStore(str(paths.observations_path))
    observation_store.save(observation)
    state_store = CognitiveStateStore(paths.state_path)
    records = CalibrationRecordStore(state_store)
    old_engine = CalibrationEngine(validators=[HistoricalCrossSource()])
    receipt, _ = records.commit(
        observation,
        old_engine.calibrate(observation, [observation], [source]),
    )
    records.apply_to_observation(observation_store, receipt)
    return receipt


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _current_engine() -> CalibrationEngine:
    return CalibrationEngine(validators=[CrossSourceValidator()])


def test_dry_run_reconstructs_replay_without_writes(tmp_path):
    paths = _paths(tmp_path)
    _seed_stale_record(paths)
    before = {
        path.name: _file_hash(path)
        for path in (paths.state_path, paths.observations_path, paths.raw_path)
    }

    plan = build_calibration_reconciliation_plan(paths, engine=_current_engine())

    assert plan.ok is True, plan.blocked
    assert len(plan.replays) == 1
    assert not plan.retirements
    assert plan.replays[0].expected_payload_hash.startswith("sha256:")
    assert before == {
        path.name: _file_hash(path)
        for path in (paths.state_path, paths.observations_path, paths.raw_path)
    }


def test_apply_backs_up_replays_projects_and_is_idempotent(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    old = _seed_stale_record(paths)
    engine = _current_engine()
    plan = build_calibration_reconciliation_plan(paths, engine=engine)
    monkeypatch.setattr(
        "core.cognitive.calibration_reconcile_executor.runtime_writers_are_inactive",
        lambda _path: True,
    )

    result = apply_calibration_reconciliation(
        paths,
        expected_inventory_hash=plan.inventory_hash,
        backup_dir=tmp_path / "backups",
        engine=engine,
    )

    assert result["ok"] is True, result
    assert result["status"] == "verified"
    assert result["applied_revision_count"] == 1
    assert all(value.get("integrity_check", "ok") == "ok" for value in result["backups"])
    state_store = CognitiveStateStore(paths.state_path)
    current = state_store.current_revision("calibration_record", old.observation_id)
    assert current is not None
    assert current.revision_id != old.revision_id
    assert current.payload["validator_spec_hash"] == engine.spec_hash
    rebound = ObservationStore(str(paths.observations_path)).get_by_id(old.observation_id)
    assert rebound is not None
    assert rebound.calibration_revision_id == current.revision_id
    assert old.revision_id != current.revision_id
    assert state_store.revision(old.revision_id) is not None
    assert state_store.pending_commands("observation_index") == []
    assert state_store.pending_commands("wiki_projection") == []
    projection = paths.projection_dir / "attention.md"
    assert current.revision_id in projection.read_text()

    clean = build_calibration_reconciliation_plan(paths, engine=engine)
    second = apply_calibration_reconciliation(
        paths,
        expected_inventory_hash=clean.inventory_hash,
        backup_dir=tmp_path / "unused-backups",
        engine=engine,
    )
    assert second["status"] == "noop"
    assert second["backups"] == []


def test_expected_hash_drift_blocks_before_backup(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    old = _seed_stale_record(paths)
    engine = _current_engine()
    plan = build_calibration_reconciliation_plan(paths, engine=engine)
    with sqlite3.connect(paths.observations_path) as conn:
        conn.execute(
            "UPDATE observations SET user_notes='drift' WHERE id=?",
            (old.observation_id,),
        )
    monkeypatch.setattr(
        "core.cognitive.calibration_reconcile_executor.runtime_writers_are_inactive",
        lambda _path: True,
    )

    result = apply_calibration_reconciliation(
        paths,
        expected_inventory_hash=plan.inventory_hash,
        backup_dir=tmp_path / "backups",
        engine=engine,
    )

    assert result["status"] == "blocked"
    assert result["error"] == "inventory_hash_mismatch"
    assert not (tmp_path / "backups").exists()
    current = CognitiveStateStore(paths.state_path).current_revision(
        "calibration_record",
        old.observation_id,
    )
    assert current is not None and current.revision_id == old.revision_id


def test_mid_apply_failure_restores_exact_semantic_inventory(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    old = _seed_stale_record(paths)
    engine = _current_engine()
    plan = build_calibration_reconciliation_plan(paths, engine=engine)
    monkeypatch.setattr(
        "core.cognitive.calibration_reconcile_executor.runtime_writers_are_inactive",
        lambda _path: True,
    )

    def failpoint(stage: str) -> None:
        if stage == "replay:0":
            raise RuntimeError("injected failure")

    result = apply_calibration_reconciliation(
        paths,
        expected_inventory_hash=plan.inventory_hash,
        backup_dir=tmp_path / "backups",
        engine=engine,
        failpoint=failpoint,
    )

    assert result["status"] == "rolled_back"
    assert result["rollback_verified"] is True
    rolled_back = build_calibration_reconciliation_plan(paths, engine=engine)
    assert rolled_back.inventory_hash == plan.inventory_hash
    current = CognitiveStateStore(paths.state_path).current_revision(
        "calibration_record",
        old.observation_id,
    )
    assert current is not None and current.revision_id == old.revision_id


def test_exact_system_collision_retires_only_mutable_head_and_row(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    source, _ = _raw_source(paths)
    observation = Observation(
        id="system-collision",
        dimension=Dimension.ATTENTION,
        observation_type=ObservationType.PATTERN,
        value={"user_intent_distribution": {"asking_question": 10}, "total_with_intent": 10},
        unit="signals",
        confidence=0.7,
        source_type=SourceType.WIKI,
        source_path="system:user_intent_stats",
        source_id="system",
    )
    observation_store = ObservationStore(str(paths.observations_path))
    observation_store.save(observation)
    state_store = CognitiveStateStore(paths.state_path)
    records = CalibrationRecordStore(state_store)
    receipt, _ = records.commit(
        observation,
        CalibrationEngine(validators=[HistoricalCrossSource()]).calibrate(
            observation,
            [observation],
            [source],
        ),
    )
    records.apply_to_observation(observation_store, receipt)
    for consumer in ("observation_index", "wiki_projection"):
        records.record_effect(
            receipt,
            consumer_id=consumer,
            target_effect_id=f"historical:{consumer}",
            before_hash="sha256:" + "1" * 64,
            after_hash="sha256:" + "2" * 64,
            evidence_refs=(f"calibration-revision:{receipt.revision_id}",),
        )
    with sqlite3.connect(paths.observations_path) as conn:
        conn.execute(
            """
            UPDATE observations
            SET source_path='system:content_source_stats',
                value=?, confidence=base_confidence,
                calibration_revision_id='', calibration_input_hash='',
                calibration_spec_hash='', calibration_record_hash=''
            WHERE id=?
            """,
            (
                json.dumps(
                    {
                        "user_intent_distribution": {"asking_question": 3},
                        "total_with_intent": 3,
                    }
                ),
                observation.id,
            ),
        )
    before_counts = {}
    with sqlite3.connect(paths.state_path) as conn:
        for table in (
            "cognitive_state_revisions",
            "cognitive_state_outbox",
            "cognitive_state_effect_receipts",
        ):
            before_counts[table] = int(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # nosec B608
            )

    engine = _current_engine()
    plan = build_calibration_reconciliation_plan(paths, engine=engine)
    assert len(plan.retirements) == 1
    assert not plan.replays
    monkeypatch.setattr(
        "core.cognitive.calibration_reconcile_executor.runtime_writers_are_inactive",
        lambda _path: True,
    )
    result = apply_calibration_reconciliation(
        paths,
        expected_inventory_hash=plan.inventory_hash,
        backup_dir=tmp_path / "backups",
        engine=engine,
    )

    assert result["status"] == "verified"
    assert result["retired_collision_count"] == 1
    assert state_store.current_revision("calibration_record", observation.id) is None
    assert state_store.revision(receipt.revision_id) is not None
    assert state_store.integrity_report()["current_state_hash_mismatch"] == 0
    assert ObservationStore(str(paths.observations_path)).get_by_id(observation.id) is None
    with sqlite3.connect(paths.state_path) as conn:
        after_counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # nosec B608
            for table in before_counts
        }
        quarantine_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_state_migration_quarantine "
                "WHERE source_table='cognitive_state_revisions' AND source_key=?",
                (receipt.revision_id,),
            ).fetchone()[0]
        )
    assert after_counts == before_counts
    assert quarantine_count == 1
