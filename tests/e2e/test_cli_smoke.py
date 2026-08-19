"""
End-to-end smoke tests for the Mnemos CLI.

These tests exercise the real CLI entry point with external dependencies mocked
so the suite remains fast and deterministic.  No LLM calls or real stores are
involved.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import mnemos_cli

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# CLI --help smoke test
# ---------------------------------------------------------------------------


def test_cli_help_returns_zero():
    """``mnemos_cli.py --help`` exits 0 and prints usage."""
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "mnemos_cli.py"), "--help"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "usage:" in result.stdout.lower()
    assert "reflect" in result.stdout


# ---------------------------------------------------------------------------
# Reflection pipeline smoke test (mocked stores / LLM)
# ---------------------------------------------------------------------------


class FakeRoute:
    """Minimal stand-in for core.reflection.reflection_router.ReflectionRoute."""

    def __init__(self):
        self.should_reflect = True
        self.scene = "new_project"
        self.reason = "识别到 new_project 场景信号（score=2）"
        self.role = MagicMock(value="builder")
        self.role_activation = MagicMock(confidence=0.9)


class FakeRecord:
    """Minimal stand-in for a ReflectionRecord."""

    def __init__(self):
        self.id = "r1-test"


class FakeInsight:
    """Minimal stand-in for an InsightResult."""

    def __init__(self):
        self.prompt_used = "Mock reflection prompt"
        self.llm_called = False
        self.summary = ""
        self.key_points = []
        self.llm_error = ""


class FakeReflectionResult:
    """Minimal stand-in for core.reflection.reflection_engine.ReflectionResult."""

    def __init__(self):
        self.triggered = True
        self.record = FakeRecord()
        self.insight = FakeInsight()


class FakeReflectionEngine:
    """Mock engine that returns a deterministic result."""

    def __init__(self, *args, **kwargs):
        self._manual_result = FakeReflectionResult()

    def reflect_on_user_input(self, text: str):
        return FakeReflectionResult()

    def reflect_manually(self, query: str):
        self._manual_result.record.id = "r1-manual"
        return self._manual_result


class FakeReflectionRouter:
    """Mock router that always routes to reflection."""

    def route(self, query: str, recent_context=None):
        return FakeRoute()


def _build_mocked_modules() -> tuple[MagicMock, MagicMock]:
    """Create sys.modules-compatible mocks for the reflection submodules."""
    engine_mod = MagicMock()
    engine_mod.ReflectionEngine = FakeReflectionEngine
    engine_mod.ReflectionResult = FakeReflectionResult

    router_mod = MagicMock()
    router_mod.ReflectionRouter = FakeReflectionRouter

    return engine_mod, router_mod


def test_reflect_on_pipeline_smoke(monkeypatch, capsys):
    """``reflect on <text>`` runs end-to-end with mocked reflection engine."""
    engine_mod, router_mod = _build_mocked_modules()
    monkeypatch.setitem(sys.modules, "core.reflection.reflection_engine", engine_mod)
    monkeypatch.setitem(sys.modules, "core.reflection.reflection_router", router_mod)

    args = argparse.Namespace(reflect_cmd="on", text="我想启动一个新项目")
    mnemos_cli.cmd_reflect(args)

    captured = capsys.readouterr()
    assert "Triggered: True" in captured.out
    assert "Record ID: r1-test" in captured.out
    assert "Router:" in captured.out


def test_reflect_manual_pipeline_smoke(monkeypatch, capsys):
    """``reflect manual <query>`` runs end-to-end with mocked reflection engine."""
    engine_mod, router_mod = _build_mocked_modules()
    monkeypatch.setitem(sys.modules, "core.reflection.reflection_engine", engine_mod)
    monkeypatch.setitem(sys.modules, "core.reflection.reflection_router", router_mod)

    args = argparse.Namespace(reflect_cmd="manual", query="分析最近决策模式")
    mnemos_cli.cmd_reflect(args)

    captured = capsys.readouterr()
    assert "Triggered: True" in captured.out
    assert "Record ID: r1-manual" in captured.out
