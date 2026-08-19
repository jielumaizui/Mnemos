"""P3 audit unit tests for core.cli.helpers helper functions."""

from unittest.mock import MagicMock

import pytest


class FakeConfig:
    """Lightweight config stub for CLI helper tests."""

    def __init__(self, tmp_path):
        self._tmp = tmp_path
        self.data_dir = tmp_path / ".mnemos"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.wiki_dir = tmp_path / "wiki"
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir = tmp_path / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def vault_dir(self, name: str):
        if name == "mnemos":
            return self.wiki_dir
        if name == "raw":
            return self.raw_dir
        raise KeyError(name)

    def list_vaults(self):
        return ["mnemos", "raw"]


@pytest.fixture
def fake_config(tmp_path):
    return FakeConfig(tmp_path)


# ---------------------------------------------------------------------------
# core/cli/helpers.py::_check_vault_health
# ---------------------------------------------------------------------------


class TestCheckVaultHealth:
    def test_existing_writable_vault(self, monkeypatch, fake_config):
        from core.cli import helpers

        fake_config.wiki_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            helpers,
            "is_vault_registered",
            lambda path: True,
        )

        info = helpers._check_vault_health(fake_config, "mnemos")
        assert info["name"] == "mnemos"
        assert info["exists"] is True
        assert info["writable"] is True
        assert info["registered"] is True

    def test_missing_vault(self, fake_config):
        from core.cli import helpers

        info = helpers._check_vault_health(fake_config, "unknown")
        assert info["exists"] is False
        assert info["writable"] is False
        assert info["registered"] is False


# ---------------------------------------------------------------------------
# core/cli/helpers.py::_print_vault_status
# ---------------------------------------------------------------------------


class TestPrintVaultStatus:
    def test_status_output(self, monkeypatch, fake_config):
        from core.cli import helpers

        fake_config.wiki_dir.mkdir(parents=True, exist_ok=True)
        fake_config.raw_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(helpers, "is_vault_registered", lambda path: False)

        status, warnings = helpers._print_vault_status(fake_config)
        assert "mnemos" in status
        assert "raw" in status
        assert any("未注册到 Obsidian" in w for w in warnings)


# ---------------------------------------------------------------------------
# core/cli/helpers.py::_daemon_processes._looks_like_daemon_cmd
# ---------------------------------------------------------------------------


class TestDaemonProcesses:
    def test_detects_daemon_and_filters_noisy_commands(self, monkeypatch):
        from core.cli import helpers

        def fake_run(cmd, **kwargs):
            from pathlib import Path

            result = MagicMock()
            result.returncode = 0
            if Path(cmd[0]).name == "pgrep":
                result.stdout = "123\n456\n"
            elif Path(cmd[0]).name == "ps":
                pid = cmd[2]
                if pid == "123":
                    result.stdout = "python3 /path/mnemos_daemon.py start"
                elif pid == "456":
                    result.stdout = "sh -c grep mnemos_daemon.py"
            return result

        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("subprocess.run", fake_run)

        procs = helpers._daemon_processes()
        assert len(procs) == 1
        assert "123" in procs[0]
        assert "mnemos_daemon.py" in procs[0]

    def test_returns_empty_when_pgrep_fails(self, monkeypatch):
        from core.cli import helpers

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 2
            result.stdout = ""
            return result

        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("subprocess.run", fake_run)

        assert helpers._daemon_processes() == []
