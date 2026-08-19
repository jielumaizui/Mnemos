"""Characterization tests for scripts/auto_setup.py.

These tests lock argparse handling, step layout, default vault fallbacks, and
exit codes before cyclomatic-complexity refactoring.  All real side effects
(installs, subprocesses, filesystem writes outside tmp_path) are mocked.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts import auto_setup as aus


@pytest.fixture
def _mock_env(tmp_path: Path, monkeypatch):
    """Patch HOME and PROJECT_ROOT so real paths are never touched."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("MNEMOS_DIR", str(fake_home / ".mnemos"))
    project_root = tmp_path / "mnemos"
    project_root.mkdir()
    monkeypatch.setattr(aus, "PROJECT_ROOT", project_root)
    return fake_home, project_root


def _run_main(argv: list[str]) -> None:
    with patch.object(sys, "argv", ["auto_setup.py"] + argv):
        with pytest.raises(SystemExit) as exc:
            aus.main()
    return exc.value.code


def _clear_llm_env(monkeypatch) -> None:
    for name in (
        "MNEMOS_LLM_API_KEY",
        "MNEMOS_LLM_PROVIDER",
        "MNEMOS_LLM_BASE_URL",
        "MNEMOS_LLM_MODEL",
        "MNEMOS_EMBEDDING_API_KEY",
        "MNEMOS_EMBEDDING_BASE_URL",
        "MNEMOS_EMBEDDING_MODEL",
        "MNEMOS_RERANKER_API_KEY",
        "MNEMOS_RERANKER_BASE_URL",
        "MNEMOS_RERANKER_MODEL",
        "MNEMOS_MULTIMODAL_API_KEY",
        "MNEMOS_MULTIMODAL_BASE_URL",
        "MNEMOS_MULTIMODAL_MODEL",
        "MNEMOS_MULTIMODAL_PROVIDER",
        "SILICONFLOW_API_KEY",
        "SILICONFLOW_BASE_URL",
        "SILICONFLOW_MODEL",
        "SILICONFLOW_EMBEDDING_MODEL",
        "SILICONFLOW_RERANKER_MODEL",
        "SILICONFLOW_MULTIMODAL_MODEL",
        "DMXAPI_API_KEY",
        "DMX_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


# ── argparse / dry-run tests ──


def test_dry_run_json_output_structure(_mock_env, capsys) -> None:
    """--dry-run --json prints the expected JSON structure and exits 0."""
    code = _run_main(["--dry-run", "--json"])
    assert code == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["ok"] is True
    assert "project_root" in data
    assert "platform" in data
    assert "python" in data
    assert "obsidian" in data
    assert "vaults" in data
    assert "actions" in data
    assert data["vaults"]["default_mnemos"]
    assert data["vaults"]["default_raw"]


def test_dry_run_text_output(_mock_env, capsys) -> None:
    """--dry-run without --json prints human-readable check report."""
    code = _run_main(["--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Mnemos 部署检查" in out
    assert "Python:" in out
    assert "dry-run 完成" in out


def test_non_interactive_terminal_forces_dry_run(_mock_env, monkeypatch, capsys) -> None:
    """When stdin is not a tty and --yes is not given, dry-run is forced."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    code = _run_main([])
    assert code == 0
    out = capsys.readouterr().out
    assert "dry-run" in out


def test_auto_setup_uses_single_main_entrypoint() -> None:
    """The legacy main wrapper has been retired; __main__ calls main() directly."""
    assert not hasattr(aus, "_main_legacy")


# ── exit-code tests ──


def test_exit_1_on_python_check_failure(_mock_env, monkeypatch) -> None:
    """If check_python fails, main exits with code 1."""
    monkeypatch.setattr(aus, "check_python", lambda: False)
    code = _run_main(["--yes"])
    assert code == 1


def test_exit_1_on_user_abort_obsidian(_mock_env, monkeypatch) -> None:
    """If setup_obsidian reports abort, main exits with code 1."""
    monkeypatch.setattr(aus, "setup_obsidian", lambda **kw: (False, None, None))
    # Skip install step to avoid pip calls
    monkeypatch.setattr(aus, "install_dependencies", lambda **kw: True)
    code = _run_main(["--yes"])
    assert code == 1


def test_setup_obsidian_requires_installed_app(_mock_env, monkeypatch, capsys) -> None:
    monkeypatch.setattr(aus, "detect_obsidian_app", lambda: (False, None, None))

    ok, mnemos_vault, raw_vault = aus.setup_obsidian(yes_mode=True)

    assert ok is False
    assert mnemos_vault is None
    assert raw_vault is None
    out = capsys.readouterr().out
    assert "必装依赖" in out
    assert "半成品知识库配置" in out
    assert "raw Vault 保存各 Agent 的原始对话" in out
    assert "Mnemos Vault 保存蒸馏后的认知库" in out
    assert "请先安装 Obsidian，然后重新运行 setup" in out
    assert "https://obsidian.md/download" in out


def test_install_dependencies_uses_runtime_dependencies_only(_mock_env, monkeypatch) -> None:
    """First-time setup must not require dev/ML extras such as hnswlib."""
    _, project_root = _mock_env
    (project_root / "pyproject.toml").write_text("[project]\nname='mnemos'\n", encoding="utf-8")
    monkeypatch.setattr(aus, "_PYTHON_EXE", "/fake/python")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(aus.subprocess, "run", fake_run)

    assert aus.install_dependencies(yes_mode=True) is True
    assert calls[0] == ["/fake/python", "-m", "pip", "install", "-e", str(project_root)]
    assert all("[dev]" not in " ".join(map(str, call)) for call in calls)
    assert all("[ml]" not in " ".join(map(str, call)) for call in calls)


def test_ensure_venv_continues_when_pip_upgrade_times_out(
    _mock_env, monkeypatch, capsys
) -> None:
    _, project_root = _mock_env
    venv_python = project_root / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(aus.subprocess, "run", fake_run)

    assert aus._ensure_venv() == venv_python
    out = capsys.readouterr().out
    assert "pip/setuptools/wheel 升级超时" in out


def test_install_dependencies_retries_venv_without_build_isolation(
    _mock_env, monkeypatch
) -> None:
    _, project_root = _mock_env
    (project_root / "pyproject.toml").write_text("[project]\nname='mnemos'\n", encoding="utf-8")
    monkeypatch.setattr(aus, "_PYTHON_EXE", "/system/python")
    venv_python = project_root / ".venv" / "bin" / "python"
    monkeypatch.setattr(aus, "_ensure_venv", lambda: venv_python)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "/system/python":
            stderr = "externally-managed-environment" if kwargs.get("capture_output") else ""
            return MagicMock(returncode=1, stderr=stderr)
        if "--no-build-isolation" in cmd:
            return MagicMock(returncode=0, stderr="")
        stderr = (
            "installing build dependencies did not run successfully"
            if kwargs.get("capture_output")
            else ""
        )
        return MagicMock(returncode=1, stderr=stderr)

    class ExecCalled(Exception):
        pass

    exec_calls = []

    def fake_execv(python, argv):
        exec_calls.append((python, argv))
        raise ExecCalled((python, argv))

    monkeypatch.setattr(aus.subprocess, "run", fake_run)
    monkeypatch.setattr(aus.os, "execv", fake_execv)

    with pytest.raises(ExecCalled):
        aus.install_dependencies(yes_mode=True, reexec_args=["setup"])

    assert any("--no-build-isolation" in call for call in calls)
    assert exec_calls[0][1][1] == str(Path(aus.__file__).resolve())


# ── step counter / layout tests ──


def test_step_counter_and_print_step_layout(capsys) -> None:
    """print_step uses the [n/total] format and 60-character separators."""
    aus.print_step(5, 13, "生成配置")
    out = capsys.readouterr().out
    assert "=" * 60 in out
    assert "[5/13] 生成配置" in out


def test_total_steps_is_thirteen(_mock_env, monkeypatch, capsys) -> None:
    """The full setup path counts 13 steps and prints them all."""
    # Neutralize every real side effect
    monkeypatch.setattr(aus, "check_python", lambda: True)
    monkeypatch.setattr(aus, "install_dependencies", lambda **kw: True)
    monkeypatch.setattr(aus, "setup_l1_backend", lambda **kw: True)
    monkeypatch.setattr(aus, "setup_obsidian", lambda **kw: (True, None, None))
    monkeypatch.setattr(aus, "generate_config", lambda *a, **kw: Path("/cfg"))
    monkeypatch.setattr(aus, "init_vaults", lambda *a: None)
    monkeypatch.setattr(aus, "init_wiki_structure", lambda a: None)
    monkeypatch.setattr(aus, "register_vaults", lambda *a: None)
    monkeypatch.setattr(aus, "install_agent_hooks", lambda **kw: True)
    monkeypatch.setattr(aus, "setup_distillation", lambda **kw: "api")
    monkeypatch.setattr(aus, "start_daemon", lambda **kw: True)
    monkeypatch.setattr(aus, "setup_scheduler", lambda **kw: True)
    monkeypatch.setattr(
        aus.subprocess,
        "run",
        lambda *a, **kw: MagicMock(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(aus, "_schedule_backfill_background", lambda: True)
    monkeypatch.setattr(aus, "_print_completion_summary", lambda *a, **kw: None)

    _run_main(["--yes"])
    out = capsys.readouterr().out
    # All 13 step headers should appear
    for i in range(1, 14):
        assert f"[{i}/13]" in out


# ── default-vault fallback tests ──


def test_default_vault_fallbacks_when_setup_obsidian_returns_none(_mock_env, monkeypatch, capsys) -> None:
    """If setup_obsidian returns None vaults, defaults are used."""
    captured = {}

    def fake_generate_config(
        mnemos_vault, raw_vault, yes_mode=False, preserve=False, max_smoke_attempts=3
    ):
        captured["mnemos"] = mnemos_vault
        captured["raw"] = raw_vault
        return Path("/cfg")

    monkeypatch.setattr(aus, "check_python", lambda: True)
    monkeypatch.setattr(aus, "install_dependencies", lambda **kw: True)
    monkeypatch.setattr(aus, "setup_l1_backend", lambda **kw: True)
    monkeypatch.setattr(aus, "setup_obsidian", lambda **kw: (True, None, None))
    monkeypatch.setattr(aus, "generate_config", fake_generate_config)
    monkeypatch.setattr(aus, "init_vaults", lambda *a: None)
    monkeypatch.setattr(aus, "init_wiki_structure", lambda a: None)
    monkeypatch.setattr(aus, "register_vaults", lambda *a: None)
    monkeypatch.setattr(aus, "install_agent_hooks", lambda **kw: True)
    monkeypatch.setattr(aus, "setup_distillation", lambda **kw: "api")
    monkeypatch.setattr(aus, "start_daemon", lambda **kw: True)
    monkeypatch.setattr(aus, "setup_scheduler", lambda **kw: True)
    monkeypatch.setattr(aus, "_schedule_backfill_background", lambda: True)
    monkeypatch.setattr(aus, "_print_completion_summary", lambda *a, **kw: None)

    _run_main(["--yes"])
    assert captured["mnemos"] == aus.DEFAULT_MNEMOS_VAULT
    assert captured["raw"] == aus.DEFAULT_RAW_VAULT


# ── skip-flag tests ──


def test_skip_flags_avoid_steps(_mock_env, monkeypatch, capsys) -> None:
    """--skip-* flags bypass the corresponding steps."""
    calls = []
    monkeypatch.setattr(aus, "check_python", lambda: True)
    monkeypatch.setattr(aus, "install_dependencies", lambda **kw: True)
    monkeypatch.setattr(aus, "setup_l1_backend", lambda **kw: True)
    monkeypatch.setattr(aus, "setup_obsidian", lambda **kw: (True, Path("/m"), Path("/r")))
    monkeypatch.setattr(aus, "generate_config", lambda *a, **kw: Path("/cfg"))
    monkeypatch.setattr(aus, "init_vaults", lambda *a: None)
    monkeypatch.setattr(aus, "init_wiki_structure", lambda a: None)
    monkeypatch.setattr(aus, "register_vaults", lambda *a: None)
    monkeypatch.setattr(
        aus, "install_agent_hooks", lambda **kw: calls.append("hooks") or True
    )
    monkeypatch.setattr(aus, "setup_distillation", lambda **kw: "api")
    monkeypatch.setattr(
        aus, "start_daemon", lambda **kw: calls.append("daemon") or True
    )
    monkeypatch.setattr(
        aus, "setup_scheduler", lambda **kw: calls.append("scheduler") or True
    )
    monkeypatch.setattr(aus, "_schedule_backfill_background", lambda: True)
    monkeypatch.setattr(aus, "_print_completion_summary", lambda *a, **kw: None)

    _run_main(["--yes", "--skip-hooks", "--skip-daemon", "--skip-scheduler"])
    assert "hooks" not in calls
    assert "daemon" not in calls
    assert "scheduler" not in calls


# ── helper-level tests ──


def test_normalize_args_sets_dry_run_for_non_tty(_mock_env, monkeypatch) -> None:
    """_normalize_args forces dry_run when stdin is not a tty."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    parser = aus._build_parser()
    args = parser.parse_args([])
    norm = aus._normalize_args(args)
    assert norm.dry_run is True


def test_normalize_args_preserves_explicit_flags() -> None:
    """_normalize_args keeps explicit flag values."""
    parser = aus._build_parser()
    args = parser.parse_args(["--yes", "--skip-hooks", "--preserve-config", "--dry-run"])
    norm = aus._normalize_args(args)
    assert norm.yes is True
    assert norm.skip_hooks is True
    assert norm.preserve_config is True
    assert norm.dry_run is True


def test_run_dry_run_json(_mock_env, capsys) -> None:
    """_run_dry_run with json=True prints JSON and exits 0."""
    with pytest.raises(SystemExit) as exc:
        aus._run_dry_run(json_output=True)
    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True


def test_run_dry_run_text(_mock_env, capsys) -> None:
    """_run_dry_run with json=False prints text and exits 0."""
    with pytest.raises(SystemExit) as exc:
        aus._run_dry_run(json_output=False)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Mnemos 部署检查" in out


# ── per-step helper smoke tests ──


def test_step_check_python_true(monkeypatch) -> None:
    monkeypatch.setattr(aus, "check_python", lambda: True)
    assert aus._step_check_python() is True


def test_step_check_python_false(monkeypatch) -> None:
    monkeypatch.setattr(aus, "check_python", lambda: False)
    assert aus._step_check_python() is False


def test_step_install_deps_success(monkeypatch) -> None:
    monkeypatch.setattr(aus, "install_dependencies", lambda **kw: True)
    assert aus._step_install_deps(yes_mode=True, venv_reexec=False) is True


def _never_called(*args, **kwargs):
    raise AssertionError("should not be called")


def test_step_install_deps_skipped_on_reexec(monkeypatch) -> None:
    monkeypatch.setattr(aus, "install_dependencies", _never_called)
    assert aus._step_install_deps(yes_mode=True, venv_reexec=True) is True


def test_run_setup_aborts_on_dependency_failure(_mock_env, monkeypatch) -> None:
    parser = aus._build_parser()
    args = parser.parse_args(["--yes"])
    monkeypatch.setattr(aus, "check_python", lambda: True)
    monkeypatch.setattr(aus, "install_dependencies", lambda **kw: False)

    with pytest.raises(aus.SetupAbort, match="dependency installation failed"):
        aus._run_setup(args)


def test_step_setup_obsidian_returns_paths(monkeypatch) -> None:
    monkeypatch.setattr(aus, "setup_obsidian", lambda **kw: (True, Path("/m"), Path("/r")))
    ok, m, r = aus._step_setup_obsidian(yes_mode=True)
    assert ok is True
    assert m == Path("/m")
    assert r == Path("/r")


def test_step_setup_obsidian_defaults_none(monkeypatch) -> None:
    monkeypatch.setattr(aus, "setup_obsidian", lambda **kw: (True, None, None))
    ok, m, r = aus._step_setup_obsidian(yes_mode=True)
    assert m == aus.DEFAULT_MNEMOS_VAULT
    assert r == aus.DEFAULT_RAW_VAULT


def test_step_generate_config(monkeypatch, tmp_path: Path) -> None:
    called = {}

    def fake_generate_config(m, r, yes_mode, preserve, max_smoke_attempts):
        called.update(m=m, r=r, y=yes_mode, p=preserve, max=max_smoke_attempts)
        return tmp_path / "cfg"

    monkeypatch.setattr(aus, "generate_config", fake_generate_config)
    path = aus._step_generate_config(
        Path("/m"), Path("/r"), yes_mode=True, preserve=True, max_smoke_attempts=5
    )
    assert called == {"m": Path("/m"), "r": Path("/r"), "y": True, "p": True, "max": 5}
    assert path == tmp_path / "cfg"


def test_generate_config_requires_llm_key_in_yes_mode(_mock_env, monkeypatch, tmp_path: Path) -> None:
    _clear_llm_env(monkeypatch)
    with pytest.raises(aus.SetupAbort):
        aus.generate_config(tmp_path / "m", tmp_path / "r", yes_mode=True)


def test_generate_config_writes_safe_llm_env_chain(_mock_env, monkeypatch, tmp_path: Path) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("MNEMOS_LLM_API_KEY", "llm-secret")
    monkeypatch.setenv("MNEMOS_LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("MNEMOS_LLM_MODEL", "llm-model")
    monkeypatch.setenv("MNEMOS_EMBEDDING_API_KEY", "embed-secret")
    monkeypatch.setenv("MNEMOS_EMBEDDING_BASE_URL", "https://embedding.example.test/v1")
    monkeypatch.setenv("MNEMOS_EMBEDDING_MODEL", "embedding-model")
    monkeypatch.setenv("MNEMOS_RERANKER_API_KEY", "rerank-secret")
    monkeypatch.setenv("MNEMOS_RERANKER_BASE_URL", "https://reranker.example.test/v1")
    monkeypatch.setenv("MNEMOS_RERANKER_MODEL", "reranker-model")
    monkeypatch.setattr(aus, "_smoke_required_model_endpoints", lambda data: (True, {}))

    config_path = aus.generate_config(tmp_path / "m", tmp_path / "r", yes_mode=True)
    data = json.loads(config_path.read_text(encoding="utf-8"))

    assert data["llm"]["provider"] == "openai-compatible"
    assert data["llm"]["api_key"] == ""
    assert data["llm"]["api_key_source"] == "env:MNEMOS_LLM_API_KEY"
    assert data["llm"]["base_url"] == "https://llm.example.test/v1"
    assert data["llm"]["model"] == "llm-model"
    assert data["llm"]["chain"][0]["provider"] == "openai-compatible"
    assert data["llm"]["chain"][0]["api_key_source"] == "env:MNEMOS_LLM_API_KEY"
    assert data["embedding"]["api_key_source"] == "env:MNEMOS_EMBEDDING_API_KEY"
    assert data["embedding"]["base_url"] == "https://embedding.example.test/v1"
    assert data["embedding"]["model"] == "embedding-model"
    assert data["reranker"]["api_key_source"] == "env:MNEMOS_RERANKER_API_KEY"
    assert data["reranker"]["base_url"] == "https://reranker.example.test/v1"
    assert data["reranker"]["model"] == "reranker-model"
    text = config_path.read_text(encoding="utf-8")
    assert "llm-secret" not in text
    assert "embed-secret" not in text
    assert "rerank-secret" not in text
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_write_distill_config_keeps_runtime_config_private(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "configs" / "main.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"distill": {}}), encoding="utf-8")
    config_path.chmod(0o644)
    monkeypatch.setattr(aus, "_runtime_config_path", lambda: config_path)

    aus._write_distill_config("api")

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["distill"]["strategy"] == "api"
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_write_distill_config_never_follows_a_leaf_symlink(
    monkeypatch,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text('{"sentinel": true}\n', encoding="utf-8")
    config_path = tmp_path / "configs" / "main.json"
    config_path.parent.mkdir(parents=True)
    config_path.symlink_to(outside)
    monkeypatch.setattr(aus, "_runtime_config_path", lambda: config_path)
    original_read_text = Path.read_text
    unsafe_reads: list[Path] = []

    def track_read_text(path: Path, *args, **kwargs):
        if path == config_path:
            unsafe_reads.append(path)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", track_read_text)

    aus._write_distill_config("api")

    assert config_path.is_symlink()
    assert outside.read_text(encoding="utf-8") == '{"sentinel": true}\n'
    assert unsafe_reads == []


def test_start_daemon_fails_closed_on_unsafe_pid_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.pid"
    outside.write_text("123\n", encoding="utf-8")
    pid_path = tmp_path / "daemon.pid"
    pid_path.symlink_to(outside)
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: type("_Config", (), {"database_dir": tmp_path})(),
    )
    run = MagicMock()
    monkeypatch.setattr(aus.subprocess, "run", run)

    assert aus.start_daemon(yes_mode=True) is False
    run.assert_not_called()


def test_generate_config_preserves_existing_provider_config(
    _mock_env, monkeypatch, tmp_path: Path
) -> None:
    """重跑 setup 时，用户已填 provider 配置不应被全局默认覆盖。"""
    _clear_llm_env(monkeypatch)
    monkeypatch.setattr(aus, "_smoke_required_model_endpoints", lambda data: (True, {}))
    config_path = aus._runtime_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "openai-compatible",
                    "api_key": "",
                    "api_key_source": "keyring:setup:llm",
                    "base_url": "https://user-llm.example.test/v1",
                    "model": "user-chat-model",
                    "providers": {
                        "openai": {
                            "api_key": "",
                            "api_key_source": "keyring:custom-openai",
                            "api_key_env": "USER_OPENAI_KEY",
                            "base_url": "https://user-openai.example.test/v1",
                            "model": "user-openai-model",
                        }
                    },
                },
                "embedding": {
                    "api_key_source": "keyring:setup:embedding",
                    "base_url": "https://embedding.example.test/v1",
                    "model": "user-embedding-model",
                },
                "reranker": {
                    "api_key_source": "keyring:setup:reranker",
                    "base_url": "https://reranker.example.test/v1",
                    "model": "user-reranker-model",
                },
            }
        ),
        encoding="utf-8",
    )

    aus.generate_config(tmp_path / "m", tmp_path / "r", yes_mode=True, preserve=True)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    provider_cfg = data["llm"]["providers"]["openai"]

    assert provider_cfg["api_key_source"] == "keyring:custom-openai"
    assert provider_cfg["api_key_env"] == "USER_OPENAI_KEY"
    assert provider_cfg["base_url"] == "https://user-openai.example.test/v1"
    assert provider_cfg["model"] == "user-openai-model"


def test_load_config_data_preserve_handles_invalid_json(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "configs" / "main.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{", encoding="utf-8")

    data = aus._load_config_data(config_path, preserve=True)

    assert "llm" in data
    assert "现有配置读取失败，将重新生成" in capsys.readouterr().out


def test_load_config_data_never_follows_a_leaf_symlink(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text('{"performance_tier": "performance"}', encoding="utf-8")
    config_path = tmp_path / "configs" / "main.json"
    config_path.parent.mkdir(parents=True)
    config_path.symlink_to(outside)

    data = aus._load_config_data(config_path, preserve=True)

    assert data["performance_tier"] == "default"
    assert outside.read_text(encoding="utf-8") == (
        '{"performance_tier": "performance"}'
    )


def test_required_model_configs_use_effective_llm_chain(monkeypatch) -> None:
    """安装向导 smoke 应与运行链路读取同一个 LLM 主节点。"""
    _clear_llm_env(monkeypatch)
    monkeypatch.delenv("CHAIN_LLM_KEY", raising=False)
    monkeypatch.setenv("CHAIN_LLM_KEY", "chain-secret")
    data = {
        "llm": {
            "provider": "openai",
            "base_url": "https://stale.example.test/v1",
            "model": "stale-model",
            "api_key_source": "env:MISSING_STALE_LLM_KEY",
            "chain": [
                {
                    "provider": "siliconflow",
                    "base_url": "https://api.siliconflow.cn/v1",
                    "model": "deepseek-ai/DeepSeek-V4-Flash",
                    "api_key_source": "env:CHAIN_LLM_KEY",
                }
            ],
        },
        "embedding": {
            "base_url": "https://embedding.example.test/v1",
            "model": "embedding-model",
            "api_key_source": "env:MISSING_EMBEDDING_KEY",
        },
        "reranker": {
            "base_url": "https://reranker.example.test/v1",
            "model": "reranker-model",
            "api_key_source": "env:MISSING_RERANKER_KEY",
        },
    }

    configs = aus._resolve_required_model_configs(data)

    assert configs["llm"].provider == "siliconflow"
    assert configs["llm"].model == "deepseek-ai/DeepSeek-V4-Flash"
    assert configs["llm"].api_key == "chain-secret"


def test_required_model_configs_reuse_global_siliconflow_key(monkeypatch) -> None:
    """Embedding/Reranker smoke 应复用运行时支持的全局 SiliconFlow key。"""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("SILICONFLOW_API_KEY", "global-sf-secret")
    data = {
        "llm": {
            "provider": "siliconflow",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "deepseek-ai/DeepSeek-V4-Flash",
            "api_key_source": "env:SILICONFLOW_API_KEY",
        },
        "embedding": {
            "enabled": True,
            "provider": "siliconflow",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "BAAI/bge-m3",
        },
        "reranker": {
            "enabled": True,
            "provider": "siliconflow",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "BAAI/bge-reranker-v2-m3",
        },
    }

    configs = aus._resolve_required_model_configs(data)

    assert configs["embedding"].configured is True
    assert configs["embedding"].source == "env:SILICONFLOW_API_KEY"
    assert configs["reranker"].configured is True
    assert configs["reranker"].source == "env:SILICONFLOW_API_KEY"


def test_generate_config_fails_when_required_model_smoke_fails(
    _mock_env, monkeypatch, tmp_path: Path
) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("MNEMOS_LLM_API_KEY", "llm-secret")
    monkeypatch.setenv("MNEMOS_LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("MNEMOS_LLM_MODEL", "llm-model")
    monkeypatch.setenv("MNEMOS_EMBEDDING_API_KEY", "embed-secret")
    monkeypatch.setenv("MNEMOS_EMBEDDING_BASE_URL", "https://embedding.example.test/v1")
    monkeypatch.setenv("MNEMOS_EMBEDDING_MODEL", "embedding-model")
    monkeypatch.setenv("MNEMOS_RERANKER_API_KEY", "rerank-secret")
    monkeypatch.setenv("MNEMOS_RERANKER_BASE_URL", "https://reranker.example.test/v1")
    monkeypatch.setenv("MNEMOS_RERANKER_MODEL", "reranker-model")
    monkeypatch.setattr(
        aus,
        "_smoke_required_model_endpoints",
        lambda data: (False, {"embedding": "smoke failed"}),
    )

    with pytest.raises(aus.SetupAbort):
        aus.generate_config(tmp_path / "m", tmp_path / "r", yes_mode=True)


def test_step_hooks_skipped(monkeypatch, capsys) -> None:
    aus._step_hooks(yes_mode=True, skip=True, step=7, total=13)
    assert "已跳过" in capsys.readouterr().out


def test_step_hooks_runs(monkeypatch) -> None:
    monkeypatch.setattr(aus, "install_agent_hooks", lambda **kw: True)
    aus._step_hooks(yes_mode=True, skip=False, step=7, total=13)


def test_step_hooks_aborts_on_install_failure(monkeypatch) -> None:
    monkeypatch.setattr(aus, "install_agent_hooks", lambda **kw: False)

    with pytest.raises(aus.SetupAbort, match="Agent hooks"):
        aus._step_hooks(yes_mode=True, skip=False, step=7, total=13)


def test_install_agent_hooks_includes_mcp_only(monkeypatch, capsys) -> None:
    mock_registry = MagicMock()
    mock_registry.discover_all.return_value = []
    monkeypatch.setitem(sys.modules, "integrations.olympus", MagicMock(AgentRegistry=mock_registry))

    from core.cli.commands import mcp as mcp_cmd

    calls = []
    monkeypatch.setattr(mcp_cmd, "_MCP_ONLY_AGENTS", frozenset({"codex", "opencode"}))
    monkeypatch.setattr(mcp_cmd, "_install_mcp_only_agent", lambda name: calls.append(name) or True)

    assert aus.install_agent_hooks(yes_mode=True) is True
    assert calls == ["codex", "opencode"]
    assert "MCP-only" in capsys.readouterr().out


def test_step_daemon_skipped(monkeypatch, capsys) -> None:
    aus._step_daemon(yes_mode=True, skip=True, step=9, total=13)
    assert "已跳过" in capsys.readouterr().out


def test_step_daemon_aborts_on_failure(monkeypatch) -> None:
    monkeypatch.setattr(aus, "start_daemon", lambda **kw: False)

    with pytest.raises(aus.SetupAbort, match="daemon startup failed"):
        aus._step_daemon(yes_mode=True, skip=False, step=9, total=13)


def test_step_scheduler_skipped(monkeypatch, capsys) -> None:
    aus._step_scheduler(yes_mode=True, skip=True, step=10, total=13)
    assert "已跳过" in capsys.readouterr().out


def test_step_scheduler_aborts_on_failure(monkeypatch) -> None:
    monkeypatch.setattr(aus, "setup_scheduler", lambda **kw: False)

    with pytest.raises(aus.SetupAbort, match="scheduler setup failed"):
        aus._step_scheduler(yes_mode=True, skip=False, step=10, total=13)


def test_setup_scheduler_windows_invokes_cli(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(aus.platform, "system", lambda: "Windows")
    monkeypatch.setattr(aus, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(aus, "_PYTHON_EXE", "/fake/python")
    run = MagicMock(return_value=MagicMock(returncode=0, stdout="registered\n", stderr=""))
    monkeypatch.setattr(aus.subprocess, "run", run)

    assert aus.setup_scheduler(yes_mode=True) is True
    run.assert_called_once_with(
        ["/fake/python", str(tmp_path / "mnemos_cli.py"), "scheduler", "install-windows"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "Windows Task Scheduler 已配置" in capsys.readouterr().out


def test_setup_scheduler_windows_prints_manual_command_on_failure(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setattr(aus.platform, "system", lambda: "Windows")
    monkeypatch.setattr(aus, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(aus, "_PYTHON_EXE", "/fake/python")
    run = MagicMock(return_value=MagicMock(returncode=1, stdout="", stderr="denied"))
    monkeypatch.setattr(aus.subprocess, "run", run)

    assert aus.setup_scheduler(yes_mode=True) is False
    out = capsys.readouterr().out
    assert "Windows Task Scheduler 自动配置失败" in out
    assert "scheduler install-windows" in out


def test_step_verify_runs_when_not_skipped(monkeypatch) -> None:
    run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr("scripts.auto_setup.subprocess.run", run)
    aus._step_verify(skip=False, step=11, total=13)
    assert run.called


def test_step_verify_aborts_in_yes_mode_on_failure(monkeypatch) -> None:
    run = MagicMock(return_value=MagicMock(returncode=1, stdout="", stderr=""))
    monkeypatch.setattr("scripts.auto_setup.subprocess.run", run)

    with pytest.raises(aus.SetupAbort):
        aus._step_verify(skip=False, step=11, total=13, yes_mode=True)


def test_step_verify_aborts_interactive_on_failure(monkeypatch, capsys) -> None:
    run = MagicMock(
        return_value=MagicMock(returncode=1, stdout="model api failed\n", stderr="agent failed\n")
    )
    monkeypatch.setattr("scripts.auto_setup.subprocess.run", run)

    with pytest.raises(aus.SetupAbort):
        aus._step_verify(skip=False, step=11, total=13, yes_mode=False)
    out = capsys.readouterr().out
    assert "部署验证未通过" in out
    assert "model api failed" in out
    assert "agent failed" in out


def test_step_verify_skipped(monkeypatch, capsys) -> None:
    aus._step_verify(skip=True, step=11, total=13)
    assert "已跳过" in capsys.readouterr().out


def test_step_backfill_skipped(monkeypatch) -> None:
    monkeypatch.setattr(aus, "_schedule_backfill_background", _never_called)
    aus._step_backfill(skip=True, step=12, total=13)


def test_step_backfill_runs(monkeypatch) -> None:
    monkeypatch.setattr(aus, "_schedule_backfill_background", lambda: True)
    aus._step_backfill(skip=False, step=12, total=13)


def test_step_e2e_skipped(monkeypatch, capsys) -> None:
    aus._step_e2e(skip=True, step=13, total=13)
    assert "已跳过" in capsys.readouterr().out


def test_step_e2e_uses_readonly_no_api_probe(monkeypatch) -> None:
    run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(aus.subprocess, "run", run)

    aus._step_e2e(skip=False, step=13, total=13)

    cmd = run.call_args.args[0]
    assert str(aus.PROJECT_ROOT / "scripts" / "e2e_probe.py") in cmd
    assert "--dry-run" in cmd
    assert "--no-api" in cmd


def test_step_e2e_aborts_on_failure(monkeypatch) -> None:
    run = MagicMock(return_value=MagicMock(returncode=1, stdout="bad\n", stderr=""))
    monkeypatch.setattr(aus.subprocess, "run", run)

    with pytest.raises(aus.SetupAbort, match="E2E probe failed"):
        aus._step_e2e(skip=False, step=13, total=13)


def test_print_completion_summary_output(capsys) -> None:
    """_print_completion_summary prints vaults, strategy label, and commands."""
    aus._print_completion_summary(Path("/m"), Path("/r"), "api", backfill_started=True)
    out = capsys.readouterr().out
    assert "Mnemos 部署完成" in out
    assert "Mnemos Vault: /m" in out
    assert "raw Vault: /r" in out
    assert "API 自动蒸馏（已配置）" in out
    assert "mnemos doctor" in out
    assert "历史全量回填" in out
