# -*- coding: utf-8 -*-
"""
P1-3 单元测试 — Blindspot 修复 dataclass/dict 错配
"""

from unittest.mock import MagicMock, patch

import pytest


class FakeBlindSpot:
    def __init__(self, type_, confidence=0.8, description=""):
        self.type = type_
        self.confidence = confidence
        self.description = description


class FakeProfile:
    def __init__(self, suspected=None, confirmed=None):
        self.suspected = suspected or []
        self.confirmed = confirmed or []


def test_detect_blindspots_no_dict_get_error():
    """BlindSpotProfile 是 dataclass，不应调用 .get()"""
    from core.app.blindspot_discovery import BlindspotDiscovery

    bd = BlindspotDiscovery()
    fake_profile = FakeProfile(suspected=[FakeBlindSpot("framing", 0.8)])

    with patch("core.persona.hamartia.BlindSpotProfileManager._load_profile", return_value=fake_profile):
        results, notes = bd._detect_blindspots("test query")

    # 不应有异常，且应检测到 framing 盲区
    assert any(r.topic == "framing_rigidity" for r in results)


def test_detect_blindspots_framing_trigger():
    """framing 类型且 confidence > 0.6 应触发盲区提醒"""
    from core.app.blindspot_discovery import BlindspotDiscovery

    bd = BlindspotDiscovery()
    fake_profile = FakeProfile(
        suspected=[
            FakeBlindSpot("framing", 0.9, "过度依赖单一视角"),
            FakeBlindSpot("temporal", 0.5),
        ]
    )

    with patch("core.persona.hamartia.BlindSpotProfileManager._load_profile", return_value=fake_profile):
        results, notes = bd._detect_blindspots("test")

    framing = [r for r in results if r.topic == "framing_rigidity"]
    assert len(framing) == 1
    assert "过度依赖单一视角" in framing[0].description


def test_check_blindspot_returns_degraded_info():
    """降级时返回 degraded=true 和 reasons"""
    from core.app.blindspot_discovery import BlindspotDiscovery

    bd = BlindspotDiscovery()
    # 让知识图谱和画像都失败
    with patch("core.kia.knowledge_graph.KnowledgeGraph.search", side_effect=RuntimeError("kg down")):
        with patch("core.persona.hamartia.BlindSpotProfileManager._load_profile", side_effect=RuntimeError("profile down")):
            result = bd.check_blind_spot("unknown concept")

    assert result.degraded is True
    assert len(result.degraded_reasons) == 2
    assert result.reminder is None
