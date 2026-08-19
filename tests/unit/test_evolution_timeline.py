# -*- coding: utf-8 -*-
"""PersonaEvolutionTimeline 单元测试（P118）。"""

from __future__ import annotations

from unittest.mock import patch


from core.persona.evolution_timeline import PersonaEvolutionTimeline


class TestPersonaEvolutionTimeline:
    def test_empty_snapshots_returns_placeholder(self):
        timeline = PersonaEvolutionTimeline(snapshots=[])
        assert "数据积累中" in timeline.generate()

    def test_loaded_snapshots_generate_report(self):
        snapshots = [
            {
                "date": "2026-06-01",
                "profile": {"energy": {"focus_depth": 0.5}, "cognitive": {}, "value": {}},
            },
            {
                "date": "2026-06-02",
                "profile": {"energy": {"focus_depth": 0.7}, "cognitive": {}, "value": {}},
            },
        ]
        timeline = PersonaEvolutionTimeline(snapshots=snapshots)
        report = timeline.generate()
        assert "# 画像演化时间线" in report
        assert "专注深度" in report

    def test_loads_from_signal_store(self):
        """默认构造应从 SignalStore 加载 persona_versions。"""
        fake_versions = [
            {
                "generated_at": "2026-06-02T10:00:00",
                "energy_profile": {"focus_depth": 0.7},
                "cognitive_profile": {},
                "value_profile": {},
            },
            {
                "generated_at": "2026-06-01T10:00:00",
                "energy_profile": {"focus_depth": 0.5},
                "cognitive_profile": {},
                "value_profile": {},
            },
        ]

        class FakeStore:
            def get_recent_persona_versions(self, limit):
                return fake_versions

        with patch("core.persona.psyche.get_signal_store", return_value=FakeStore()):
            timeline = PersonaEvolutionTimeline()

        assert len(timeline._snapshots) == 2
        # 按时间升序
        assert timeline._snapshots[0]["date"] == "2026-06-01"
        assert timeline._snapshots[1]["date"] == "2026-06-02"
        report = timeline.generate()
        assert "专注深度" in report
