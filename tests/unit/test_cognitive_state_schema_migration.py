from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import sqlite3
from pathlib import Path

import pytest

from core.cognitive.state_schema import (
    CognitiveStateSchemaError,
    LEGACY_CANONICAL_V2_DDL,
    LEGACY_CANONICAL_V2_DDL_HASH,
    LEGACY_CANONICAL_V2_SCHEMA_VERSION,
    LEGACY_CANONICAL_V3_DDL,
    LEGACY_CANONICAL_V3_DDL_HASH,
    LEGACY_CANONICAL_V3_SCHEMA_VERSION,
    SCHEMA_COMPONENT,
    STATE_SCHEMA_VERSION,
    inspect_cognitive_state_schema,
    reconcile_cognitive_state_schema,
)
from core.cognitive.state_schema_ddl import (
    LEGACY_CANONICAL_V4_DDL,
    LEGACY_CANONICAL_V4_DDL_HASH,
    LEGACY_CANONICAL_V4_SCHEMA_VERSION,
)
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.search_state_headers import (
    initialize_state_search_headers,
    insert_state_search_header,
    inspect_state_search_headers,
)
from core.cognitive.state_store import CognitiveStateStore
from scripts import reconcile_cognitive_state_store as reconcile_cli
from scripts.reconcile_cognitive_state_store import main as reconcile_main


LEGACY_DDL = """
CREATE TABLE runtime_flow_registry (
    flow_id TEXT PRIMARY KEY, data_type TEXT NOT NULL, topic TEXT NOT NULL,
    producer_refs TEXT NOT NULL, consumer_refs TEXT NOT NULL,
    pending_budget INTEGER NOT NULL DEFAULT 0,
    dead_letter_budget INTEGER NOT NULL DEFAULT 0,
    max_lag_seconds INTEGER NOT NULL,
    registered_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 1,
    min_observations INTEGER NOT NULL DEFAULT 1,
    observation_mode TEXT NOT NULL DEFAULT 'continuous',
    not_applicable_reason TEXT NOT NULL DEFAULT '',
    freshness_required INTEGER NOT NULL DEFAULT 1,
    receipt_grace_seconds INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE runtime_flow_events (
    event_id TEXT PRIMARY KEY, flow_id TEXT NOT NULL, direction TEXT NOT NULL,
    topic TEXT NOT NULL, source TEXT NOT NULL, item_id TEXT NOT NULL,
    created_at TEXT NOT NULL, metadata TEXT NOT NULL,
    generation_id TEXT NOT NULL DEFAULT 'legacy-unknown',
    intended_consumers TEXT NOT NULL DEFAULT '[]',
    idempotency_key TEXT NOT NULL DEFAULT ''
);
CREATE TABLE runtime_flow_receipts (
    receipt_id TEXT PRIMARY KEY, production_event_id TEXT NOT NULL,
    flow_id TEXT NOT NULL, consumer_id TEXT NOT NULL, status TEXT NOT NULL,
    item_id TEXT NOT NULL, generation_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
    metadata TEXT NOT NULL
);
CREATE TABLE cognitive_data_events (
    event_id TEXT PRIMARY KEY, source_id TEXT, asset_id TEXT,
    source_kind TEXT NOT NULL, source_uri TEXT NOT NULL,
    content_hash TEXT NOT NULL, canonical_subject TEXT NOT NULL,
    data_type TEXT NOT NULL, producer TEXT NOT NULL,
    intended_consumers TEXT NOT NULL, privacy_level TEXT NOT NULL,
    confidence REAL NOT NULL, evidence_refs TEXT NOT NULL,
    dedupe_key TEXT NOT NULL, lifecycle_status TEXT NOT NULL,
    retention_policy TEXT NOT NULL, metadata TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE cognitive_data_consumptions (
    consumption_id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
    consumer_id TEXT NOT NULL, action_changed INTEGER NOT NULL DEFAULT 0,
    outcome TEXT, status TEXT NOT NULL, metadata TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE cognitive_data_reconciliations (
    reconciliation_id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
    related_event_id TEXT NOT NULL, relation_type TEXT NOT NULL,
    dedupe_key TEXT NOT NULL, reason TEXT NOT NULL,
    metadata TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(event_id, related_event_id, relation_type)
);
"""


EPISODE_PAYLOAD = {
    "situation": "Legacy semantic metadata was complete enough to classify.",
    "goal": "Migrate only evidence-complete historical cognition.",
    "constraints": ["do not invent evidence"],
    "facts": ["the event has an exact source revision"],
    "hypotheses": [],
    "causal_links": [],
    "alternatives": [],
    "tradeoffs": [],
    "decisions": ["admit a typed historical candidate"],
    "actions": [],
    "outcomes": [],
    "corrections": [],
}


def _legacy_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_DDL)
        conn.execute(
            "INSERT INTO runtime_flow_registry VALUES "
            "('flow', 'event', 'flow', '[\"producer\"]', '[\"consumer\"]', "
            "0, 0, 86400, '2026-07-15T00:00:00+00:00', "
            "'2026-07-15T00:00:00+00:00', 1, 1, 'continuous', '', 1, 0)"
        )
        conn.execute(
            "INSERT INTO runtime_flow_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "runtime-produced",
                "flow",
                "produced",
                "flow",
                "producer",
                "item-1",
                "2026-07-15T00:00:00+00:00",
                "{}",
                "generation-1",
                '["consumer"]',
                "flow:item-1",
            ),
        )
        _insert_legacy_event(
            conn,
            event_id="regular-event",
            data_type="conversation_turn",
            producer="capture_service",
            consumers=("distill",),
            metadata={},
        )
        _insert_legacy_event(
            conn,
            event_id="semantic-complete",
            data_type="cognition_episode",
            producer="cognitive_state_store",
            consumers=("wiki",),
            metadata={
                "schema_version": "mnemos.cognition_episode.v1",
                "source_revision_id": "raw-revision-complete",
                "scope_type": "project",
                "scope_id": "mnemos",
                "evidence_refs": ["raw-event-complete#0:50"],
                "payload": EPISODE_PAYLOAD,
            },
        )
        _insert_legacy_event(
            conn,
            event_id="semantic-incomplete",
            data_type="cognition_episode",
            producer="cognitive_state_store",
            consumers=("wiki",),
            metadata={"payload": {"goal": "missing lineage and fields"}},
        )
        conn.execute(
            "INSERT INTO cognitive_data_consumptions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "regular-consumed",
                "regular-event",
                "distill",
                0,
                "queued",
                "consumed",
                "{}",
                "2026-07-15T00:00:01+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO cognitive_data_consumptions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "orphan-consumed",
                "missing-event",
                "distill",
                1,
                "claimed change",
                "consumed",
                "{}",
                "2026-07-15T00:00:02+00:00",
            ),
        )


def _insert_legacy_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    data_type: str,
    producer: str,
    consumers: tuple[str, ...],
    metadata: dict,
) -> None:
    conn.execute(
        "INSERT INTO cognitive_data_events VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            f"source-{event_id}",
            f"asset-{event_id}",
            "legacy",
            f"legacy://{event_id}",
            f"sha256:{event_id}",
            event_id,
            data_type,
            producer,
            json.dumps(consumers),
            "private",
            0.8,
            json.dumps([f"raw:{event_id}#0:10"]),
            f"dedupe:{event_id}",
            "consumed",
            "default",
            json.dumps(metadata),
            "2026-07-15T00:00:00+00:00",
            "2026-07-15T00:00:00+00:00",
        ),
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_v2_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_CANONICAL_V2_DDL)
        conn.execute(
            "INSERT INTO mnemos_schema_registry VALUES (?, ?, ?, ?)",
            (
                SCHEMA_COMPONENT,
                LEGACY_CANONICAL_V2_SCHEMA_VERSION,
                LEGACY_CANONICAL_V2_DDL_HASH,
                "2026-07-18T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO cognitive_state_migration_quarantine VALUES (
                'cogquarantine-v2-existing', 'legacy.table', 'row-v2',
                'historical_unverifiable_prediction', '[]', '{}', ?,
                '2026-07-18T00:00:00+00:00'
            )
            """,
            ("sha256:" + "a" * 64,),
        )


def _canonical_v3_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_CANONICAL_V3_DDL)
        conn.execute(
            "INSERT INTO mnemos_schema_registry VALUES (?, ?, ?, ?)",
            (
                SCHEMA_COMPONENT,
                LEGACY_CANONICAL_V3_SCHEMA_VERSION,
                LEGACY_CANONICAL_V3_DDL_HASH,
                "2026-07-19T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO cognitive_state_migration_quarantine VALUES (
                'cogquarantine-v3-existing', 'legacy.training', 'row-v3',
                'historical_training_asset', '[]', '{}', ?,
                '2026-07-19T00:00:00+00:00'
            )
            """,
            ("sha256:" + "b" * 64,),
        )


def _canonical_v4_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_CANONICAL_V4_DDL)
        conn.execute(
            "INSERT INTO mnemos_schema_registry VALUES (?, ?, ?, ?)",
            (
                SCHEMA_COMPONENT,
                LEGACY_CANONICAL_V4_SCHEMA_VERSION,
                LEGACY_CANONICAL_V4_DDL_HASH,
                "2026-07-25T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO runtime_flow_registry (
                flow_id, data_type, topic, producer_refs, consumer_refs,
                pending_budget, dead_letter_budget, max_lag_seconds,
                registered_at, updated_at, required, min_observations,
                observation_mode, not_applicable_reason, freshness_required,
                receipt_grace_seconds
            ) VALUES (
                'raw_quality_to_distill_gate', 'runtime observed',
                'raw_quality_to_distill_gate', '[]', '[]', 0, 0, 3600,
                '2026-07-25T00:00:00+00:00', '2026-07-25T00:00:00+00:00',
                1, 1, 'continuous', '', 1, 0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO runtime_flow_events (
                event_id, flow_id, direction, topic, source, item_id,
                created_at, metadata, generation_id, intended_consumers,
                idempotency_key
            ) VALUES (
                'rte-v4-produced', 'raw_quality_to_distill_gate', 'produced',
                'raw_quality_to_distill_gate', 'core/sync_framework/sync_engine.py',
                'distill-session:v4', '2026-07-25T00:00:00+00:00', '{}',
                'distill-task:v4-task:v4-revision',
                '["core/hephaestus/distillation_engine.py"]', ''
            )
            """
        )
        conn.execute(
            """
            INSERT INTO runtime_flow_receipts (
                receipt_id, production_event_id, flow_id, consumer_id,
                status, item_id, generation_id, idempotency_key,
                created_at, metadata
            ) VALUES (
                'rtr-v4-consumed', 'rte-v4-produced',
                'raw_quality_to_distill_gate',
                'core/hephaestus/distillation_engine.py', 'consumed',
                'distill-session:v4', 'distill-task:v4-task:v4-revision',
                'raw_quality_to_distill_gate:rte-v4-produced:consumed',
                '2026-07-25T00:00:00+00:00', '{}'
            )
            """
        )


def _canonical_v4_db_with_search_projection(path: Path) -> None:
    _canonical_v4_db(path)
    access = make_cognitive_access_envelope(
        owner_principal_id="test:migration",
        owner_agent="migration-test",
        scope_type="project",
        scope_id="mnemos",
        project="mnemos",
        purposes=("cognitive_state_read",),
        consent_provenance_refs=("raw:test-migration#0:10",),
        sensitivity="sensitive",
        retention_policy="cognitive_state",
        source_acl_lineage=("sha256:" + "a" * 64,),
    )
    payload = {"access_control": access, "claim": "coupled projection survives"}
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload_hash = "sha256:" + hashlib.sha256(payload_json.encode()).hexdigest()
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            INSERT INTO cognitive_data_events(
                event_id, source_id, asset_id, source_kind, source_uri,
                content_hash, canonical_subject, data_type, producer,
                intended_consumers, privacy_level, confidence, evidence_refs,
                dedupe_key, lifecycle_status, retention_policy, metadata,
                created_at, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "event-search-v4",
                "raw-search-v4",
                "raw-search-v4",
                "test",
                "raw://search-v4",
                "sha256:" + "b" * 64,
                "search-v4",
                "decision_trace",
                "test",
                '["search"]',
                "local",
                1.0,
                '["raw:test-migration#0:10"]',
                "search-v4",
                "produced",
                "cognitive_state",
                "{}",
                "2026-07-25T00:00:00+00:00",
                "2026-07-25T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO cognitive_state_revisions(
                revision_id, object_type, object_id, schema_version, revision_no,
                source_event_id, source_revision_id, source_content_hash,
                scope_type, scope_id, evidence_refs, evidence_hash, payload_json,
                payload_hash, supersedes_revision_id, correction_of_revision_id,
                admission_state, redaction_policy, redaction_counts, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "revision-search-v4",
                "decision_trace",
                "search-v4",
                "mnemos.decision_trace.v1",
                1,
                "event-search-v4",
                "raw-search-v4",
                "sha256:" + "b" * 64,
                "project",
                "mnemos",
                '["raw:test-migration#0:10"]',
                "sha256:" + "c" * 64,
                payload_json,
                payload_hash,
                None,
                None,
                "active",
                "none",
                "{}",
                "2026-07-25T00:00:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO cognitive_state_heads VALUES (?, ?, ?, ?)",
            (
                "decision_trace",
                "search-v4",
                "revision-search-v4",
                "2026-07-25T00:00:00+00:00",
            ),
        )
        initialize_state_search_headers(conn)
        insert_state_search_header(
            conn,
            revision_id="revision-search-v4",
            object_type="decision_trace",
            object_id="search-v4",
            scope_type="project",
            scope_id="mnemos",
            payload=payload,
            revision_payload_hash=payload_hash,
            created_at="2026-07-25T00:00:00+00:00",
        )
        conn.commit()


def test_v4_to_v5_upgrade_preserves_coupled_search_projection(tmp_path: Path) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    _canonical_v4_db_with_search_projection(db)

    with sqlite3.connect(db) as conn:
        before = inspect_state_search_headers(conn)
        report = reconcile_cognitive_state_schema(conn, apply=True)
        after = inspect_state_search_headers(conn)
        foreign_keys = conn.execute(
            "PRAGMA foreign_key_list(typed_search_state_revision_bindings)"
        ).fetchall()
        joined = conn.execute(
            "SELECT h.revision_id FROM typed_search_state_headers AS h "
            "JOIN cognitive_state_revisions AS r USING(revision_id)"
        ).fetchall()

    assert before["ok"] is True
    assert report["applied"] is True
    assert after["ok"] is True
    assert after["schema_definition_mismatch_count"] == 0
    assert {row[2] for row in foreign_keys} == {"cognitive_state_revisions"}
    assert joined == [("revision-search-v4",)]


def test_v4_to_v5_coupled_projection_failure_rolls_back_every_object(
    tmp_path: Path,
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    _canonical_v4_db_with_search_projection(db)
    with sqlite3.connect(db) as conn:
        before_schema = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        before_revision = conn.execute(
            "SELECT * FROM cognitive_state_revisions"
        ).fetchall()
        before_headers = conn.execute(
            "SELECT * FROM typed_search_state_headers"
        ).fetchall()

        def failpoint(stage: str) -> None:
            if stage == "after_copy":
                raise sqlite3.OperationalError("injected coupled migration failure")

        with pytest.raises(
            sqlite3.OperationalError,
            match="injected coupled migration failure",
        ):
            reconcile_cognitive_state_schema(conn, apply=True, failpoint=failpoint)

        assert conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall() == before_schema
        assert conn.execute(
            "SELECT * FROM cognitive_state_revisions"
        ).fetchall() == before_revision
        assert conn.execute(
            "SELECT * FROM typed_search_state_headers"
        ).fetchall() == before_headers
        assert inspect_state_search_headers(conn)["ok"] is True


def test_exact_canonical_v4_requires_explicit_lossless_v5_upgrade(
    tmp_path: Path,
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    _canonical_v4_db(db)
    with sqlite3.connect(db) as conn:
        before = inspect_cognitive_state_schema(conn)
        preview = reconcile_cognitive_state_schema(conn, apply=False)
        report = reconcile_cognitive_state_schema(conn, apply=True)
        after = inspect_cognitive_state_schema(conn)
        receipts = conn.execute(
            "SELECT receipt_id, status FROM runtime_flow_receipts"
        ).fetchall()
        receipt_ddl = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='runtime_flow_receipts'"
            ).fetchone()[0]
        )
        # The upgraded schema accepts a nonterminal stage receipt.
        conn.execute(
            """
            INSERT INTO runtime_flow_receipts (
                receipt_id, production_event_id, flow_id, consumer_id,
                status, item_id, generation_id, idempotency_key,
                created_at, metadata
            ) VALUES (
                'rtr-v5-stage', 'rte-v4-produced',
                'raw_quality_to_distill_gate',
                'core/hephaestus/distillation_engine.py', 'in_progress',
                'distill-session:v4', 'distill-task:v4-task:v4-revision',
                'raw_quality_to_distill_gate:rte-v4-produced:in_progress',
                '2026-07-25T00:00:01+00:00', '{}'
            )
            """
        )

    assert STATE_SCHEMA_VERSION == "mnemos.cognitive_state_store.v5"
    assert before.classification == "canonical_v4_stage_receipt_upgrade_required"
    assert preview["action"] == "upgrade_stage_receipt_schema"
    assert report["applied"] is True
    assert after.classification == "canonical"
    assert receipts == [("rtr-v4-consumed", "consumed")]
    assert "'in_progress'" in receipt_ddl


def test_stage_receipt_is_rejected_by_legacy_v4_schema(tmp_path: Path) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    _canonical_v4_db(db)
    with sqlite3.connect(db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO runtime_flow_receipts (
                    receipt_id, production_event_id, flow_id, consumer_id,
                    status, item_id, generation_id, idempotency_key,
                    created_at, metadata
                ) VALUES (
                    'rtr-v4-stage-rejected', 'rte-v4-produced',
                    'raw_quality_to_distill_gate',
                    'core/hephaestus/distillation_engine.py', 'in_progress',
                    'distill-session:v4', 'distill-task:v4-task:v4-revision',
                    'raw_quality_to_distill_gate:rte-v4-produced:in_progress',
                    '2026-07-25T00:00:01+00:00', '{}'
                )
                """
            )


def test_exact_canonical_v3_requires_explicit_lossless_v4_upgrade(
    tmp_path: Path,
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    _canonical_v3_db(db)
    with sqlite3.connect(db) as conn:
        before = inspect_cognitive_state_schema(conn)
        preview = reconcile_cognitive_state_schema(conn, apply=False)
        report = reconcile_cognitive_state_schema(conn, apply=True)
        after = inspect_cognitive_state_schema(conn)
        preserved = conn.execute(
            "SELECT source_key, reason_code FROM cognitive_state_migration_quarantine"
        ).fetchall()
        revision_ddl = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='cognitive_state_revisions'"
            ).fetchone()[0]
        )

    assert STATE_SCHEMA_VERSION == "mnemos.cognitive_state_store.v5"
    assert before.classification == "canonical_v3_training_governance_upgrade_required"
    assert preview["action"] == "upgrade_training_governance_schema"
    assert report["applied"] is True
    assert after.classification == "canonical"
    assert preserved == [("row-v3", "historical_training_asset")]
    assert "training_admission_record" in revision_ddl
    assert "training_run_record" in revision_ddl


def test_exact_canonical_v2_requires_explicit_lossless_v3_upgrade(
    tmp_path: Path,
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    _canonical_v2_db(db)
    with sqlite3.connect(db) as conn:
        before = inspect_cognitive_state_schema(conn)
        preview = reconcile_cognitive_state_schema(conn, apply=False)
        report = reconcile_cognitive_state_schema(conn, apply=True)
        after = inspect_cognitive_state_schema(conn)
        preserved = conn.execute(
            "SELECT source_key, reason_code FROM cognitive_state_migration_quarantine"
        ).fetchall()

    assert before.classification == "canonical_v2_feedback_attribution_upgrade_required"
    assert preview["action"] == "upgrade_feedback_attribution_schema"
    assert report["applied"] is True
    assert after.classification == "canonical"
    assert preserved == [("row-v2", "historical_unverifiable_prediction")]


def test_reconciliation_dry_run_is_read_only_and_classifies_legacy(tmp_path: Path) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    _legacy_db(db)
    before = _sha(db)
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        report = reconcile_cognitive_state_schema(conn, apply=False)

    assert report["before"]["classification"] == "legacy_runtime_v1_or_v2"
    assert report["action"] == "migrate_with_quarantine"
    assert report["candidate_counts"]["typed_candidates"] == 1
    assert report["candidate_counts"]["quarantined_semantic_events"] == 1
    assert _sha(db) == before


def test_reconciliation_migrates_complete_candidate_and_quarantines_unknowns(
    tmp_path: Path,
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    _legacy_db(db)
    with sqlite3.connect(db) as conn:
        report = reconcile_cognitive_state_schema(conn, apply=True)
        state = inspect_cognitive_state_schema(conn)
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "cognitive_data_events",
                "cognitive_data_consumptions",
                "cognitive_data_consumer_heads",
                "cognitive_state_revisions",
                "cognitive_state_heads",
                "cognitive_state_migration_quarantine",
            )
        }

    assert report["applied"] is True
    assert state.ok is True
    assert counts == {
        "cognitive_data_events": 2,
        "cognitive_data_consumptions": 1,
        "cognitive_data_consumer_heads": 1,
        "cognitive_state_revisions": 1,
        "cognitive_state_heads": 0,
        "cognitive_state_migration_quarantine": 2,
    }
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute(
                "SELECT lifecycle_status FROM cognitive_data_events WHERE event_id='regular-event'"
            ).fetchone()[0]
            == "produced"
        )
        assert conn.execute(
            "SELECT object_type, schema_version, admission_state " "FROM cognitive_state_revisions"
        ).fetchone() == (
            "cognition_episode",
            "mnemos.cognition_episode.pre_cog010.v1",
            "historical_candidate",
        )
        migrated_payload = json.loads(
            conn.execute("SELECT payload_json FROM cognitive_state_revisions").fetchone()[0]
        )
        assert migrated_payload["_migration_contract"] == {
            "active_schema_upgrade": False,
            "classification": "pre_cog010_historical_candidate",
            "source_schema_version": "mnemos.cognition_episode.v1",
        }
        assert (
            conn.execute("SELECT action_changed FROM cognitive_data_consumptions").fetchone()[0]
            == 0
        )

    store = CognitiveStateStore(db)
    assert store.current_revisions() == ()
    assert store.rebuild_current_state()["heads"] == []
    assert store.rebuild_current_state()["projection_hash_matches"] is True

    with sqlite3.connect(db) as conn:
        second = reconcile_cognitive_state_schema(conn, apply=True)
    assert second["applied"] is False
    assert second["action"] == "already_canonical"


def test_failed_typed_candidate_does_not_leave_an_envelope_without_revision(
    tmp_path: Path,
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    _legacy_db(db)
    with sqlite3.connect(db) as conn:
        _insert_legacy_event(
            conn,
            event_id="semantic-broken-lineage",
            data_type="cognition_episode",
            producer="cognitive_state_store",
            consumers=("wiki",),
            metadata={
                "schema_version": "mnemos.cognition_episode.v1",
                "source_revision_id": "raw-revision-broken-lineage",
                "scope_type": "project",
                "scope_id": "mnemos",
                "evidence_refs": ["raw-event-broken-lineage#0:50"],
                "payload": EPISODE_PAYLOAD,
                "supersedes_revision_id": "missing-parent-revision",
            },
        )
        report = reconcile_cognitive_state_schema(conn, apply=True)
        event_count = conn.execute(
            "SELECT COUNT(*) FROM cognitive_data_events " "WHERE event_id='semantic-broken-lineage'"
        ).fetchone()[0]
        quarantine_count = conn.execute(
            "SELECT COUNT(*) FROM cognitive_state_migration_quarantine "
            "WHERE source_key='semantic-broken-lineage'"
        ).fetchone()[0]

    assert report["applied"] is True
    assert event_count == 0
    assert quarantine_count == 1


def test_reconciliation_failure_rolls_back_to_byte_equivalent_logical_schema(
    tmp_path: Path,
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    _legacy_db(db)
    with sqlite3.connect(db) as conn:
        before_tables = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        before_events = conn.execute(
            "SELECT * FROM cognitive_data_events ORDER BY event_id"
        ).fetchall()

        def failpoint(stage: str) -> None:
            if stage == "after_copy":
                raise sqlite3.OperationalError("injected migration failure")

        with pytest.raises(sqlite3.OperationalError, match="injected migration failure"):
            reconcile_cognitive_state_schema(conn, apply=True, failpoint=failpoint)

        assert (
            conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
            == before_tables
        )
        assert (
            conn.execute("SELECT * FROM cognitive_data_events ORDER BY event_id").fetchall()
            == before_events
        )


def test_reconcile_cli_requires_backup_and_creates_verified_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    monkeypatch.setattr(
        "scripts.reconcile_cognitive_state_store.runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    _legacy_db(db)

    assert reconcile_main(["--db-path", str(db), "--apply", "--json"]) == 2
    assert "--apply requires --backup-dir" in capsys.readouterr().out

    backup_dir = tmp_path / "backups"
    assert (
        reconcile_main(
            [
                "--db-path",
                str(db),
                "--backup-dir",
                str(backup_dir),
                "--json",
            ]
        )
        == 0
    )
    plan_hash = json.loads(capsys.readouterr().out)["plan_hash"]
    assert (
        reconcile_main(
            [
                "--db-path",
                str(db),
                "--apply",
                "--backup-dir",
                str(backup_dir),
                "--expected-plan-hash",
                plan_hash,
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    backup = Path(payload["backup"]["path"])
    assert backup.is_file()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert payload["backup"]["integrity_check"] == "ok"
    with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as conn:
        assert inspect_cognitive_state_schema(conn).classification == ("legacy_runtime_v1_or_v2")


def test_reconcile_cli_backs_up_and_preserves_coupled_v4_search_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The operator path preserves both the v4 preimage and coupled projection."""
    db = tmp_path / "producer_consumer_ledger.db"
    backup_dir = tmp_path / "backups"
    _canonical_v4_db_with_search_projection(db)
    monkeypatch.setattr(
        "scripts.reconcile_cognitive_state_store.runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    assert (
        reconcile_main(
            [
                "--db-path",
                str(db),
                "--backup-dir",
                str(backup_dir),
                "--json",
            ]
        )
        == 0
    )
    plan_hash = json.loads(capsys.readouterr().out)["plan_hash"]

    assert (
        reconcile_main(
            [
                "--db-path",
                str(db),
                "--apply",
                "--backup-dir",
                str(backup_dir),
                "--expected-plan-hash",
                plan_hash,
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    backup = Path(payload["backup"]["path"])
    with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as conn:
        assert inspect_cognitive_state_schema(conn).classification == (
            "canonical_v4_stage_receipt_upgrade_required"
        )
        assert inspect_state_search_headers(conn)["ok"] is True
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        assert inspect_cognitive_state_schema(conn).classification == "canonical"
        assert inspect_state_search_headers(conn)["ok"] is True
        assert conn.execute(
            "SELECT COUNT(*) FROM typed_search_state_headers"
        ).fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    first_backups = list(backup_dir.glob("producer-consumer-before-*.db"))
    assert len(first_backups) == 1

    assert (
        reconcile_main(
            [
                "--db-path",
                str(db),
                "--apply",
                "--backup-dir",
                str(backup_dir),
                "--expected-plan-hash",
                plan_hash,
                "--json",
            ]
        )
        == 0
    )
    second = json.loads(capsys.readouterr().out)
    assert second["action"] == "same_plan_second_apply"
    assert second["physical_delta"] == 0
    assert second["semantic_delta"] == 0
    assert second["physical_pre_signature"] == second["physical_post_signature"]
    assert list(backup_dir.glob("producer-consumer-before-*.db")) == first_backups

    assert (
        reconcile_main(
            [
                "--db-path",
                str(db),
                "--backup-dir",
                str(backup_dir),
                "--json",
            ]
        )
        == 0
    )
    canonical_plan_hash = json.loads(capsys.readouterr().out)["plan_hash"]
    assert (
        reconcile_main(
            [
                "--db-path",
                str(db),
                "--apply",
                "--backup-dir",
                str(backup_dir),
                "--expected-plan-hash",
                canonical_plan_hash,
                "--json",
            ]
        )
        == 0
    )
    canonical_noop = json.loads(capsys.readouterr().out)
    assert canonical_noop["action"] == "already_canonical"
    assert canonical_noop["backup"] is None
    assert canonical_noop["physical_delta"] == 0
    assert list(backup_dir.glob("producer-consumer-before-*.db")) == first_backups


def test_completed_cognitive_receipt_rejects_current_code_dependency_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    backup_dir = tmp_path / "backups"
    _canonical_v4_db_with_search_projection(db)
    monkeypatch.setattr(
        "scripts.reconcile_cognitive_state_store.runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    assert reconcile_main(
        [
            "--db-path",
            str(db),
            "--backup-dir",
            str(backup_dir),
            "--json",
        ]
    ) == 0
    plan_hash = json.loads(capsys.readouterr().out)["plan_hash"]
    command = [
        "--db-path",
        str(db),
        "--apply",
        "--backup-dir",
        str(backup_dir),
        "--expected-plan-hash",
        plan_hash,
        "--json",
    ]
    assert reconcile_main(command) == 0
    capsys.readouterr()
    original = reconcile_cli._cognitive_migration_dependency_hashes  # noqa: SLF001
    monkeypatch.setattr(
        reconcile_cli,
        "_cognitive_migration_dependency_hashes",
        lambda: {**original(), "core/future_dependency.py": "sha256:" + "0" * 64},
    )

    assert reconcile_main(command) == 1
    assert "migration_receipt_code_drift" in capsys.readouterr().out


def test_completed_cognitive_receipt_rejects_runtime_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    backup_dir = tmp_path / "backups"
    _canonical_v4_db_with_search_projection(db)
    monkeypatch.setattr(
        "scripts.reconcile_cognitive_state_store.runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    assert reconcile_main(
        [
            "--db-path",
            str(db),
            "--backup-dir",
            str(backup_dir),
            "--json",
        ]
    ) == 0
    plan_hash = json.loads(capsys.readouterr().out)["plan_hash"]
    command = [
        "--db-path",
        str(db),
        "--apply",
        "--backup-dir",
        str(backup_dir),
        "--expected-plan-hash",
        plan_hash,
        "--json",
    ]
    assert reconcile_main(command) == 0
    capsys.readouterr()
    current = reconcile_cli.runtime_execution_identity()
    monkeypatch.setattr(
        reconcile_cli,
        "runtime_execution_identity",
        lambda: {**current, "sqlite_runtime_version": "drifted"},
    )

    assert reconcile_main(command) == 1
    assert "migration_receipt_runtime_drift" in capsys.readouterr().out


def test_completed_cognitive_receipt_rejects_public_backup_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    backup_dir = tmp_path / "backups"
    _canonical_v4_db_with_search_projection(db)
    monkeypatch.setattr(
        "scripts.reconcile_cognitive_state_store.runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    assert reconcile_main(
        [
            "--db-path",
            str(db),
            "--backup-dir",
            str(backup_dir),
            "--json",
        ]
    ) == 0
    plan_hash = json.loads(capsys.readouterr().out)["plan_hash"]
    command = [
        "--db-path",
        str(db),
        "--apply",
        "--backup-dir",
        str(backup_dir),
        "--expected-plan-hash",
        plan_hash,
        "--json",
    ]
    assert reconcile_main(command) == 0
    payload = json.loads(capsys.readouterr().out)
    backup = Path(payload["backup"]["path"])
    backup.chmod(0o644)

    assert reconcile_main(command) == 1
    assert "migration_receipt_backup_invalid" in capsys.readouterr().out


def test_completed_cognitive_receipt_rejects_public_receipt_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    backup_dir = tmp_path / "backups"
    _canonical_v4_db_with_search_projection(db)
    monkeypatch.setattr(
        "scripts.reconcile_cognitive_state_store.runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    assert reconcile_main(
        [
            "--db-path",
            str(db),
            "--backup-dir",
            str(backup_dir),
            "--json",
        ]
    ) == 0
    plan_hash = json.loads(capsys.readouterr().out)["plan_hash"]
    command = [
        "--db-path",
        str(db),
        "--apply",
        "--backup-dir",
        str(backup_dir),
        "--expected-plan-hash",
        plan_hash,
        "--json",
    ]
    assert reconcile_main(command) == 0
    payload = json.loads(capsys.readouterr().out)
    receipt = Path(payload["receipt_path"])
    receipt.chmod(0o644)

    assert reconcile_main(command) == 1
    assert "migration_receipt_permissions_invalid" in capsys.readouterr().out


def test_completed_cognitive_receipt_detects_physical_scope_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    backup_dir = tmp_path / "backups"
    _canonical_v4_db_with_search_projection(db)
    monkeypatch.setattr(
        reconcile_cli,
        "runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    assert reconcile_main(
        ["--db-path", str(db), "--backup-dir", str(backup_dir), "--json"]
    ) == 0
    plan_hash = json.loads(capsys.readouterr().out)["plan_hash"]
    command = [
        "--db-path",
        str(db),
        "--apply",
        "--backup-dir",
        str(backup_dir),
        "--expected-plan-hash",
        plan_hash,
        "--json",
    ]
    assert reconcile_main(command) == 0
    capsys.readouterr()
    original = reconcile_cli._verified_plan_backup  # noqa: SLF001

    def inject_scope_write(**kwargs):
        value = original(**kwargs)
        (backup_dir / "unexpected-second-apply-write").write_text("drift")
        return value

    monkeypatch.setattr(reconcile_cli, "_verified_plan_backup", inject_scope_write)
    assert reconcile_main(command) == 1
    assert "migration_second_apply_physical_drift" in capsys.readouterr().out


def test_cognitive_receipt_publish_fsyncs_parent_after_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "private" / "receipt.json"
    events: list[tuple[str, Path]] = []
    original_replace = reconcile_cli.os.replace

    def tracked_replace(source, destination):
        original_replace(source, destination)
        events.append(("replace", Path(destination)))

    monkeypatch.setattr(reconcile_cli.os, "replace", tracked_replace)
    monkeypatch.setattr(
        reconcile_cli,
        "fsync_directory",
        lambda path: events.append(("fsync", Path(path))),
    )
    reconcile_cli._atomic_write_json(target, {"status": "prepared"})  # noqa: SLF001

    assert events[-2:] == [
        ("replace", target),
        ("fsync", target.parent),
    ]


def test_reconcile_cli_rejects_forged_prepared_backup_without_touching_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    backup_dir = tmp_path / "backups"
    _canonical_v4_db_with_search_projection(db)
    monkeypatch.setattr(
        "scripts.reconcile_cognitive_state_store.runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    assert (
        reconcile_main(
            [
                "--db-path",
                str(db),
                "--backup-dir",
                str(backup_dir),
                "--json",
            ]
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    plan_hash = preview["plan_hash"]
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    attacker = backup_dir / "producer-consumer-before-cognitive-state-attacker.db"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    with sqlite3.connect(attacker) as conn:
        conn.execute("CREATE TABLE attacker_owned (value TEXT)")
        conn.execute("INSERT INTO attacker_owned VALUES ('forged')")
    attacker.chmod(0o600)
    receipt = backup_dir / (
        f"cognitive-state-migration.{plan_hash.removeprefix('sha256:')}.json"
    )
    receipt.write_text(
        json.dumps(
            {
                "schema_version": "mnemos.cognitive_state_migration_receipt.v1",
                "status": "prepared",
                "plan_hash": plan_hash,
                "reviewed_plan": preview["plan"],
                "db_path": str(db.resolve()),
                "backup_dir": str(backup_dir.resolve()),
                "backup": {
                    "path": str(attacker),
                    "sha256": "sha256:" + hashlib.sha256(attacker.read_bytes()).hexdigest(),
                    "integrity_check": "ok",
                },
                "before_logical_hash": preview["plan"]["database_logical_hash"],
            }
        ),
        encoding="utf-8",
    )
    receipt.chmod(0o600)

    assert (
        reconcile_main(
            [
                "--db-path",
                str(db),
                "--apply",
                "--backup-dir",
                str(backup_dir),
                "--expected-plan-hash",
                plan_hash,
                "--json",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error"] == (
        "migration_receipt_backup_invalid"
    )
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='attacker_owned'"
        ).fetchone()[0] == 0


def test_active_writer_dry_run_plan_cannot_authorize_later_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    backup_dir = tmp_path / "backups"
    _canonical_v4_db_with_search_projection(db)
    monkeypatch.setattr(
        "scripts.reconcile_cognitive_state_store.runtime_writers_are_inactive",
        lambda _database_dir: False,
    )
    assert (
        reconcile_main(
            [
                "--db-path",
                str(db),
                "--backup-dir",
                str(backup_dir),
                "--json",
            ]
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["plan"]["apply_eligible"] is False
    assert preview["plan"]["writer_lock_state"] == "active_or_unverified"
    monkeypatch.setattr(
        "scripts.reconcile_cognitive_state_store.runtime_writers_are_inactive",
        lambda _database_dir: True,
    )

    assert (
        reconcile_main(
            [
                "--db-path",
                str(db),
                "--apply",
                "--backup-dir",
                str(backup_dir),
                "--expected-plan-hash",
                preview["plan_hash"],
                "--json",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error"] == "expected_plan_hash_mismatch"
    assert not list(backup_dir.glob("producer-consumer-before-*.db"))


def test_cognitive_state_plan_binds_shared_ddl_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    backup_dir = tmp_path / "backups"
    _canonical_v4_db_with_search_projection(db)
    monkeypatch.setattr(
        reconcile_cli,
        "runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    assert (
        reconcile_main(
            [
                "--db-path",
                str(db),
                "--backup-dir",
                str(backup_dir),
                "--json",
            ]
        )
        == 0
    )
    first = json.loads(capsys.readouterr().out)
    original_hash = reconcile_cli._sha256  # noqa: SLF001

    def changed_ddl_hash(path: Path):
        if Path(path).name == "state_schema_ddl.py":
            return "sha256:" + ("0" * 64)
        return original_hash(path)

    monkeypatch.setattr(reconcile_cli, "_sha256", changed_ddl_hash)
    assert (
        reconcile_main(
            [
                "--db-path",
                str(db),
                "--backup-dir",
                str(backup_dir),
                "--json",
            ]
        )
        == 0
    )
    second = json.loads(capsys.readouterr().out)

    assert first["plan_hash"] != second["plan_hash"]
    assert (
        first["plan"]["dependency_hashes"]["core/cognitive/state_schema_ddl.py"]
        != second["plan"]["dependency_hashes"]["core/cognitive/state_schema_ddl.py"]
    )


def test_cognitive_state_prepared_recovery_rejects_drift_before_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    backup_dir = tmp_path / "backups"
    _canonical_v4_db_with_search_projection(db)
    monkeypatch.setattr(
        reconcile_cli,
        "runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    assert (
        reconcile_main(
            [
                "--db-path",
                str(db),
                "--backup-dir",
                str(backup_dir),
                "--json",
            ]
        )
        == 0
    )
    plan_hash = json.loads(capsys.readouterr().out)["plan_hash"]

    def killed_after_schema_apply():
        original = reconcile_cli._sqlite_logical_hash  # noqa: SLF001
        calls = 0

        def exit_on_post_hash(path: Path):
            nonlocal calls
            calls += 1
            if calls == 3:
                os._exit(79)
            return original(path)

        reconcile_cli._sqlite_logical_hash = exit_on_post_hash  # noqa: SLF001
        reconcile_main(
            [
                "--db-path",
                str(db),
                "--apply",
                "--backup-dir",
                str(backup_dir),
                "--expected-plan-hash",
                plan_hash,
                "--json",
            ]
        )

    process = multiprocessing.get_context("fork").Process(
        target=killed_after_schema_apply
    )
    process.start()
    process.join(30)
    if process.is_alive():
        process.terminate()
        process.join(5)
        raise AssertionError("cognitive migration fault-injection child hung")
    assert process.exitcode == 79
    receipt = backup_dir / (
        f"cognitive-state-migration.{plan_hash.removeprefix('sha256:')}.json"
    )
    assert json.loads(receipt.read_text())["status"] == "prepared"
    crashed_target_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    command = [
        "--db-path",
        str(db),
        "--apply",
        "--backup-dir",
        str(backup_dir),
        "--expected-plan-hash",
        plan_hash,
        "--json",
    ]
    current_runtime = reconcile_cli.runtime_execution_identity()
    with monkeypatch.context() as drift:
        drift.setattr(
            reconcile_cli,
            "runtime_execution_identity",
            lambda: {**current_runtime, "sqlite_runtime_version": "drifted"},
        )
        assert reconcile_main(command) == 1
        assert "migration_receipt_runtime_drift" in capsys.readouterr().out
    assert hashlib.sha256(db.read_bytes()).hexdigest() == crashed_target_hash
    assert json.loads(receipt.read_text())["status"] == "prepared"

    current_dependencies = reconcile_cli._cognitive_migration_dependency_hashes  # noqa: SLF001
    with monkeypatch.context() as drift:
        drift.setattr(
            reconcile_cli,
            "_cognitive_migration_dependency_hashes",
            lambda: {
                **current_dependencies(),
                "core/recovery_drift.py": "sha256:" + "0" * 64,
            },
        )
        assert reconcile_main(command) == 1
        assert "migration_receipt_code_drift" in capsys.readouterr().out
    assert hashlib.sha256(db.read_bytes()).hexdigest() == crashed_target_hash
    assert json.loads(receipt.read_text())["status"] == "prepared"

    assert reconcile_main(command) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["applied"] is True
    assert json.loads(receipt.read_text())["status"] == "completed"
    with sqlite3.connect(db) as conn:
        assert inspect_cognitive_state_schema(conn).classification == "canonical"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_reconcile_cli_refuses_apply_while_daemon_is_active(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    _legacy_db(db)
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(
        "scripts.reconcile_cognitive_state_store.runtime_writers_are_inactive",
        lambda _database_dir: False,
    )

    assert (
        reconcile_main(
            [
                "--db-path",
                str(db),
                "--apply",
                "--backup-dir",
                str(backup_dir),
                "--json",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["error"] == "daemon_not_inactive"
    assert not backup_dir.exists()


def test_unknown_partial_schema_is_never_auto_migrated(tmp_path: Path) -> None:
    db = tmp_path / "producer_consumer_ledger.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE cognitive_state_revisions (revision_id TEXT PRIMARY KEY)")
        with pytest.raises(CognitiveStateSchemaError, match="unknown"):
            reconcile_cognitive_state_schema(conn, apply=True)
