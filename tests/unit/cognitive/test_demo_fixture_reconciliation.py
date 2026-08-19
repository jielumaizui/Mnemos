"""Exact object-level cleanup for historical deterministic demo leakage."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

import pytest
import docs.demo.run_demo as demo_run

from core.cognitive.demo_fixture_reconcile_contracts import (
    DemoFixtureReconciliationPaths,
)
from core.cognitive.demo_fixture_reconcile_executor import (
    apply_demo_fixture_reconciliation,
)
from core.cognitive.demo_fixture_reconcile_planner import (
    build_demo_fixture_reconciliation_plan,
)
from core.cognitive.state_store import CognitiveStateStore
from core.hephaestus.distillation_engine import DistillationEngine
from core.sync_framework.raw_event_store import RawEventStore
from docs.demo.run_demo import DemoConfig, _run_capture_and_distill


REPO_ROOT = Path(__file__).resolve().parents[3]


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_historical_leak(tmp_path: Path, monkeypatch):
    cfg = DemoConfig(tmp_path)
    monkeypatch.setattr("core.config.get_config", lambda: cfg)

    def fail_after_cognition_commit(_self, _result, _accepted, _config):
        raise RuntimeError("historical synthetic fixture interruption")

    monkeypatch.setattr(
        DistillationEngine,
        "_route_structured_actions",
        fail_after_cognition_commit,
    )
    with pytest.raises(RuntimeError, match="historical synthetic fixture interruption"):
        _run_capture_and_distill(cfg)

    for suffix in ("", "-wal", "-shm"):
        (cfg.database_dir / f"raw_events.db{suffix}").unlink(missing_ok=True)
    raw = RawEventStore(db_path=cfg.database_dir / "raw_events.db", config=cfg)
    raw.close()
    return cfg, DemoFixtureReconciliationPaths(cfg.database_dir, REPO_ROOT)


def test_dry_run_binds_both_demo_objects_without_writes(tmp_path, monkeypatch):
    _cfg, paths = _seed_historical_leak(tmp_path, monkeypatch)
    before = {
        path.name: _file_hash(path)
        for path in (paths.state_path, paths.raw_path, paths.action_path)
    }

    plan = build_demo_fixture_reconciliation_plan(paths)

    assert plan.ok is True, plan.blocked
    assert len(plan.episodes) == 1
    assert len(plan.actions) == 1
    assert len(plan.episodes[0].commands) == 3
    assert before == {
        path.name: _file_hash(path)
        for path in (paths.state_path, paths.raw_path, paths.action_path)
    }


@pytest.mark.parametrize("tampered_field", ("claim_text", "behavior_summary"))
def test_dry_run_rejects_non_fixture_v2_cognition_metadata(
    tmp_path,
    monkeypatch,
    tampered_field,
):
    original = demo_run._make_structured_output

    def build_tampered_output(input_spec):
        payload = original(input_spec)
        if tampered_field == "claim_text":
            payload["claims"][0]["claim_text"] = "Unrelated non-demo cognition claim."
        else:
            payload["user_behavior_intent"]["behavior_summary"] = (
                "Unrelated non-demo behavior intent."
            )
        return payload

    monkeypatch.setattr(demo_run, "_make_structured_output", build_tampered_output)
    _cfg, paths = _seed_historical_leak(tmp_path, monkeypatch)

    plan = build_demo_fixture_reconciliation_plan(paths)

    assert plan.ok is False
    assert plan.episodes == ()
    assert any(
        blocked["reason"] == "cognition episode does not match the tracked demo fixture"
        for blocked in plan.blocked
    )


def test_apply_retires_only_heads_and_closes_exact_effects(tmp_path, monkeypatch):
    _cfg, paths = _seed_historical_leak(tmp_path, monkeypatch)
    plan = build_demo_fixture_reconciliation_plan(paths)
    episode = plan.episodes[0]
    action = plan.actions[0]
    monkeypatch.setattr(
        "core.cognitive.demo_fixture_reconcile_executor.runtime_writers_are_inactive",
        lambda _path: True,
    )

    result = apply_demo_fixture_reconciliation(
        paths,
        expected_inventory_hash=plan.inventory_hash,
        backup_dir=tmp_path / "backups",
    )

    assert result["status"] == "verified", result
    assert result["retired_episode_count"] == 1
    assert result["closed_command_count"] == 3
    assert result["skipped_action_count"] == 1
    assert all(value.get("integrity_check", "ok") == "ok" for value in result["backups"])
    state = CognitiveStateStore(paths.state_path)
    assert state.current_revision("cognition_episode", episode.object_id) is None
    assert state.revision(episode.revision_id) is not None
    assert state.integrity_report()["current_state_hash_mismatch"] == 0
    assert {
        str(state.effect_receipt(command.command_id)["status"])
        for command in episode.commands
    } == {"intentional_skip"}
    with sqlite3.connect(paths.state_path) as conn:
        quarantine_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_state_migration_quarantine "
                "WHERE source_key=?",
                (episode.revision_id,),
            ).fetchone()[0]
        )
        runtime_status = str(
            conn.execute(
                "SELECT status FROM runtime_flow_receipts WHERE production_event_id=?",
                (action.production_event_id,),
            ).fetchone()[0]
        )
    assert quarantine_count == 1
    assert runtime_status == "skipped"
    with sqlite3.connect(paths.action_path) as conn:
        assert int(
            conn.execute(
                "SELECT COUNT(*) FROM action_ledger WHERE action_id=?",
                (action.action_id,),
            ).fetchone()[0]
        ) == 1

    clean = build_demo_fixture_reconciliation_plan(paths)
    second = apply_demo_fixture_reconciliation(
        paths,
        expected_inventory_hash=clean.inventory_hash,
        backup_dir=tmp_path / "unused",
    )
    assert clean.requires_apply is False
    assert second["status"] == "noop"
    assert second["backups"] == []


def test_mid_apply_failure_restores_reviewed_inventory(tmp_path, monkeypatch):
    _cfg, paths = _seed_historical_leak(tmp_path, monkeypatch)
    plan = build_demo_fixture_reconciliation_plan(paths)
    monkeypatch.setattr(
        "core.cognitive.demo_fixture_reconcile_executor.runtime_writers_are_inactive",
        lambda _path: True,
    )

    def failpoint(stage: str) -> None:
        if stage == "episode:0":
            raise RuntimeError("injected failure")

    result = apply_demo_fixture_reconciliation(
        paths,
        expected_inventory_hash=plan.inventory_hash,
        backup_dir=tmp_path / "backups",
        failpoint=failpoint,
    )

    assert result["status"] == "rolled_back", result
    assert result["rollback_verified"] is True
    rolled_back = build_demo_fixture_reconciliation_plan(paths)
    assert rolled_back.inventory_hash == plan.inventory_hash


def test_apply_rejects_an_unreviewed_inventory_hash(tmp_path, monkeypatch):
    _cfg, paths = _seed_historical_leak(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "core.cognitive.demo_fixture_reconcile_executor.runtime_writers_are_inactive",
        lambda _path: True,
    )

    result = apply_demo_fixture_reconciliation(
        paths,
        expected_inventory_hash="sha256:" + "0" * 64,
        backup_dir=tmp_path / "backups",
    )

    assert result["status"] == "blocked"
    assert result["error"] == "inventory_hash_mismatch"
    assert not (tmp_path / "backups").exists()
