"""P3 audit unit tests for core.persona.evolution_timeline methods."""

import pytest

from core.persona.evolution_timeline import PersonaEvolutionTimeline


@pytest.fixture
def timeline():
    return PersonaEvolutionTimeline()


# ---------------------------------------------------------------------------
# core/persona/evolution_timeline.py::PersonaEvolutionTimeline._get_value
# ---------------------------------------------------------------------------


class TestGetValue:
    def test_nested_value(self, timeline):
        profile = {"energy": {"focus_depth": 0.8}, "cognitive": {"abstraction": 0.3}}
        assert timeline._get_value(profile, "energy.focus_depth") == pytest.approx(0.8)
        assert timeline._get_value(profile, "cognitive.abstraction") == pytest.approx(0.3)

    def test_missing_path_returns_none(self, timeline):
        assert timeline._get_value({}, "energy.focus_depth") is None

    def test_non_float_returns_none(self, timeline):
        assert (
            timeline._get_value({"energy": {"focus_depth": "high"}}, "energy.focus_depth") is None
        )


# ---------------------------------------------------------------------------
# core/persona/evolution_timeline.py::PersonaEvolutionTimeline._detect_events
# ---------------------------------------------------------------------------


class TestDetectEvents:
    def test_burnout_signal(self, timeline):
        timeline._snapshots = [
            {"date": "2026-01-01", "profile": {"energy": {"focus_depth": 0.8}}},
            {"date": "2026-01-02", "profile": {"energy": {"focus_depth": 0.55}}},
            {"date": "2026-01-03", "profile": {"energy": {"focus_depth": 0.5}}},
        ]
        events = timeline._detect_events()
        assert len(events) == 1
        assert events[0].event_type == "burnout_signal"
        assert events[0].dimension == "专注深度"

    def test_cognitive_shift(self, timeline):
        timeline._snapshots = [
            {"date": "2026-01-01", "profile": {"cognitive": {"abstraction": 0.2}}},
            {"date": "2026-01-02", "profile": {"cognitive": {"abstraction": 0.3}}},
            {"date": "2026-01-03", "profile": {"cognitive": {"abstraction": 0.5}}},
        ]
        events = timeline._detect_events()
        assert len(events) == 1
        assert events[0].event_type == "cognitive_shift"

    def test_value_flip(self, timeline):
        timeline._snapshots = [
            {"date": "2026-01-01", "profile": {"value": {"correctness_vs_efficiency": 0.4}}},
            {"date": "2026-01-02", "profile": {"value": {"correctness_vs_efficiency": 0.45}}},
            {"date": "2026-01-03", "profile": {"value": {"correctness_vs_efficiency": 0.6}}},
        ]
        events = timeline._detect_events()
        assert len(events) == 1
        assert events[0].event_type == "value_flip"


# ---------------------------------------------------------------------------
# core/persona/evolution_timeline.py::PersonaEvolutionTimeline._generate_mermaid_chart
# ---------------------------------------------------------------------------


class TestGenerateMermaidChart:
    def test_chart_contains_axes_and_lines(self, timeline):
        timeline._snapshots = [
            {"date": "2026-01-01", "profile": {"energy": {"focus_depth": 0.8}}},
            {"date": "2026-01-02", "profile": {"energy": {"focus_depth": 0.5}}},
        ]
        chart = timeline._generate_mermaid_chart()
        assert "xychart-beta" in chart
        assert "画像演化趋势" in chart
        assert "2026-01-01" in chart
        assert "0.80" in chart or "0.8" in chart
