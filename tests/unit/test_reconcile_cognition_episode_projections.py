from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

import pytest


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_reconciliation_plan_is_read_only_and_binds_exact_inventory(tmp_path):
    from core.cognitive.cognition_episode_reconciliation import build_reconciliation_plan
    from tests.integration.test_cognition_episode_event_dispatch import (
        _RuntimeConfig,
        _committed_full_episode,
    )

    config = _RuntimeConfig(tmp_path)
    _result, receipt = _committed_full_episode(config)
    state_db = config.database_dir / "producer_consumer_ledger.db"
    before = _file_hash(state_db)

    plan = build_reconciliation_plan(config)

    assert plan["schema_version"] == "mnemos.cognition_episode_reconciliation.v1"
    assert plan["apply_required"] is True
    assert plan["inventory_hash"].startswith("sha256:")
    assert plan["pending_revision_ids"] == [receipt.revision_id]
    assert plan["bound_wiki_page_counts"] == {receipt.revision_id: 1}
    assert "dispatch_pending_cognition_episode" in plan["actions"]
    assert _file_hash(state_db) == before


def test_reconciliation_apply_requires_reviewed_hash_backup_and_stopped_runtime(tmp_path):
    from core.cognitive.cognition_episode_reconciliation import (
        apply_reconciliation,
        build_reconciliation_plan,
    )
    from tests.integration.test_cognition_episode_event_dispatch import (
        _RuntimeConfig,
        _committed_full_episode,
    )

    config = _RuntimeConfig(tmp_path)
    _committed_full_episode(config)
    plan = build_reconciliation_plan(config)

    with pytest.raises(ValueError, match="reviewed inventory hash"):
        apply_reconciliation(
            config,
            expected_inventory_hash="sha256:" + "0" * 64,
            backup_dir=tmp_path / "backup-invalid",
            daemon_check=lambda _path: True,
        )
    with pytest.raises(RuntimeError, match="conclusively stopped"):
        apply_reconciliation(
            config,
            expected_inventory_hash=plan["inventory_hash"],
            backup_dir=tmp_path / "backup-running",
            daemon_check=lambda _path: False,
        )


def test_opening_legacy_evidence_graph_does_not_silently_install_projection_schema(tmp_path):
    from core.evidence.evidence_graph import EvidenceGraph

    db_path = tmp_path / "legacy-evidence.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE evidence_nodes (
                id TEXT PRIMARY KEY,
                node_type TEXT NOT NULL,
                title TEXT DEFAULT '',
                source_path TEXT DEFAULT '',
                content TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE evidence_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                confidence REAL DEFAULT 1.0,
                evidence TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                UNIQUE(source_id, target_id, relation_type)
            );
            """)
        conn.commit()
    EvidenceGraph(str(db_path))

    with sqlite3.connect(db_path) as conn:
        node_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(evidence_nodes)")}
        tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "access_control" not in node_columns
    assert "cognition_episode_projection_effects" not in tables


def test_reconciliation_apply_backups_projects_and_is_idempotent(tmp_path):
    from core.cognitive.cognition_episode_reconciliation import (
        apply_reconciliation,
        build_reconciliation_plan,
    )
    from tests.integration.test_cognition_episode_event_dispatch import (
        _RuntimeConfig,
        _committed_full_episode,
    )

    config = _RuntimeConfig(tmp_path)
    _committed_full_episode(config)
    plan = build_reconciliation_plan(config)
    backup_dir = tmp_path / "backup"

    result = apply_reconciliation(
        config,
        expected_inventory_hash=plan["inventory_hash"],
        backup_dir=backup_dir,
        daemon_check=lambda _path: True,
    )

    assert result["ok"] is True
    assert result["event_dispatch_audit"]["ok"] is True
    assert result["evidence_direction_audit"]["ok"] is True
    assert result["backup_manifest"]["integrity_ok"] is True
    assert (backup_dir / "manifest.json").is_file()
    closed = build_reconciliation_plan(config)
    assert closed["apply_required"] is False
    assert closed["pending_revision_ids"] == []

    with sqlite3.connect(config.database_dir / "evidence_graph.db") as conn:
        conn.execute("DELETE FROM cognition_episode_projection_effects")
        conn.commit()
    repair = build_reconciliation_plan(config)
    assert repair["repair_revision_ids"]
    assert "repair_missing_target_effects" in repair["actions"]

    repaired = apply_reconciliation(
        config,
        expected_inventory_hash=repair["inventory_hash"],
        backup_dir=tmp_path / "backup-repair",
        daemon_check=lambda _path: True,
    )
    assert repaired["ok"] is True
    assert repaired["event_dispatch_audit"]["ok"] is True
    final = build_reconciliation_plan(config)
    assert final["apply_required"] is False
    assert final["repair_revision_ids"] == []


def test_reconciliation_blocks_duplicate_cognition_episode_terminal_receipts(tmp_path):
    from core.cognitive.cognition_episode_event_schema import INDEX_NAME
    from core.cognitive.cognition_episode_reconciliation import (
        apply_reconciliation,
        build_reconciliation_plan,
    )
    from tests.integration.test_cognition_episode_event_dispatch import (
        _RuntimeConfig,
        _committed_full_episode,
    )

    config = _RuntimeConfig(tmp_path)
    _committed_full_episode(config)
    initial = build_reconciliation_plan(config)
    apply_reconciliation(
        config,
        expected_inventory_hash=initial["inventory_hash"],
        backup_dir=tmp_path / "backup-initial",
        daemon_check=lambda _path: True,
    )
    event_db = config.mnemos_dir / "events.db"
    with sqlite3.connect(event_db) as conn:
        conn.execute(f"DROP INDEX {INDEX_NAME}")
        terminal = conn.execute("""SELECT trace_id, event_type, handler_name, consumer, disposition,
                      reason, mutation_id, page_revision, output_json, created_at
               FROM handler_receipts
               WHERE event_type='cognition_episode_committed'
                 AND disposition IN ('ack','noop')
               ORDER BY id LIMIT 1""").fetchone()
        assert terminal is not None
        conn.execute(
            """INSERT INTO handler_receipts
               (trace_id, event_type, handler_name, consumer, disposition,
                reason, mutation_id, page_revision, output_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            terminal,
        )
        conn.commit()

    blocked = build_reconciliation_plan(config)
    assert "duplicate_terminal_receipts_require_explicit_classification" in blocked["blockers"]
    assert any(
        gap.startswith("duplicate_terminal_receipts:")
        for gap in blocked["cognition_episode_event_schema_gaps"]
    )
    with pytest.raises(RuntimeError, match="duplicate_terminal_receipts"):
        apply_reconciliation(
            config,
            expected_inventory_hash=blocked["inventory_hash"],
            backup_dir=tmp_path / "backup-blocked",
            daemon_check=lambda _path: True,
        )


def test_reconciliation_rebuilds_exact_classified_legacy_direction(tmp_path):
    from core.cognitive.cognition_episode_reconciliation import (
        apply_reconciliation,
        build_reconciliation_plan,
    )
    from tests.integration.test_cognition_episode_event_dispatch import (
        _RuntimeConfig,
        _committed_full_episode,
    )

    config = _RuntimeConfig(tmp_path)
    _committed_full_episode(config)
    initial = build_reconciliation_plan(config)
    apply_reconciliation(
        config,
        expected_inventory_hash=initial["inventory_hash"],
        backup_dir=tmp_path / "backup-initial",
        daemon_check=lambda _path: True,
    )
    evidence_db = config.database_dir / "evidence_graph.db"
    with sqlite3.connect(evidence_db) as conn:
        edge = conn.execute("""SELECT id, source_id, target_id FROM evidence_edges
               WHERE relation_type='observed_in' ORDER BY id LIMIT 1""").fetchone()
        assert edge is not None
        conn.execute(
            "UPDATE evidence_edges SET source_id=?, target_id=? WHERE id=?",
            (edge[2], edge[1], edge[0]),
        )
        conn.commit()

    plan = build_reconciliation_plan(config)
    assert plan["blockers"] == []
    assert "rebuild_legacy_evidence_edge_directions" in plan["actions"]
    assert plan["direction_rebuild_candidates"] == [
        {
            "edge_id": edge[0],
            "source_id": edge[2],
            "target_id": edge[1],
            "relation_type": "observed_in",
            "source_type": "raw_revision_span",
            "target_type": "observation",
            "classification": "reverse",
            "reversed_source_id": edge[1],
            "reversed_target_id": edge[2],
        }
    ]

    repaired = apply_reconciliation(
        config,
        expected_inventory_hash=plan["inventory_hash"],
        backup_dir=tmp_path / "backup-direction",
        daemon_check=lambda _path: True,
    )
    assert repaired["direction_rebuild_receipts"] == [
        {
            "edge_id": edge[0],
            "action": "reversed",
            "source_id": edge[1],
            "target_id": edge[2],
            "relation_type": "observed_in",
        }
    ]
    assert repaired["evidence_direction_audit"]["ok"] is True
    assert build_reconciliation_plan(config)["apply_required"] is False


def test_unclassifiable_direction_candidate_remains_blocked():
    from core.cognitive.cognition_episode_reconciliation import (
        _classify_direction_candidate,
    )

    classified = _classify_direction_candidate(
        {
            "edge_id": 7,
            "source_id": "claim-1",
            "target_id": "raw-1",
            "relation_type": "contains",
            "source_type": "claim",
            "target_type": "raw_revision_span",
        }
    )
    assert classified["classification"] == "unclassified"
    assert classified["reversed_source_id"] == ""
