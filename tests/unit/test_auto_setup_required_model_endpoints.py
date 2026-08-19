"""Tests for required model endpoint setup retry limits."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import auto_setup as aus


@pytest.fixture
def isolated_setup_env(tmp_path: Path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("MNEMOS_DIR", str(fake_home / ".mnemos"))
    project_root = tmp_path / "mnemos"
    project_root.mkdir()
    monkeypatch.setattr(aus, "PROJECT_ROOT", project_root)
    return fake_home


def test_required_model_parser_defaults_to_three_smoke_attempts() -> None:
    args = aus._build_parser().parse_args([])

    assert args.max_smoke_attempts == 3


def test_mnemos_setup_forwards_max_smoke_attempts() -> None:
    import mnemos_cli
    from core.cli.commands.setup import _auto_setup_namespace

    args = mnemos_cli.build_parser().parse_args(["setup", "--max-smoke-attempts", "4"])
    setup_args = _auto_setup_namespace(args)

    assert setup_args.max_smoke_attempts == 4
    assert setup_args.reexec_args[-2:] == ["--max-smoke-attempts", "4"]


def test_required_model_prompt_aborts_after_max_smoke_attempts(monkeypatch) -> None:
    smoke_calls = []
    prompt_calls = []

    def fake_smoke(data):
        smoke_calls.append(dict(data))
        return False, {"embedding": "still failing"}

    monkeypatch.setattr(aus, "_smoke_required_model_endpoints", fake_smoke)
    monkeypatch.setattr(
        aus,
        "_prompt_one_model_endpoint",
        lambda data, kind: prompt_calls.append(kind),
    )
    monkeypatch.setattr(aus, "ask", lambda *args, **kwargs: "r")

    with pytest.raises(aus.RequiredModelEndpointSetupAbort) as exc:
        aus._prompt_required_model_endpoints(
            {}, yes_mode=False, max_smoke_attempts=2, interactive=True
        )

    assert exc.value.failure_code == "required_model_endpoints_failed"
    assert exc.value.attempts == 2
    assert len(smoke_calls) == 2
    assert prompt_calls == ["embedding"]


def test_required_model_prompt_fails_fast_without_tty(monkeypatch) -> None:
    monkeypatch.setattr(
        aus,
        "_smoke_required_model_endpoints",
        lambda data: (False, {"llm": "missing"}),
    )
    monkeypatch.setattr(aus, "_prompt_one_model_endpoint", lambda *args: pytest.fail("prompted"))

    with pytest.raises(aus.RequiredModelEndpointSetupAbort) as exc:
        aus._prompt_required_model_endpoints(
            {}, yes_mode=False, max_smoke_attempts=3, interactive=False
        )

    assert exc.value.user_action == "non_interactive"
    assert exc.value.errors == {"llm": "missing"}
    assert exc.value.attempts == 1


@pytest.mark.usefixtures("isolated_setup_env")
def test_generate_config_save_and_exit_writes_current_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(aus, "_setup_optional_multimodal", lambda data, yes_mode: None)
    monkeypatch.setattr(
        aus,
        "_smoke_required_model_endpoints",
        lambda data: (False, {"reranker": "service unavailable"}),
    )
    monkeypatch.setattr(aus, "ask", lambda *args, **kwargs: "s")

    with pytest.raises(aus.RequiredModelEndpointSetupAbort) as exc:
        aus.generate_config(
            tmp_path / "mnemos",
            tmp_path / "raw",
            yes_mode=False,
            max_smoke_attempts=3,
        )

    config_path = aus._runtime_config_path()
    assert config_path.exists()
    assert exc.value.user_action == "save_and_exit"
    assert exc.value.config_path == str(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["vaults"]["mnemos"]["path"] == str((tmp_path / "mnemos").resolve())
