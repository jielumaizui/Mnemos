from __future__ import annotations

import sqlite3
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_event_dispatch_audit_fails_closed_for_uninitialized_runtime(tmp_path):
    from scripts.audit_cognitive_event_dispatch import build_report

    report = build_report(
        database_dir=tmp_path / "databases",
        event_db_path=tmp_path / "events.db",
        wiki_dir=tmp_path / "wiki",
    )

    assert report["ok"] is False
    assert report["runtime"]["initialized"] is False
    assert report["gaps"]["schema_gap"] > 0


def test_event_dispatch_audit_detects_missing_reciprocal_target_effect(tmp_path):
    from core.cognitive.cognition_episode_dispatch import CognitionEpisodeDispatchOwner
    from core.mnemos_bus import EventBus
    from scripts.audit_cognitive_event_dispatch import build_report
    from tests.integration.test_cognition_episode_event_dispatch import (
        _RuntimeConfig,
        _committed_full_episode,
        _wait_for_terminal_event,
    )

    config = _RuntimeConfig(tmp_path)
    _result, _receipt = _committed_full_episode(config)
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

    closed = build_report(
        database_dir=config.database_dir,
        event_db_path=config.mnemos_dir / "events.db",
        wiki_dir=config.wiki_dir,
    )
    assert closed["ok"] is True
    assert closed["runtime"]["episode_count"] == 1
    assert closed["runtime"]["committed_effect_receipt_count"] == 3

    with sqlite3.connect(config.database_dir / "evidence_graph.db") as conn:
        conn.execute("DELETE FROM cognition_episode_projection_effects")
        conn.commit()

    broken = build_report(
        database_dir=config.database_dir,
        event_db_path=config.mnemos_dir / "events.db",
        wiki_dir=config.wiki_dir,
    )
    assert broken["ok"] is False
    assert broken["gaps"]["target_effect_gap"] == 1


def test_cog030_audits_are_in_every_required_gate_denominator():
    from scripts import run_full_score_gates, run_local_gates

    local_commands = {name: command for name, command in run_local_gates.GATES}
    dispatch_command = [
        "python",
        "scripts/audit_cognitive_event_dispatch.py",
        "--strict",
        "--json",
    ]
    direction_command = [
        "python",
        "scripts/audit_evidence_graph_direction.py",
        "--strict",
        "--json",
    ]
    assert local_commands["cognition episode event dispatch"] == dispatch_command
    assert local_commands["evidence graph direction"] == direction_command

    full_ids = {gate.gate_id for gate in run_full_score_gates.contract_gates()}
    assert "contracts.cognitive_event_dispatch" in full_ids
    assert "contracts.evidence_graph_direction" in full_ids

    precommit = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for command in (
        "scripts/audit_cognitive_event_dispatch.py --strict --json",
        "scripts/audit_evidence_graph_direction.py --strict --json",
    ):
        assert command in precommit
        assert command in ci


def test_only_exact_quarantined_fixture_receipts_count_as_terminal_omission():
    from core.cognitive.cognition_episode_dispatch_audit import (
        _valid_revision_omission,
    )

    revision = type(
        "Revision",
        (),
        {"revision_id": "cogrev-1", "payload_hash": "sha256:payload"},
    )()
    quarantine = {
        "source_key": "cogrev-1",
        "reason_code": "synthetic_fixture_source_not_in_canonical_raw",
        "payload_hash": "sha256:payload",
        "quarantine_id": "quarantine-1",
    }
    receipts = {
        consumer: {
            "status": "intentional_skip",
            "target_effect_id": "retired-demo-fixture:cogrev-1",
            "evidence_refs": '["cognition-revision:cogrev-1",'
            '"cognitive-quarantine:quarantine-1"]',
            "consumption_outcome": "synthetic demo object retired without projection",
            "consumption_metadata": (
                '{"terminal_reason_code":' '"synthetic_fixture_source_not_in_canonical_raw"}'
            ),
        }
        for consumer in ("wiki", "knowledge_graph", "cognitive_graph")
    }

    assert _valid_revision_omission(revision, receipts, quarantine) is True
    receipts["wiki"] = {**receipts["wiki"], "evidence_refs": "[]"}
    assert _valid_revision_omission(revision, receipts, quarantine) is False


def test_event_dispatch_auditor_does_not_reuse_production_projection_oracles():
    import core.cognitive.cognition_episode_dispatch_audit as audit

    source = inspect.getsource(audit)
    assert "from core.cognitive.cognition_episode_dispatch import" not in source
    assert "projection_effect_id" not in source
    assert "sha256_json" not in source


def test_event_dispatch_audit_requires_done_event_and_each_terminal_handler(tmp_path):
    from core.cognitive.cognition_episode_dispatch import CognitionEpisodeDispatchOwner
    from core.mnemos_bus import EventBus
    from scripts.audit_cognitive_event_dispatch import build_report
    from tests.integration.test_cognition_episode_event_dispatch import (
        _RuntimeConfig,
        _committed_full_episode,
        _wait_for_terminal_event,
    )

    config = _RuntimeConfig(tmp_path)
    _committed_full_episode(config)
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

    with sqlite3.connect(config.mnemos_dir / "events.db") as conn:
        conn.execute("UPDATE events SET status='pending' WHERE trace_id=?", (trace_id,))
        conn.execute(
            "DELETE FROM handler_receipts WHERE trace_id=? AND consumer='wiki'",
            (trace_id,),
        )
        conn.commit()
    report = build_report(
        database_dir=config.database_dir,
        event_db_path=config.mnemos_dir / "events.db",
        wiki_dir=config.wiki_dir,
    )
    assert report["ok"] is False
    assert report["gaps"]["event_terminal_gap"] == 1
    assert report["gaps"]["handler_terminal_gap"] == 1


def test_event_dispatch_audit_uses_direct_sql_to_detect_missing_relation(tmp_path):
    from core.cognitive.cognition_episode_dispatch import CognitionEpisodeDispatchOwner
    from core.mnemos_bus import EventBus
    from scripts.audit_cognitive_event_dispatch import build_report
    from tests.integration.test_cognition_episode_event_dispatch import (
        _RuntimeConfig,
        _committed_full_episode,
        _wait_for_terminal_event,
    )

    config = _RuntimeConfig(tmp_path)
    _committed_full_episode(config)
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
    with sqlite3.connect(config.cognitive_graph_db_path) as conn:
        row = conn.execute("""SELECT id FROM cognitive_relations
               WHERE relation_type='based_on' ORDER BY id LIMIT 1""").fetchone()
        assert row is not None
        conn.execute("DELETE FROM cognitive_relations WHERE id=?", (row[0],))
        conn.commit()

    report = build_report(
        database_dir=config.database_dir,
        event_db_path=config.mnemos_dir / "events.db",
        wiki_dir=config.wiki_dir,
    )
    assert report["ok"] is False
    assert report["gaps"]["relation_gap"] == 1
    assert report["gaps"]["target_effect_gap"] == 1


def test_event_dispatch_audit_honors_the_explicit_cognitive_graph_target(tmp_path):
    from core.cognitive.cognition_episode_dispatch import CognitionEpisodeDispatchOwner
    from core.mnemos_bus import EventBus
    from scripts.audit_cognitive_event_dispatch import build_report
    from tests.integration.test_cognition_episode_event_dispatch import (
        _RuntimeConfig,
        _committed_full_episode,
        _wait_for_terminal_event,
    )

    config = _RuntimeConfig(tmp_path)
    config.cognitive_graph_db_path = tmp_path / "configured-targets" / "cognition.sqlite"
    config.cognitive_graph_db_path.parent.mkdir(parents=True)
    _committed_full_episode(config)
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

    explicit = build_report(
        database_dir=config.database_dir,
        event_db_path=config.mnemos_dir / "events.db",
        wiki_dir=config.wiki_dir,
        cognitive_graph_db_path=config.cognitive_graph_db_path,
        wiki_projection_db_path=config.database_dir / "wiki_projection.db",
    )
    assert explicit["ok"] is True

    default_target = build_report(
        database_dir=config.database_dir,
        event_db_path=config.mnemos_dir / "events.db",
        wiki_dir=config.wiki_dir,
    )
    assert default_target["ok"] is False
    assert default_target["gaps"]["schema_gap"] > 0
