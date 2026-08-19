# -*- coding: utf-8 -*-
"""Tests for daemon.kia_services."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from daemon import kia_services


class FakeConfig:
    def __init__(self, tmp_path, values=None):
        self.wiki_dir = tmp_path / "wiki"
        self.database_dir = tmp_path / ".mnemos"
        self.data_dir = tmp_path / "data"
        self.cognitive_graph_enabled = True
        self._values = values or {}

    def get(self, key, default=None):
        return self._values.get(key, default)


def test_cognitive_graph_reconcile_disabled_returns_enabled_false():
    cfg = SimpleNamespace()
    cfg.cognitive_graph_enabled = False

    with patch("core.config.get_config", return_value=cfg):
        result = kia_services.run_cognitive_graph_reconcile(lambda service_name, exc: None)

    assert result == {"enabled": False, "processed": 0, "relations": 0, "errors": 0}


def test_cognitive_graph_reconcile_drains_canonical_belief_projection_commands(tmp_path):
    cfg = FakeConfig(
        tmp_path,
        {"daemon.services.cognitive_graph_reconcile": True},
    )
    cfg.database_dir.mkdir(parents=True)
    (cfg.database_dir / "producer_consumer_ledger.db").touch()
    graph_store = MagicMock()
    updater = MagicMock()
    updater.reconcile.return_value = {
        "outbox": {"processed": 2},
        "stats": {"relations": 7},
    }
    belief_projector = MagicMock()
    belief_projector.process_pending.return_value = {
        "committed": 3,
        "failed": 0,
        "pending": 0,
    }

    with (
        patch("core.config.get_config", return_value=cfg),
        patch("core.cognitive_graph.CognitiveGraphStore", return_value=graph_store),
        patch("core.cognitive_graph.CognitiveGraphUpdater", return_value=updater),
        patch("core.cognitive.state_store.CognitiveStateStore") as state_store,
        patch(
            "core.cognitive.belief_revision.BeliefRevisionProjector",
            return_value=belief_projector,
        ) as projector_type,
    ):
        result = kia_services.run_cognitive_graph_reconcile(
            lambda service_name, exc: None
        )

    assert result["processed"] == 2
    assert result["relations"] == 7
    assert result["belief_projections"] == 3
    assert result["belief_projection_pending"] == 0
    assert result["errors"] == 0
    projector_type.assert_called_once_with(state_store.return_value, graph_store)
    belief_projector.process_pending.assert_called_once_with(limit=1000)


def test_recap_consumption_service_drains_durable_pending_receipts(tmp_path):
    cfg = FakeConfig(
        tmp_path,
        {"daemon.services.recap_consumption": True},
    )
    router = MagicMock()
    router.drain_pending.return_value = {
        "schema_version": "mnemos.recap_consumption_drain.v1",
        "plans_processed": 2,
        "feedback_events_processed": 1,
        "plans": [],
        "feedback_events": [],
        "errors": 0,
    }

    with (
        patch("core.config.get_config", return_value=cfg),
        patch(
            "core.app.retrospective_consumption_router.RetrospectiveConsumptionRouter",
            return_value=router,
        ),
    ):
        result = kia_services.run_recap_consumption(lambda service_name, exc: None)

    assert result["processed"] == 3
    assert result["errors"] == 0
    router.drain_pending.assert_called_once_with(limit=100)


def test_dispute_scan_updates_report_and_logs(tmp_path):
    cfg = FakeConfig(
        tmp_path,
        {
            "dispute_scan.enabled": True,
            "daemon.services.dispute_scan": True,
        },
    )
    resolver = MagicMock()
    resolver.scan.return_value = {
        "disputes_created": 1,
        "auto_resolved": 2,
        "merged": 3,
        "skipped": 4,
    }
    info_calls = []

    with (
        patch("core.config.get_config", return_value=cfg),
        patch("core.app.dispute_resolver.DisputeResolver", return_value=resolver),
    ):
        result = kia_services.run_dispute_scan(
            lambda service_name, exc: None,
            log_info=lambda *args: info_calls.append(args),
        )

    assert result == {
        "disputes_created": 1,
        "auto_resolved": 2,
        "merged": 3,
        "skipped": 4,
        "errors": 0,
    }
    assert len(info_calls) == 1


def test_entropy_scan_skips_unknown_strategy():
    cfg = MagicMock()
    cfg.get.side_effect = (
        lambda key, default=None: True if key == "daemon.services.entropy_scan" else 10
    )
    report = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                page_a="a.md",
                page_b="b.md",
                merge_strategy="manual_review",
                reason="similar",
                recommended_action="review",
            )
        ]
    )
    engine = MagicMock()
    engine.scan.return_value = report
    queue = MagicMock()

    with (
        patch("core.config.get_config", return_value=cfg),
        patch("core.kia.eris.EntropyEngine", return_value=engine),
        patch("core.kia.dialog_reminder.DialogReminderQueue", return_value=queue),
    ):
        result = kia_services.run_entropy_scan(lambda service_name, exc: None)

    assert result == {"candidates": 1, "enqueued": 0, "errors": 0}
    queue.enqueue.assert_not_called()


def test_entropy_scan_uses_module_registry_for_engine():
    cfg = MagicMock()
    cfg.get.side_effect = (
        lambda key, default=None: True if key == "daemon.services.entropy_scan" else 10
    )
    report = SimpleNamespace(candidates=[])
    engine = MagicMock()
    engine.scan.return_value = report
    registry = MagicMock()
    registry.start_module.return_value = {
        "genos": {"state": "running"},
        "eris": {"state": "running"},
    }
    registry.get_instance.return_value = engine

    with (
        patch("core.config.get_config", return_value=cfg),
        patch("core.kia.eris.EntropyEngine", side_effect=AssertionError("direct engine used")),
    ):
        result = kia_services.run_entropy_scan(
            lambda service_name, exc: None,
            module_registry=registry,
        )

    assert result == {"candidates": 0, "enqueued": 0, "errors": 0}
    registry.start_module.assert_called_once_with("eris")
    registry.get_instance.assert_called_once_with("eris")
    engine.scan.assert_called_once_with(sample_size=10)


def test_entropy_scan_skips_disabled_registry_module():
    cfg = MagicMock()
    cfg.get.side_effect = (
        lambda key, default=None: True if key == "daemon.services.entropy_scan" else 10
    )
    registry = MagicMock()
    registry.start_module.return_value = {"eris": {"state": "disabled"}}

    with patch("core.config.get_config", return_value=cfg):
        result = kia_services.run_entropy_scan(
            lambda service_name, exc: None,
            module_registry=registry,
        )

    assert result == {
        "candidates": 0,
        "enqueued": 0,
        "errors": 0,
        "enabled": False,
        "module_state": "disabled",
    }
    registry.start_module.assert_called_once_with("eris")
    registry.get_instance.assert_not_called()
