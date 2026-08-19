# -*- coding: utf-8 -*-
"""Tests for daemon TriggerDispatcher / FileIngestor integration (P1-#18)."""

from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch


class FakeConfig:
    def __init__(self, **kwargs):
        self._data = kwargs

    def get(self, key, default=None):
        keys = key.split(".")
        value = self._data
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value


class TestServiceFileIngestor(unittest.TestCase):
    def test_service_file_ingestor_ingests_supported_files(self):
        from mnemos_daemon import service_file_ingestor

        with tempfile.TemporaryDirectory() as tmp:
            ingest_dir = Path(tmp)
            (ingest_dir / "a.txt").write_text("hello")

            mock_ingestor = MagicMock()
            mock_ingestor.ingest_directory.return_value = 1

            cfg = FakeConfig(file_ingestor={"watch_dir": str(ingest_dir)})

            with patch(
                "core.sync_framework.file_ingestor.FileIngestor", return_value=mock_ingestor
            ):
                result = service_file_ingestor(cfg)

            self.assertEqual(result["ingested"], 1)
            self.assertEqual(result["errors"], 0)
            mock_ingestor.ingest_directory.assert_called_once_with(
                ingest_dir, agent_name="file", recursive=True
            )


class TestSyncTriggerDirtySources(unittest.TestCase):
    def test_syncs_dirty_sources(self):
        from mnemos_daemon import _sync_trigger_dirty_sources, _trigger_dirty_sources, _trigger_lock

        with patch("mnemos_daemon._triggers.sync_dirty_sources") as sync_dirty:
            with _trigger_lock:
                _trigger_dirty_sources.add("claude")

            cfg = FakeConfig(daemon={"services": {"raw_sync": True}})
            cfg.database_dir = Path("/tmp/mnemos-trigger-test")
            _sync_trigger_dirty_sources(cfg)

        sync_dirty.assert_called_once()
        args, kwargs = sync_dirty.call_args
        self.assertEqual(args[:2], (["claude"], cfg))
        self.assertEqual(
            kwargs["continuous_sync_limits"](),
            {
                "tail_sessions_per_source": 10,
                "reconciliation_sessions_per_source": 10,
                "turns_per_session": 100,
            },
        )
        self.assertEqual(kwargs["cursor_store"].path, cfg.database_dir / "agent_sync_cursors.db")


class TestStartTriggerDispatcher(unittest.TestCase):
    def tearDown(self):
        import mnemos_daemon

        mnemos_daemon._trigger_dispatcher = None
        mnemos_daemon._file_ingestor_instance = None

    def test_registers_sources_and_file_ingest_dir(self):
        from mnemos_daemon import _start_trigger_dispatcher

        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / "agent"
            agent_dir.mkdir()
            ingest_dir = Path(tmp) / "ingest"
            ingest_dir.mkdir()

            source = MagicMock()
            source.name = "claude"
            source.data_dir = agent_dir
            source.trigger_strategy = {"type": "watchdog", "events": ["created"]}

            mock_dispatcher = MagicMock()
            mock_dispatcher_cls = MagicMock(return_value=mock_dispatcher)

            cfg = FakeConfig(
                daemon={"services": {"trigger_dispatcher": True}},
                file_ingestor={"watch_dir": str(ingest_dir)},
            )

            with (
                patch("core.sync_framework.triggers.TriggerDispatcher", mock_dispatcher_cls),
                patch("core.sync_framework.registry.SourceRegistry") as mock_registry,
                patch("core.sync_framework.file_ingestor.FileIngestor"),
            ):
                mock_registry.register_builtin_agents = MagicMock()
                mock_registry.auto_discover.return_value = [source]
                mock_registry.list_sources.return_value = [source]

                _start_trigger_dispatcher(cfg)

                mock_registry.register_builtin_agents.assert_called_once()
                mock_registry.auto_discover.assert_called_once()
                # Source + file_ingestor synthetic source
                self.assertEqual(mock_dispatcher.register.call_count, 2)
                self.assertEqual(mock_dispatcher.start_all.call_count, 2)

    def test_rejects_trigger_accelerator_that_drifts_from_manifest(self):
        from mnemos_daemon import _start_trigger_dispatcher

        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / "agent"
            agent_dir.mkdir()
            source = MagicMock()
            source.name = "claude"
            source.data_dir = agent_dir
            source.trigger_strategy = {"type": "polling", "interval": 60}
            mock_dispatcher = MagicMock()

            cfg = FakeConfig(daemon={"services": {"trigger_dispatcher": True}})
            with (
                patch("core.sync_framework.triggers.TriggerDispatcher", return_value=mock_dispatcher),
                patch("core.sync_framework.registry.SourceRegistry") as mock_registry,
                patch("core.sync_framework.file_ingestor.FileIngestor"),
            ):
                mock_registry.register_builtin_agents = MagicMock()
                mock_registry.auto_discover.return_value = [source]

                _start_trigger_dispatcher(cfg)

            mock_dispatcher.register.assert_not_called()


class TestCmdIngest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_cmd_ingest_file(self):
        from mnemos_cli import cmd_ingest

        test_file = Path(self.tmpdir.name) / "note.txt"
        test_file.write_text("hello", encoding="utf-8")

        mock_ingestor = MagicMock()
        mock_ingestor.ingest_file.return_value = [Mock(uid="uid-1")]

        args = Namespace(path=str(test_file), agent_name="file", recursive=True, no_recursive=False)
        with patch("core.sync_framework.file_ingestor.FileIngestor", return_value=mock_ingestor):
            rc = cmd_ingest(args)

        self.assertEqual(rc, 0)
        mock_ingestor.ingest_file.assert_called_once_with(test_file, agent_name="file")

    def test_cmd_ingest_directory(self):
        from mnemos_cli import cmd_ingest

        subdir = Path(self.tmpdir.name) / "docs"
        subdir.mkdir()
        (subdir / "a.txt").write_text("a")

        mock_ingestor = MagicMock()
        mock_ingestor.ingest_directory.return_value = 1

        args = Namespace(path=str(subdir), agent_name="file", recursive=True, no_recursive=False)
        with patch("core.sync_framework.file_ingestor.FileIngestor", return_value=mock_ingestor):
            rc = cmd_ingest(args)

        self.assertEqual(rc, 0)
        mock_ingestor.ingest_directory.assert_called_once_with(
            subdir, agent_name="file", recursive=True
        )

    def test_cmd_ingest_missing_path(self):
        from mnemos_cli import cmd_ingest

        args = Namespace(
            path="/nonexistent/path", agent_name="file", recursive=True, no_recursive=False
        )
        rc = cmd_ingest(args)
        self.assertEqual(rc, 1)


class TestServiceEventBus(unittest.TestCase):
    def tearDown(self):
        import mnemos_daemon

        mnemos_daemon._event_bus_instance = None

    def test_eventbus_health_check_restarts_stopped_dispatch(self):
        from mnemos_daemon import service_eventbus
        import mnemos_daemon

        mock_bus = MagicMock()
        mock_bus._dispatch_thread = MagicMock()
        mock_bus._dispatch_thread.is_alive.return_value = False
        mock_bus._queue.qsize.return_value = 3
        mnemos_daemon._event_bus_instance = mock_bus

        result = service_eventbus()

        mock_bus.start_dispatch.assert_called_once()
        self.assertEqual(result["queue_depth"], 3)
        self.assertTrue(result.get("restarted"))

    def test_eventbus_health_check_no_bus_returns_ok(self):
        from mnemos_daemon import service_eventbus
        import mnemos_daemon

        mnemos_daemon._event_bus_instance = None
        result = service_eventbus()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["queue_depth"], 0)


class TestServiceFeedbackPrompt(unittest.TestCase):
    def tearDown(self):
        import mnemos_daemon

        mnemos_daemon._event_bus_instance = None

    @patch("core.config.get_config")
    @patch("mnemos_daemon._get_reflection_engine")
    def test_feedback_prompt_uses_global_event_bus(self, mock_get_engine, mock_get_config):
        from mnemos_daemon import service_feedback_prompt
        import mnemos_daemon

        mock_get_config.return_value = FakeConfig(
            feedback={"enabled": True, "pending_hours": 24, "pending_limit": 10},
            daemon={"services": {"feedback_prompt": True}},
        )

        pending = [MagicMock(id="r1")]
        engine = MagicMock()
        engine.get_pending_feedback.return_value = pending
        mock_get_engine.return_value = engine

        mock_bus = MagicMock()
        mnemos_daemon._event_bus_instance = mock_bus

        result = service_feedback_prompt()

        self.assertEqual(result["pending_count"], 1)
        self.assertTrue(result["prompted"])
        mock_bus.publish.assert_called_once()
        call_args = mock_bus.publish.call_args
        self.assertEqual(call_args.args[0], "feedback.prompt_due")
        self.assertEqual(call_args.kwargs["payload"]["reflection_ids"], ["r1"])

    def test_feedback_prompt_skips_when_bus_unavailable(self):
        from mnemos_daemon import service_feedback_prompt
        import mnemos_daemon

        mnemos_daemon._event_bus_instance = None
        result = service_feedback_prompt()
        self.assertFalse(result["prompted"])
        self.assertEqual(result["errors"], 0)


if __name__ == "__main__":
    unittest.main()
