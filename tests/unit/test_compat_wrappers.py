"""Tests for root compatibility wrappers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load_wrapper(filename: str):
    module_name = f"_test_wrapper_{filename.replace('.', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "filename",
    [
        "build_embedding_index.py",
        "context_search.py",
        "blindspot_discovery.py",
        "index_manager.py",
    ],
)
@pytest.mark.parametrize("argv", [[], ["--help"], ["-h"]])
def test_compat_wrappers_help_and_no_args_are_side_effect_free(
    filename, argv, monkeypatch, capsys
):
    wrapper = _load_wrapper(filename)
    calls = []
    monkeypatch.setattr(sys, "argv", [filename, *argv])
    if hasattr(wrapper, "subprocess"):
        monkeypatch.setattr(
            wrapper.subprocess, "call", lambda command: calls.append(command) or 0
        )

    try:
        retval = wrapper.main()
    except SystemExit as exc:
        retval = exc.code

    assert retval == 0

    captured = capsys.readouterr()
    assert "Compatibility wrapper. Prefer:" in captured.out
    assert calls == []


@pytest.mark.parametrize("argv", [[], ["--help"], ["-h"]])
def test_predictive_push_wrapper_help_and_no_args_are_side_effect_free(
    argv, monkeypatch, capsys
):
    """predictive_push.py 已改为真实 CLI wrapper，帮助/无参数时不应调用服务。"""
    wrapper = _load_wrapper("predictive_push.py")
    monkeypatch.setattr(sys, "argv", ["predictive_push.py", *argv])

    calls = []
    import core.application.intelligence as _intelligence_mod

    monkeypatch.setattr(
        _intelligence_mod,
        "IntelligenceApplicationService",
        lambda: MagicMock(predictive_push=lambda **kwargs: calls.append(kwargs) or {"success": True}),
    )

    try:
        retval = wrapper.main()
    except SystemExit as exc:
        retval = exc.code

    assert retval == 0
    assert calls == []


@pytest.mark.parametrize(
    ("filename", "argv", "expected_tail"),
    [
        ("build_embedding_index.py", ["--force"], ["scripts/build_embedding_index.py", "--force"]),
        ("context_search.py", ["Redis", "--limit", "1"], ["search", "Redis", "--limit", "1"]),
    ],
)
def test_compat_wrappers_forward_valid_args(filename, argv, expected_tail, monkeypatch):
    wrapper = _load_wrapper(filename)
    calls = []
    monkeypatch.setattr(sys, "argv", [filename, *argv])
    monkeypatch.setattr(
        wrapper.subprocess, "call", lambda command: calls.append(command) or 7
    )

    assert wrapper.main() == 7

    assert len(calls) == 1
    if filename == "build_embedding_index.py":
        assert str(calls[0][-2]).endswith(expected_tail[0])
        assert calls[0][-1:] == expected_tail[1:]
    else:
        assert calls[0][-len(expected_tail) :] == expected_tail


def test_predictive_push_wrapper_calls_intelligence_service(monkeypatch, capsys):
    """predictive_push.py 应直接调用 IntelligenceApplicationService.predictive_push。"""
    wrapper = _load_wrapper("predictive_push.py")
    monkeypatch.setattr(sys, "argv", ["predictive_push.py", "--context", "Redis"])

    mock_service = MagicMock()
    mock_service.predictive_push.return_value = {
        "success": True,
        "push_available": True,
        "pushes": [{"title": "Redis 连接池", "topic": "redis"}],
    }

    import core.application.intelligence as _intelligence_mod

    monkeypatch.setattr(
        _intelligence_mod,
        "IntelligenceApplicationService",
        lambda: mock_service,
    )

    assert wrapper.main() == 0

    mock_service.predictive_push.assert_called_once_with(
        user_input="Redis",
        working_dir="",
    )

    captured = capsys.readouterr()
    assert "Redis 连接池" in captured.out
