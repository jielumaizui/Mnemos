"""Tests for scripts/health_check.py helpers."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from scripts.health_check import (
    _check_sensitive_permissions,
    _filter_sensitive,
    _git_diff_summary,
    _git_last_commit,
    _git_uncommitted_files,
    _git_untracked_files,
    _permission_repair_actions,
    _run_git,
    check_security,
    check_git_uncommitted,
)


class TestRunGit:
    def test_run_git_passes_args(self):
        with patch("scripts.health_check.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock()
            _run_git(["log", "-1"])
            args, kwargs = mock_run.call_args
            assert args[0] == ["git", "log", "-1"]
            assert kwargs["capture_output"] is True
            assert kwargs["text"] is True


class TestFilterSensitive:
    def test_filters_by_prefix_and_exact(self):
        names = ["config/main.json", "core/foo.py", "README.md", "config/secrets.yaml"]
        sensitive = _filter_sensitive(names)
        assert "config/main.json" in sensitive
        assert "config/secrets.yaml" in sensitive
        assert "README.md" not in sensitive


class TestGitHelpers:
    def test_last_commit_returns_stdout(self):
        with patch("scripts.health_check._run_git") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc123 2026-01-01\n")
            assert _git_last_commit() == "abc123 2026-01-01"

    def test_last_commit_empty_on_failure(self):
        with patch("scripts.health_check._run_git") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _git_last_commit() == ""

    def test_uncommitted_files_parses_output(self):
        with patch("scripts.health_check._run_git") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="a.py\n b.py\n\n")
            assert _git_uncommitted_files() == ["a.py", "b.py"]

    def test_uncommitted_files_raises_on_failure(self):
        with patch("scripts.health_check._run_git") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="bad")
            with pytest.raises(RuntimeError, match="git diff failed"):
                _git_uncommitted_files()

    def test_diff_summary_truncates(self):
        with patch("scripts.health_check._run_git") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="x" * 600)
            assert len(_git_diff_summary(["a.py"])) == 500

    def test_untracked_files_returns_list(self):
        with patch("scripts.health_check._run_git") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="new.py\n")
            assert _git_untracked_files() == ["new.py"]


class TestCheckGitUncommitted:
    def test_returns_ok_when_clean(self):
        with patch("scripts.health_check._git_last_commit", return_value="abc"):
            with patch("scripts.health_check._git_uncommitted_files", return_value=[]):
                with patch("scripts.health_check._git_untracked_files", return_value=[]):
                    result = check_git_uncommitted()
        assert result["status"] == "ok"
        assert result["uncommitted_files"] == []

    def test_returns_warning_for_sensitive_uncommitted(self):
        with patch("scripts.health_check._git_last_commit", return_value="abc"):
            with patch(
                "scripts.health_check._git_uncommitted_files",
                return_value=["config/main.json"],
            ):
                with patch("scripts.health_check._git_untracked_files", return_value=[]):
                    with patch("scripts.health_check._git_diff_summary", return_value="summary"):
                        result = check_git_uncommitted()
        assert result["status"] == "warning"
        assert result["uncommitted_files"] == ["config/main.json"]
        assert result["diff_summary"] == "summary"

    def test_returns_error_on_exception(self):
        with patch(
            "scripts.health_check._git_last_commit",
            side_effect=OSError("git not found"),
        ):
            result = check_git_uncommitted()
        assert result["status"] == "error"
        assert "git not found" in result["error"]


class TestSecurityHealth:
    def test_sensitive_permissions_detect_group_or_other_bits(self, tmp_path):
        logs_dir = tmp_path / "logs"
        config_file = tmp_path / "configs" / "main.json"
        logs_dir.mkdir()
        config_file.parent.mkdir()
        config_file.write_text("{}", encoding="utf-8")
        logs_dir.chmod(0o755)
        config_file.chmod(0o644)

        violations = _check_sensitive_permissions([logs_dir, config_file])

        assert f"{logs_dir}: dir mode=0o755" in violations
        assert f"{config_file}: file mode=0o644" in violations
        assert _permission_repair_actions(violations) == [
            f"chmod 700 {logs_dir}",
            f"chmod 600 {config_file}",
        ]

    def test_check_security_classifies_keyring_unavailable_warning(
        self, tmp_path, monkeypatch
    ):
        data_dir = tmp_path / "data"
        database_dir = tmp_path / "db"
        config_path = data_dir / "configs" / "main.json"
        for directory in (
            data_dir,
            database_dir,
            data_dir / "logs",
            database_dir / "logs",
            config_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
        config_path.write_text("{}", encoding="utf-8")
        config_path.chmod(0o600)
        cfg = SimpleNamespace(
            config_path=config_path,
            database_dir=database_dir,
            data_dir=data_dir,
            _data={
                "llm": {"api_key_source": "env:MNEMOS_LLM_API_KEY"},
                "security": {"accept_env_secret_fallback": False},
            },
            get=lambda key, default=None: default,
        )
        monkeypatch.setattr("scripts.health_check._scan_for_pickle", lambda _roots: [])
        monkeypatch.setattr("scripts.health_check._scan_for_weak_hash", lambda _roots: [])
        monkeypatch.setattr(
            "scripts.health_check._probe_keyring",
            lambda: {
                "available": False,
                "backend": None,
                "error": "ModuleNotFoundError: No module named 'keyring'",
            },
        )

        result = check_security(config=cfg)

        assert result["status"] == "warning"
        assert result["permission_violations"] == []
        assert result["keyring_available"] is False
        assert result["keyring_status"] == "warning"
        assert result["keyring_risk_level"] == "safe_but_not_best"
        assert result["keyring_safe_but_not_best"] is True
        assert result["keyring_env_fallback_accepted"] is False
        assert result["keyring_requires_user_choice"] is True
        assert "ModuleNotFoundError" in result["keyring_error"]
        assert any("safe for this config but not best" in warning for warning in result["warnings"])
        assert any("Keychain" in action for action in result["repair_actions"])

    def test_check_security_reports_accepted_env_fallback(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        database_dir = tmp_path / "db"
        config_path = data_dir / "configs" / "main.json"
        for directory in (
            data_dir,
            database_dir,
            data_dir / "logs",
            database_dir / "logs",
            config_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
        config_path.write_text("{}", encoding="utf-8")
        config_path.chmod(0o600)

        config_data = {
            "llm": {"api_key_source": "env:MNEMOS_LLM_API_KEY"},
            "security": {"accept_env_secret_fallback": True},
        }

        def cfg_get(key, default=None):
            value = config_data
            for part in key.split("."):
                if not isinstance(value, dict) or part not in value:
                    return default
                value = value[part]
            return value

        cfg = SimpleNamespace(
            config_path=config_path,
            database_dir=database_dir,
            data_dir=data_dir,
            _data=config_data,
            get=cfg_get,
        )
        monkeypatch.setattr("scripts.health_check._scan_for_pickle", lambda _roots: [])
        monkeypatch.setattr("scripts.health_check._scan_for_weak_hash", lambda _roots: [])
        monkeypatch.setattr(
            "scripts.health_check._probe_keyring",
            lambda: {
                "available": False,
                "backend": None,
                "error": "ModuleNotFoundError: No module named 'keyring'",
            },
        )

        result = check_security(config=cfg)

        assert result["status"] == "warning"
        assert result["keyring_status"] == "accepted"
        assert result["keyring_env_fallback_accepted"] is True
        assert result["keyring_requires_user_choice"] is False
        assert any("explicitly accepted" in warning for warning in result["warnings"])

    def test_check_security_reports_secret_inventory_without_leaking_values(
        self, tmp_path, monkeypatch
    ):
        data_dir = tmp_path / "data"
        database_dir = tmp_path / "db"
        config_path = data_dir / "configs" / "main.json"
        for directory in (
            data_dir,
            database_dir,
            data_dir / "logs",
            database_dir / "logs",
            config_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
        config_data = {
            "memos": {"token": "legacy-token-that-must-not-leak"},
            "distill": {"token_budget_total": 16000},
            "llm": {"api_key": "env:MNEMOS_LLM_API_KEY"},
            "service": {"bearer": "Bearer secret-that-must-not-leak"},
            "credentials": {"primary": "credential-secret-that-must-not-leak"},
        }
        config_path.write_text("{}", encoding="utf-8")
        config_path.chmod(0o600)
        cfg = SimpleNamespace(
            config_path=config_path,
            database_dir=database_dir,
            data_dir=data_dir,
            _data=config_data,
        )
        monkeypatch.setattr("scripts.health_check._scan_for_pickle", lambda _roots: [])
        monkeypatch.setattr("scripts.health_check._scan_for_weak_hash", lambda _roots: [])
        monkeypatch.setattr(
            "scripts.health_check._probe_keyring",
            lambda: {"available": True, "backend": "test", "error": None},
        )

        result = check_security(config=cfg)

        assert result["status"] == "warning"
        inventory = result["secret_inventory"]
        assert inventory["plaintext_count"] == 3
        paths = {item["path"] for item in inventory["findings"]}
        assert "memos.token" in paths
        assert "service.bearer" in paths
        assert "credentials.primary" in paths
        assert "distill.token_budget_total" not in paths
        assert result["plaintext_api_key_risks"] == []
        assert "legacy-token-that-must-not-leak" not in json.dumps(result)
        assert "secret-that-must-not-leak" not in json.dumps(result)
        assert "credential-secret-that-must-not-leak" not in json.dumps(result)
