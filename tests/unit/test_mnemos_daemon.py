# -*- coding: utf-8 -*-
"""Unit tests for mnemos_daemon core utilities."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import mnemos_daemon as daemon
from core.ops.durable_io import DurableIOError
from daemon.raw_projection_state import (
    load_raw_projection_state,
    write_raw_projection_state,
)


@pytest.fixture
def pid_file(tmp_path):
    return tmp_path / "daemon.pid"


class TestPidLock:
    def test_acquire_and_release_pid_lock(self, pid_file):
        with patch.object(daemon, "PID_FILE", pid_file):
            assert daemon._acquire_pid_lock() is True
            assert pid_file.exists()
            daemon._release_pid_lock()
            assert not pid_file.exists()

    def test_load_daemon_config_propagates_programming_errors(self, monkeypatch):
        monkeypatch.setattr(
            "core.config.get_config",
            lambda: (_ for _ in ()).throw(AssertionError("config contract bug")),
        )

        with pytest.raises(AssertionError, match="config contract bug"):
            daemon._load_daemon_config()

    def test_acquire_pid_lock_already_held(self, pid_file):
        with patch.object(daemon, "PID_FILE", pid_file):
            fd1 = daemon._acquire_pid_lock()
            assert fd1 is True
            # A second non-blocking lock on the same file should fail
            fd2 = daemon._acquire_pid_lock()
            assert fd2 is False
            daemon._release_pid_lock()

    def test_startup_status_timeout_covers_real_daemon_initialization(self):
        assert daemon.STARTUP_STATUS_TIMEOUT_SECONDS >= 30.0


def test_persona_analyzer_uses_canonical_previous_and_skips_unchanged(monkeypatch):
    previous = MagicMock(version=7)
    signal_store = MagicMock()
    persona_store = MagicMock()
    persona_store.load_persona.return_value = (previous, None)
    analyzer = MagicMock()
    analyzer.analyze.return_value = previous
    config = MagicMock()
    config.get.return_value = True

    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr("core.persona.psyche.get_signal_store", lambda: signal_store)
    monkeypatch.setattr("core.persona.pythia.PreferenceAnalyzer", lambda _store: analyzer)
    monkeypatch.setattr("core.persona.delphi.PersonaStore", lambda **_kwargs: persona_store)

    result = daemon.service_persona_analyzer()

    analyzer.analyze.assert_called_once_with(days=30, previous_profile=previous, incremental=True)
    signal_store.replay_profile_usage_outbox.assert_called_once_with()
    persona_store.save_persona.assert_not_called()
    assert result == {
        "analyzed": False,
        "unchanged": True,
        "version": 7,
        "profile_usage_replayed": 0,
    }


def test_persona_analyzer_rejects_nonmaterial_increment(monkeypatch):
    previous = MagicMock(version=7)
    candidate = MagicMock(version=8)
    candidate.source_signal_ids = {}
    signal_store = MagicMock()
    persona_store = MagicMock()
    persona_store.load_persona.return_value = (previous, None)
    analyzer = MagicMock()
    analyzer.analyze.return_value = candidate
    analyzer.is_material_change.return_value = False
    config = MagicMock()
    config.get.return_value = True

    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr("core.persona.psyche.get_signal_store", lambda: signal_store)
    monkeypatch.setattr("core.persona.pythia.PreferenceAnalyzer", lambda _store: analyzer)
    monkeypatch.setattr("core.persona.delphi.PersonaStore", lambda **_kwargs: persona_store)

    result = daemon.service_persona_analyzer()

    analyzer.is_material_change.assert_called_once_with(previous, candidate)
    signal_store.replay_profile_usage_outbox.assert_called_once_with()
    persona_store.save_persona.assert_not_called()
    assert result == {
        "analyzed": False,
        "unchanged": True,
        "version": 8,
        "profile_usage_replayed": 0,
    }


def test_raw_projection_state_is_atomic_and_never_follows_symlinks(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "raw_projection_state.json"
    write_raw_projection_state(state_path, {"generation": 1})
    assert load_raw_projection_state(state_path) == {"generation": 1}

    state_path.unlink()
    sentinel = tmp_path / "foreign-state.json"
    sentinel.write_text('{"sentinel":true}', encoding="utf-8")
    state_path.symlink_to(sentinel)

    assert load_raw_projection_state(state_path) == {}
    with pytest.raises(DurableIOError, match="durable_target_unsafe"):
        write_raw_projection_state(state_path, {"generation": 2})
    assert sentinel.read_text(encoding="utf-8") == '{"sentinel":true}'


def test_persona_application_is_the_only_runtime_writer_for_material_candidate():
    from core.application.persona import PersonaApplicationService

    previous = MagicMock(version=7)
    candidate = MagicMock(version=8)
    candidate.source_signal_ids = {}
    signal_store = MagicMock()
    persona_store = MagicMock()
    persona_store.load_persona.return_value = (previous, None)
    analyzer = MagicMock()
    analyzer.analyze.return_value = candidate
    analyzer.is_material_change.return_value = True

    result = PersonaApplicationService().run_canonical_revision_cycle(
        signal_store=signal_store,
        days=30,
        analyzer=analyzer,
        persona_store=persona_store,
    )

    analyzer.analyze.assert_called_once_with(
        days=30,
        previous_profile=previous,
        incremental=True,
    )
    analyzer.is_material_change.assert_called_once_with(previous, candidate)
    persona_store.save_persona.assert_called_once_with(candidate)
    assert result == {"analyzed": True, "version": 8}


def test_persona_application_binds_material_candidate_signal_cursor_to_writer():
    from core.application.persona import PersonaApplicationService

    previous = MagicMock(version=7)
    candidate = MagicMock(version=8)
    candidate.source_signal_ids = {"knowledge": [7, 3, 7], "session": [2]}
    signal_store = MagicMock()
    persona_store = MagicMock()
    persona_store.load_persona.return_value = (previous, None)
    analyzer = MagicMock()
    analyzer.analyze.return_value = candidate
    analyzer.is_material_change.return_value = True

    result = PersonaApplicationService().run_canonical_revision_cycle(
        signal_store=signal_store,
        analyzer=analyzer,
        persona_store=persona_store,
    )

    persona_store.save_persona.assert_called_once_with(
        candidate,
        consume_signal_ids={"knowledge": [3, 7], "session": [2]},
    )
    assert result == {"analyzed": True, "version": 8}


def test_persona_application_rejects_unbound_signal_cursor_source():
    from core.application.persona import PersonaApplicationService

    previous = MagicMock(version=7)
    candidate = MagicMock(version=8)
    candidate.source_signal_ids = {"unregistered": [1]}
    signal_store = MagicMock()
    persona_store = MagicMock()
    persona_store.load_persona.return_value = (previous, None)
    analyzer = MagicMock()
    analyzer.analyze.return_value = candidate
    analyzer.is_material_change.return_value = True

    with pytest.raises(ValueError, match="unsupported Persona signal cursor source"):
        PersonaApplicationService().run_canonical_revision_cycle(
            signal_store=signal_store,
            analyzer=analyzer,
            persona_store=persona_store,
        )

    persona_store.save_persona.assert_not_called()


def test_persona_challenge_empty_queue_is_typed_noop(monkeypatch):
    class _EmptyConsumer:
        def __init__(self, _config):
            pass

        def run_once(self):
            return {
                "challenges": 0,
                "consumed": 0,
                "status": "noop",
                "reason": "no_pending_decision_command",
            }

    monkeypatch.setattr(
        "core.persona.challenge_queue.PersonaChallengeQueueConsumer",
        _EmptyConsumer,
    )

    result = daemon._run_persona_challenge()

    assert result == {
        "challenges": 0,
        "consumed": 0,
        "status": "noop",
        "reason": "no_pending_decision_command",
    }


class TestDaemonInstanceDelegation:
    def test_cmd_start_honors_instance_control_decision(self, monkeypatch, capsys):
        monkeypatch.setattr(
            daemon._instance_control,
            "prepare_start",
            lambda *_args, **_kwargs: daemon._instance_control.CommandResult(
                1, ("identity drift",), proceed=False
            ),
        )

        assert daemon.cmd_start() == 1
        assert "identity drift" in capsys.readouterr().out

    def test_cmd_stop_delegates_to_instance_control(self, monkeypatch, capsys):
        monkeypatch.setattr(
            daemon._instance_control,
            "stop",
            lambda *_args, **_kwargs: daemon._instance_control.CommandResult(
                0, ("stopped safely",)
            ),
        )

        assert daemon.cmd_stop() == 0
        assert "stopped safely" in capsys.readouterr().out

    def test_cmd_status_prints_models_only_for_verified_instance(self, monkeypatch, capsys):
        monkeypatch.setattr(
            daemon._instance_control,
            "status",
            lambda *_args, **_kwargs: daemon._instance_control.CommandResult(
                0, ("verified",), show_model_status=True, daemon_pid=42
            ),
        )
        monkeypatch.setattr(daemon, "_print_model_status", lambda _pid: "models ok")

        assert daemon.cmd_status() == 0
        output = capsys.readouterr().out
        assert "verified" in output
        assert "models ok" in output

    def test_controlled_profile_binds_all_control_commands_to_same_manifest(
        self, monkeypatch, capsys
    ):
        observed = {}

        def prepare_start(*_args, **kwargs):
            observed["start"] = kwargs["service_names"]
            return daemon._instance_control.CommandResult(1, ("profile inspected",), proceed=False)

        def stop(*_args, **kwargs):
            observed["stop"] = kwargs["service_names"]
            return daemon._instance_control.CommandResult(0, ("stopped safely",))

        def status(*_args, **kwargs):
            observed["status"] = kwargs["service_names"]
            return daemon._instance_control.CommandResult(0, ("verified",))

        monkeypatch.setattr(daemon._instance_control, "prepare_start", prepare_start)
        monkeypatch.setattr(daemon._instance_control, "stop", stop)
        monkeypatch.setattr(daemon._instance_control, "status", status)

        assert daemon.cmd_start(controlled_raw_sync_only=True) == 1
        assert daemon.cmd_stop(controlled_raw_sync_only=True) == 0
        assert daemon.cmd_status(controlled_raw_sync_only=True) == 0
        assert capsys.readouterr().out
        assert observed == {
            "start": ("heartbeat", "raw_sync"),
            "stop": ("heartbeat", "raw_sync"),
            "status": ("heartbeat", "raw_sync"),
        }

    def test_main_routes_controlled_profile_flag_to_runtime_command(self, monkeypatch):
        observed = {}
        monkeypatch.setattr(daemon, "_configure_runtime_paths", lambda: None)

        def start(*, controlled_raw_sync_only=False):
            observed["controlled"] = controlled_raw_sync_only
            return 0

        monkeypatch.setattr(
            daemon,
            "cmd_start",
            start,
        )

        assert daemon.main(["--controlled-raw-sync-only", "start"]) == 0
        assert observed == {"controlled": True}


class TestIntervals:
    def test_intervals_contain_core_services(self):
        assert "heartbeat" in daemon.INTERVALS
        assert "capture_worker" in daemon.INTERVALS
        assert "raw_sync" in daemon.INTERVALS
        assert "raw_projection" in daemon.INTERVALS
        assert "l1_sync" not in daemon.INTERVALS
        assert "retry_failed" in daemon.INTERVALS
        assert "persona_challenge" in daemon.INTERVALS
        assert "persona_extensions" not in daemon.INTERVALS
        assert "distill_and_merge" in daemon.INTERVALS
        assert "distill_cognitive_actions" in daemon.INTERVALS
        assert "operational_incidents" in daemon.INTERVALS
        assert "wiki_route" in daemon.INTERVALS

    def test_legacy_l1_sync_resolves_to_raw_sync_service(self):
        assert daemon._resolve_service_call(None, "l1_sync") is daemon.service_raw_sync
        assert daemon.service_l1_sync is daemon.service_raw_sync

    def test_controlled_raw_sync_profile_has_only_audited_services(self):
        assert daemon._service_names_for_profile(controlled_raw_sync_only=True) == (
            "heartbeat",
            "raw_sync",
        )

    def test_controlled_raw_sync_profile_overrides_persisted_service_toggles(self):
        class DisabledConfig:
            def get(self, _key, _default=None):
                return False

        daemon._activate_daemon_profile(controlled_raw_sync_only=True)
        try:
            assert daemon._service_enabled(DisabledConfig(), "heartbeat") is True
            assert daemon._service_enabled(DisabledConfig(), "raw_sync") is True
            assert daemon._service_enabled(DisabledConfig(), "capture_worker") is False
        finally:
            daemon._reset_daemon_profile()

    def test_apply_interval_overrides_uses_distill_tick_interval(self):
        original = dict(daemon.INTERVALS)

        class Cfg:
            def get(self, key, default=None):
                values = {
                    "capture.tick_interval_seconds": 5,
                    "distill.tick_interval_seconds": 60,
                    "distill.cognitive_action_worker_interval_seconds": 15,
                    "distill.operational_incident_worker_interval_seconds": 20,
                    "wiki_route.interval_seconds": 120,
                }
                return values.get(key, default)

        try:
            daemon._apply_interval_overrides(Cfg())

            assert daemon.INTERVALS["capture_worker"] == 5
            assert daemon.INTERVALS["distill_and_merge"] == 60
            assert daemon.INTERVALS["distill_cognitive_actions"] == 15
            assert daemon.INTERVALS["operational_incidents"] == 20
            assert daemon.INTERVALS["wiki_route"] == 120
        finally:
            daemon.INTERVALS.clear()
            daemon.INTERVALS.update(original)


class TestEventBusStartup:
    def test_wiki_lifecycle_registers_every_required_projection_consumer(self):
        subscriptions = []

        class Bus:
            def subscribe(self, event_type, handler, *, consumer_id):
                subscriptions.append((event_type, handler, consumer_id))

        daemon._register_kg_event_handlers(Bus())

        wiki_handlers = [
            handler
            for event_type, handler, _consumer_id in subscriptions
            if event_type == "wiki_page_updated"
        ]
        assert len(wiki_handlers) == 5
        assert {handler.__name__ for handler in wiki_handlers} == {
            "_kg_page_updated_handler",
            "_relation_embeddings_handler",
            "_moc_navigation_handler",
            "_wiki_search_index_handler",
            "_metrics_page_updated_handler",
        }
        assert {
            consumer_id
            for event_type, _handler, consumer_id in subscriptions
            if event_type == "wiki_page_updated"
        } == {
            "knowledge_graph",
            "relation_embeddings",
            "moc_navigation",
            "wiki_search_index",
            "wiki_metrics",
        }

    def test_wiki_lifecycle_binds_every_consumer_to_passed_config(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        import daemon.wiki_projection_handlers as handlers_module
        from core.mnemos_bus import Event
        from core.wiki_projection_lifecycle import WikiProjectionLedger

        subscriptions = {}
        constructed = {"kg": [], "metrics": [], "search": [], "relation": []}

        class Bus:
            def subscribe(self, event_type, handler, *, consumer_id):
                subscriptions[(event_type, consumer_id)] = handler

        class FakeKGEventHandler:
            def __init__(self, **kwargs):
                constructed["kg"].append(kwargs)

            def on_distilled(self, _payload):
                return {"status": "ok"}

            def on_page_updated(self, _payload):
                return {"status": "ok"}

        class FakeWikiMetrics:
            def __init__(self, **kwargs):
                constructed["metrics"].append(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def reconcile_page_lifecycle(self, **_kwargs):
                return {"status": "ok"}

        class FakeEmbeddingIndexManager:
            def __init__(self, **kwargs):
                constructed["search"].append(kwargs)

            def build_index(self, force_full=False):
                assert force_full is False
                return {"status": "ok"}

            def audit_coverage(self):
                return {"ok": True}

        class FakeKnowledgeGraph:
            def __init__(self, **kwargs):
                constructed["relation"].append(kwargs)

            def repair_relation_embedding_orphans(self):
                return {"failed": 0}

            def audit_relation_embedding_projection(self):
                return {"ok": True}

            def close(self):
                return None

        wiki_dir = tmp_path / "custom-wiki"
        database_dir = tmp_path / "custom-db"
        config = SimpleNamespace(wiki_dir=wiki_dir, database_dir=database_dir)
        monkeypatch.setattr(handlers_module, "KGEventHandler", FakeKGEventHandler)
        monkeypatch.setattr("core.wiki_metrics.WikiMetrics", FakeWikiMetrics)
        monkeypatch.setattr("core.embeddings.EmbeddingIndexManager", FakeEmbeddingIndexManager)
        monkeypatch.setattr("core.kia.knowledge_graph.KnowledgeGraph", FakeKnowledgeGraph)
        handlers_module.register_wiki_projection_handlers(Bus(), config)
        page = wiki_dir / "page.md"
        page.parent.mkdir(parents=True)
        page.write_text("# Page\n", encoding="utf-8")
        mutation = WikiProjectionLedger(database_dir / "wiki_projection.db").record_mutation(
            page, mutation_type="create"
        )
        event = Event(
            "wiki_page_updated",
            "test",
            {
                "page_path": mutation.page_path,
                "previous_path": mutation.previous_path,
                "page_id": mutation.page_id,
                "page_revision": mutation.page_revision,
                "mutation_id": mutation.mutation_id,
                "mutation_type": mutation.mutation_type,
                "tombstone": mutation.tombstone,
            },
            trace_id=mutation.mutation_id,
        )
        subscriptions[("wiki_page_updated", "wiki_metrics")](event)
        subscriptions[("wiki_page_updated", "wiki_search_index")](event)
        subscriptions[("wiki_page_updated", "relation_embeddings")](event)

        assert constructed["kg"] == [
            {
                "db_path": database_dir / "knowledge_graph.db",
                "wiki_base": wiki_dir,
                "embedding_index_dir": database_dir / "embedding_index",
                "embedding_client": None,
                "config": config,
                "projection_lifecycle": None,
            }
        ]
        assert constructed["metrics"] == [
            {
                "db_path": str(database_dir / "wiki_metrics.db"),
                "wiki_dir": str(wiki_dir),
            }
        ]
        assert constructed["search"] == [
            {
                "wiki_base": wiki_dir,
                "index_dir": database_dir / "embedding_index",
                "client": None,
                "config": config,
            }
        ]
        assert constructed["relation"] == [
            {
                "db_path": str(database_dir / "knowledge_graph.db"),
                "wiki_base": str(wiki_dir),
                "embedding_index_dir": str(database_dir / "embedding_index"),
                "embedding_client": None,
                "config": config,
            }
        ]

    def test_initialize_event_bus_can_defer_dispatch(self, monkeypatch):
        import core.mnemos_bus as bus_mod

        fake_bus = MagicMock()
        get_event_bus = MagicMock(return_value=fake_bus)
        monkeypatch.setattr(bus_mod, "get_event_bus", get_event_bus)
        monkeypatch.setattr(daemon, "_register_kg_event_handlers", lambda _bus: None)
        monkeypatch.setattr(daemon, "_register_kia_event_handlers", lambda _bus: None)
        monkeypatch.setattr(daemon, "_register_telemetry_handlers", lambda _bus: None)
        monkeypatch.setattr(daemon, "_register_cognitive_graph", lambda _bus: None)
        register_episode_dispatch = MagicMock()
        monkeypatch.setattr(
            daemon,
            "_register_cognition_episode_dispatch",
            register_episode_dispatch,
        )
        monkeypatch.setattr(daemon, "_register_session_event_handlers", lambda _bus: None)
        monkeypatch.setattr(daemon, "_replay_dead_letters", lambda _bus, _cfg: None)
        write_status = MagicMock()
        monkeypatch.setattr(daemon, "_write_startup_status", write_status)

        config = MagicMock()
        assert daemon._initialize_event_bus(config, start_dispatch=False) is fake_bus

        get_event_bus.assert_called_once_with(config=config)
        fake_bus.start_dispatch.assert_not_called()
        register_episode_dispatch.assert_called_once_with(fake_bus, config)
        write_status.assert_not_called()

        daemon._start_event_bus_dispatch(fake_bus)

        fake_bus.start_dispatch.assert_called_once_with()
        write_status.assert_not_called()

    def test_run_daemon_subscribes_kia_modules_before_eventbus_dispatch(self, monkeypatch):
        calls = []
        fake_bus = MagicMock()
        fake_executor = MagicMock()

        monkeypatch.setattr(daemon, "_configure_runtime_paths", lambda _cfg: None)
        monkeypatch.setattr(daemon, "_setup_logging", lambda: None)
        monkeypatch.setattr(daemon, "_start_file_guardian", lambda: None)
        monkeypatch.setattr(daemon, "_ensure_vault_directories", lambda: None)
        monkeypatch.setattr(daemon, "_bootstrap_runtime_schema", lambda: None)
        monkeypatch.setattr(daemon, "_load_daemon_config", lambda: MagicMock())
        monkeypatch.setattr(daemon, "_acquire_pid_lock", lambda _cfg: True)
        monkeypatch.setattr(daemon, "_release_pid_lock", lambda: None)
        monkeypatch.setattr(daemon, "_run_startup_compensation", lambda: None)
        monkeypatch.setattr(daemon, "_run_startup_cleanup", lambda: None)
        monkeypatch.setattr(daemon, "_register_wiki_auto_commit", lambda _ctx, _cfg: None)
        monkeypatch.setattr(daemon, "_apply_interval_overrides", lambda _cfg: None)
        monkeypatch.setattr(daemon, "_register_trigger_dispatcher", lambda _ctx, _cfg: None)
        monkeypatch.setattr(daemon, "service_heartbeat", lambda: calls.append(("heartbeat", None)))
        monkeypatch.setattr(
            daemon,
            "_write_startup_status",
            lambda success, error="": calls.append(("startup_status", success)),
        )
        monkeypatch.setattr(daemon, "_build_service_executor", lambda _cfg: fake_executor)
        monkeypatch.setattr(daemon, "_run_daemon_main_loop", lambda _cfg, _executor: None)

        def initialize(_cfg, *, start_dispatch=True):
            calls.append(("initialize", start_dispatch))
            return fake_bus

        def register_kia(_ctx, _cfg):
            calls.append(("kia_modules", None))

        def start_dispatch(bus):
            assert bus is fake_bus
            calls.append(("dispatch", None))

        monkeypatch.setattr(daemon, "_initialize_event_bus", initialize)
        monkeypatch.setattr(daemon, "_register_kia_modules", register_kia)
        monkeypatch.setattr(daemon, "_start_event_bus_dispatch", start_dispatch)

        daemon.run_daemon(foreground=True)

        assert calls == [
            ("initialize", False),
            ("kia_modules", None),
            ("dispatch", None),
            ("heartbeat", None),
            ("startup_status", True),
        ]
        fake_executor.shutdown.assert_called_once_with(wait=False)

    def test_controlled_raw_sync_profile_skips_unrelated_runtime_initialization(self, monkeypatch):
        calls = []
        fake_executor = MagicMock()

        monkeypatch.setattr(daemon, "_configure_runtime_paths", lambda _cfg: None)
        monkeypatch.setattr(daemon, "_setup_logging", lambda: None)
        monkeypatch.setattr(daemon, "_load_daemon_config", lambda: MagicMock())
        monkeypatch.setattr(daemon, "_acquire_pid_lock", lambda _cfg: True)
        monkeypatch.setattr(daemon, "_release_pid_lock", lambda: None)
        monkeypatch.setattr(daemon, "_apply_interval_overrides", lambda _cfg: None)
        monkeypatch.setattr(
            daemon,
            "service_heartbeat",
            lambda: calls.append(("heartbeat", None)),
        )
        monkeypatch.setattr(
            daemon,
            "_write_startup_status",
            lambda success, error="": calls.append(("startup_status", success)),
        )
        monkeypatch.setattr(daemon, "_build_service_executor", lambda _cfg: fake_executor)

        def run_loop(_cfg, _executor, *, service_names=None):
            calls.append(("run_loop", service_names))

        monkeypatch.setattr(daemon, "_run_daemon_main_loop", run_loop)
        for name in (
            "_start_file_guardian",
            "_ensure_vault_directories",
            "_bootstrap_runtime_schema",
            "_bootstrap_runtime_flow_ledger",
            "_run_startup_compensation",
            "_run_startup_cleanup",
            "_initialize_event_bus",
            "_register_wiki_auto_commit",
            "_register_kia_modules",
            "_start_event_bus_dispatch",
            "_register_trigger_dispatcher",
        ):
            monkeypatch.setattr(
                daemon,
                name,
                MagicMock(side_effect=AssertionError(f"{name} must not run")),
            )

        daemon.run_daemon(foreground=True, controlled_raw_sync_only=True)

        assert calls == [
            ("heartbeat", None),
            ("startup_status", True),
            ("run_loop", ("heartbeat", "raw_sync")),
        ]
        fake_executor.shutdown.assert_called_once_with(wait=False)
        assert daemon._daemon_run_profile == daemon._PRODUCTION_RUN_PROFILE
        assert daemon._daemon_service_names is None


class TestWikiAutoCommitLifecycle:
    def test_register_wiki_auto_commit_closes_handler_via_runtime_context(self, monkeypatch):
        handler = MagicMock()

        class Cfg:
            def get(self, key, default=None):
                if key == "daemon.services.wiki_auto_commit":
                    return True
                return default

        monkeypatch.setattr(daemon, "_wiki_auto_commit_handler", None, raising=False)
        monkeypatch.setattr("scripts.auto_commit_wiki.start_auto_commit", lambda: handler)
        monkeypatch.setattr(daemon, "_release_pid_lock", lambda: None)

        ctx = daemon._runtime.RuntimeContext()
        daemon._register_wiki_auto_commit(ctx, Cfg())

        assert daemon._wiki_auto_commit_handler is handler

        daemon._shutdown_daemon(ctx)

        handler.stop.assert_called_once_with()
        assert daemon._wiki_auto_commit_handler is None


class TestTriggerDispatcherLifecycle:
    def test_register_trigger_dispatcher_closes_dispatcher_via_runtime_context(self, monkeypatch):
        dispatcher = MagicMock()

        def fake_start(_cfg):
            daemon._trigger_dispatcher = dispatcher

        monkeypatch.setattr(daemon, "_trigger_dispatcher", None, raising=False)
        monkeypatch.setattr(daemon, "_start_trigger_dispatcher", fake_start)
        monkeypatch.setattr(daemon, "_release_pid_lock", lambda: None)

        ctx = daemon._runtime.RuntimeContext()
        daemon._register_trigger_dispatcher(ctx, object())

        assert daemon._trigger_dispatcher is dispatcher

        daemon._shutdown_daemon(ctx)

        dispatcher.stop_all.assert_called_once_with()
        assert daemon._trigger_dispatcher is None


class TestRawProjectionService:
    def test_recovery_intent_cleanup_revalidates_full_scope(
        self,
        tmp_path: Path,
    ) -> None:
        from daemon.raw_projection_service import (
            _clear_recovery_intent,
            _write_recovery_intent,
        )

        raw_dir = tmp_path / "raw"
        backup_dir = tmp_path / "backups"
        intent_path = tmp_path / "raw_projection_recovery_intent.json"
        plan_hash = "a" * 64
        _write_recovery_intent(
            intent_path,
            plan_hash=plan_hash,
            raw_dir=raw_dir,
            backup_dir=backup_dir,
        )
        payload = json.loads(intent_path.read_text(encoding="utf-8"))
        payload["backup_dir"] = str((tmp_path / "other-backups").resolve())
        intent_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(RuntimeError, match="backup scope does not match"):
            _clear_recovery_intent(
                intent_path,
                expected_plan_hash=plan_hash,
                raw_dir=raw_dir,
                backup_dir=backup_dir,
            )

        assert intent_path.exists()

    def test_recovery_intent_cleanup_rejects_disappeared_owner(
        self,
        tmp_path: Path,
    ) -> None:
        from daemon.raw_projection_service import (
            _clear_recovery_intent,
            _write_recovery_intent,
        )

        raw_dir = tmp_path / "raw"
        backup_dir = tmp_path / "backups"
        intent_path = tmp_path / "raw_projection_recovery_intent.json"
        plan_hash = "a" * 64
        _write_recovery_intent(
            intent_path,
            plan_hash=plan_hash,
            raw_dir=raw_dir,
            backup_dir=backup_dir,
        )
        intent_path.unlink()

        with pytest.raises(RuntimeError, match="disappeared before cleanup"):
            _clear_recovery_intent(
                intent_path,
                expected_plan_hash=plan_hash,
                raw_dir=raw_dir,
                backup_dir=backup_dir,
            )

    def test_service_raw_projection_recovery_requires_daemon_owned_exact_plan_intent(
        self, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "raw_events.db"
        db_path.write_text("db", encoding="utf-8")
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        backup_dir = tmp_path / "backups" / "raw-vault-projection-metadata"
        reviewed_plan_hash = "a" * 64
        (tmp_path / "raw_projection_recovery_intent.json").write_text(
            json.dumps(
                {
                    "schema_version": "mnemos.raw_projection_recovery_intent.v1",
                    "plan_hash": reviewed_plan_hash,
                    "raw_dir": str(raw_dir.resolve()),
                    "backup_dir": str(backup_dir.resolve()),
                }
            ),
            encoding="utf-8",
        )

        class Cfg:
            database_dir = tmp_path
            obsidian_vault_path = raw_dir

            def get(self, key, default=None):  # noqa: ARG002
                return default

        class Store:
            def close(self):
                pass

        recovery_calls = []

        def recover_interrupted_projection(
            recovery_raw_dir,
            *,
            expected_plan_hash="",
            expected_backup_dir=None,
        ):
            recovery_calls.append(
                {
                    "raw_dir": recovery_raw_dir,
                    "expected_plan_hash": expected_plan_hash,
                    "expected_backup_dir": expected_backup_dir,
                }
            )
            return {"recovered": False, "plan_hash": ""}

        stats = {
            "raw_dir": str(raw_dir),
            "db_path": str(db_path),
            "existing_md_files": 0,
            "existing_managed_files": 0,
            "candidate_turns": 0,
            "projected_files": 0,
            "projection_plan": {
                "schema_version": "mnemos.raw_projection_plan.v1",
                "plan_hash": reviewed_plan_hash,
                "write_set_empty": True,
            },
        }

        monkeypatch.setattr("core.config.get_config", lambda: Cfg())
        monkeypatch.setattr(daemon, "_service_enabled", lambda _cfg, _name: True)
        monkeypatch.setattr(
            "scripts.project_raw_vault.recover_interrupted_projection",
            recover_interrupted_projection,
        )
        monkeypatch.setattr(
            "scripts.project_raw_vault.plan_projection",
            lambda _args: (Store(), [], stats),
        )
        monkeypatch.setattr(
            "scripts.project_raw_vault.validate_projection_plan",
            lambda plan: plan,
        )
        monkeypatch.setattr(
            "scripts.project_raw_vault.apply_projection",
            lambda _args, _store, _chunks, _stats: {
                **stats,
                "raw_index": {"indexed": 0},
            },
        )

        result = daemon.service_raw_projection()

        assert result["status"] == "skipped"
        assert recovery_calls == [
            {
                "raw_dir": raw_dir,
                "expected_plan_hash": reviewed_plan_hash,
                "expected_backup_dir": backup_dir,
            }
        ]

    def test_service_raw_projection_restart_reuses_only_crashed_apply_plan_identity(
        self, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "raw_events.db"
        db_path.write_text("db", encoding="utf-8")
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        backup_dir = tmp_path / "backups" / "raw-vault-projection-metadata"
        reviewed_plan_hash = "e" * 64

        class Cfg:
            database_dir = tmp_path
            obsidian_vault_path = raw_dir

            def get(self, key, default=None):  # noqa: ARG002
                return default

        class Store:
            def close(self):
                pass

        stats = {
            "raw_dir": str(raw_dir),
            "db_path": str(db_path),
            "existing_md_files": 0,
            "existing_managed_files": 0,
            "candidate_turns": 1,
            "projected_files": 1,
            "projection_plan": {
                "schema_version": "mnemos.raw_projection_plan.v1",
                "plan_hash": reviewed_plan_hash,
                "write_set_empty": False,
            },
        }
        recovery_calls = []
        apply_calls = 0

        def recover_interrupted_projection(
            recovery_raw_dir,
            *,
            expected_plan_hash="",
            expected_backup_dir=None,
        ):
            recovery_calls.append(
                (recovery_raw_dir, expected_plan_hash, expected_backup_dir)
            )
            return {
                "recovered": True,
                "plan_hash": expected_plan_hash,
            }

        def apply_projection(args, _store, _chunks, _stats):
            nonlocal apply_calls
            apply_calls += 1
            assert args.expected_plan_hash == reviewed_plan_hash
            if apply_calls == 1:
                raise RuntimeError("simulated crash after intent commit")
            return {**stats, "raw_index": {"indexed": 1}}

        monkeypatch.setattr("core.config.get_config", lambda: Cfg())
        monkeypatch.setattr(daemon, "_service_enabled", lambda _cfg, _name: True)
        monkeypatch.setattr(
            "scripts.project_raw_vault.recover_interrupted_projection",
            recover_interrupted_projection,
        )
        monkeypatch.setattr(
            "scripts.project_raw_vault.plan_projection",
            lambda _args: (Store(), [object()], stats),
        )
        monkeypatch.setattr(
            "scripts.project_raw_vault.validate_projection_plan",
            lambda plan: plan,
        )
        monkeypatch.setattr(
            "scripts.project_raw_vault.managed_projection_paths",
            lambda _raw_dir: [],
        )
        monkeypatch.setattr(
            "scripts.project_raw_vault._chunk_path",
            lambda _raw_dir, _chunk: raw_dir / "codex" / "day" / "chunk.md",
        )
        monkeypatch.setattr(
            "scripts.project_raw_vault.apply_projection",
            apply_projection,
        )

        first = daemon.service_raw_projection()
        second = daemon.service_raw_projection()

        assert first["status"] == "error"
        assert second["status"] == "applied"
        assert recovery_calls == [
            (raw_dir, reviewed_plan_hash, backup_dir),
        ]
        assert not (tmp_path / "raw_projection_recovery_intent.json").exists()

    def test_service_raw_projection_rejects_tampered_recovery_intent_before_plan(
        self, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "raw_events.db"
        db_path.write_text("db", encoding="utf-8")
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        (tmp_path / "raw_projection_recovery_intent.json").write_text(
            json.dumps(
                {
                    "schema_version": "mnemos.raw_projection_recovery_intent.v1",
                    "plan_hash": "not-a-sha256",
                    "raw_dir": str(raw_dir.resolve()),
                    "backup_dir": str(
                        (
                            tmp_path
                            / "backups"
                            / "raw-vault-projection-metadata"
                        ).resolve()
                    ),
                }
            ),
            encoding="utf-8",
        )

        class Cfg:
            database_dir = tmp_path
            obsidian_vault_path = raw_dir

            def get(self, key, default=None):  # noqa: ARG002
                return default

        plan_calls = 0

        def plan_projection(_args):
            nonlocal plan_calls
            plan_calls += 1
            raise AssertionError("tampered recovery intent must fail before planning")

        monkeypatch.setattr("core.config.get_config", lambda: Cfg())
        monkeypatch.setattr(daemon, "_service_enabled", lambda _cfg, _name: True)
        monkeypatch.setattr(
            "scripts.project_raw_vault.plan_projection",
            plan_projection,
        )

        result = daemon.service_raw_projection()

        assert result["status"] == "error"
        assert "plan hash is invalid" in result["error"]
        assert plan_calls == 0

    def test_service_raw_projection_does_not_skip_a_caller_shaped_partial_plan(
        self, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "raw_events.db"
        db_path.write_text("db", encoding="utf-8")
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()

        class Cfg:
            database_dir = tmp_path
            obsidian_vault_path = raw_dir

            def get(self, key, default=None):  # noqa: ARG002
                return default

        class Store:
            def close(self):
                pass

        partial_plan = {
            "schema_version": "mnemos.raw_projection_plan.v1",
            "plan_hash": "caller-shaped",
            "write_set_empty": True,
        }
        stats = {
            "raw_dir": str(raw_dir),
            "db_path": str(db_path),
            "existing_md_files": 0,
            "existing_managed_files": 0,
            "candidate_turns": 1,
            "projected_files": 1,
            "projection_plan": partial_plan,
        }
        calls = {"apply": 0}

        monkeypatch.setattr("core.config.get_config", lambda: Cfg())
        monkeypatch.setattr(daemon, "_service_enabled", lambda _cfg, _name: True)
        monkeypatch.setattr(
            "scripts.project_raw_vault.plan_projection",
            lambda _args: (Store(), [object()], stats),
        )
        monkeypatch.setattr(
            "scripts.project_raw_vault.managed_projection_paths",
            lambda _raw_dir: [],
        )
        monkeypatch.setattr(
            "scripts.project_raw_vault._chunk_path",
            lambda _raw_dir, _chunk: raw_dir / "codex" / "day" / "chunk.md",
        )

        def apply_projection(_args, _store, _chunks, _stats):
            calls["apply"] += 1
            return {**stats, "raw_index": {"indexed": 1}}

        monkeypatch.setattr(
            "scripts.project_raw_vault.apply_projection",
            apply_projection,
        )

        result = daemon.service_raw_projection()

        assert result["status"] == "error"
        assert "complete validated plan" in result["error"]
        assert calls["apply"] == 0

    def test_service_raw_projection_uses_exact_plan_for_index_recovery(self, tmp_path, monkeypatch):
        db_path = tmp_path / "raw_events.db"
        db_path.write_text("db", encoding="utf-8")
        raw_dir = tmp_path / "raw"
        projected_file = raw_dir / "codex" / "2026-06-28" / "chunk.md"
        projected_file.parent.mkdir(parents=True)
        projected_file.write_text("chunk", encoding="utf-8")

        class Cfg:
            database_dir = tmp_path
            obsidian_vault_path = raw_dir

            def get(self, key, default=None):  # noqa: ARG002
                return default

        class Store:
            def close(self):
                pass

        calls = {"apply": 0, "plan": 0}
        reviewed_plan_hash = "b" * 64
        base_stats = {
            "raw_dir": str(raw_dir),
            "db_path": str(db_path),
            "existing_md_files": 1,
            "existing_managed_files": 1,
            "candidate_turns": 1,
            "projected_files": 1,
            "projected_sources": {"codex": 1},
        }
        monkeypatch.setattr("core.config.get_config", lambda: Cfg())
        monkeypatch.setattr(daemon, "_service_enabled", lambda _cfg, _name: True)
        monkeypatch.setattr(
            "scripts.project_raw_vault._chunk_path",
            lambda _raw_dir, _chunk: projected_file,
        )

        def plan_projection(_args):
            calls["plan"] += 1
            return (
                Store(),
                [object()],
                {
                    **base_stats,
                    "projection_plan": {
                        "schema_version": "mnemos.raw_projection_plan.v1",
                        "plan_hash": reviewed_plan_hash,
                        "write_set_empty": calls["plan"] >= 3,
                    },
                },
            )

        def apply_projection(args, _store, _chunks, _stats):
            assert args.expected_plan_hash == reviewed_plan_hash
            calls["apply"] += 1
            return {**base_stats, "raw_index": {"indexed": 1}}

        monkeypatch.setattr("scripts.project_raw_vault.plan_projection", plan_projection)
        monkeypatch.setattr("scripts.project_raw_vault.apply_projection", apply_projection)
        monkeypatch.setattr(
            "scripts.project_raw_vault.validate_projection_plan",
            lambda plan: (
                plan
                if plan.get("plan_hash") == reviewed_plan_hash
                else (_ for _ in ()).throw(RuntimeError("invalid plan"))
            ),
        )

        first = daemon.service_raw_projection()
        second = daemon.service_raw_projection()
        third = daemon.service_raw_projection()

        assert first["status"] == "applied"
        assert second["status"] == "applied"
        assert third["status"] == "skipped"
        assert calls["apply"] == 3

    def test_service_raw_projection_applies_then_skips_when_unchanged(self, tmp_path, monkeypatch):
        db_path = tmp_path / "raw_events.db"
        db_path.write_text("db", encoding="utf-8")
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()

        class Cfg:
            database_dir = tmp_path
            obsidian_vault_path = raw_dir

            def get(self, key, default=None):  # noqa: ARG002
                return default

        cfg = Cfg()

        class Store:
            closed = False

            def close(self):
                self.closed = True

        chunks = [object()]
        stats = {
            "raw_dir": str(raw_dir),
            "db_path": str(db_path),
            "existing_md_files": 0,
            "candidate_turns": 1,
            "projected_files": 1,
            "projected_sources": {"codex": 1},
        }
        applied = {**stats, "raw_index": {"indexed": 1}}
        calls = {"apply": 0}
        reviewed_plan_hash = "c" * 64
        projected_file = raw_dir / "codex" / "2026-06-28" / "chunk.md"

        monkeypatch.setattr("core.config.get_config", lambda: cfg)
        monkeypatch.setattr(daemon, "_service_enabled", lambda _cfg, name: True)
        monkeypatch.setattr(
            "scripts.project_raw_vault._chunk_path",
            lambda _raw_dir, _chunk: projected_file,
        )

        def plan_projection(_args):
            planned = dict(stats)
            planned["existing_md_files"] = calls["apply"]
            planned["projection_plan"] = {
                "plan_hash": reviewed_plan_hash,
                "write_set_empty": calls["apply"] > 0,
            }
            return Store(), chunks, planned

        monkeypatch.setattr("scripts.project_raw_vault.plan_projection", plan_projection)
        monkeypatch.setattr(
            "scripts.project_raw_vault.validate_projection_plan",
            lambda plan: plan,
        )

        def apply_projection(_args, _store, _chunks, _stats):
            calls["apply"] += 1
            projected_file.parent.mkdir(parents=True, exist_ok=True)
            projected_file.write_text("chunk", encoding="utf-8")
            return dict(applied)

        monkeypatch.setattr("scripts.project_raw_vault.apply_projection", apply_projection)

        first = daemon.service_raw_projection()
        second = daemon.service_raw_projection()

        assert first["status"] == "applied"
        assert second["status"] == "skipped"
        assert second["reason"] == "up_to_date"
        assert calls["apply"] == 2
        assert (tmp_path / "raw_projection_state.json").exists()

    def test_service_raw_projection_recovery_clears_error_and_records_ledger(
        self, tmp_path, monkeypatch
    ):
        from core.system_contracts import ActionLedger

        db_path = tmp_path / "raw_events.db"
        db_path.write_text("db", encoding="utf-8")
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()

        class Cfg:
            database_dir = tmp_path
            obsidian_vault_path = raw_dir

            def get(self, key, default=None):  # noqa: ARG002
                return default

        cfg = Cfg()

        class Store:
            def close(self):
                pass

        chunks = [object()]
        stats = {
            "raw_dir": str(raw_dir),
            "db_path": str(db_path),
            "existing_md_files": 0,
            "candidate_turns": 1,
            "projected_files": 1,
            "projected_sources": {"codex": 1},
        }
        projected_file = raw_dir / "codex" / "2026-06-28" / "chunk.md"
        calls = {"apply": 0}
        reviewed_plan_hash = "d" * 64
        monkeypatch.setattr("core.config.get_config", lambda: cfg)
        monkeypatch.setattr(daemon, "_service_enabled", lambda _cfg, name: True)
        monkeypatch.setattr(
            daemon,
            "_service_error_state",
            {
                "raw_projection": {
                    "count": 4,
                    "last_error": "database is locked",
                    "last_error_type": "OperationalError",
                    "last_error_at": "2026-01-01T00:00:00",
                    "last_context": "raw_projection",
                }
            },
            raising=False,
        )
        monkeypatch.setattr(
            "scripts.project_raw_vault._chunk_path",
            lambda _raw_dir, _chunk: projected_file,
        )

        def plan_projection(_args):
            planned = dict(stats)
            planned["existing_md_files"] = calls["apply"]
            planned["projection_plan"] = {
                "plan_hash": reviewed_plan_hash,
                "write_set_empty": calls["apply"] > 0,
            }
            return Store(), chunks, planned

        monkeypatch.setattr("scripts.project_raw_vault.plan_projection", plan_projection)
        monkeypatch.setattr(
            "scripts.project_raw_vault.validate_projection_plan",
            lambda plan: plan,
        )

        def apply_projection(_args, _store, _chunks, _stats):
            calls["apply"] += 1
            projected_file.parent.mkdir(parents=True, exist_ok=True)
            projected_file.write_text("chunk", encoding="utf-8")
            return {**stats, "raw_index": {"indexed": 1}}

        monkeypatch.setattr("scripts.project_raw_vault.apply_projection", apply_projection)

        result = daemon.service_raw_projection()
        second = daemon.service_raw_projection()

        assert result["status"] == "applied"
        assert result["_recovered_error_count"] == 4
        assert second["status"] == "skipped"
        assert second["reason"] == "up_to_date"
        assert "_recovered_error_count" not in second
        assert "raw_projection" not in daemon._service_error_state
        rows = ActionLedger(tmp_path / "action_ledger.db").recent(limit=5)
        assert len(rows) == 1
        row = rows[0]
        assert row["action_type"] == "raw_projection_recovered"
        assert row["status"] == "verified"
        assert row["verification"]["last_error"] == "database is locked"

    def test_service_recovery_does_not_fail_when_ledger_is_locked(self, monkeypatch):
        class LockedLedger:
            def record(self, _record):
                raise sqlite3.OperationalError("database is locked")

        from core import system_contracts

        monkeypatch.setattr(
            system_contracts.ActionLedger, "from_config", lambda _cfg: LockedLedger()
        )
        monkeypatch.setattr(
            daemon,
            "_service_error_state",
            {
                "raw_projection": {
                    "count": 1,
                    "last_error": "database is locked",
                    "last_error_type": "OperationalError",
                    "last_error_at": "2026-01-01T00:00:00",
                    "last_context": "raw_projection",
                }
            },
            raising=False,
        )

        result = daemon._mark_service_recovered(
            "raw_projection", {"status": "skipped", "reason": "up_to_date"}, object()
        )

        assert result["status"] == "skipped"
        assert result["_recovered_error_count"] == 1
        assert "raw_projection" not in daemon._service_error_state

    def test_service_raw_projection_skips_missing_db(self, tmp_path, monkeypatch):
        class Cfg:
            database_dir = tmp_path
            obsidian_vault_path = tmp_path / "raw"

            def get(self, key, default=None):  # noqa: ARG002
                return default

        monkeypatch.setattr("core.config.get_config", lambda: Cfg())
        monkeypatch.setattr(daemon, "_service_enabled", lambda _cfg, name: True)
        monkeypatch.setattr(daemon, "_service_error_state", {}, raising=False)

        result = daemon.service_raw_projection()

        assert result == {
            "enabled": True,
            "status": "skipped",
            "reason": "raw_events_missing",
        }

    def test_service_raw_projection_does_not_label_uninspectable_db_missing(
        self,
        tmp_path,
        monkeypatch,
    ):
        db_path = tmp_path / "raw_events.db"

        class Cfg:
            database_dir = tmp_path
            obsidian_vault_path = tmp_path / "raw"

            def get(self, key, default=None):  # noqa: ARG002
                return default

        original_stat = Path.stat

        def denied(path, *args, **kwargs):
            if path == db_path:
                raise PermissionError("sentinel")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", denied)
        monkeypatch.setattr("core.config.get_config", lambda: Cfg())
        monkeypatch.setattr(daemon, "_service_enabled", lambda _cfg, _name: True)
        monkeypatch.setattr(
            "daemon.runtime_flow_receipts.record_raw_projection_error",
            lambda *_args, **_kwargs: None,
        )

        result = daemon.service_raw_projection()

        assert result["status"] == "error"
        assert result.get("reason") != "raw_events_missing"
        assert "durable_path_inspection_failed" in result["error"]


class TestWikiRouteService:
    def test_service_wiki_route_reports_connect_cycle_counts(self, monkeypatch, tmp_path):
        def fake_run_connect_cycle(dry_run=False, *, write_relations=True):
            assert dry_run is False
            assert write_relations is False
            return {
                "classified": 3,
                "moved": 2,
                "review": 1,
                "tech": 2,
                "concepts": 1,
            }

        monkeypatch.setattr("core.kia.charon.run_connect_cycle", fake_run_connect_cycle)
        cfg = type("Cfg", (), {"database_dir": tmp_path, "wiki_dir": tmp_path})()
        monkeypatch.setattr("core.config.get_config", lambda: cfg)
        monkeypatch.setattr("core.wiki_projection_lifecycle.get_config", lambda: cfg)
        monkeypatch.setattr(
            "core.wiki_projection_publisher.publish_unpublished_mutations",
            lambda _ledger, limit=100: {"published": 0, "events": []},
        )

        result = daemon.service_wiki_route()

        assert result["status"] == "ok"
        assert result["classified"] == 3
        assert result["moved"] == 2
        assert result["review"] == 1
        assert result["counts"]["tech"] == 2


class TestServiceScheduler:
    """P105: daemon 主循环应使用线程池并行调度，并防止同一服务重叠执行。"""

    def test_resolve_service_call_returns_callable_for_all_intervals(self):
        cfg = MagicMock()
        cfg.get.return_value = None
        for service_name in daemon.INTERVALS:
            fn = daemon._resolve_service_call(cfg, service_name)
            assert callable(fn), f"{service_name} 应解析为可调用对象"

    def test_resolve_service_call_unknown_service_raises(self):
        with pytest.raises(ValueError):
            daemon._resolve_service_call(None, "unknown_service")

    def test_main_loop_submits_services_to_executor(self):
        """主循环应将到期服务提交到线程池，而不是同步阻塞执行。"""
        last_run = {"heartbeat": 0}  # 强制立即触发

        mock_executor = MagicMock()
        mock_future = MagicMock()
        mock_executor.submit.return_value = mock_future

        class FakeBudget:
            def can_run(self, service: str) -> bool:
                assert service == "heartbeat"
                return True

        with (
            patch.object(daemon, "_service_enabled", return_value=True),
            patch.object(daemon, "_service_futures", {}),
            patch.object(daemon, "_service_results", {}),
            patch.object(daemon, "_resolve_service_call") as mock_resolve,
            patch("core.resource_budget.get_budget", return_value=FakeBudget()),
        ):
            mock_fn = MagicMock()
            mock_resolve.return_value = mock_fn

            daemon._schedule_service_if_due(None, "heartbeat", 1, 10, last_run, mock_executor)

            mock_executor.submit.assert_called_once_with(mock_fn)
            assert "heartbeat" in daemon._service_futures
            assert last_run["heartbeat"] == 10
            mock_future.add_done_callback.assert_called_once()

    def test_main_loop_skips_overlapping_service(self):
        """当服务仍在运行时，应跳过本次调度，不重复提交。"""
        running_future = MagicMock()
        running_future.done.return_value = False
        mock_executor = MagicMock()
        last_run = {"heartbeat": 0}

        with (
            patch.object(daemon, "_service_futures", {"heartbeat": running_future}),
            patch.object(daemon, "_service_enabled", return_value=True),
            patch.object(daemon._resource_budget, "deferral") as mock_budget_deferral,
        ):
            daemon._schedule_service_if_due(None, "heartbeat", 1, 10, last_run, mock_executor)

        mock_executor.submit.assert_not_called()
        mock_budget_deferral.assert_not_called()
        assert last_run["heartbeat"] == 0

    def test_main_loop_defers_service_when_resource_budget_blocks(self):
        """资源预算不足时，全局调度入口应延后后台服务且不提交线程池。"""
        mock_executor = MagicMock()
        last_run = {"scheduler_tick": 0}

        class FakeBudget:
            def can_run(self, service: str) -> bool:
                assert service == "kia_sched"
                return False

            def throttle_delay(self, service: str) -> float:
                assert service == "kia_sched"
                return 45.0

            def status(self) -> dict:
                return {
                    "state": "throttled",
                    "cpu": "95.0%",
                    "memory": "70.0%",
                    "thermal": "normal",
                    "power": "ac",
                }

        with (
            patch.object(daemon, "_service_enabled", return_value=True),
            patch.object(daemon, "_service_futures", {}),
            patch.object(daemon, "_service_results", {}),
            patch("core.resource_budget.get_budget", return_value=FakeBudget()),
        ):
            daemon._schedule_service_if_due(
                None, "scheduler_tick", 60, 100, last_run, mock_executor
            )

            result = daemon._service_results["scheduler_tick"]

        mock_executor.submit.assert_not_called()
        assert result["status"] == "deferred"
        assert result["reason"] == "resource_budget"
        assert result["budget_service"] == "kia_sched"
        assert result["resource_state"] == "throttled"
        assert result["retry_after_seconds"] == 45
        assert last_run["scheduler_tick"] == 85

    def test_main_loop_runs_service_when_resource_budget_check_fails(self):
        """资源预算检查自身失败时应 fail-open，避免 daemon 调度停摆。"""
        mock_executor = MagicMock()
        mock_future = MagicMock()
        mock_executor.submit.return_value = mock_future
        last_run = {"heartbeat": 0}

        with (
            patch.object(daemon, "_service_enabled", return_value=True),
            patch.object(daemon, "_service_futures", {}),
            patch.object(daemon, "_service_results", {}),
            patch.object(daemon, "_resolve_service_call") as mock_resolve,
            patch("core.resource_budget.get_budget", side_effect=RuntimeError("budget down")),
        ):
            mock_fn = MagicMock()
            mock_resolve.return_value = mock_fn

            daemon._schedule_service_if_due(None, "heartbeat", 1, 10, last_run, mock_executor)

        mock_executor.submit.assert_called_once_with(mock_fn)
        assert last_run["heartbeat"] == 10


class TestWindowsScheduler:
    def test_windows_task_command_quotes_python_and_exports_runtime_env(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("MNEMOS_DIR", str(tmp_path / ".mnemos"))
        monkeypatch.setenv("MNEMOS_LLM_MODEL", "llm-model")

        command = daemon._windows_task_command(tmp_path / "mnemos_daemon.py")

        assert "powershell.exe" in command
        assert "$env:MNEMOS_DIR=" in command
        assert "$env:MNEMOS_LLM_MODEL='llm-model';" in command
        assert str(tmp_path / "mnemos_daemon.py") in command
        assert " start" in command


class TestObservationService:
    """P107: daemon 的 observation_engine 服务应使用增量运行。"""

    def test_service_observation_engine_uses_incremental_with_latest_update(self, tmp_path):
        with (
            patch("core.config.get_config") as mock_cfg,
            patch("core.cognitive.observation_engine.ObservationEngine") as mock_engine_cls,
        ):
            cfg = MagicMock()
            cfg.get.return_value = True
            cfg.obsidian_vault_path = tmp_path / "raw"
            cfg.wiki_dir = tmp_path / "wiki"
            cfg.database_dir = tmp_path / "database"
            mock_cfg.return_value = cfg

            mock_engine = MagicMock()
            mock_engine.get_store_stats.return_value = {"latest_update": "2026-06-01T00:00:00"}
            mock_batch = MagicMock()
            mock_batch.observations = []
            mock_batch.total_observations = 0
            mock_batch.dimension_counts = {}
            mock_batch.source_count = 0
            mock_batch.extraction_status = "ok"
            mock_batch.extraction_reason = "no_new_observations"
            mock_engine.run_incremental.return_value = mock_batch
            mock_engine_cls.return_value = mock_engine

            result = daemon.service_observation_engine()

            mock_engine.get_store_stats.assert_called_once()
            mock_engine.run_incremental.assert_called_once()
            mock_engine.run.assert_not_called()
            assert result["observations"] == 0

    def test_service_observation_engine_falls_back_to_recent_incremental_without_latest_update(
        self, tmp_path
    ):
        """无 latest_update 时也应回退到最近 24h 的增量扫描，避免全量扫描导致 CPU/IO 爆炸。"""
        with (
            patch("core.config.get_config") as mock_cfg,
            patch("core.cognitive.observation_engine.ObservationEngine") as mock_engine_cls,
        ):
            cfg = MagicMock()
            cfg.get.return_value = True
            cfg.obsidian_vault_path = tmp_path / "raw"
            cfg.wiki_dir = tmp_path / "wiki"
            cfg.database_dir = tmp_path / "database"
            mock_cfg.return_value = cfg

            mock_engine = MagicMock()
            mock_engine.get_store_stats.return_value = {"latest_update": None}
            mock_batch = MagicMock()
            mock_batch.observations = []
            mock_batch.total_observations = 0
            mock_batch.dimension_counts = {}
            mock_batch.source_count = 0
            mock_batch.extraction_status = "ok"
            mock_batch.extraction_reason = "no_new_observations"
            mock_engine.run_incremental.return_value = mock_batch
            mock_engine_cls.return_value = mock_engine

            daemon.service_observation_engine()

            mock_engine.run.assert_not_called()
            mock_engine.run_incremental.assert_called_once()
            since = mock_engine.run_incremental.call_args.kwargs["since"]
            from datetime import datetime, timedelta, timezone

            assert since >= datetime.now(timezone.utc) - timedelta(hours=25)
            assert since <= datetime.now(timezone.utc) - timedelta(hours=23)


class TestServiceReminderScan:
    def test_service_reminder_scan_enqueues_high_priority(
        self,
        monkeypatch,
        tmp_path,
    ):
        """service_reminder_scan 应将高优先级过期页面入队提醒。"""
        from core.kia.reminder_engine import Reminder

        fake_reminder = Reminder(
            reminder_type="freshness",
            page_path="03-Tech/stale.md",
            title="stale",
            message="已过期",
            reason="freshness",
            confidence=0.9,
            priority="high",
        )

        class FakeReminderEngine:
            def scan_all_freshness(self):
                return [fake_reminder]

        calls = []

        class FakeQueue:
            def enqueue(self, issue_id, page_path, severity, content, choices):
                calls.append({"issue_id": issue_id, "page_path": page_path, "severity": severity})
                return "reminder-test-receipt"

        monkeypatch.setattr("core.kia.reminder_engine.ReminderEngine", FakeReminderEngine)
        monkeypatch.setattr("core.kia.dialog_reminder.DialogReminderQueue", FakeQueue)

        class Cfg:
            wiki_dir = tmp_path / "wiki"
            database_dir = tmp_path / "database"

            def get(self, key, default=None):
                if key in ("reminder.enabled", "daemon.services.reminder_scan"):
                    return True
                return default

        monkeypatch.setattr("core.config.get_config", lambda: Cfg())
        result = daemon.service_reminder_scan()
        assert result["enqueued"] == 1
        assert calls[0]["issue_id"] == "freshness:03-Tech/stale.md"
        assert calls[0]["severity"] == "high"


class TestServiceFreshnessRefresh:
    def test_service_freshness_refresh_refreshes_high_priority(
        self,
        monkeypatch,
        tmp_path,
    ):
        from core.kia.reminder_engine import Reminder

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        page = wiki / "stale.md"
        page.write_text("---\nupdated_at: 2000-01-01\n---\n\nbody", encoding="utf-8")

        fake_reminder = Reminder(
            reminder_type="freshness",
            page_path=str(page),
            title="stale",
            message="已过期",
            reason="freshness",
            confidence=0.9,
            priority="high",
        )

        class FakeReminderEngine:
            def scan_all_freshness(self):
                return [fake_reminder]

        monkeypatch.setattr("core.kia.reminder_engine.ReminderEngine", FakeReminderEngine)

        class Cfg:
            wiki_dir = wiki
            database_dir = wiki / ".mnemos"

            def get(self, key, default=None):
                if key in ("reminder.enabled", "daemon.services.freshness_refresh"):
                    return True
                if key == "freshness_refresh.auto_refresh_limit":
                    return 3
                return default

        monkeypatch.setattr("core.config.get_config", lambda: Cfg())
        result = daemon.service_freshness_refresh()
        assert result["refreshed"] == 1


class TestServiceEntropyScan:
    def test_service_entropy_scan_enqueues_candidates(self, monkeypatch, tmp_path):
        from core.kia.eris import EntropyReport, MergeCandidate

        class FakeEntropyEngine:
            def scan(self, sample_size=None):
                return EntropyReport(
                    total_pairs_scanned=10,
                    candidates=[
                        MergeCandidate(
                            page_a="a.md",
                            page_b="b.md",
                            similarity=0.9,
                            merge_strategy="merge_into_one",
                            reason="相似",
                            recommended_action="合并",
                            keep_page="a.md",
                            confidence=0.9,
                        ),
                    ],
                )

        class FakeRegistry:
            def __init__(self):
                self.started = []
                self.engine = FakeEntropyEngine()

            def start_module(self, module_id):
                self.started.append(module_id)
                return {"eris": {"state": "running"}}

            def get_instance(self, module_id):
                return self.engine

        registry = FakeRegistry()
        monkeypatch.setattr(daemon, "_get_kia_module_registry", lambda cfg=None: registry)

        calls = []

        class FakeQueue:
            def enqueue(self, issue_id, page_path, severity, content, choices):
                calls.append({"issue_id": issue_id, "severity": severity})

        monkeypatch.setattr("core.kia.dialog_reminder.DialogReminderQueue", FakeQueue)

        class Cfg:
            wiki_dir = tmp_path / "wiki"
            database_dir = tmp_path / ".mnemos"

            def get(self, key, default=None):
                if key == "daemon.services.entropy_scan":
                    return True
                if key == "entropy.scan_sample_size":
                    return 10
                return default

        monkeypatch.setattr("core.config.get_config", lambda: Cfg())
        result = daemon.service_entropy_scan()
        assert result["enqueued"] == 1
        assert calls[0]["issue_id"].startswith("entropy:")
        assert registry.started == ["eris"]


class TestKiaModuleLifecycle:
    def test_start_kia_modules_starts_enabled_registry_modules(self, monkeypatch):
        cfg = object()
        calls = []

        class FakeRegistry:
            def start_enabled(self):
                calls.append("start_enabled")
                return {"eris": {"state": "running"}}

        registry = FakeRegistry()
        monkeypatch.setattr(
            daemon,
            "_get_kia_module_registry",
            lambda got_cfg=None: calls.append(("get_registry", got_cfg)) or registry,
        )

        result = daemon._start_kia_modules(cfg)

        assert result == {"eris": {"state": "running"}}
        assert calls == [("get_registry", cfg), "start_enabled"]

    def test_stop_kia_modules_stops_registry_modules(self, monkeypatch):
        calls = []

        class FakeRegistry:
            def stop_all(self):
                calls.append("stop_all")
                return {"eris": {"state": "stopped"}}

        monkeypatch.setattr(daemon, "_kia_module_registry", FakeRegistry(), raising=False)

        result = daemon._stop_kia_modules()

        assert result == {"eris": {"state": "stopped"}}
        assert calls == ["stop_all"]


class TestServiceHeartBeatAndDisabled:
    def test_log_service_error_records_structured_state(self, monkeypatch):
        monkeypatch.setattr(daemon, "_service_error_state", {}, raising=False)

        daemon._log_service_error("raw_sync:codex", RuntimeError("source boom"))
        daemon._log_service_error("raw_sync:codex", ValueError("bad turn"))

        state = daemon._service_error_state["raw_sync"]
        assert state["count"] == 2
        assert state["last_error"] == "bad turn"
        assert state["last_error_type"] == "ValueError"
        assert state["last_context"] == "raw_sync:codex"
        assert "last_error_at" in state

    def test_service_heartbeat_includes_services_summary(self, monkeypatch, tmp_path):
        """heartbeat 应返回各服务最近状态摘要。"""

        monkeypatch.setattr(
            daemon,
            "_service_results",
            {
                "signal_collector": {
                    "at": "2026-01-01T00:00:00",
                    "ok": True,
                    "result": {"enabled": True, "collected": 42},
                },
                "link_probe": {
                    "at": "2026-01-01T00:00:00",
                    "ok": True,
                    "result": {"enabled": False, "probed": 0},
                },
            },
            raising=False,
        )
        monkeypatch.setattr(
            daemon,
            "INTERVALS",
            {"signal_collector": 60, "link_probe": 3600},
            raising=False,
        )
        monkeypatch.setattr(
            daemon,
            "_daemon_instance_identity",
            {"schema_version": "mnemos.daemon_instance.v2", "instance_id": "test-1"},
        )

        fake_cfg = MagicMock()
        fake_cfg.get = MagicMock(return_value=True)
        heartbeat_file = tmp_path / "daemon_heartbeat.json"
        module_health = {
            "eris": {
                "state": "registered",
                "enabled": True,
                "dependencies": ["genos"],
                "details": {"healthy": False},
            }
        }
        fake_registry = MagicMock()
        fake_registry.health_check.return_value = module_health
        monkeypatch.setattr(daemon, "_get_kia_module_registry", lambda cfg=None: fake_registry)
        with (
            patch("core.config.get_config", return_value=fake_cfg),
            patch.object(daemon, "DAEMON_HEARTBEAT_FILE", heartbeat_file),
        ):
            hb = daemon.service_heartbeat()

        assert "timestamp" in hb
        assert "services" in hb
        assert hb["services"]["signal_collector"]["enabled"] is True
        assert hb["services"]["signal_collector"]["metrics"]["collected"] == 42
        assert hb["services"]["link_probe"]["enabled"] is False
        assert hb["modules"] == module_health
        saved = json.loads(heartbeat_file.read_text(encoding="utf-8"))
        assert saved == hb

    def test_controlled_profile_heartbeat_omits_kia_modules_and_exposes_profile(
        self, monkeypatch, tmp_path
    ):
        fake_cfg = MagicMock()
        fake_cfg.get = MagicMock(return_value=True)
        fake_cfg.database_dir = tmp_path
        heartbeat_file = tmp_path / "daemon_heartbeat.json"
        monkeypatch.setattr(
            daemon,
            "_daemon_instance_identity",
            {"schema_version": "mnemos.daemon_instance.v2", "instance_id": "test-2"},
        )
        monkeypatch.setattr(
            daemon._agent_source_runtime,
            "persisted_source_coverage",
            lambda _database_dir: None,
        )
        monkeypatch.setattr(
            daemon,
            "_get_kia_module_registry",
            MagicMock(side_effect=AssertionError("controlled profile must not initialise KIA")),
        )

        daemon._activate_daemon_profile(controlled_raw_sync_only=True)
        try:
            with (
                patch("core.config.get_config", return_value=fake_cfg),
                patch.object(daemon, "DAEMON_HEARTBEAT_FILE", heartbeat_file),
            ):
                heartbeat = daemon.service_heartbeat()
        finally:
            daemon._reset_daemon_profile()

        assert heartbeat["run_profile"] == daemon._CONTROLLED_RAW_SYNC_ONLY_RUN_PROFILE
        assert set(heartbeat["services"]) == {"heartbeat", "raw_sync"}
        assert "modules" not in heartbeat

    def test_service_heartbeat_exposes_recent_caught_service_errors(self, monkeypatch, tmp_path):
        """服务内部 broad except 捕获的错误也应进入 heartbeat/health 可见状态。"""

        monkeypatch.setattr(
            daemon,
            "_service_results",
            {
                "raw_sync": {
                    "at": "2026-01-01T00:00:00",
                    "ok": True,
                    "result": {"enabled": True, "synced": 0, "errors": 1},
                }
            },
            raising=False,
        )
        monkeypatch.setattr(
            daemon,
            "_service_error_state",
            {
                "raw_sync": {
                    "count": 1,
                    "last_error": "source boom",
                    "last_error_type": "RuntimeError",
                    "last_error_at": "2026-01-01T00:00:00",
                    "last_context": "raw_sync:codex",
                }
            },
            raising=False,
        )
        monkeypatch.setattr(daemon, "INTERVALS", {"raw_sync": 60}, raising=False)
        monkeypatch.setattr(
            daemon,
            "_daemon_instance_identity",
            {"schema_version": "mnemos.daemon_instance.v2", "instance_id": "test-1"},
        )

        fake_cfg = MagicMock()
        fake_cfg.get = MagicMock(return_value=True)
        heartbeat_file = tmp_path / "daemon_heartbeat.json"
        with (
            patch("core.config.get_config", return_value=fake_cfg),
            patch.object(daemon, "DAEMON_HEARTBEAT_FILE", heartbeat_file),
        ):
            hb = daemon.service_heartbeat()

        raw_sync = hb["services"]["raw_sync"]
        assert raw_sync["errors"] == 1
        assert raw_sync["error_count"] == 1
        assert raw_sync["last_error"] == "source boom"
        assert raw_sync["last_error_type"] == "RuntimeError"
        assert raw_sync["last_error_context"] == "raw_sync:codex"

    def test_service_cognitive_graph_reconcile_disabled_returns_enabled_false(self, monkeypatch):
        """认知图 reconciler 在功能禁用时返回 enabled:false 而非空指标。"""
        fake_cfg = MagicMock()
        fake_cfg.cognitive_graph_enabled = False
        monkeypatch.setattr("core.config.get_config", lambda: fake_cfg)

        result = daemon.service_cognitive_graph_reconcile()
        assert result["enabled"] is False
        assert result["processed"] == 0

    def test_service_signal_collector_returns_actual_counts(self, monkeypatch):
        """signal_collector 应返回真实采集数量，而非固定 1。"""

        class FakeCollector:
            def collect_all(self):
                return {"session": 5, "wiki": 3, "git": 0}

        class FakeApplicationSignalService:
            def __init__(self, config=None):
                self.config = config

            def run(self):
                return {"persisted": 2, "detected": 2, "cooldown_skipped": 0}

        monkeypatch.setattr("core.persona.daimon.SignalCollector", FakeCollector)
        monkeypatch.setattr(
            "core.app.application_signal_service.ApplicationSignalService",
            FakeApplicationSignalService,
        )
        result = daemon.service_signal_collector()
        assert result["collected"] == 10
        assert result["sources"] == {"session": 5, "wiki": 3, "git": 0}
        assert result["application_signals"]["persisted"] == 2
