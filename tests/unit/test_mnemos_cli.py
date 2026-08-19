"""
Mnemos CLI 单元测试 — 覆盖辅助函数、cmd_* 命令和 main() 入口。

测试策略：
- 纯辅助函数：直接测试各种输入输出
- cmd_* 函数：用 monkeypatch 隔离外部调用，capsys 捕获输出
- main()：测试 argparse 关键命令路径
- 外部依赖（subprocess.run、sqlite3、get_config、各模块函数）均用 monkeypatch 替换

注意：mnemos_cli 中大量 import 是函数内局部 import，因此：
  1. 标准库函数（subprocess.run、shutil.which 等）直接 patch 全局模块
  2. 项目内模块通过 monkeypatch.setitem(sys.modules, ...) 注入 mock
"""

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import mnemos_cli

# ---------------------------------------------------------------------------
# FakeConfig fixture
# ---------------------------------------------------------------------------


class FakeConfig:
    """用于测试的轻量配置对象。"""

    def __init__(self, tmp_path):
        self._tmp = tmp_path
        self.config_path = tmp_path / "configs" / "main.json"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir = tmp_path / ".mnemos"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.mnemos_dir = self.data_dir
        self.database_dir = self.data_dir
        self.wiki_dir = tmp_path / "wiki"
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        self.storage_backend = "obsidian"
        self.l1_storage_enabled = True
        self.l1_storage_token = (
            "fake-token"  # noqa: Vulture - keep FakeConfig aligned with legacy L1 config shape.
        )
        self.l1_storage_api_url = "http://localhost:8080"  # noqa: Vulture - keep FakeConfig aligned with legacy L1 config shape.
        self.persona_enabled = True
        self.claude_code_enabled = False
        self.mcp_enabled = False
        self.claude_settings_path = tmp_path / "settings.json"
        self._store = {}

    def get(self, key, default=None):
        parts = key.split(".")
        val = self._store
        for p in parts:
            if isinstance(val, dict) and p in val:
                val = val[p]
            else:
                return default
        return val

    def set(self, key, value):
        parts = key.split(".")
        d = self._store
        for p in parts[:-1]:
            if p not in d:
                d[p] = {}
            d = d[p]
        d[parts[-1]] = value

    def to_dict(self):
        return dict(self._store)

    def save(self):
        self.config_path.write_text(json.dumps(self._store), encoding="utf-8")

    @property
    def obsidian_vault_path(self):
        return self.wiki_dir.parent.parent / "raw"

    @property
    def persona_data_sources(self):
        return {
            "session": {"enabled": True, "description": "会话数据"},
            "git": {"enabled": False, "description": "Git 数据"},
        }

    def vault_dir(self, name: str):
        if name == "mnemos":
            return self.wiki_dir
        if name == "raw":
            return self.obsidian_vault_path
        raise KeyError(name)

    def list_vaults(self):
        return ["mnemos", "raw"]

    @property
    def cognitive_graph_enabled(self):
        return True

    @property
    def cognitive_graph_db_path(self):
        return self.data_dir / "cognitive_graph.db"


@pytest.fixture
def fake_config(tmp_path, monkeypatch):
    """返回 FakeConfig 并 patch get_config。"""
    cfg = FakeConfig(tmp_path)
    monkeypatch.setattr(mnemos_cli, "get_config", lambda: cfg)
    import core.config as _config_mod

    monkeypatch.setattr(_config_mod, "get_config", lambda: cfg)
    return cfg


# ===========================================================================
# 1. 纯辅助函数
# ===========================================================================


class TestFormatBytes:
    def test_bytes(self):
        assert mnemos_cli._format_bytes(0) == "0B"
        assert mnemos_cli._format_bytes(512) == "512B"

    def test_kilobytes(self):
        assert mnemos_cli._format_bytes(1024) == "1.0KB"
        assert mnemos_cli._format_bytes(1536) == "1.5KB"

    def test_megabytes(self):
        assert mnemos_cli._format_bytes(1024 * 1024) == "1.0MB"
        assert mnemos_cli._format_bytes(2.5 * 1024 * 1024) == "2.5MB"

    def test_gigabytes(self):
        assert mnemos_cli._format_bytes(1024**3) == "1.0GB"
        assert mnemos_cli._format_bytes(3.5 * 1024**3) == "3.5GB"

    def test_large_goes_to_gb(self):
        assert mnemos_cli._format_bytes(1024**4).endswith("GB")


class TestCompressRanges:
    def test_empty(self):
        assert mnemos_cli._compress_ranges([]) == ""

    def test_single(self):
        assert mnemos_cli._compress_ranges([5]) == "5"

    def test_consecutive(self):
        assert mnemos_cli._compress_ranges([1, 2, 3]) == "1-3"

    def test_non_consecutive(self):
        assert mnemos_cli._compress_ranges([1, 3, 5]) == "1,3,5"

    def test_mixed(self):
        assert mnemos_cli._compress_ranges([1, 2, 3, 5, 7, 8, 10]) == "1-3,5,7-8,10"

    def test_unsorted_input(self):
        # [5,1,3,2] -> sorted [1,2,3,5] -> "1-3,5"
        assert mnemos_cli._compress_ranges([5, 1, 3, 2]) == "1-3,5"


class TestGetBackfillStatus:
    def test_no_file(self, fake_config):
        assert mnemos_cli._get_backfill_status(fake_config) == {}

    def test_valid_json(self, fake_config):
        state_path = fake_config.data_dir / "backfill_state.json"
        state_path.write_text('{"status": "running"}', encoding="utf-8")
        result = mnemos_cli._get_backfill_status(fake_config)
        assert result == {"status": "running"}

    def test_invalid_json(self, fake_config):
        state_path = fake_config.data_dir / "backfill_state.json"
        state_path.write_text("not json", encoding="utf-8")
        assert mnemos_cli._get_backfill_status(fake_config) == {}


class TestWriteBackfillStatus:
    def test_writes_file(self, fake_config):
        mnemos_cli._write_backfill_status(fake_config, "done", {"agents": 2})
        state_path = fake_config.data_dir / "backfill_state.json"
        assert state_path.exists()
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["status"] == "done"
        assert data["stats"]["agents"] == 2
        assert "updated_at" in data

    def test_no_stats(self, fake_config):
        mnemos_cli._write_backfill_status(fake_config, "running")
        state_path = fake_config.data_dir / "backfill_state.json"
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["status"] == "running"
        assert "stats" not in data


class TestSqliteGroupCounts:
    def test_db_not_exists(self, tmp_path):
        db = tmp_path / "nonexistent.db"
        assert mnemos_cli._sqlite_group_counts(db, "events", "event_type, status") == []

    def test_invalid_table_name(self, tmp_path):
        db = tmp_path / "test.db"
        db.write_text("", encoding="utf-8")
        assert mnemos_cli._sqlite_group_counts(db, "evil;drop", "status") == []

    def test_invalid_group_cols(self, tmp_path):
        db = tmp_path / "test.db"
        db.write_text("", encoding="utf-8")
        assert mnemos_cli._sqlite_group_counts(db, "events", "status;drop") == []

    def test_valid_query(self, tmp_path, monkeypatch):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE events (event_type TEXT, status TEXT)")
        conn.execute("INSERT INTO events VALUES ('a', 'ok')")
        conn.execute("INSERT INTO events VALUES ('a', 'ok')")
        conn.execute("INSERT INTO events VALUES ('b', 'fail')")
        conn.commit()
        conn.close()

        real_conn = sqlite3.connect(str(db))
        monkeypatch.setattr(
            mnemos_cli,
            "sqlite_conn",
            lambda path, timeout=5: MagicMock(
                __enter__=lambda s: real_conn, __exit__=lambda *a: None
            ),
        )
        result = mnemos_cli._sqlite_group_counts(db, "events", "event_type, status")
        assert len(result) == 2
        assert result[0][0] == "a"
        assert result[0][1] == "ok"
        assert result[0][2] == 2
        real_conn.close()


# ===========================================================================
# 2. cmd_* 函数
# ===========================================================================


class TestCmdStatus:
    def test_basic_output(self, fake_config, capsys, monkeypatch):
        monkeypatch.setattr("core.config.Config", lambda **_kwargs: fake_config)
        monkeypatch.setattr("core.cli.commands.status._print_today_summary", lambda c: None)
        monkeypatch.setattr("core.cli.commands.status._print_runtime_health", lambda c: None)
        args = argparse.Namespace()
        mnemos_cli.cmd_status(args)
        captured = capsys.readouterr()
        assert "Mnemos 状态" in captured.out
        assert str(fake_config.config_path) in captured.out

    def test_with_wiki_files(self, fake_config, capsys, monkeypatch):
        monkeypatch.setattr("core.config.Config", lambda **_kwargs: fake_config)
        (fake_config.wiki_dir / "test.md").write_text("# Hello", encoding="utf-8")
        monkeypatch.setattr(mnemos_cli, "_print_today_summary", lambda c: None)
        monkeypatch.setattr(mnemos_cli, "_print_runtime_health", lambda c: None)
        args = argparse.Namespace()
        mnemos_cli.cmd_status(args)
        captured = capsys.readouterr()
        assert "Wiki 页面数:   1" in captured.out

    def test_persona_signal_lock_timeout_degrades_status(self, fake_config, capsys, monkeypatch):
        monkeypatch.setattr("core.cli.commands.status._print_today_summary", lambda c: None)
        monkeypatch.setattr("core.cli.commands.status._print_runtime_health", lambda c: None)

        def locked_signal_store():
            raise sqlite3.OperationalError("encrypted sqlite lock timeout")

        monkeypatch.setattr("core.config.Config", lambda **_kwargs: fake_config)

        args = argparse.Namespace()
        mnemos_cli.cmd_status(args)

        captured = capsys.readouterr()
        assert "画像数据库:    未初始化" in captured.out

    def test_distillation_pause_status_visible(self, fake_config, capsys, monkeypatch):
        monkeypatch.setattr("core.config.Config", lambda **_kwargs: fake_config)
        monkeypatch.setattr("core.cli.commands.status._print_today_summary", lambda c: None)
        monkeypatch.setattr("core.cli.commands.status._print_runtime_health", lambda c: None)
        monkeypatch.setattr(
            "core.hephaestus.distillation_pause.get_pause_status",
            lambda: {
                "paused": True,
                "reason": "LLM API 故障",
                "paused_at": "2026-07-01T10:00:00+00:00",
                "resume_at": "2026-07-01T10:05:00+00:00",
                "api_chain_desc": "1. siliconflow / deepseek-ai/DeepSeek-V4-Flash",
                "last_error": "all providers failed",
            },
        )

        args = argparse.Namespace()
        mnemos_cli.cmd_status(args)
        captured = capsys.readouterr()

        assert "蒸馏暂停:      是" in captured.out
        assert "原因: LLM API 故障" in captured.out
        assert "恢复时间: 2026-07-01T10:05:00+00:00" in captured.out
        assert "API 链: 1. siliconflow / deepseek-ai/DeepSeek-V4-Flash" in captured.out
        assert "最后错误: all providers failed" in captured.out

    def test_adaptive_config_metrics_summary_visible(self, fake_config, capsys, monkeypatch):
        monkeypatch.setattr("core.config.Config", lambda **_kwargs: fake_config)
        monkeypatch.setattr("core.cli.commands.status._print_today_summary", lambda c: None)
        monkeypatch.setattr("core.cli.commands.status._print_runtime_health", lambda c: None)

        class FakeAdaptiveConfig:
            def __init__(self, *args, **kwargs):
                pass

            def get_metrics_summary(self):
                return {
                    "app": {
                        "push_ignore_rate": {
                            "ewma": 0.375,
                            "trend": "up",
                            "last_value": 0.5,
                            "sample_count": 4,
                        }
                    }
                }

        monkeypatch.setattr("core.kia.adaptive_config.AdaptiveConfig", FakeAdaptiveConfig)

        args = argparse.Namespace()
        mnemos_cli.cmd_status(args)
        captured = capsys.readouterr()

        assert "自适应配置指标:" in captured.out
        assert "app.push_ignore_rate" in captured.out
        assert "ewma=0.375" in captured.out
        assert "trend=up" in captured.out
        assert "samples=4" in captured.out


class TestCmdConfig:
    def test_show_config(self, fake_config, capsys):
        fake_config.set("wiki.vault_path", "/tmp/wiki")
        args = argparse.Namespace(set=None)
        mnemos_cli.cmd_config(args)
        captured = capsys.readouterr()
        assert "vault_path" in captured.out

    def test_set_config(self, fake_config, capsys):
        args = argparse.Namespace(set="wiki.vault_path=/new/wiki")
        mnemos_cli.cmd_config(args)
        captured = capsys.readouterr()
        assert "已设置" in captured.out
        assert fake_config.get("wiki.vault_path") == "/new/wiki"

    def test_set_with_auto_type_int(self, fake_config, capsys):
        args = argparse.Namespace(set="capture.max_workers=8")
        mnemos_cli.cmd_config(args)
        assert fake_config.get("capture.max_workers") == 8

    def test_set_with_auto_type_float(self, fake_config, capsys):
        args = argparse.Namespace(set="scoring.threshold=0.5")
        mnemos_cli.cmd_config(args)
        assert fake_config.get("scoring.threshold") == 0.5

    def test_set_with_auto_type_bool(self, fake_config, capsys):
        args = argparse.Namespace(set="embedding.enabled=true")
        mnemos_cli.cmd_config(args)
        assert fake_config.get("embedding.enabled") is True


class TestCmdSecrets:
    def test_secrets_doctor_accepts_env_fallback(self, fake_config, capsys, monkeypatch):
        fake_config._data = {
            "llm": {"api_key_source": "env:MNEMOS_LLM_API_KEY"},
            "security": {"accept_env_secret_fallback": False},
        }
        monkeypatch.setattr("core.cli.commands.secrets._get_config", lambda: fake_config)
        monkeypatch.setattr(
            "core.ops.keyring_doctor.probe_keyring",
            lambda: {
                "available": False,
                "backend": None,
                "error": "ModuleNotFoundError: No module named 'keyring'",
            },
        )

        result = mnemos_cli.cmd_secrets(
            argparse.Namespace(
                secrets_cmd="doctor",
                json=True,
                accept_env_fallback=True,
            )
        )

        payload = json.loads(capsys.readouterr().out)
        assert result == 0
        assert fake_config.get("security.accept_env_secret_fallback") is True
        assert payload["status"] == "accepted"
        assert payload["env_fallback_accepted"] is True
        assert payload["applied_actions"] == ["security.accept_env_secret_fallback=true"]
        assert payload["secret_reference_counts"]["env"] == 1

    def test_secrets_doctor_refuses_env_fallback_when_plaintext_exists(
        self, fake_config, capsys, monkeypatch
    ):
        fake_config._data = {
            "llm": {"api_key": "plaintext-secret"},
            "security": {"accept_env_secret_fallback": False},
        }
        fake_config.set("security.accept_env_secret_fallback", False)
        monkeypatch.setattr("core.cli.commands.secrets._get_config", lambda: fake_config)
        monkeypatch.setattr(
            "core.ops.keyring_doctor.probe_keyring",
            lambda: {
                "available": False,
                "backend": None,
                "error": "ModuleNotFoundError: No module named 'keyring'",
            },
        )

        result = mnemos_cli.cmd_secrets(
            argparse.Namespace(
                secrets_cmd="doctor",
                json=True,
                accept_env_fallback=True,
            )
        )

        payload = json.loads(capsys.readouterr().out)
        assert result == 1
        assert fake_config.get("security.accept_env_secret_fallback") is False
        assert payload["applied_actions"] == []
        assert payload["secret_inventory_plaintext_count"] == 1
        assert any("not accepted" in warning for warning in payload["warnings"])


class TestCmdDaemon:
    def test_start(self, fake_config, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(sys, "executable", "/fake/python")
        monkeypatch.setattr(
            subprocess, "run", lambda cmd, **kw: calls.append(cmd) or MagicMock(returncode=0)
        )
        # 使用临时目录隔离，避免覆盖项目根目录的 mnemos_daemon.py
        monkeypatch.setattr(mnemos_cli, "__file__", str(tmp_path / "mnemos_cli.py"))
        daemon_script = tmp_path / "mnemos_daemon.py"
        daemon_script.write_text("# daemon stub", encoding="utf-8")

        args = argparse.Namespace(daemon_cmd="start")
        mnemos_cli.cmd_daemon(args)
        assert len(calls) == 1
        assert calls[0][-1] == "start"

    def test_stop(self, fake_config, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(sys, "executable", "/fake/python")
        monkeypatch.setattr(
            subprocess, "run", lambda cmd, **kw: calls.append(cmd) or MagicMock(returncode=0)
        )
        # 使用临时目录隔离，避免覆盖项目根目录的 mnemos_daemon.py
        monkeypatch.setattr(mnemos_cli, "__file__", str(tmp_path / "mnemos_cli.py"))
        daemon_script = tmp_path / "mnemos_daemon.py"
        daemon_script.write_text("# daemon stub", encoding="utf-8")

        args = argparse.Namespace(daemon_cmd="stop")
        mnemos_cli.cmd_daemon(args)
        assert calls[0][-1] == "stop"

    def test_missing_script(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr(mnemos_cli, "__file__", str(tmp_path / "mnemos_cli.py"))
        args = argparse.Namespace(daemon_cmd="start")
        mnemos_cli.cmd_daemon(args)
        captured = capsys.readouterr()
        assert "守护进程脚本不存在" in captured.out


class TestCmdScheduler:
    def test_install_windows(self, fake_config, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(sys, "executable", "/fake/python")
        monkeypatch.setattr(
            subprocess, "run", lambda cmd, **kw: calls.append(cmd) or MagicMock(returncode=0)
        )
        # 使用临时目录隔离，避免覆盖项目根目录的 mnemos_daemon.py
        monkeypatch.setattr(mnemos_cli, "__file__", str(tmp_path / "mnemos_cli.py"))
        daemon_script = tmp_path / "mnemos_daemon.py"
        daemon_script.write_text("# daemon stub", encoding="utf-8")

        args = argparse.Namespace(scheduler_cmd="install-windows")
        assert mnemos_cli.cmd_scheduler(args) == 0
        assert len(calls) == 1
        assert "install-windows" in calls[0]

    def test_install_windows_propagates_failure(self, fake_config, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "executable", "/fake/python")
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: MagicMock(returncode=7))
        monkeypatch.setattr(mnemos_cli, "__file__", str(tmp_path / "mnemos_cli.py"))
        daemon_script = tmp_path / "mnemos_daemon.py"
        daemon_script.write_text("# daemon stub", encoding="utf-8")

        args = argparse.Namespace(scheduler_cmd="install-windows")
        assert mnemos_cli.cmd_scheduler(args) == 7

    def test_unknown_cmd(self, fake_config, capsys, monkeypatch):
        monkeypatch.setattr(mnemos_cli, "__file__", str(fake_config._tmp / "mnemos_cli.py"))
        daemon_script = fake_config._tmp / "mnemos_daemon.py"
        daemon_script.write_text("# daemon stub", encoding="utf-8")

        args = argparse.Namespace(scheduler_cmd="unknown")
        assert mnemos_cli.cmd_scheduler(args) == 2
        captured = capsys.readouterr()
        assert "可用子命令" in captured.out

    def test_status_prints_registered_steps(self, fake_config, capsys, monkeypatch):
        mock_scheduler = MagicMock()
        mock_scheduler.DB_PATH = fake_config.data_dir / "live_sync.db"
        mock_scheduler.register_all_default_steps = MagicMock()
        mock_scheduler.get_step_status.return_value = {
            "knowledge_immune": {
                "trigger": "cron:0 2 * * *",
                "enabled": True,
                "consecutive_failures": 0,
                "timeout": 300,
                "deps": [],
            }
        }
        mock_mod = MagicMock(KnowledgeScheduler=lambda: mock_scheduler)
        monkeypatch.setitem(sys.modules, "core.kia.chronos", mock_mod)

        args = argparse.Namespace(scheduler_cmd="status")
        mnemos_cli.cmd_scheduler(args)
        captured = capsys.readouterr()

        assert "KIA 调度步骤状态" in captured.out
        assert "knowledge_immune" in captured.out
        assert "cron:0 2 * * *" in captured.out

    def test_status_prints_live_last_results(self, fake_config, capsys, monkeypatch):
        mock_scheduler = MagicMock()
        mock_scheduler.DB_PATH = fake_config.data_dir / "live_sync.db"
        mock_scheduler.register_all_default_steps = MagicMock()
        mock_scheduler.get_step_status.return_value = {
            "knowledge_immune": {
                "trigger": "cron:0 2 * * *",
                "enabled": True,
                "consecutive_failures": 0,
                "timeout": 300,
                "deps": [],
            }
        }
        mock_scheduler.get_last_results.return_value = {
            "knowledge_immune": {"status": "deferred", "error": "budget"}
        }
        mock_mod = MagicMock(KnowledgeScheduler=lambda: mock_scheduler)
        monkeypatch.setitem(sys.modules, "core.kia.chronos", mock_mod)

        args = argparse.Namespace(scheduler_cmd="status")
        mnemos_cli.cmd_scheduler(args)
        captured = capsys.readouterr()

        assert "live_status=deferred" in captured.out
        assert "live_error=budget" in captured.out

    def test_list_prints_registered_steps(self, fake_config, capsys, monkeypatch):
        mock_scheduler = MagicMock()
        mock_scheduler.DB_PATH = fake_config.data_dir / "live_sync.db"
        mock_scheduler.register_all_default_steps = MagicMock()
        mock_scheduler.get_step_status.return_value = {
            "shadow_page": {
                "trigger": "cron:0 7 * * 0",
                "enabled": True,
                "consecutive_failures": 0,
                "timeout": 600,
                "deps": ["knowledge_profile"],
            }
        }
        mock_mod = MagicMock(KnowledgeScheduler=lambda: mock_scheduler)
        monkeypatch.setitem(sys.modules, "core.kia.chronos", mock_mod)

        args = argparse.Namespace(scheduler_cmd="list")
        mnemos_cli.cmd_scheduler(args)
        captured = capsys.readouterr()

        assert "KIA 调度步骤列表" in captured.out
        assert "shadow_page" in captured.out
        assert "knowledge_profile" in captured.out

    def test_tick_dry_run_does_not_execute_steps(self, fake_config, capsys, monkeypatch):
        step_func = MagicMock()

        class Trigger:
            def is_due(self):
                return True

            def describe(self):
                return "cron:* * * * *"

        class Step:
            name = "due_step"
            trigger = Trigger()
            enabled = True
            deps = []
            timeout = 60
            consecutive_failures = 0
            func = step_func

        mock_scheduler = MagicMock()
        mock_scheduler.DB_PATH = fake_config.data_dir / "live_sync.db"
        mock_scheduler.steps = {"due_step": Step()}
        mock_scheduler.register_all_default_steps = MagicMock()
        mock_scheduler._topological_sort = lambda steps: steps
        mock_scheduler.tick = MagicMock(return_value={"due_step": {"status": "ok"}})
        mock_mod = MagicMock(KnowledgeScheduler=lambda: mock_scheduler)
        monkeypatch.setitem(sys.modules, "core.kia.chronos", mock_mod)

        args = argparse.Namespace(scheduler_cmd="tick", dry_run=True)
        mnemos_cli.cmd_scheduler(args)
        captured = capsys.readouterr()

        assert "dry-run" in captured.out
        assert "would_run" in captured.out
        mock_scheduler.tick.assert_not_called()
        step_func.assert_not_called()

    def test_reminders_json_uses_check_reminders(self, capsys, monkeypatch):
        from core.kia.chronos import ScheduledTask

        task = ScheduledTask(
            task_id="review-wiki-1",
            task_type="review",
            subtype="wiki",
            due_date="2026-07-02T09:00:00",
            reminder_date="2026-07-01T09:00:00",
            is_periodic=False,
            period=None,
            status="pending",
            context="review page",
            created_at="2026-07-01T08:00:00",
            reminded_at="2026-07-01T10:00:00",
            priority=5,
        )
        mock_mod = MagicMock(check_reminders=lambda: [task])
        monkeypatch.setitem(sys.modules, "core.kia.chronos", mock_mod)

        args = argparse.Namespace(scheduler_cmd="reminders", json=True)
        assert mnemos_cli.cmd_scheduler(args) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "count": 1,
            "reminders": [
                {
                    "task_id": "review-wiki-1",
                    "task_type": "review",
                    "subtype": "wiki",
                    "due_date": "2026-07-02T09:00:00",
                    "reminder_date": "2026-07-01T09:00:00",
                    "status": "pending",
                    "context": "review page",
                    "reminded_at": "2026-07-01T10:00:00",
                    "priority": 5,
                }
            ],
        }

    def test_schedule_uses_schedule_task_helper(self, capsys, monkeypatch):
        calls = []

        def fake_schedule_task(
            task_type,
            subtype,
            due_date,
            context="",
            is_periodic=False,
            period=None,
            priority=0,
        ):
            calls.append(
                {
                    "task_type": task_type,
                    "subtype": subtype,
                    "due_date": due_date,
                    "context": context,
                    "is_periodic": is_periodic,
                    "period": period,
                    "priority": priority,
                }
            )
            return "review-wiki-20260710-test"

        mock_mod = MagicMock(schedule_task=fake_schedule_task)
        monkeypatch.setitem(sys.modules, "core.kia.chronos", mock_mod)

        args = argparse.Namespace(
            scheduler_cmd="schedule",
            task_type="review",
            subtype="wiki",
            due_date="2026-07-10T09:30:00",
            context="weekly review",
            periodic=True,
            period="weekly",
            priority=7,
        )
        assert mnemos_cli.cmd_scheduler(args) == 0

        assert len(calls) == 1
        assert calls[0]["task_type"] == "review"
        assert calls[0]["subtype"] == "wiki"
        assert calls[0]["due_date"].isoformat() == "2026-07-10T09:30:00"
        assert calls[0]["context"] == "weekly review"
        assert calls[0]["is_periodic"] is True
        assert calls[0]["period"] == "weekly"
        assert calls[0]["priority"] == 7
        assert "review-wiki-20260710-test" in capsys.readouterr().out


class TestCmdScorer:
    def test_status_no_steps(self, fake_config, capsys, monkeypatch):
        mock_scheduler = MagicMock()
        mock_scheduler.register_all_default_steps = MagicMock()
        mock_scheduler.get_step_status.return_value = {}
        mock_mod = MagicMock(KnowledgeScheduler=lambda: mock_scheduler)
        monkeypatch.setitem(sys.modules, "core.kia.chronos", mock_mod)

        args = argparse.Namespace(scorer_cmd="status")
        mnemos_cli.cmd_scorer(args)
        captured = capsys.readouterr()
        assert "KIA 调度步骤状态" in captured.out

    def test_retrain(self, fake_config, capsys):
        args = argparse.Namespace(scorer_cmd="retrain")
        mnemos_cli.cmd_scorer(args)
        captured = capsys.readouterr()
        assert "旧 scorer retrain 已停用" in captured.out
        assert "canonical TrainingGovernanceStore" in captured.out

    def test_rollback(self, fake_config, capsys):
        args = argparse.Namespace(scorer_cmd="rollback")
        mnemos_cli.cmd_scorer(args)
        captured = capsys.readouterr()
        assert "旧 scorer rollback 已停用" in captured.out
        assert "不会重新激活 legacy 版本" in captured.out

    def test_unknown(self, fake_config, capsys):
        args = argparse.Namespace(scorer_cmd="unknown")
        mnemos_cli.cmd_scorer(args)
        captured = capsys.readouterr()
        assert "用法" in captured.out


class TestCmdSync:
    def test_status_no_db(self, fake_config, capsys):
        args = argparse.Namespace(sync_cmd="status")
        mnemos_cli.cmd_sync(args)
        captured = capsys.readouterr()
        assert "同步数据库不存在" in captured.out

    def test_status_with_db(self, fake_config, capsys, monkeypatch):
        db_path = fake_config.data_dir / "sync_log.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE sync_log (agent_name TEXT, synced_at TEXT)")
        conn.execute("INSERT INTO sync_log VALUES ('claude', datetime('now'))")
        conn.commit()
        conn.close()

        real_conn = sqlite3.connect(str(db_path))
        monkeypatch.setattr(
            mnemos_cli,
            "sqlite_conn",
            lambda path, timeout=10: MagicMock(
                __enter__=lambda s: real_conn, __exit__=lambda *a: None
            ),
        )

        args = argparse.Namespace(sync_cmd="status")
        mnemos_cli.cmd_sync(args)
        captured = capsys.readouterr()
        assert "claude" in captured.out
        real_conn.close()

    def test_retry_failed(self, fake_config, capsys, monkeypatch):
        mock_engine = MagicMock()
        mock_engine.retry_failed.return_value = {"retried": 5}
        mock_mod = MagicMock(SyncEngine=lambda: mock_engine)
        monkeypatch.setitem(sys.modules, "core.sync_framework.sync_engine", mock_mod)

        args = argparse.Namespace(sync_cmd="retry-failed")
        mnemos_cli.cmd_sync(args)
        captured = capsys.readouterr()
        assert "重试完成" in captured.out

    def test_unknown_cmd(self, fake_config, capsys):
        args = argparse.Namespace(sync_cmd="unknown")
        mnemos_cli.cmd_sync(args)
        captured = capsys.readouterr()
        assert "用法" in captured.out


class TestCmdEvents:
    def test_stats_no_db(self, fake_config, capsys):
        args = argparse.Namespace(events_cmd="stats")
        mnemos_cli.cmd_events(args)
        captured = capsys.readouterr()
        assert "events.db 不存在" in captured.out

    def test_stats_with_db(self, fake_config, capsys, monkeypatch):
        db_path = fake_config.data_dir / "events.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE events (event_type TEXT, status TEXT)")
        conn.execute("INSERT INTO events VALUES ('test', 'pending')")
        conn.execute("CREATE TABLE dead_letters (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        real_conn = sqlite3.connect(str(db_path))
        monkeypatch.setattr(
            mnemos_cli,
            "sqlite_conn",
            lambda path, timeout=5: MagicMock(
                __enter__=lambda s: real_conn, __exit__=lambda *a: None
            ),
        )

        args = argparse.Namespace(events_cmd="stats")
        mnemos_cli.cmd_events(args)
        captured = capsys.readouterr()
        assert "总数:" in captured.out
        real_conn.close()

    def test_cleanup_dry_run(self, fake_config, capsys, monkeypatch):
        db_path = fake_config.data_dir / "events.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE events (status TEXT, created_at TEXT)")
        conn.execute("INSERT INTO events VALUES ('done', datetime('now', '-10 days'))")
        conn.execute("CREATE TABLE dead_letters (timestamp TEXT)")
        conn.execute("INSERT INTO dead_letters VALUES (datetime('now', '-40 days'))")
        conn.commit()
        conn.close()

        real_conn = sqlite3.connect(str(db_path))
        monkeypatch.setattr(
            mnemos_cli,
            "sqlite_conn",
            lambda path, timeout=10: MagicMock(
                __enter__=lambda s: real_conn, __exit__=lambda *a: None
            ),
        )

        args = argparse.Namespace(events_cmd="cleanup", confirm=False)
        mnemos_cli.cmd_events(args)
        captured = capsys.readouterr()
        assert "dry-run" in captured.out
        assert "未指定 --confirm" in captured.out
        real_conn.close()

    def test_archive_orphans(self, fake_config, capsys, monkeypatch):
        mock_bus = MagicMock()
        mock_bus.archive_no_consumer_events.return_value = 3
        mock_mod = MagicMock(_get_bus=lambda: mock_bus)
        monkeypatch.setitem(sys.modules, "core.mnemos_bus", mock_mod)

        args = argparse.Namespace(events_cmd="archive-orphans")
        mnemos_cli.cmd_events(args)
        captured = capsys.readouterr()
        assert "归档完成: 3 个" in captured.out

    def test_replay_dead_letter_by_trace_id(self, fake_config, capsys, monkeypatch):
        mock_bus = MagicMock()
        mock_bus.replay_dead_letter.return_value = True
        mock_mod = MagicMock(_get_bus=lambda: mock_bus)
        monkeypatch.setitem(sys.modules, "core.mnemos_bus", mock_mod)

        args = argparse.Namespace(
            events_cmd="replay", trace_id="trace-1", event_types=[], limit=100
        )
        mnemos_cli.cmd_events(args)
        captured = capsys.readouterr()

        mock_bus.replay_dead_letter.assert_called_once_with("trace-1")
        assert "trace_id=trace-1" in captured.out

    def test_replay_no_consumer_dead_letters(self, fake_config, capsys, monkeypatch):
        mock_bus = MagicMock()
        mock_bus.replay_no_consumer_dead_letters.return_value = 2
        mock_mod = MagicMock(_get_bus=lambda: mock_bus)
        monkeypatch.setitem(sys.modules, "core.mnemos_bus", mock_mod)

        args = argparse.Namespace(
            events_cmd="replay",
            trace_id="",
            no_consumer=True,
            event_types=["wiki_page_updated"],
            limit=3,
        )
        mnemos_cli.cmd_events(args)
        captured = capsys.readouterr()

        mock_bus.replay_no_consumer_dead_letters.assert_called_once_with(
            event_types=["wiki_page_updated"],
            limit=3,
        )
        assert "重放完成: 2 个 no_consumer" in captured.out

    def test_unknown_cmd(self, fake_config, capsys):
        args = argparse.Namespace(events_cmd="unknown")
        mnemos_cli.cmd_events(args)
        captured = capsys.readouterr()
        assert "用法" in captured.out
        assert "stats" in captured.out
        assert "cleanup" in captured.out
        assert "archive-orphans" in captured.out
        assert "replay" in captured.out


class TestCmdDistill:
    def test_amphora_status_rejects_a_leaf_symlink(self, tmp_path):
        from core.cli.commands.distill import _amphora_status_counts

        target = tmp_path / "distill_queue.real.db"
        with sqlite3.connect(target) as connection:
            connection.execute("CREATE TABLE distillation_tasks(status TEXT)")
            connection.execute("INSERT INTO distillation_tasks VALUES ('pending')")
        link = tmp_path / "distill_queue.db"
        link.symlink_to(target)

        result = _amphora_status_counts(link)

        assert result["state"] == "unavailable"
        assert result["total"] == 0
        assert result["error"] == "distill_queue_path_not_regular"

    def test_amphora_status_reports_corrupt_database_unavailable(self, tmp_path):
        from core.cli.commands.distill import _amphora_status_counts

        path = tmp_path / "distill_queue.db"
        path.write_bytes(b"not sqlite")

        result = _amphora_status_counts(path)

        assert result["state"] == "unavailable"
        assert result["total"] == 0
        assert result["error"] == "distill_queue_unreadable"

    def test_status_shows_daemon_and_drain_suggestions(self, fake_config, capsys, monkeypatch):
        monkeypatch.setattr("core.config.Config", lambda **_kwargs: fake_config)
        monkeypatch.setattr(
            "core.cli.commands.distill._distill_status_snapshot",
            lambda _config: {
                "pending": 12,
                "delegated": 0,
                "queue_dir": "/tmp/q",
                "inbox_dir": "/tmp/inbox",
            },
        )
        monkeypatch.setattr("core.cli.commands.distill._amphora_status_counts", lambda _path: {})
        monkeypatch.setattr("core.cli.helpers._daemon_processes", lambda: [])

        args = argparse.Namespace(distill_cmd="status")
        mnemos_cli.cmd_distill(args)
        captured = capsys.readouterr()

        assert "蒸馏队列状态" in captured.out
        assert "pending: 12" in captured.out
        assert "daemon start" in captured.out
        assert "distill drain --limit 5" in captured.out

    def test_status_redacts_paths_by_default(self, fake_config, capsys, monkeypatch):
        home_queue = Path.home() / ".mnemos" / "distill_queue"
        home_inbox = Path.home() / "Documents" / "MnemosVault" / "00-Inbox"

        monkeypatch.setattr("core.config.Config", lambda **_kwargs: fake_config)
        monkeypatch.setattr(
            "core.cli.commands.distill._distill_status_snapshot",
            lambda _config: {
                "pending": 0,
                "delegated": 0,
                "queue_dir": home_queue,
                "inbox_dir": home_inbox,
            },
        )
        monkeypatch.setattr("core.cli.commands.distill._amphora_status_counts", lambda _path: {})
        monkeypatch.setattr("core.cli.helpers._daemon_processes", lambda: [])

        args = argparse.Namespace(distill_cmd="status")
        mnemos_cli.cmd_distill(args)
        captured = capsys.readouterr()

        assert str(Path.home()) not in captured.out
        assert "<HOME>" in captured.out

    def test_status_can_show_paths_for_unsafe_debug(self, fake_config, capsys, monkeypatch):
        home_queue = Path.home() / ".mnemos" / "distill_queue"

        monkeypatch.setattr("core.config.Config", lambda **_kwargs: fake_config)
        monkeypatch.setattr(
            "core.cli.commands.distill._distill_status_snapshot",
            lambda _config: {
                "pending": 0,
                "delegated": 0,
                "queue_dir": home_queue,
                "inbox_dir": home_queue / "inbox",
            },
        )
        monkeypatch.setattr("core.cli.commands.distill._amphora_status_counts", lambda _path: {})
        monkeypatch.setattr("core.cli.helpers._daemon_processes", lambda: [])

        args = argparse.Namespace(distill_cmd="status", unsafe_debug=True)
        mnemos_cli.cmd_distill(args)
        captured = capsys.readouterr()

        assert str(home_queue) in captured.out

    def test_status_suggests_failed_task_actions(self, fake_config, capsys, monkeypatch):
        monkeypatch.setattr("core.config.Config", lambda **_kwargs: fake_config)
        monkeypatch.setattr(
            "core.cli.commands.distill._distill_status_snapshot",
            lambda _config: {
                "pending": 0,
                "delegated": 0,
                "queue_dir": "/tmp/q",
                "inbox_dir": "/tmp/i",
            },
        )
        monkeypatch.setattr(
            "core.cli.commands.distill._amphora_status_counts",
            lambda _path: {
                "total": 1,
                "pending": 0,
                "processing": 0,
                "done": 0,
                "failed": 1,
                "archived": 0,
            },
        )
        monkeypatch.setattr("core.cli.helpers._daemon_processes", lambda: ["pid"])

        args = argparse.Namespace(distill_cmd="status")
        mnemos_cli.cmd_distill(args)
        captured = capsys.readouterr()

        assert "retry-failed --all" in captured.out
        assert "archive-failed --all" in captured.out

    def test_status_suggests_reset_timeouts_for_processing_tasks(
        self, fake_config, capsys, monkeypatch
    ):
        monkeypatch.setattr("core.config.Config", lambda **_kwargs: fake_config)
        monkeypatch.setattr(
            "core.cli.commands.distill._distill_status_snapshot",
            lambda _config: {
                "pending": 0,
                "delegated": 0,
                "queue_dir": "/tmp/q",
                "inbox_dir": "/tmp/i",
            },
        )
        monkeypatch.setattr(
            "core.cli.commands.distill._amphora_status_counts",
            lambda _path: {
                "total": 1,
                "pending": 0,
                "processing": 1,
                "done": 0,
                "failed": 0,
                "archived": 0,
            },
        )
        monkeypatch.setattr("core.cli.helpers._daemon_processes", lambda: ["pid"])

        args = argparse.Namespace(distill_cmd="status")
        mnemos_cli.cmd_distill(args)
        captured = capsys.readouterr()

        assert "reset-timeouts --minutes 30 --json" in captured.out

    def test_drain_dry_run_does_not_process(self, fake_config, capsys, monkeypatch):
        class FakeWorker:
            def get_stats(self):
                return {"pending": 7}

            def process_all(self, max_tasks=None):
                raise AssertionError("dry-run must not process queue")

        monkeypatch.setattr("core.hephaestus_worker.HephaestusWorker", FakeWorker)

        args = argparse.Namespace(distill_cmd="drain", limit=5, dry_run=True)
        mnemos_cli.cmd_distill(args)
        captured = capsys.readouterr()

        assert "dry-run" in captured.out
        assert "最多 5 个任务" in captured.out

    def test_drain_processes_bounded_tasks(self, fake_config, capsys, monkeypatch):
        calls = []

        class FakeWorker:
            def get_stats(self):
                return {"pending": 7}

            def process_all(self, max_tasks=None):
                calls.append(max_tasks)
                return 3

        monkeypatch.setattr("core.hephaestus_worker.HephaestusWorker", FakeWorker)

        args = argparse.Namespace(distill_cmd="drain", limit=4, dry_run=False)
        mnemos_cli.cmd_distill(args)
        captured = capsys.readouterr()

        assert calls == [4]
        assert "processed: 3" in captured.out

    def test_retry_failed_requires_scope(self, fake_config, capsys):
        args = argparse.Namespace(
            distill_cmd="retry-failed",
            task_id="",
            all=False,
            limit=None,
            reason="",
            json=False,
        )

        result = mnemos_cli.cmd_distill(args)
        captured = capsys.readouterr()

        assert result == 1
        assert "需要指定 --task-id" in captured.out

    def test_archive_failed_delegates_to_amphora(self, fake_config, capsys, monkeypatch):
        calls = []

        def fake_archive(identifier=None, *, limit=None, reason="", config=None):
            calls.append((identifier, limit, reason, config))
            return 2

        monkeypatch.setattr("core.kia.amphora.archive_failed", fake_archive)
        args = argparse.Namespace(
            distill_cmd="archive-failed",
            task_id="task-1",
            all=False,
            limit=5,
            reason="known historical failure",
            json=False,
        )

        result = mnemos_cli.cmd_distill(args)
        captured = capsys.readouterr()

        assert result == 0
        assert calls == [
            ("task-1", 5, "known historical failure", fake_config)
        ]
        assert "已归档 failed 任务: 2" in captured.out

    def test_reset_timeouts_delegates_to_amphora(self, fake_config, capsys, monkeypatch):
        calls = []

        def fake_reset(*, timeout_minutes=30):
            calls.append(timeout_minutes)
            return 1

        monkeypatch.setattr("core.kia.amphora.reset_timeouts", fake_reset)
        args = argparse.Namespace(
            distill_cmd="reset-timeouts",
            minutes=45,
            json=False,
        )

        result = mnemos_cli.cmd_distill(args)
        captured = capsys.readouterr()

        assert result == 0
        assert calls == [45]
        assert "已重置超时 processing 蒸馏任务: 1" in captured.out

    def test_audit_no_wiki(self, fake_config, capsys):
        fake_config.wiki_dir = fake_config._tmp / "no_wiki"
        args = argparse.Namespace(distill_cmd="audit")
        mnemos_cli.cmd_distill(args)
        captured = capsys.readouterr()
        assert "Wiki 目录不存在" in captured.out

    def test_audit_with_pages(self, fake_config, capsys):
        md = fake_config.wiki_dir / "test.md"
        md.write_text(
            "---\nsource_session: s1\ntruncated: true\n---\n# Test\n",
            encoding="utf-8",
        )
        args = argparse.Namespace(distill_cmd="audit")
        mnemos_cli.cmd_distill(args)
        captured = capsys.readouterr()
        assert "截断输入页面: 1" in captured.out

    def test_backfill_metadata_updates_legacy_distilled_pages(
        self,
        fake_config,
        capsys,
    ):
        md = fake_config.wiki_dir / "legacy.md"
        md.write_text("---\nsource_session: s1\n---\n# Legacy\n", encoding="utf-8")

        args = argparse.Namespace(distill_cmd="backfill-metadata", dry_run=False, limit=None)
        mnemos_cli.cmd_distill(args)
        captured = capsys.readouterr()

        updated = md.read_text(encoding="utf-8")
        assert "已回填: 1" in captured.out
        assert "distill_prompt_version:" in updated
        assert "source_coverage: legacy_unknown" in updated
        assert "distill_input_mode: legacy_unknown" in updated

        args = argparse.Namespace(distill_cmd="audit")
        mnemos_cli.cmd_distill(args)
        captured = capsys.readouterr()
        assert "缺少 prompt_version: 0" in captured.out
        assert "缺少 source_coverage: 0" in captured.out

    def test_backfill_metadata_dry_run_does_not_write(self, fake_config, capsys):
        md = fake_config.wiki_dir / "legacy.md"
        original = "---\nsource_session: s1\n---\n# Legacy\n"
        md.write_text(original, encoding="utf-8")

        args = argparse.Namespace(distill_cmd="backfill-metadata", dry_run=True, limit=None)
        mnemos_cli.cmd_distill(args)
        captured = capsys.readouterr()

        assert "将回填: 1" in captured.out
        assert md.read_text(encoding="utf-8") == original

    def test_unknown_cmd(self, fake_config, capsys):
        args = argparse.Namespace(distill_cmd="unknown")
        mnemos_cli.cmd_distill(args)
        captured = capsys.readouterr()
        assert "用法" in captured.out


class TestCmdAgent:
    def test_list_no_agents(self, fake_config, capsys, monkeypatch):
        mock_registry = MagicMock()
        mock_registry.discover_all.return_value = []
        mock_mod = MagicMock(AgentRegistry=mock_registry)
        monkeypatch.setitem(sys.modules, "integrations.olympus", mock_mod)

        args = argparse.Namespace(agent_cmd="list")
        mnemos_cli.cmd_agent(args)
        captured = capsys.readouterr()
        assert "未检测到任何 Agent" in captured.out

    def test_detect_no_host(self, fake_config, capsys, monkeypatch):
        mock_registry = MagicMock()
        mock_registry.select_best_agent.return_value = None
        mock_mod = MagicMock(AgentRegistry=mock_registry)
        monkeypatch.setitem(sys.modules, "integrations.olympus", mock_mod)

        mock_diag = MagicMock()
        storage_status = MagicMock(configured=False, reachable=False)
        wiki_status = MagicMock(exists=True, writable=True, path="/wiki")
        mock_diag.check_storage.return_value = storage_status
        mock_diag.check_wiki.return_value = wiki_status
        mock_diag.check_agents.return_value = []
        mock_diag.generate_task_list.return_value = []
        mock_diag_mod = MagicMock(ConnectionDiagnostics=mock_diag)
        monkeypatch.setitem(sys.modules, "core.diagnostics", mock_diag_mod)

        args = argparse.Namespace(agent_cmd="detect")
        mnemos_cli.cmd_agent(args)
        captured = capsys.readouterr()
        assert "未设置 MNEMOS_HOST_AGENT" in captured.out
        assert "Raw Vault" in captured.out
        mock_diag.generate_task_list.assert_called_once_with(
            wiki=wiki_status, agents=[], storage=storage_status
        )

    def test_install_no_agents(self, fake_config, capsys, monkeypatch):
        mock_registry = MagicMock()
        mock_registry.discover_all.return_value = []
        mock_mod = MagicMock(AgentRegistry=mock_registry)
        monkeypatch.setitem(sys.modules, "integrations.olympus", mock_mod)

        from core.cli.commands import agent as agent_cmd

        calls = []
        monkeypatch.setattr(agent_cmd, "_MCP_ONLY_AGENTS", frozenset({"codex", "opencode"}))
        monkeypatch.setattr(
            agent_cmd, "_install_mcp_only_agent", lambda name: calls.append(name) or True
        )

        args = argparse.Namespace(agent_cmd="install", agent_name="")
        mnemos_cli.cmd_agent(args)
        captured = capsys.readouterr()
        assert "继续安装 MCP-only 主动接入" in captured.out
        assert calls == ["codex", "opencode"]

    def test_repair_degraded_agents_and_rechecks_full_power(self, fake_config, capsys, monkeypatch):
        from core.agent_kit.report import AgentKitAgentStatus, AgentKitReport
        from core.cli.commands import agent as agent_cmd
        import core.agent_kit as agent_kit

        before = AgentKitReport(
            protocol_version="agent-kit-v2",
            target_agents=["claude"],
            workflows=[],
            agents=[
                AgentKitAgentStatus(
                    name="claude",
                    active_entrypoint="adapter",
                    installed=True,
                    full_power_gaps=["lifecycle hooks/wrapper not installed"],
                )
            ],
            missing_workflow_tools=[],
        )
        after = AgentKitReport(
            protocol_version="agent-kit-v2",
            target_agents=["claude"],
            workflows=[],
            agents=[
                AgentKitAgentStatus(
                    name="claude",
                    active_entrypoint="adapter",
                    installed=True,
                    active_adapter_registered=True,
                    active_ready=True,
                    hooks_installed=True,
                    mcp_configured=True,
                    policy_installed=True,
                    passive_source_registered=True,
                    passive_source_detected=True,
                    path_detected=True,
                    source_capabilities={
                        "visible_text": True,
                        "tool_calls": True,
                        "tool_results": True,
                        "reasoning": True,
                        "attachments": True,
                        "source_fidelity": True,
                    },
                    content_access_authorized=True,
                    authorization_state="user_authorized",
                    runtime_state="verified",
                    runtime_canary_hash="a" * 64,
                    runtime_canary_verified=True,
                    source_capture_state="verified",
                    source_capture_completeness={
                        "discovery_covered": True,
                        "content_parsed": True,
                        "raw_committed": True,
                        "runtime_canary_verified": True,
                        "runtime_canary_hash": "a" * 64,
                    },
                    discovery_covered=True,
                    content_parsed=True,
                    raw_committed=True,
                )
            ],
            missing_workflow_tools=[],
        )
        reports = [before, after]
        calls = []

        monkeypatch.setattr(agent_kit, "build_agent_kit_report", lambda *a, **kw: reports.pop(0))
        monkeypatch.setattr(
            agent_cmd,
            "_cmd_agent_install",
            lambda args: calls.append(args.agent_name) or True,
        )

        args = argparse.Namespace(agent_cmd="repair", agent_name="claude")
        result = mnemos_cli.cmd_agent(args)
        captured = capsys.readouterr()

        assert result is True
        assert calls == ["claude"]
        assert "修复后满血 Agent: claude" in captured.out

    def test_unknown_cmd(self, fake_config, capsys):
        args = argparse.Namespace(agent_cmd="unknown")
        mnemos_cli.cmd_agent(args)
        captured = capsys.readouterr()
        assert "repair" in captured.out


class TestCmdSearch:
    def test_no_results(self, fake_config, capsys, monkeypatch):
        mock_search = MagicMock()
        mock_search.search.return_value = []
        mock_mod = MagicMock(ContextAwareSearch=lambda: mock_search)
        monkeypatch.setitem(sys.modules, "core.app.context_search", mock_mod)

        args = argparse.Namespace(query="test", limit=10)
        mnemos_cli.cmd_search(args)
        captured = capsys.readouterr()
        assert "未找到" in captured.out

    def test_with_results(self, fake_config, capsys, monkeypatch):
        result = MagicMock()
        result.score = 0.95
        result.title = "Test Page"
        result.snippet = "This is a test snippet."
        result.page_path = "01-Projects/test.md"
        result.verification = "verified"
        result.source = "distill"
        result.page_embedding_score = 0.8
        result.relation_score = 0.7
        result.keyword_score = 0.9
        result.match_reason = "关键词命中:test；命中字段:标题"
        result.matched_terms = ["test"]
        result.score_breakdown = {"relevance": 0.8, "confidence": 0.7}

        mock_search = MagicMock()
        mock_search.search.return_value = [result]
        mock_mod = MagicMock(ContextAwareSearch=lambda: mock_search)
        monkeypatch.setitem(sys.modules, "core.app.context_search", mock_mod)

        args = argparse.Namespace(query="test", limit=10)
        mnemos_cli.cmd_search(args)
        captured = capsys.readouterr()
        assert "Test Page" in captured.out
        assert "verified" in captured.out
        assert "关键词命中:test" in captured.out
        assert "relevance=0.8" in captured.out

    def test_json_no_results(self, fake_config, capsys, monkeypatch):
        mock_search = MagicMock()
        mock_search.search.return_value = []
        mock_mod = MagicMock(ContextAwareSearch=lambda: mock_search)
        monkeypatch.setitem(sys.modules, "core.app.context_search", mock_mod)

        args = argparse.Namespace(query="test", limit=10, json=True)
        mnemos_cli.cmd_search(args)
        captured = capsys.readouterr()
        payload = json.loads(captured.out)

        assert payload == {"query": "test", "count": 0, "results": []}

    def test_json_with_results(self, fake_config, capsys, monkeypatch):
        result = MagicMock()
        result.score = 0.95
        result.title = "Test Page"
        result.snippet = "This is a test snippet."
        result.page_path = "01-Projects/test.md"
        result.verification = "verified"
        result.source = "distill"
        result.page_embedding_score = 0.8
        result.relation_score = 0.7
        result.keyword_score = 0.9
        result.match_reason = "关键词命中:test；命中字段:标题"
        result.matched_terms = ["test"]
        result.score_breakdown = {"relevance": 0.8, "confidence": 0.7}
        result.match_source = "hybrid"
        result.heat_level = "warm"
        result.heat_score = 0.42

        mock_search = MagicMock()
        mock_search.search.return_value = [result]
        mock_mod = MagicMock(ContextAwareSearch=lambda: mock_search)
        monkeypatch.setitem(sys.modules, "core.app.context_search", mock_mod)

        args = argparse.Namespace(query="test", limit=10, json=True)
        mnemos_cli.cmd_search(args)
        captured = capsys.readouterr()
        payload = json.loads(captured.out)

        assert payload["query"] == "test"
        assert payload["count"] == 1
        item = payload["results"][0]
        assert item["title"] == "Test Page"
        assert item["path"] == "01-Projects/test.md"
        assert item["score"] == 0.95
        assert item["snippet"] == "This is a test snippet."
        assert item["reason"] == "关键词命中:test；命中字段:标题"
        assert item["source"] == "distill"
        assert item["verification"] == "verified"
        assert item["match_source"] == "hybrid"
        assert item["matched_terms"] == ["test"]
        assert item["score_breakdown"] == {"relevance": 0.8, "confidence": 0.7}
        assert item["scores"]["page_embedding"] == 0.8
        assert item["scores"]["relation"] == 0.7
        assert item["scores"]["keyword"] == 0.9
        assert item["heat"]["level"] == "warm"
        assert item["heat"]["score"] == 0.42


class TestCmdMetricsScan:
    def test_scan(self, fake_config, capsys, monkeypatch):
        mock_wm = MagicMock()
        mock_wm.scan_all_pages.return_value = {
            "total": 10,
            "inserted": 2,
            "updated": 3,
            "deleted": 0,
        }
        mock_mod = MagicMock(
            WikiMetrics=lambda **kw: mock_wm,
            write_mnemos_home=lambda *a: "/fake/home.md",
        )
        monkeypatch.setitem(sys.modules, "core.wiki_metrics", mock_mod)

        args = argparse.Namespace()
        mnemos_cli.cmd_metrics_scan(args)
        captured = capsys.readouterr()
        assert "扫描完成: 10 个页面" in captured.out


class TestCmdMetricsAssess:
    def test_assess_uses_quick_assess(self, fake_config, capsys, monkeypatch):
        page = fake_config.wiki_dir / "note.md"
        page.write_text("# Note\n\n正文内容\n", encoding="utf-8")
        calls = []

        def fake_quick_assess(path, content, source_count=1):
            calls.append((path, content, source_count))
            return {
                "quality_score": 64.5,
                "stage": "P2",
                "evidence_level": 3,
            }

        mock_mod = MagicMock(quick_assess=fake_quick_assess)
        monkeypatch.setitem(sys.modules, "core.wiki_metrics", mock_mod)

        args = argparse.Namespace(page="note.md", source_count=4)
        mnemos_cli.cmd_metrics_assess(args)

        captured = capsys.readouterr()
        assert calls == [("note.md", "# Note\n\n正文内容\n", 4)]
        assert "质量分: 64.5" in captured.out
        assert "知识阶段: P2" in captured.out
        assert "证据等级: 3" in captured.out


class TestCmdPerf:
    def test_basic(self, fake_config, capsys, monkeypatch):
        monkeypatch.setattr("core.cli.commands.perf._daemon_processes", lambda: [])
        args = argparse.Namespace()
        mnemos_cli.cmd_perf(args)
        captured = capsys.readouterr()
        assert "Mnemos 性能状态" in captured.out
        assert "未检测到运行中的 daemon" in captured.out

    def test_with_daemon(self, fake_config, capsys, monkeypatch):
        monkeypatch.setattr(
            "core.cli.commands.perf._daemon_processes",
            lambda: ["12345 python mnemos_daemon.py start"],
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: MagicMock(returncode=0, stdout="12345  0.5  1.2  1024  01:23:45"),
        )
        args = argparse.Namespace()
        mnemos_cli.cmd_perf(args)
        captured = capsys.readouterr()
        assert "daemon 进程:" in captured.out


class TestCmdRawIndex:
    def test_status_json(self, capsys, monkeypatch):
        monkeypatch.setattr(
            "core.cli.commands.raw_index._read_raw_index_health",
            lambda *_args, **_kwargs: {
                "status": "ok",
                "indexed_files": 2,
                "fts_entries": 2,
                "db_size_mb": 0.01,
                "raw_dir": "/tmp/raw",
                "db_path": "/tmp/raw_index.db",
            },
        )

        args = argparse.Namespace(raw_index_cmd="status", json=True)
        assert mnemos_cli.cmd_raw_index(args) == 0
        result = json.loads(capsys.readouterr().out)

        assert result["status"] == "ok"
        assert result["indexed_files"] == 2

    def test_rebuild_defaults_to_dry_run(self, capsys, monkeypatch, tmp_path):
        def fail_raw_index(*_args, **_kwargs):
            raise AssertionError("dry-run must not instantiate RawIndex")

        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        monkeypatch.setattr("core.app.raw_search.RawIndex", fail_raw_index)
        monkeypatch.setattr(
            "core.cli.commands.raw_index._read_raw_index_health",
            lambda *_args, **_kwargs: {
                "status": "ok",
                "indexed_files": 5,
                "raw_dir": str(raw_dir),
                "db_path": str(tmp_path / "raw_index.db"),
                "markdown_files": 5,
            },
        )

        args = argparse.Namespace(raw_index_cmd="rebuild", apply=False, json=False)
        assert mnemos_cli.cmd_raw_index(args) == 0
        captured = capsys.readouterr()

        assert "dry-run" in captured.out
        assert "--apply" in captured.out

    def test_rebuild_apply_runs_force_full_sync(self, capsys, monkeypatch, tmp_path):
        calls = []
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()

        class FakeRawIndex:
            def __init__(self, *_args, **_kwargs):
                pass

            def close(self):
                pass

            def health_check(self):
                return {"indexed_files": 7, "fts_entries": 7}

            def sync_index(self, force_full=False):
                calls.append(force_full)
                return {"indexed": 7, "removed": 1, "skipped": 0, "errors": 0}

        monkeypatch.setattr("core.app.raw_search.RawIndex", FakeRawIndex)
        monkeypatch.setattr(
            "core.cli.commands.raw_index._read_raw_index_health",
            lambda *_args, **_kwargs: {
                "status": "ok",
                "indexed_files": 0,
                "raw_dir": str(raw_dir),
                "db_path": str(tmp_path / "raw_index.db"),
                "markdown_files": 7,
            },
        )

        args = argparse.Namespace(raw_index_cmd="rebuild", apply=True, json=False)
        assert mnemos_cli.cmd_raw_index(args) == 0
        captured = capsys.readouterr()

        assert calls == [True]
        assert "RawIndex rebuild complete" in captured.out
        assert "indexed_files: 7" in captured.out


class TestCmdVaults:
    def test_sync_defaults_to_dry_run(self, fake_config, capsys, monkeypatch):
        def fail_sync(*_args, **_kwargs):
            raise AssertionError("vaults sync dry-run must not write projections")

        monkeypatch.setattr("core.cli.commands.vaults._get_config", lambda: fake_config)
        monkeypatch.setattr("core.cli.commands.vaults.sync_all_projections", fail_sync)

        args = argparse.Namespace(vaults_cmd="sync", apply=False, dry_run=False, no_commit=False)
        assert mnemos_cli.cmd_vaults(args) == 0
        captured = capsys.readouterr()

        assert "dry-run" in captured.out
        assert "--apply" in captured.out

    def test_sync_apply_runs_projection(self, fake_config, capsys, monkeypatch):
        calls = []

        def fake_sync_all_projections(commit=True):
            calls.append(commit)
            return {
                "vault_dir": str(fake_config.wiki_dir),
                "kg": {"status": "ok"},
                "observation": {"status": "ok"},
                "reflection": {"status": "ok"},
                "persona": {"status": "ok"},
                "git": {"committed": False, "output": "skipped"},
            }

        monkeypatch.setattr("core.cli.commands.vaults._get_config", lambda: fake_config)
        monkeypatch.setattr(
            "core.cli.commands.vaults.sync_all_projections",
            fake_sync_all_projections,
        )

        args = argparse.Namespace(vaults_cmd="sync", apply=True, dry_run=False, no_commit=True)
        assert mnemos_cli.cmd_vaults(args) == 0
        captured = capsys.readouterr()

        assert calls == [False]
        assert "开始重建认知 Vault Markdown 投影" in captured.out
        assert "Git 快照" in captured.out

    def test_sync_apply_refuses_dirty_vault(self, fake_config, capsys, monkeypatch):
        from core.cli.commands import vaults

        monkeypatch.setattr("core.cli.commands.vaults._get_config", lambda: fake_config)
        monkeypatch.setattr(vaults, "_vault_git_dirty", lambda _vault_dir: " M page.md")
        monkeypatch.setattr(
            "core.cli.commands.vaults.sync_all_projections",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("dirty vault must stop before sync")
            ),
        )

        args = argparse.Namespace(
            vaults_cmd="sync",
            apply=True,
            allow_dirty=False,
            dry_run=False,
            no_commit=True,
        )
        assert mnemos_cli.cmd_vaults(args) == 2

        captured = capsys.readouterr()
        assert "dirty" in captured.out
        assert "--allow-dirty" in captured.out


class TestCmdWiki:
    def test_read_not_found(self, fake_config, capsys, monkeypatch):
        mock_reader = MagicMock()
        mock_reader.read_page.return_value = None
        mock_mod = MagicMock(WikiReader=lambda: mock_reader)
        monkeypatch.setitem(sys.modules, "integrations.oracle", mock_mod)

        args = argparse.Namespace(wiki_cmd="read", page_path="missing.md", depth="full")
        mnemos_cli.cmd_wiki(args)
        captured = capsys.readouterr()
        assert "未找到页面" in captured.out

    def test_read_success(self, fake_config, capsys, monkeypatch):
        mock_reader = MagicMock()
        mock_reader.read_page.return_value = {
            "title": "Test",
            "content": "Hello World",
            "related": [{"title": "Other", "relation": "see_also"}],
        }
        mock_mod = MagicMock(WikiReader=lambda: mock_reader)
        monkeypatch.setitem(sys.modules, "integrations.oracle", mock_mod)

        args = argparse.Namespace(wiki_cmd="read", page_path="test.md", depth="full")
        mnemos_cli.cmd_wiki(args)
        captured = capsys.readouterr()
        assert "Test" in captured.out
        assert "Other" in captured.out

    def test_unknown_cmd(self, fake_config, capsys):
        args = argparse.Namespace(wiki_cmd="unknown")
        mnemos_cli.cmd_wiki(args)
        captured = capsys.readouterr()
        assert "用法" in captured.out


class TestCmdReport:
    def test_generate(self, fake_config, capsys, monkeypatch):
        mock_gen = MagicMock()
        mock_gen.generate_weekly_report.return_value = "report content"
        mock_mod = MagicMock(WeeklyReportGenerator=lambda: mock_gen)
        monkeypatch.setitem(sys.modules, "core.app.weekly_report", mock_mod)

        args = argparse.Namespace(report_cmd="generate")
        mnemos_cli.cmd_report(args)
        captured = capsys.readouterr()
        assert "周报已生成" in captured.out
        # [P0-4] 生成内容必须被打印出来，不能仅提示成功
        assert "report content" in captured.out

    def test_unknown(self, fake_config, capsys):
        args = argparse.Namespace(report_cmd="unknown")
        mnemos_cli.cmd_report(args)
        captured = capsys.readouterr()
        assert "用法" in captured.out


class TestCmdBuildRelationIndex:
    def test_success(self, fake_config, capsys, monkeypatch):
        mock_kg = MagicMock()
        mock_kg.rebuild_relation_index.return_value = {
            "total": 100,
            "updated": 95,
            "failed": 3,
            "skipped": 2,
        }
        mock_rel = MagicMock()
        mock_rel.get_stats.side_effect = [
            {"total_relations": 10},
            {"total_relations": 95},
        ]
        mock_kg_mod = MagicMock(KnowledgeGraph=lambda **kw: mock_kg)
        mock_rel_mod = MagicMock(RelationEmbeddingManager=lambda **kw: mock_rel)
        monkeypatch.setitem(sys.modules, "core.kia.knowledge_graph", mock_kg_mod)
        monkeypatch.setitem(sys.modules, "core.embeddings.relation_manager", mock_rel_mod)

        args = argparse.Namespace()
        mnemos_cli.cmd_build_relation_index(args)
        captured = capsys.readouterr()
        assert "重建关联上下文向量索引" in captured.out
        assert "100 个关系" in captured.out


class TestCmdKg:
    def test_build_graph(self, capsys, monkeypatch):
        calls = []
        fake_graph = MagicMock(wiki_base=Path("/tmp/wiki"))
        monkeypatch.setitem(
            sys.modules,
            "core.kia.knowledge_graph",
            MagicMock(
                build_graph_for_wiki=lambda wiki_base=None: calls.append(wiki_base) or fake_graph
            ),
        )

        args = argparse.Namespace(wiki_base="/tmp/wiki")
        assert mnemos_cli.cmd_kg_build_graph(args) == 0

        captured = capsys.readouterr()
        assert "已构建 Wiki 知识图谱" in captured.out
        assert calls == ["/tmp/wiki"]

    def test_export_dataview(self, fake_config, capsys, monkeypatch):
        import core.cli.commands.kg as kg_commands

        monkeypatch.setattr(kg_commands, "get_config", lambda: fake_config)
        created = []

        class FakeKnowledgeGraph:
            def __init__(self, **kwargs):
                created.append(kwargs)

            def export_dataview_query(self, page):
                return f'```dataview\nWHERE file.path = "{page}"\n```'

        monkeypatch.setitem(
            sys.modules,
            "core.kia.knowledge_graph",
            MagicMock(KnowledgeGraph=FakeKnowledgeGraph),
        )

        args = argparse.Namespace(page="a.md")
        assert mnemos_cli.cmd_kg_export_dataview(args) == 0

        captured = capsys.readouterr()
        assert 'WHERE file.path = "a.md"' in captured.out
        assert created == [
            {
                "db_path": str(fake_config.database_dir / "knowledge_graph.db"),
                "wiki_base": str(fake_config.wiki_dir),
            }
        ]


class TestCmdPush:
    def test_check_json_uses_predictive_push_helper(self, capsys, monkeypatch):
        from core.kia.teiresias import KnowledgeMatch, PushDecision
        from core.cli.commands.push import cmd_push

        calls = []

        def fake_check_and_push(user_message, wiki_base=None, current_task="", session_id=""):
            calls.append((user_message, wiki_base, current_task, session_id))
            return PushDecision(
                should_push=True,
                reason="matched",
                matches=[
                    KnowledgeMatch(
                        page_path="03-Tech/python-debug.md",
                        page_title="Python Debug",
                        match_score=0.91,
                    )
                ],
                push_content="排查 Python 报错时先定位堆栈和复现步骤。",
            )

        monkeypatch.setattr("core.cli.commands.push.check_and_push", fake_check_and_push)

        args = argparse.Namespace(
            push_cmd="check",
            message="Python 报错怎么处理",
            wiki_base="/tmp/wiki",
            task="debugging",
            session_id="s1",
            json=True,
        )
        assert cmd_push(args) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "should_push": True,
            "reason": "matched",
            "push_content": "排查 Python 报错时先定位堆栈和复现步骤。",
            "matches": [
                {
                    "page_path": "03-Tech/python-debug.md",
                    "page_title": "Python Debug",
                    "match_score": 0.91,
                    "match_reason": "",
                }
            ],
        }
        assert calls == [("Python 报错怎么处理", "/tmp/wiki", "debugging", "s1")]

    def test_stats_json_uses_predictive_push_engine(self, capsys, monkeypatch):
        from core.cli.commands.push import cmd_push

        created = []

        class FakeEngine:
            def __init__(self, wiki_base=None):
                created.append(wiki_base)

            def get_push_stats(self, days):
                assert days == 14
                return {
                    "total_pushes": 3,
                    "response_distribution": {"accept": 2, "ignore": 1},
                    "accept_rate": 2 / 3,
                }

        monkeypatch.setattr("core.cli.commands.push.PredictivePushEngine", FakeEngine)

        args = argparse.Namespace(push_cmd="stats", days=14, wiki_base="/tmp/wiki", json=True)
        assert cmd_push(args) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["total_pushes"] == 3
        assert payload["response_distribution"] == {"accept": 2, "ignore": 1}
        assert payload["accept_rate"] == 2 / 3
        assert created == ["/tmp/wiki"]


class TestCmdGenos:
    def test_compute_json_uses_compute_and_save_helper(self, capsys, monkeypatch):
        from core.kia.genos import KnowledgeDNA
        from core.cli.commands.genos import cmd_genos

        created = []
        calls = []

        class FakeDNAEngine:
            def __init__(self, wiki_base=None):
                self.wiki_base = wiki_base
                created.append(wiki_base)

        def fake_compute_and_save(page_path, engine=None):
            calls.append((page_path, engine.wiki_base if engine else None))
            return KnowledgeDNA(
                page_path="03-Tech/python.md",
                content_md5="md5",
                content_simhash="simhash",
                semantic_signature="engineering:guide:basic:neutral",
                domain_type_hash="domain-hash",
                domain="engineering",
                knowledge_type="guide",
                complexity="basic",
                emotion="neutral",
                keyword_set={"python"},
                core_concepts={"Python"},
                tool_entities={"pytest"},
                scenario_tags={"debug"},
                title_keywords={"python"},
                title_pattern="statement",
                confidence=0.8,
                evidence_level="curated",
                temporal="stable",
            )

        monkeypatch.setattr("core.cli.commands.genos.DNAEngine", FakeDNAEngine)
        monkeypatch.setattr("core.cli.commands.genos.compute_and_save", fake_compute_and_save)

        args = argparse.Namespace(
            genos_cmd="compute",
            page="03-Tech/python.md",
            wiki_base="/tmp/wiki",
            json=True,
        )
        assert cmd_genos(args) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "page_path": "03-Tech/python.md",
            "computed": True,
            "dna": {
                "page_path": "03-Tech/python.md",
                "content_md5": "md5",
                "content_simhash": "simhash",
                "semantic_signature": "engineering:guide:basic:neutral",
                "domain_type_hash": "domain-hash",
                "domain": "engineering",
                "knowledge_type": "guide",
                "complexity": "basic",
                "emotion": "neutral",
                "keyword_set": ["python"],
                "core_concepts": ["Python"],
                "tool_entities": ["pytest"],
                "scenario_tags": ["debug"],
                "title_keywords": ["python"],
                "title_pattern": "statement",
                "confidence": 0.8,
                "evidence_level": "curated",
                "temporal": "stable",
                "created_at": "",
                "updated_at": "",
            },
        }
        assert created == ["/tmp/wiki"]
        assert calls == [("03-Tech/python.md", "/tmp/wiki")]

    def test_duplicate_json_uses_check_duplicate_helper(self, capsys, monkeypatch):
        from core.kia.genos import SimilarityResult
        from core.cli.commands.genos import cmd_genos

        created = []
        calls = []

        class FakeDNAEngine:
            def __init__(self, wiki_base=None):
                self.wiki_base = wiki_base
                created.append(wiki_base)

        def fake_check_duplicate(page_path, engine=None):
            calls.append((page_path, engine.wiki_base if engine else None))
            return [
                SimilarityResult(
                    target_page="03-Tech/python-debug.md",
                    overall_score=0.93,
                    dimension_scores={"content": 0.9},
                    verdict="duplicate",
                    reason="content hash match",
                )
            ]

        monkeypatch.setattr("core.cli.commands.genos.DNAEngine", FakeDNAEngine)
        monkeypatch.setattr("core.cli.commands.genos.check_duplicate", fake_check_duplicate)

        args = argparse.Namespace(
            genos_cmd="duplicate",
            page="03-Tech/python.md",
            wiki_base="/tmp/wiki",
            json=True,
        )
        assert cmd_genos(args) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "page_path": "03-Tech/python.md",
            "count": 1,
            "duplicates": [
                {
                    "target_page": "03-Tech/python-debug.md",
                    "overall_score": 0.93,
                    "dimension_scores": {"content": 0.9},
                    "verdict": "duplicate",
                    "reason": "content hash match",
                }
            ],
        }
        assert created == ["/tmp/wiki"]
        assert calls == [("03-Tech/python.md", "/tmp/wiki")]


class TestCmdImmune:
    def test_scan_writes_markdown_report(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("core.cli.commands.immune.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path
        created = []

        class FakeReport:
            scanned_pages = 2
            issues = [object()]
            critical_count = 1
            auto_fixable_count = 0
            health_score = 85.0
            summary = {"冲突": 1}

        class FakeImmune:
            def __init__(self, wiki_base):
                created.append(wiki_base)

            def full_scan(self):
                return FakeReport()

            def generate_report_markdown(self, report):
                return f"# 知识库健康报告\n\npages={report.scanned_pages}"

        monkeypatch.setattr("core.cli.commands.immune.KnowledgeImmuneSystem", FakeImmune)

        args = argparse.Namespace(immune_cmd="scan", write_report=True)
        assert mnemos_cli.cmd_immune(args) == 0

        captured = capsys.readouterr()
        reports = list((tmp_path / "99-Reports").glob("知识免疫报告-*.md"))
        assert created == [str(tmp_path)]
        assert len(reports) == 1
        assert reports[0].read_text(encoding="utf-8").startswith("# 知识库健康报告")
        assert "免疫扫描完成: pages=2, issues=1, critical=1" in captured.out
        assert "报告已写入:" in captured.out


class TestCmdMcpServe:
    def test_serve(self, fake_config, monkeypatch):
        calls = []
        mock_mod = MagicMock(run_mcp_server=lambda: calls.append(1))
        monkeypatch.setitem(sys.modules, "integrations.agora", mock_mod)

        args = argparse.Namespace()
        mnemos_cli.cmd_mcp_serve(args)
        assert len(calls) == 1


class TestCmdCalibrate:
    def test_no_challenges(self, fake_config, monkeypatch):
        calls = []
        mock_mod = MagicMock(run_calibration=lambda: calls.append(1))
        monkeypatch.setitem(sys.modules, "core.persona.calibration_cli", mock_mod)

        args = argparse.Namespace()
        mnemos_cli.cmd_calibrate(args)
        assert len(calls) == 1


# ===========================================================================
# 3. main() 函数
# ===========================================================================


class TestMain:
    def test_help(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["mnemos", "--help"])
        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Mnemos" in captured.out

    def test_doctor(self, monkeypatch, fake_config):
        monkeypatch.setattr(sys, "argv", ["mnemos", "doctor"])
        monkeypatch.setattr(mnemos_cli, "cmd_doctor", lambda a: True)
        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()
        assert exc_info.value.code == 0

    def test_doctor_fail(self, monkeypatch, fake_config):
        monkeypatch.setattr(sys, "argv", ["mnemos", "doctor"])
        monkeypatch.setattr(mnemos_cli, "cmd_doctor", lambda a: False)
        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()
        assert exc_info.value.code == 1

    def test_doctor_repair_routes_with_agent_name(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "doctor", "repair", "kimi"])
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_doctor",
            lambda a: calls.append((a.doctor_action, a.agent_name)) or True,
        )
        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()

        assert exc_info.value.code == 0
        assert calls == [("repair", "kimi")]

    def test_secrets_doctor_parser(self):
        parser = mnemos_cli.build_parser()

        args = parser.parse_args(["secrets", "doctor", "--json", "--accept-env-fallback"])

        assert args.command == "secrets"
        assert args.secrets_cmd == "doctor"
        assert args.json is True
        assert args.accept_env_fallback is True

    def test_status(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "status"])
        monkeypatch.setattr(mnemos_cli, "cmd_status", lambda a: calls.append(1))
        mnemos_cli.main()
        assert len(calls) == 1

    def test_secrets_doctor_route(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "secrets", "doctor", "--json"])
        monkeypatch.setattr(mnemos_cli, "cmd_secrets", lambda a: calls.append(a.json) or 0)

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()

        assert exc_info.value.code == 0
        assert calls == [True]

    def test_vaults_repair_placement_parser(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            ["mnemos", "vaults", "repair-placement", "--limit", "3", "--json"],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_vaults",
            lambda a: calls.append((a.vaults_cmd, a.limit, a.json, a.apply, a.allow_dirty)) or 0,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()
        assert exc_info.value.code == 0
        assert calls == [("repair-placement", 3, True, False, False)]

    def test_vaults_audit_content_parser(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            ["mnemos", "vaults", "audit-content", "--json"],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_vaults",
            lambda a: calls.append((a.vaults_cmd, a.json)) or 0,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()
        assert exc_info.value.code == 0
        assert calls == [("audit-content", True)]

    def test_vaults_audit_links_parser(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "mnemos",
                "vaults",
                "audit-links",
                "--vault",
                "/tmp/wiki",
                "--scope",
                "kg",
                "--limit",
                "3",
                "--json",
            ],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_vaults",
            lambda a: calls.append((a.vaults_cmd, a.vault, a.scope, a.limit, a.json)) or 0,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()
        assert exc_info.value.code == 0
        assert calls == [("audit-links", "/tmp/wiki", "kg", 3, True)]

    def test_vaults_repair_links_parser(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "mnemos",
                "vaults",
                "repair-links",
                "--scope",
                "shadow",
                "--strip-broken",
                "--apply",
                "--allow-dirty",
            ],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_vaults",
            lambda a: calls.append(
                (
                    a.vaults_cmd,
                    a.scope,
                    a.strip_broken,
                    a.apply,
                    a.allow_dirty,
                )
            )
            or 0,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()
        assert exc_info.value.code == 0
        assert calls == [("repair-links", "shadow", True, True, True)]

    def test_vaults_exit_code_is_propagated(self, monkeypatch, fake_config):
        monkeypatch.setattr(
            sys,
            "argv",
            ["mnemos", "vaults", "repair-placement", "--apply"],
        )
        monkeypatch.setattr(mnemos_cli, "cmd_vaults", lambda a: 2)

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()
        assert exc_info.value.code == 2

    def test_config(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "config"])
        monkeypatch.setattr(mnemos_cli, "cmd_config", lambda a: calls.append(1))
        mnemos_cli.main()
        assert len(calls) == 1

    def test_kg_export_dataview(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "kg", "export-dataview", "a.md"])
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_kg_export_dataview",
            lambda a: calls.append(a.page) or 0,
            raising=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()

        assert exc_info.value.code == 0
        assert calls == ["a.md"]

    def test_kg_build_graph(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            ["mnemos", "kg", "build-graph", "--wiki-base", str(fake_config.wiki_dir)],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_kg_build_graph",
            lambda a: calls.append(a.wiki_base) or 0,
            raising=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()

        assert exc_info.value.code == 0
        assert calls == [str(fake_config.wiki_dir)]

    def test_kg_normalize_endpoints_parser(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "mnemos",
                "kg",
                "normalize-endpoints",
                "--json",
                "--min-concept-refs",
                "3",
            ],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_kg_normalize_endpoints",
            lambda a: calls.append((a.json, a.apply, a.min_concept_refs)) or 0,
            raising=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()

        assert exc_info.value.code == 0
        assert calls == [(True, False, 3)]

    def test_push_check(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "mnemos",
                "push",
                "check",
                "Python 报错怎么处理",
                "--task",
                "debugging",
                "--session-id",
                "s1",
                "--wiki-base",
                str(fake_config.wiki_dir),
            ],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_push",
            lambda a: calls.append((a.message, a.task, a.session_id, a.wiki_base)) or 0,
            raising=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()

        assert exc_info.value.code == 0
        assert calls == [("Python 报错怎么处理", "debugging", "s1", str(fake_config.wiki_dir))]

    def test_push_stats(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "mnemos",
                "push",
                "stats",
                "--days",
                "14",
                "--wiki-base",
                str(fake_config.wiki_dir),
                "--json",
            ],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_push",
            lambda a: calls.append((a.push_cmd, a.days, a.wiki_base, a.json)) or 0,
            raising=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()

        assert exc_info.value.code == 0
        assert calls == [("stats", 14, str(fake_config.wiki_dir), True)]

    def test_genos_duplicate(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "mnemos",
                "genos",
                "duplicate",
                "03-Tech/python.md",
                "--wiki-base",
                str(fake_config.wiki_dir),
            ],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_genos",
            lambda a: calls.append((a.genos_cmd, a.page, a.wiki_base)) or 0,
            raising=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()

        assert exc_info.value.code == 0
        assert calls == [("duplicate", "03-Tech/python.md", str(fake_config.wiki_dir))]

    def test_genos_compute(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "mnemos",
                "genos",
                "compute",
                "03-Tech/python.md",
                "--wiki-base",
                str(fake_config.wiki_dir),
                "--json",
            ],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_genos",
            lambda a: calls.append((a.genos_cmd, a.page, a.wiki_base, a.json)) or 0,
            raising=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()

        assert exc_info.value.code == 0
        assert calls == [("compute", "03-Tech/python.md", str(fake_config.wiki_dir), True)]

    def test_scheduler_reminders(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "scheduler", "reminders", "--json"])
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_scheduler",
            lambda a: calls.append((a.scheduler_cmd, a.json)) or 0,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()

        assert exc_info.value.code == 0
        assert calls == [("reminders", True)]

    def test_scheduler_schedule_parser(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "mnemos",
                "scheduler",
                "schedule",
                "review",
                "wiki",
                "2026-07-10T09:30:00",
                "--context",
                "weekly review",
                "--period",
                "weekly",
                "--priority",
                "7",
            ],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_scheduler",
            lambda a: calls.append(
                (
                    a.scheduler_cmd,
                    a.task_type,
                    a.subtype,
                    a.due_date,
                    a.context,
                    a.period,
                    a.priority,
                )
            )
            or 0,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()

        assert exc_info.value.code == 0
        assert calls == [
            (
                "schedule",
                "review",
                "wiki",
                "2026-07-10T09:30:00",
                "weekly review",
                "weekly",
                7,
            )
        ]

    def test_policy_commit(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "policy", "commit", "exp-1"])
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_policy",
            lambda a: calls.append((a.policy_cmd, a.experiment_id)) or 0,
            raising=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()

        assert exc_info.value.code == 0
        assert calls == [("commit", "exp-1")]

    def test_immune_scan(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "immune", "scan", "--write-report"])
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_immune",
            lambda a: calls.append((a.immune_cmd, a.write_report)) or 0,
            raising=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()

        assert exc_info.value.code == 0
        assert calls == [("scan", True)]

    def test_reminder_resolve_issue(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            ["mnemos", "reminder", "resolve", "--issue", "test:page", "--choice", "已处理"],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_reminder",
            lambda a: calls.append((a.reminder_cmd, a.reminder_id, a.issue, a.choice)) or 0,
            raising=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()

        assert exc_info.value.code == 0
        assert calls == [("resolve", None, "test:page", "已处理")]

    def test_persona_daily_summary(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            ["mnemos", "persona", "daily-summary", "2026-07-01", "--json"],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_persona",
            lambda a: calls.append((a.persona_cmd, a.date, a.json)) or 0,
            raising=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()

        assert exc_info.value.code == 0
        assert calls == [("daily-summary", "2026-07-01", True)]

    def test_persona_project_signals(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            ["mnemos", "persona", "project-signals", "/repo/mnemos", "--days", "14", "--json"],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_persona",
            lambda a: calls.append((a.persona_cmd, a.project_dir, a.days, a.json)) or 0,
            raising=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()

        assert exc_info.value.code == 0
        assert calls == [("project-signals", "/repo/mnemos", 14, True)]

    def test_persona_projects(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            ["mnemos", "persona", "projects", "--days", "14", "--json"],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_persona",
            lambda a: calls.append((a.persona_cmd, a.days, a.json)) or 0,
            raising=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()

        assert exc_info.value.code == 0
        assert calls == [("projects", 14, True)]

    def test_persona_recent_signals(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            ["mnemos", "persona", "recent-signals", "--source", "all", "--days", "14", "--json"],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_persona",
            lambda a: calls.append((a.persona_cmd, a.source, a.days, a.json)) or 0,
            raising=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            mnemos_cli.main()

        assert exc_info.value.code == 0
        assert calls == [("recent-signals", "all", 14, True)]

    def test_no_args_prints_help(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["mnemos"])
        mnemos_cli.main()
        captured = capsys.readouterr()
        assert "Mnemos" in captured.out

    def test_search(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "search", "test query"])
        monkeypatch.setattr(mnemos_cli, "cmd_search", lambda a: calls.append(a.query))
        mnemos_cli.main()
        assert calls == ["test query"]

    def test_search_json_flag(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "search", "test query", "--json"])
        monkeypatch.setattr(mnemos_cli, "cmd_search", lambda a: calls.append(a.json))
        mnemos_cli.main()
        assert calls == [True]

    def test_metrics_scan(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "metrics", "scan"])
        monkeypatch.setattr(mnemos_cli, "cmd_metrics_scan", lambda a: calls.append(1))
        mnemos_cli.main()
        assert len(calls) == 1

    def test_metrics_assess(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            ["mnemos", "metrics", "assess", "note.md", "--source-count", "4"],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_metrics_assess",
            lambda a: calls.append((a.page, a.source_count)),
            raising=False,
        )
        mnemos_cli.main()
        assert calls == [("note.md", 4)]

    def test_perf(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "perf"])
        monkeypatch.setattr(mnemos_cli, "cmd_perf", lambda a: calls.append(1))
        mnemos_cli.main()
        assert len(calls) == 1

    def test_build_relation_index(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "build-relation-index"])
        monkeypatch.setattr(mnemos_cli, "cmd_build_relation_index", lambda a: calls.append(1))
        mnemos_cli.main()
        assert len(calls) == 1

    def test_wiki_read(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "wiki", "read", "test.md"])
        monkeypatch.setattr(mnemos_cli, "cmd_wiki", lambda a: calls.append(a.wiki_cmd))
        mnemos_cli.main()
        assert calls == ["read"]

    def test_report_generate(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "report", "generate"])
        monkeypatch.setattr(mnemos_cli, "cmd_report", lambda a: calls.append(a.report_cmd))
        mnemos_cli.main()
        assert calls == ["generate"]

    def test_distill_audit(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "distill", "audit"])
        monkeypatch.setattr(mnemos_cli, "cmd_distill", lambda a: calls.append(a.distill_cmd))
        mnemos_cli.main()
        assert calls == ["audit"]

    def test_distill_drain_parser(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            ["mnemos", "distill", "drain", "--limit", "4", "--dry-run"],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_distill",
            lambda a: calls.append((a.distill_cmd, a.limit, a.dry_run)),
        )
        mnemos_cli.main()
        assert calls == [("drain", 4, True)]

    def test_distill_archive_failed_parser(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "mnemos",
                "distill",
                "archive-failed",
                "--all",
                "--limit",
                "3",
                "--reason",
                "known",
            ],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_distill",
            lambda a: calls.append((a.distill_cmd, a.all, a.limit, a.reason)),
        )
        mnemos_cli.main()
        assert calls == [("archive-failed", True, 3, "known")]

    def test_distill_reset_timeouts_parser(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            ["mnemos", "distill", "reset-timeouts", "--minutes", "45", "--json"],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_distill",
            lambda a: calls.append((a.distill_cmd, a.minutes, a.json)),
        )
        mnemos_cli.main()
        assert calls == [("reset-timeouts", 45, True)]

    def test_recap_dismiss_parser(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            ["mnemos", "recap", "dismiss", "--all", "--severity", "high", "--reason", "done"],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_recap",
            lambda a: calls.append((a.recap_cmd, a.all, a.severity, a.reason)) or 0,
        )
        with pytest.raises(SystemExit) as exc:
            mnemos_cli.main()
        assert exc.value.code == 0
        assert calls == [("dismiss", True, "high", "done")]

    def test_events_stats(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "events", "stats"])
        monkeypatch.setattr(mnemos_cli, "cmd_events", lambda a: calls.append(a.events_cmd))
        mnemos_cli.main()
        assert calls == ["stats"]

    def test_events_replay_parser(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "mnemos",
                "events",
                "replay",
                "--no-consumer",
                "--event-type",
                "wiki_page_updated",
                "--limit",
                "7",
            ],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_events",
            lambda a: calls.append((a.events_cmd, a.no_consumer, a.event_types, a.limit)),
        )
        mnemos_cli.main()
        assert calls == [("replay", True, ["wiki_page_updated"], 7)]

    def test_daemon_start(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "daemon", "start"])
        monkeypatch.setattr(mnemos_cli, "cmd_daemon", lambda a: calls.append(a.daemon_cmd))
        mnemos_cli.main()
        assert calls == ["start"]

    def test_scheduler_install(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "scheduler", "install-windows"])
        monkeypatch.setattr(
            mnemos_cli, "cmd_scheduler", lambda a: calls.append(a.scheduler_cmd) or 0
        )
        with pytest.raises(SystemExit) as exc:
            mnemos_cli.main()
        assert exc.value.code == 0
        assert calls == ["install-windows"]

    def test_calibrate(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "calibrate"])
        monkeypatch.setattr(mnemos_cli, "cmd_calibrate", lambda a: calls.append(1))
        mnemos_cli.main()
        assert len(calls) == 1

    def test_mcp_serve(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "mcp", "serve"])
        monkeypatch.setattr(mnemos_cli, "cmd_mcp_serve", lambda a: calls.append(1))
        mnemos_cli.main()
        assert len(calls) == 1

    def test_agent_list(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "agent", "list"])
        monkeypatch.setattr(mnemos_cli, "cmd_agent", lambda a: calls.append(a.agent_cmd))
        mnemos_cli.main()
        assert calls == ["list"]

    def test_sync_status(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "sync", "status"])
        monkeypatch.setattr(mnemos_cli, "cmd_sync", lambda a: calls.append(a.sync_cmd))
        mnemos_cli.main()
        assert calls == ["status"]

    def test_scorer_status(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "scorer", "status"])
        monkeypatch.setattr(mnemos_cli, "cmd_scorer", lambda a: calls.append(a.scorer_cmd))
        mnemos_cli.main()
        assert calls == ["status"]

    def test_capsule_list(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "capsule", "list"])
        monkeypatch.setattr(mnemos_cli, "cmd_capsule", lambda a: calls.append(a.capsule_cmd))
        mnemos_cli.main()
        assert calls == ["list"]

    def test_capsule_dismiss(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "capsule", "dismiss", "42"])
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_capsule",
            lambda a: calls.append((a.capsule_cmd, a.capsule_id)),
        )
        mnemos_cli.main()
        assert calls == [("dismiss", 42)]

    def test_capsule_set(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "capsule", "set", "note.md", "--days", "14"])
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_capsule",
            lambda a: calls.append((a.capsule_cmd, a.page_path, a.days)),
        )
        mnemos_cli.main()
        assert calls == [("set", "note.md", 14)]

    def test_version_list(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "version", "list", "note.md"])
        monkeypatch.setattr(mnemos_cli, "cmd_version", lambda a: calls.append(a.version_cmd))
        mnemos_cli.main()
        assert calls == ["list"]

    def test_shadow_sync(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "shadow", "sync"])
        monkeypatch.setattr(mnemos_cli, "cmd_shadow", lambda a: calls.append(a.shadow_cmd))
        mnemos_cli.main()
        assert calls == ["sync"]

    def test_shadow_premise(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(
            sys,
            "argv",
            ["mnemos", "shadow", "premise", "--page", "03-Tech/*.md"],
        )
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_shadow",
            lambda a: calls.append((a.shadow_cmd, a.page)),
        )
        mnemos_cli.main()
        assert calls == [("premise", "03-Tech/*.md")]

    def test_stress_run(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "stress", "run"])
        monkeypatch.setattr(mnemos_cli, "cmd_stress", lambda a: calls.append(a.stress_cmd))
        mnemos_cli.main()
        assert calls == ["run"]

    def test_stress_run_accepts_page(self, monkeypatch, fake_config):
        calls = []
        monkeypatch.setattr(sys, "argv", ["mnemos", "stress", "run", "--page", "03-Tech/a.md"])
        monkeypatch.setattr(
            mnemos_cli,
            "cmd_stress",
            lambda a: calls.append((a.stress_cmd, a.page)),
        )
        mnemos_cli.main()
        assert calls == [("run", "03-Tech/a.md")]


# ===========================================================================
# 4. _daemon_processes 辅助函数
# ===========================================================================


class TestDaemonProcesses:
    def test_no_processes(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: MagicMock(returncode=1, stdout=""))
        import platform

        monkeypatch.setattr(platform, "system", lambda: "Linux")
        assert mnemos_cli._daemon_processes() == []

    def test_darwin_pgrep(self, monkeypatch):
        def fake_run(cmd, **kw):
            from pathlib import Path

            if Path(cmd[0]).name == "pgrep":
                return MagicMock(returncode=0, stdout="12345\n")
            if Path(cmd[0]).name == "ps":
                return MagicMock(returncode=0, stdout="python3 mnemos_daemon.py start\n")
            return MagicMock(returncode=1, stdout="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        import platform

        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        result = mnemos_cli._daemon_processes()
        assert len(result) == 1
        assert "mnemos_daemon.py" in result[0]

    def test_filters_noisy(self, monkeypatch):
        def fake_run(cmd, **kw):
            from pathlib import Path

            if Path(cmd[0]).name == "pgrep":
                return MagicMock(returncode=0, stdout="12345 grep mnemos_daemon\n")
            return MagicMock(returncode=1, stdout="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        import platform

        monkeypatch.setattr(platform, "system", lambda: "Linux")
        assert mnemos_cli._daemon_processes() == []

    def test_subprocess_error(self, monkeypatch):
        def raise_error(*a, **kw):
            raise subprocess.SubprocessError("fail")

        monkeypatch.setattr(subprocess, "run", raise_error)
        assert mnemos_cli._daemon_processes() == []


# ===========================================================================
# 5. _print_config_contract / _print_runtime_health
# ===========================================================================


class TestPrintConfigContract:
    def test_basic(self, fake_config, capsys):
        mnemos_cli._print_config_contract(fake_config)
        captured = capsys.readouterr()
        assert "配置契约" in captured.out
        assert str(fake_config.config_path) in captured.out

    def test_with_warnings(self, fake_config, capsys):
        warnings = []
        mnemos_cli._print_config_contract(fake_config, warnings)
        assert len(warnings) == 0


class TestPrintRuntimeHealth:
    def test_no_databases(self, fake_config, capsys, monkeypatch):
        monkeypatch.setattr("core.cli.helpers._daemon_processes", lambda: [])
        mnemos_cli._print_runtime_health(fake_config)
        captured = capsys.readouterr()
        assert "daemon 进程数: 0" in captured.out

    def test_with_events_db(self, fake_config, capsys, monkeypatch):
        events_db = fake_config.data_dir / "events.db"
        conn = sqlite3.connect(str(events_db))
        conn.execute("CREATE TABLE events (event_type TEXT, status TEXT)")
        conn.execute("INSERT INTO events VALUES ('test', 'pending')")
        conn.commit()
        conn.close()

        real_conn = sqlite3.connect(str(events_db))
        monkeypatch.setattr(
            mnemos_cli,
            "sqlite_conn",
            lambda path, timeout=5: MagicMock(
                __enter__=lambda s: real_conn, __exit__=lambda *a: None
            ),
        )
        monkeypatch.setattr(mnemos_cli, "_daemon_processes", lambda: [])

        mnemos_cli._print_runtime_health(fake_config)
        captured = capsys.readouterr()
        assert "events pending/processing" in captured.out
        real_conn.close()


# ===========================================================================
# 6. cmd_doctor 关键路径
# ===========================================================================


class TestCmdDoctor:
    def test_pending_verification_pages_helper(self, tmp_path):
        from core.cli.commands.doctor import _pending_verification_pages

        wiki = tmp_path / "wiki"
        wiki.mkdir(exist_ok=True)
        (wiki / "ok.md").write_text("---\n验证状态: 已验证\n---\n\nok", encoding="utf-8")
        (wiki / "pending.md").write_text(
            "---\n验证状态: pending-verification\n---\n\nbody",
            encoding="utf-8",
        )
        nested = wiki / "nested"
        nested.mkdir()
        (nested / "review.md").write_text("正文提示：待验证", encoding="utf-8")

        count, samples = _pending_verification_pages(wiki)

        assert count == 2
        assert "pending.md" in samples
        assert "nested/review.md" in samples

    def test_quality_gate_stats_helper(self, tmp_path):
        from core.cli.commands.doctor import _quality_gate_stats

        wiki = tmp_path / "wiki"
        wiki.mkdir(exist_ok=True)
        (wiki / "review.md").write_text(
            "---\n质量门禁状态: review\n---\n\nbody",
            encoding="utf-8",
        )
        failed_dir = tmp_path / ".mnemos" / "distill_failed"
        failed_dir.mkdir(parents=True)
        (failed_dir / "failed.json").write_text(
            '{"validation_errors": ["片段[0] 质量门禁拒绝: score=0.2"]}',
            encoding="utf-8",
        )

        stats = _quality_gate_stats(wiki, tmp_path / ".mnemos")

        assert stats == {"review_pages": 1, "rejected_records": 1}

    def test_wiki_quality_gates_warns_only_over_pending_budget(self, monkeypatch, tmp_path, capsys):
        from core.cli.commands import doctor as doctor_cmd

        class FakeConfig:
            def get(self, key, default=None):
                if key == "health.wiki_route_budgets.needs_review_pages":
                    return 2
                return default

        wiki = tmp_path / "wiki"
        wiki.mkdir(exist_ok=True)
        (wiki / "pending.md").write_text("验证状态: pending-verification", encoding="utf-8")
        warnings: list[str] = []
        monkeypatch.setattr(doctor_cmd, "_get_config", lambda: FakeConfig())

        doctor_cmd._doctor_wiki_quality_gates([], wiki, tmp_path / ".mnemos", warnings)

        assert warnings == []
        assert "预算: 2" in capsys.readouterr().out

        (wiki / "pending-2.md").write_text("验证状态: pending-verification", encoding="utf-8")
        (wiki / "pending-3.md").write_text("验证状态: pending-verification", encoding="utf-8")
        doctor_cmd._doctor_wiki_quality_gates([], wiki, tmp_path / ".mnemos", warnings)

        assert len(warnings) == 1
        assert "超过预算 2" in warnings[0]

    def test_classify_page_source_uses_lightweight_frontmatter(self):
        from core.cli.commands.doctor import _classify_page_source

        assert _classify_page_source("---\ntags:\n  - distilled\n---\nbody") == "蒸馏提取"
        assert _classify_page_source("---\nsource: raw-sync\n---\nbody") == "Raw同步"
        assert _classify_page_source("---\nsource: retrospective\n---\nbody") == "复盘经验"
        assert _classify_page_source("plain body") == "人工写入"

    def test_read_frontmatter_prefix_does_not_load_large_body(self, tmp_path):
        from core.cli.commands.doctor import (
            FRONTMATTER_READ_LIMIT_BYTES,
            _read_frontmatter_prefix,
        )

        page = tmp_path / "large.md"
        page.write_text(
            "---\nsource: distill\n---\n" + ("x" * (FRONTMATTER_READ_LIMIT_BYTES * 2)),
            encoding="utf-8",
        )

        prefix = _read_frontmatter_prefix(page)

        assert len(prefix.encode("utf-8")) <= FRONTMATTER_READ_LIMIT_BYTES
        assert "source: distill" in prefix

    def test_basic(self, fake_config, capsys, monkeypatch):
        monkeypatch.setattr("core.cli.helpers._daemon_processes", lambda: [])
        monkeypatch.setattr(
            "core.cli.commands.doctor._print_runtime_health", lambda c, w=None: None
        )
        monkeypatch.setattr(
            subprocess, "run", lambda cmd, **kw: MagicMock(returncode=0, stdout="git version 2.0")
        )
        monkeypatch.setattr(shutil, "which", lambda cmd: None)

        args = argparse.Namespace(e2e=False)
        result = mnemos_cli.cmd_doctor(args)
        captured = capsys.readouterr()
        assert "Mnemos 系统诊断" in captured.out
        assert "待验证页面" in captured.out
        assert "质量门禁" in captured.out
        assert result is True

    def test_text_output_redacts_sensitive_values_by_default(self, capsys, monkeypatch):
        import core.cli.commands.doctor as doctor_module

        home_config_path = "/" + "Users/alice/.mnemos/configs/main.json"

        def fake_run(args):
            print(
                "url=https://api.example.test/v1 "
                f"path={home_config_path} "
                "tmp=/tmp/mnemos/config.json "
                "source=env:MNEMOS_LLM_API_KEY"
            )
            return True

        monkeypatch.setattr(doctor_module, "_run_doctor_text", fake_run)

        args = argparse.Namespace(
            doctor_action=None,
            cognitive_readiness=False,
            json=False,
            e2e=False,
            unsafe_debug=False,
        )
        result = doctor_module.cmd_doctor(args)
        captured = capsys.readouterr()

        assert result is True
        assert "api.example.test" not in captured.out
        assert "/" + "Users/alice" not in captured.out
        assert "/tmp/mnemos" not in captured.out
        assert "env:MNEMOS_LLM_API_KEY" not in captured.out
        assert "https://****/v1" in captured.out
        assert "<HOME>/.mnemos/configs/main.json" in captured.out
        assert "<PATH>/config.json" in captured.out
        assert "env:****" in captured.out

    def test_text_output_preserves_sensitive_values_with_unsafe_debug(self, capsys, monkeypatch):
        import core.cli.commands.doctor as doctor_module

        home_config_path = "/" + "Users/alice/.mnemos/configs/main.json"

        def fake_run(args):
            print(f"path={home_config_path}")
            return True

        monkeypatch.setattr(doctor_module, "_run_doctor_text", fake_run)

        args = argparse.Namespace(
            doctor_action=None,
            cognitive_readiness=False,
            json=False,
            e2e=False,
            unsafe_debug=True,
        )
        result = doctor_module.cmd_doctor(args)
        captured = capsys.readouterr()

        assert result is True
        assert home_config_path in captured.out

    def test_repair_action_delegates_to_agent_repair(self, fake_config, capsys, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "core.cli.commands.agent._cmd_agent_repair",
            lambda args: calls.append(args.agent_name) or True,
        )

        args = argparse.Namespace(
            doctor_action="repair",
            agent_name="claude",
            e2e=False,
            json=False,
            verbose=False,
        )
        result = mnemos_cli.cmd_doctor(args)

        assert result is True
        assert calls == ["claude"]

    def test_warns_actionable_commands_when_distill_backlog_and_daemon_stopped(
        self, fake_config, capsys, monkeypatch
    ):
        class FakeWorker:
            def get_stats(self):
                return {
                    "pending": 12,
                    "delegated": 0,
                    "inbox_dir": str(fake_config.wiki_dir / "00-Inbox"),
                }

        monkeypatch.setattr("core.cli.helpers._daemon_processes", lambda: [])
        monkeypatch.setattr("core.cli.commands.doctor._daemon_processes", lambda: [])
        monkeypatch.setattr(
            "core.cli.commands.doctor._print_runtime_health", lambda c, w=None: None
        )
        monkeypatch.setattr(
            subprocess, "run", lambda cmd, **kw: MagicMock(returncode=0, stdout="git version 2.0")
        )
        monkeypatch.setattr(shutil, "which", lambda cmd: None)
        monkeypatch.setattr("core.hephaestus_worker.HephaestusWorker", FakeWorker)

        args = argparse.Namespace(e2e=False, json=False, verbose=False)
        mnemos_cli.cmd_doctor(args)
        captured = capsys.readouterr()

        assert "daemon start" in captured.out
        assert "distill drain --limit 5" in captured.out

    def test_e2e_mode(self, fake_config, capsys, monkeypatch):
        mock_probe = MagicMock(return_value={"step1": (True, "ok"), "step2": (True, "ok")})
        mock_mod = MagicMock(run_probe=mock_probe)
        monkeypatch.setitem(sys.modules, "scripts.e2e_probe", mock_mod)

        args = argparse.Namespace(e2e=True)
        mnemos_cli.cmd_doctor(args)
        captured = capsys.readouterr()
        assert "全链路探针" in captured.out


# ===========================================================================
# 7. _cmd_sync_backfill / _cmd_sync_audit
# ===========================================================================


class TestCmdSyncBackfill:
    def test_no_agents(self, fake_config, capsys, monkeypatch):
        mock_registry = MagicMock(register_builtin_agents=lambda: None, auto_discover=lambda: [])
        mock_mod = MagicMock(SourceRegistry=mock_registry)
        monkeypatch.setitem(sys.modules, "core.sync_framework.registry", mock_mod)
        monkeypatch.setitem(sys.modules, "core.sync_framework.sync_engine", MagicMock())

        args = argparse.Namespace(source=None, since=0, max_turns=0, max_sessions=0, dry_run=False)
        mnemos_cli._cmd_sync_backfill(args)
        captured = capsys.readouterr()
        assert "未发现任何 Agent 源" in captured.out


class TestCmdSyncAudit:
    def test_no_agents(self, fake_config, capsys, monkeypatch):
        mock_registry = MagicMock(register_builtin_agents=lambda: None, auto_discover=lambda: [])
        mock_mod = MagicMock(SourceRegistry=mock_registry)
        monkeypatch.setitem(sys.modules, "core.sync_framework.registry", mock_mod)
        monkeypatch.setitem(sys.modules, "core.sync_framework.sync_engine", MagicMock())

        args = argparse.Namespace(source="all")
        mnemos_cli._cmd_sync_audit(args)
        captured = capsys.readouterr()
        assert "未发现任何 Agent 源" in captured.out


# ===========================================================================
# 8. cmd_init 关键路径（有限覆盖，因为涉及大量 input）
# ===========================================================================


class TestCmdInit:
    def test_init_saves_config(self, fake_config, monkeypatch, capsys, tmp_path):
        called = {}
        monkeypatch.setattr(
            "scripts.auto_setup._run_setup",
            lambda setup_args: called.update(vars(setup_args)),
        )

        args = argparse.Namespace()
        mnemos_cli.cmd_init(args)

        assert called["skip_backend"] is True
        assert called["skip_daemon"] is True
        assert called["skip_scheduler"] is True
        assert called["skip_backfill"] is True
        assert called["skip_e2e"] is True
        assert called["skip_hooks"] is False
        assert called["skip_verify"] is False


class TestCmdCapsule:
    def test_list_empty(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("core.cli.commands.capsule.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path
        args = argparse.Namespace(capsule_cmd="list", page=None, status=None)
        mnemos_cli.cmd_capsule(args)
        captured = capsys.readouterr()
        assert "暂无时间胶囊记录" in captured.out

    def test_due_empty(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("core.cli.commands.capsule.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path
        args = argparse.Namespace(capsule_cmd="due", days=7)
        mnemos_cli.cmd_capsule(args)
        captured = capsys.readouterr()
        assert "未来 7 天内无到期提醒" in captured.out

    def test_due_uses_module_level_get_due(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("core.cli.commands.capsule.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path
        calls = []

        class DirectCapsule:
            def __init__(self, *args, **kwargs):
                raise AssertionError("capsule due should call get_due()")

        def fake_get_due(days_ahead=7, wiki_base=None):
            calls.append((days_ahead, wiki_base))
            return []

        monkeypatch.setattr("core.cli.commands.capsule.TimeCapsule", DirectCapsule)
        monkeypatch.setattr("core.cli.commands.capsule.get_due", fake_get_due, raising=False)

        args = argparse.Namespace(capsule_cmd="due", days=3)
        assert mnemos_cli.cmd_capsule(args) == 0

        captured = capsys.readouterr()
        assert "未来 3 天内无到期提醒" in captured.out
        assert calls == [(3, str(tmp_path))]

    def test_set_uses_module_level_set_reminder(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("core.cli.commands.capsule.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path
        calls = []

        def fake_set_reminder(page_path, days=90):
            calls.append((page_path, days))
            return True

        monkeypatch.setattr("core.cli.commands.capsule.set_reminder", fake_set_reminder)

        args = argparse.Namespace(capsule_cmd="set", page_path="note.md", days=14)
        assert mnemos_cli.cmd_capsule(args) == 0

        captured = capsys.readouterr()
        assert "已设置胶囊提醒" in captured.out
        assert calls == [("note.md", 14)]

    def test_report(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("core.cli.commands.capsule.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path
        args = argparse.Namespace(capsule_cmd="report")
        mnemos_cli.cmd_capsule(args)
        captured = capsys.readouterr()
        assert "知识时间胶囊" in captured.out

    def test_complete_and_snooze(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("core.cli.commands.capsule.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path
        page = tmp_path / "note.md"
        page.write_text("# Note", encoding="utf-8")

        from core.kia.aion import TimeCapsule

        capsule = TimeCapsule(wiki_base=str(tmp_path))
        assert capsule.set_manual_reminder(str(page), days_from_now=1, reason="review")
        first = capsule.get_all_reminders()[0]

        args = argparse.Namespace(capsule_cmd="snooze", capsule_id=first.capsule_id, days=3)
        assert mnemos_cli.cmd_capsule(args) == 0
        captured = capsys.readouterr()
        assert "已推迟胶囊" in captured.out

        snoozed = capsule.get_all_reminders()[0]
        args = argparse.Namespace(capsule_cmd="complete", capsule_id=snoozed.capsule_id)
        assert mnemos_cli.cmd_capsule(args) == 0
        captured = capsys.readouterr()
        assert "已标记胶囊" in captured.out

    def test_dismiss(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("core.cli.commands.capsule.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path
        page = tmp_path / "note.md"
        page.write_text("# Note", encoding="utf-8")

        from core.kia.aion import TimeCapsule

        capsule = TimeCapsule(wiki_base=str(tmp_path))
        assert capsule.set_manual_reminder(str(page), days_from_now=1, reason="review")
        reminder = capsule.get_all_reminders()[0]

        args = argparse.Namespace(capsule_cmd="dismiss", capsule_id=reminder.capsule_id)
        assert mnemos_cli.cmd_capsule(args) == 0
        captured = capsys.readouterr()
        assert "已忽略胶囊" in captured.out
        assert capsule.get_all_reminders(status="dismissed")[0].capsule_id == reminder.capsule_id


class TestCmdVersion:
    def test_list_no_history(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("core.cli.commands.version.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path
        (tmp_path / "note.md").write_text("# Note", encoding="utf-8")
        args = argparse.Namespace(version_cmd="list", page_path="note.md")
        mnemos_cli.cmd_version(args)
        captured = capsys.readouterr()
        assert "页面暂无版本历史" in captured.out

    def test_create_and_list(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("core.cli.commands.version.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path
        page = tmp_path / "note.md"
        page.write_text("# Note v1", encoding="utf-8")

        args = argparse.Namespace(version_cmd="create", page_path="note.md", summary="first")
        mnemos_cli.cmd_version(args)
        captured = capsys.readouterr()
        assert "已创建快照" in captured.out

        args = argparse.Namespace(version_cmd="list", page_path="note.md")
        mnemos_cli.cmd_version(args)
        captured = capsys.readouterr()
        assert "版本时间线" in captured.out

    def test_diff_and_restore(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("core.cli.commands.version.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path
        page = tmp_path / "note.md"
        page.write_text("# Note v1", encoding="utf-8")

        from core.kia.ananke import VersionTimeTravel

        vtt = VersionTimeTravel(wiki_base=str(tmp_path))
        snap1 = vtt.snapshot(page, change_summary="v1")
        page.write_text("# Note v2", encoding="utf-8")
        _ = vtt.snapshot(page, change_summary="v2")

        args = argparse.Namespace(version_cmd="diff", page_path="note.md", from_id=None, to_id=None)
        mnemos_cli.cmd_version(args)
        captured = capsys.readouterr()
        assert "版本对比" in captured.out

        args = argparse.Namespace(
            version_cmd="restore",
            page_path="note.md",
            snapshot_id=snap1.snapshot_id,
            no_backup=False,
        )
        mnemos_cli.cmd_version(args)
        captured = capsys.readouterr()
        assert "已恢复" in captured.out
        assert page.read_text(encoding="utf-8") == "# Note v1"

    def test_diff_uses_show_diff_helper_for_latest(
        self, fake_config, capsys, monkeypatch, tmp_path
    ):
        monkeypatch.setattr("core.cli.commands.version.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path
        page = tmp_path / "note.md"
        page.write_text("# Note", encoding="utf-8")
        calls = []

        def fake_show_diff(page_path, wiki_base=None):
            calls.append((page_path, wiki_base))
            return "# 版本对比\n"

        monkeypatch.setattr("core.cli.commands.version.show_diff", fake_show_diff)

        args = argparse.Namespace(version_cmd="diff", page_path="note.md", from_id=None, to_id=None)
        assert mnemos_cli.cmd_version(args) == 0

        captured = capsys.readouterr()
        assert "版本对比" in captured.out
        assert calls == [(str(page), str(tmp_path))]

    def test_scan_all(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("core.cli.commands.version.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path
        (tmp_path / "00-Inbox").mkdir(parents=True, exist_ok=True)
        (tmp_path / "00-Inbox" / "note.md").write_text("# Note", encoding="utf-8")
        args = argparse.Namespace(version_cmd="scan-all")
        mnemos_cli.cmd_version(args)
        captured = capsys.readouterr()
        assert "扫描" in captured.out


class TestCmdShadow:
    def test_status_empty(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("core.cli.commands.shadow.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path
        args = argparse.Namespace(shadow_cmd="status")
        mnemos_cli.cmd_shadow(args)
        captured = capsys.readouterr()
        assert "影子页面数量: 0" in captured.out

    def test_sync_no_pages(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("core.cli.commands.shadow.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path
        args = argparse.Namespace(shadow_cmd="sync", page="*.md")
        mnemos_cli.cmd_shadow(args)
        captured = capsys.readouterr()
        assert "影子页面同步完成" in captured.out

    def test_sync_failure_returns_nonzero(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("core.cli.commands.shadow.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path

        class FakeShadowPageManager:
            def __init__(self, wiki_base):
                self.wiki_base = wiki_base

            def batch_sync(self, page_pattern="*.md"):
                return {"status": "failed", "created": 0, "updated": 0, "failed": 1, "total": 1}

        monkeypatch.setattr("core.cli.commands.shadow.ShadowPageManager", FakeShadowPageManager)

        args = argparse.Namespace(shadow_cmd="sync", page="*.md")
        assert mnemos_cli.cmd_shadow(args) == 1
        captured = capsys.readouterr()
        assert "status=failed" in captured.out

    def test_premise_batch_uses_validator(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("core.cli.commands.shadow.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path
        calls = []

        class FakePremiseValidator:
            def __init__(self, wiki_base):
                calls.append(("init", wiki_base))

            def validate_batch(self, page_pattern="*.md"):
                calls.append(("validate", page_pattern))
                return {"a.md": [object(), object()]}

        monkeypatch.setattr("core.cli.commands.shadow.PremiseValidator", FakePremiseValidator)

        args = argparse.Namespace(shadow_cmd="premise", page="03-Tech/*.md")
        assert mnemos_cli.cmd_shadow(args) == 0

        captured = capsys.readouterr()
        assert calls == [("init", tmp_path), ("validate", "03-Tech/*.md")]
        assert "前提条件批量验证完成" in captured.out
        assert "pages=1" in captured.out
        assert "changes=2" in captured.out


class TestCmdPolicy:
    def test_list_empty(self, capsys, monkeypatch):
        class FakePolicy:
            def list_shadows(self):
                return {}

        monkeypatch.setattr(
            "core.cli.commands.policy.get_effective_policy",
            lambda: FakePolicy(),
        )

        args = argparse.Namespace(policy_cmd="list")
        assert mnemos_cli.cmd_policy(args) == 0
        captured = capsys.readouterr()
        assert "暂无待裁决策略 shadow" in captured.out

    def test_list_prints_pending_shadows(self, capsys, monkeypatch):
        class FakePolicy:
            def list_shadows(self):
                return {
                    "app.push_max_items": {
                        "experiment_id": "exp-1",
                        "old_value": 3,
                        "new_value": 5,
                        "metric_before": 0.4,
                        "applied_at": "2026-07-01T00:00:00",
                    }
                }

        monkeypatch.setattr(
            "core.cli.commands.policy.get_effective_policy",
            lambda: FakePolicy(),
        )

        args = argparse.Namespace(policy_cmd="list")
        assert mnemos_cli.cmd_policy(args) == 0
        captured = capsys.readouterr()
        assert "待裁决策略 shadow: 1" in captured.out
        assert "exp-1 app.push_max_items: 3 -> 5" in captured.out

    def test_commit_and_rollback(self, capsys, monkeypatch):
        calls = []

        class FakePolicy:
            def force_commit(self, experiment_id):
                calls.append(("commit", experiment_id))
                return experiment_id == "ok"

            def force_rollback(self, experiment_id):
                calls.append(("rollback", experiment_id))
                return experiment_id == "ok"

        monkeypatch.setattr(
            "core.cli.commands.policy.get_effective_policy",
            lambda: FakePolicy(),
        )

        assert (
            mnemos_cli.cmd_policy(argparse.Namespace(policy_cmd="commit", experiment_id="ok")) == 0
        )
        assert (
            mnemos_cli.cmd_policy(
                argparse.Namespace(policy_cmd="rollback", experiment_id="missing")
            )
            == 1
        )

        captured = capsys.readouterr()
        assert "已提交策略 shadow: ok" in captured.out
        assert "未找到可回滚的策略 shadow: missing" in captured.out
        assert calls == [("commit", "ok"), ("rollback", "missing")]


class TestCmdStress:
    def test_status_empty(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("core.cli.commands.stress.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path
        args = argparse.Namespace(stress_cmd="status")
        mnemos_cli.cmd_stress(args)
        captured = capsys.readouterr()
        assert "已记录压力测试结果: 0 条" in captured.out

    def test_run_no_pages(self, fake_config, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("core.cli.commands.stress.get_config", lambda: fake_config)
        fake_config.wiki_dir = tmp_path
        args = argparse.Namespace(stress_cmd="run", limit=None)
        mnemos_cli.cmd_stress(args)
        captured = capsys.readouterr()
        assert "未找到可测试的知识页面" in captured.out

    def test_run_page_uses_stress_test_page_helper(
        self, fake_config, capsys, monkeypatch, tmp_path
    ):
        monkeypatch.setattr("core.cli.commands.stress.get_config", lambda: fake_config)
        calls = []

        def fake_stress_test_page(page_path, **kwargs):
            calls.append((page_path, kwargs))
            return "# 压力测试报告\n\n- 页面: 单页"

        monkeypatch.setattr(
            "core.cli.commands.stress.stress_test_page",
            fake_stress_test_page,
        )
        args = argparse.Namespace(stress_cmd="run", limit=None, page="03-Tech/a.md")

        assert mnemos_cli.cmd_stress(args) == 0

        captured = capsys.readouterr()
        assert "# 压力测试报告" in captured.out
        assert calls == [
            (
                "03-Tech/a.md",
                {"wiki_base": str(fake_config.wiki_dir), "dry_run": False},
            )
        ]
