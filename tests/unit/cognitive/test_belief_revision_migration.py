from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from core.cognitive.state_schema import initialize_cognitive_state_schema


def _source_fixture(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "belief.md").write_text("# Legacy statement\n\nUnverified prose.\n", encoding="utf-8")

    graph = tmp_path / "cognitive_graph.db"
    with sqlite3.connect(graph) as conn:
        conn.execute(
            "CREATE TABLE cognitive_relations "
            "(id TEXT PRIMARY KEY, source TEXT, target TEXT, relation_type TEXT)"
        )
        conn.execute(
            "INSERT INTO cognitive_relations VALUES (?, ?, ?, ?)",
            ("relation-1", "wiki://belief.md", "kg://backup", "related_to"),
        )

    reflection = tmp_path / "reflection.db"
    with sqlite3.connect(reflection) as conn:
        conn.execute("CREATE TABLE reflection_records (record_id TEXT PRIMARY KEY, body TEXT)")
        conn.execute(
            "INSERT INTO reflection_records VALUES (?, ?)",
            ("reflection-1", "Legacy reflection body"),
        )

    profile = tmp_path / "profile.db"
    with sqlite3.connect(profile) as conn:
        conn.execute("CREATE TABLE profile_assertions (assertion_id TEXT PRIMARY KEY, body TEXT)")
        conn.execute(
            "INSERT INTO profile_assertions VALUES (?, ?)",
            ("assertion-1", "Legacy profile assertion"),
        )
    return wiki, graph, reflection, profile


def _reconciler(tmp_path):
    from core.cognitive.belief_migration import BeliefCandidateReconciler

    wiki, graph, reflection, profile = _source_fixture(tmp_path)
    state = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state)
    return (
        BeliefCandidateReconciler(
            state_db=state,
            wiki_roots=(wiki,),
            cognitive_graph_dbs=(graph,),
            reflection_dbs=(reflection,),
            profile_dbs=(profile,),
        ),
        state,
        reflection,
    )


def test_inventory_binds_exact_sources_without_inventing_belief_semantics(tmp_path):
    reconciler, _, _ = _reconciler(tmp_path)

    candidates = reconciler.inventory()

    assert {candidate.domain for candidate in candidates} == {
        "wiki",
        "cognitive_graph",
        "reflection",
        "profile_assertion",
    }
    assert len(candidates) == 4
    for candidate in candidates:
        assert candidate.source_identifier
        assert candidate.source_content_hash.startswith("sha256:")
        assert candidate.payload["classification"] == "unverified_candidate"
        assert candidate.payload["active_schema_upgrade"] is False
        assert "body" not in candidate.payload
        assert not {
            "belief_id",
            "claim_id",
            "stance",
            "confidence",
            "supersedes_revision_id",
        }.intersection(candidate.payload)


def test_dry_run_is_read_only_and_apply_requires_stop_and_backup(tmp_path):
    reconciler, state, _ = _reconciler(tmp_path)

    report = reconciler.reconcile()

    assert report["mode"] == "dry_run"
    assert report["candidate_count"] == 4
    assert report["inserted_count"] == 0
    with sqlite3.connect(state) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM cognitive_state_migration_quarantine").fetchone()[0]
            == 0
        )
    with pytest.raises(ValueError, match="stopped daemon"):
        reconciler.reconcile(apply=True, backup_dir=tmp_path / "backups")
    with pytest.raises(ValueError, match="backup directory"):
        reconciler.reconcile(apply=True, confirm_daemon_stopped=True)


def test_apply_only_appends_unverified_quarantine_and_replay_is_idempotent(tmp_path):
    reconciler, state, _ = _reconciler(tmp_path)
    backups = tmp_path / "backups"
    inventory_hash = reconciler.reconcile()["inventory_hash"]

    first = reconciler.reconcile(
        apply=True,
        backup_dir=backups,
        confirm_daemon_stopped=True,
        expected_inventory_hash=inventory_hash,
    )
    replay = reconciler.reconcile(
        apply=True,
        backup_dir=backups,
        confirm_daemon_stopped=True,
        expected_inventory_hash=inventory_hash,
    )

    assert first["mode"] == "apply"
    assert first["inserted_count"] == 4
    assert first["existing_count"] == 0
    assert first["backup"]["integrity_check"] == "ok"
    assert replay["inserted_count"] == 0
    assert replay["existing_count"] == 4
    assert first["active_head_delta"] == 0
    assert first["active_revision_delta"] == 0
    assert first["state_integrity_check"] == "ok"
    assert len(tuple(backups.glob("*.db"))) == 2
    with sqlite3.connect(state) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM cognitive_state_migration_quarantine").fetchone()[0]
            == 4
        )
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_heads").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0] == 0
        rows = conn.execute(
            "SELECT reason_code, payload_json FROM cognitive_state_migration_quarantine"
        ).fetchall()
    assert all(reason == "unverified_belief_candidate" for reason, _ in rows)
    assert all('"classification":"unverified_candidate"' in payload for _, payload in rows)


def test_changed_legacy_row_conflicts_instead_of_overwriting_quarantine(tmp_path):
    reconciler, state, reflection = _reconciler(tmp_path)
    inventory_hash = reconciler.reconcile()["inventory_hash"]
    reconciler.reconcile(
        apply=True,
        backup_dir=tmp_path / "backups",
        confirm_daemon_stopped=True,
        expected_inventory_hash=inventory_hash,
    )
    with sqlite3.connect(reflection) as conn:
        conn.execute(
            "UPDATE reflection_records SET body=? WHERE record_id=?",
            ("Changed legacy body", "reflection-1"),
        )

    with pytest.raises(RuntimeError, match="immutable quarantine conflict"):
        changed_inventory_hash = reconciler.reconcile()["inventory_hash"]
        reconciler.reconcile(
            apply=True,
            backup_dir=tmp_path / "backups",
            confirm_daemon_stopped=True,
            expected_inventory_hash=changed_inventory_hash,
        )

    with sqlite3.connect(state) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM cognitive_state_migration_quarantine").fetchone()[0]
            == 4
        )


def test_apply_rejects_source_drift_from_reviewed_inventory_before_backup_or_write(tmp_path):
    reconciler, state, reflection = _reconciler(tmp_path)
    reviewed_hash = reconciler.reconcile()["inventory_hash"]
    with sqlite3.connect(reflection) as conn:
        conn.execute(
            "UPDATE reflection_records SET body=? WHERE record_id=?",
            ("Changed after review", "reflection-1"),
        )

    with pytest.raises(ValueError, match="inventory changed since dry-run"):
        reconciler.reconcile(
            apply=True,
            backup_dir=tmp_path / "backups",
            confirm_daemon_stopped=True,
            expected_inventory_hash=reviewed_hash,
        )

    assert not (tmp_path / "backups").exists()
    with sqlite3.connect(state) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM cognitive_state_migration_quarantine").fetchone()[0]
            == 0
        )


def test_reconcile_cli_requires_and_binds_reviewed_inventory_hash(tmp_path, monkeypatch, capsys):
    from scripts import reconcile_belief_revision_candidates as cli

    wiki, graph, reflection, profile = _source_fixture(tmp_path)
    state = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state)
    monkeypatch.setattr(
        cli,
        "get_config",
        lambda: SimpleNamespace(
            database_dir=tmp_path,
            wiki_dir=wiki,
            cognitive_graph_db_path=graph,
        ),
    )
    monkeypatch.setattr(cli, "runtime_writers_are_inactive", lambda _path: True)
    common = [
        "--state-db",
        str(state),
        "--wiki-root",
        str(wiki),
        "--cognitive-graph-db",
        str(graph),
        "--reflection-db",
        str(reflection),
        "--profile-db",
        str(profile),
        "--json",
    ]

    assert cli.main(common) == 0
    inventory_hash = json.loads(capsys.readouterr().out)["inventory_hash"]
    assert cli.main([*common, "--apply", "--backup-dir", str(tmp_path / "backup")]) == 2
    assert "--expected-inventory-hash" in json.loads(capsys.readouterr().out)["error"]

    assert (
        cli.main(
            [
                *common,
                "--apply",
                "--backup-dir",
                str(tmp_path / "backup"),
                "--expected-inventory-hash",
                inventory_hash,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["inserted_count"] == 4
