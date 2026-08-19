"""Production AgentSource runtime must keep Raw capture out of semantic work."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

from daemon import agent_source_runtime


class _Config:
    def __init__(self, root: Path):
        self.database_dir = root

    def get(self, _key: str, default=None):
        return default


class _RawSync:
    def __init__(self) -> None:
        self.kwargs = {}

    @staticmethod
    def continuous_sync_limits(_cfg):
        return {
            "tail_sessions_per_source": 1,
            "reconciliation_sessions_per_source": 1,
            "turns_per_session": 1,
        }

    def run_service(self, _log_service_error, **kwargs):
        self.kwargs = kwargs
        return {"errors": 0}


def test_missing_persisted_coverage_uses_the_only_uninitialized_heartbeat_state(
    tmp_path: Path,
) -> None:
    assert agent_source_runtime.persisted_source_coverage(tmp_path) is None


def test_scheduled_raw_sync_injects_the_raw_only_engine(tmp_path: Path):
    cfg = _Config(tmp_path)
    raw_sync = _RawSync()
    constructor = Mock(return_value=Mock())

    with (
        patch("core.config.get_config", return_value=cfg),
        patch("daemon.raw_only_sync_engine.RawOnlySyncEngine", constructor),
    ):
        result = agent_source_runtime.run_raw_sync(
            raw_sync=raw_sync,
            log_service_error=lambda _service, _error: None,
        )
        raw_sync.kwargs["engine_factory"]()

    assert result == {"errors": 0}
    constructor.assert_called_once_with(config=cfg)


def test_trigger_accelerator_injects_the_same_raw_only_engine(tmp_path: Path):
    cfg = _Config(tmp_path)
    raw_sync = _RawSync()
    constructor = Mock(return_value=Mock())
    captured = {}

    def trigger_sync(*_args, **kwargs):
        captured.update(kwargs)

    with patch("daemon.raw_only_sync_engine.RawOnlySyncEngine", constructor):
        agent_source_runtime.sync_dirty_sources(
            ["codex"],
            cfg,
            raw_sync=raw_sync,
            trigger_sync=trigger_sync,
            log_service_error=lambda _service, _error: None,
            log=Mock(),
        )
        captured["engine_factory"]()

    constructor.assert_called_once_with(config=cfg)
    assert captured["cursor_store"].path == tmp_path / "agent_sync_cursors.db"
    assert captured["continuous_sync_limits"]() == {
        "tail_sessions_per_source": 1,
        "reconciliation_sessions_per_source": 1,
        "turns_per_session": 1,
    }
