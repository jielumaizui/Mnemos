# -*- coding: utf-8 -*-
"""Unit tests for core/role/role_graph.py"""

import pytest

from core.role.role_graph import Role, RoleGraph


class TestInferRole:
    """RoleGraph.infer_role 测试"""

    @pytest.fixture
    def role_graph(self):
        """提供一个全新的 RoleGraph 实例。"""
        return RoleGraph()

    @pytest.mark.parametrize(
        "query",
        [
            "帮我重构这段代码",
            "设计一个 API 服务",
            "implement a new feature",
            "这个项目的架构需要调整",
        ],
    )
    def test_builder_keywords_return_builder(self, role_graph, query):
        """包含 builder 关键词的 query 应返回 Role.BUILDER。"""
        activation = role_graph.infer_role(query)
        assert activation.role == Role.BUILDER
        assert activation.confidence > 0
        assert activation.scene_hint == "project_or_build"
        assert any("builder" in s for s in activation.signals)

    def test_no_keywords_return_default(self, role_graph):
        """没有任何角色关键词时应返回 Role.DEFAULT。"""
        activation = role_graph.infer_role("今天天气怎么样")
        assert activation.role == Role.DEFAULT
        assert activation.confidence == 0.0
        assert activation.signals == []
        assert activation.scene_hint == "default"

    def test_recent_context_can_flip_role(self, role_graph):
        """recent_context 中的 builder 信号可弥补当前 query 的不足。"""
        activation = role_graph.infer_role(
            "帮我看看",
            recent_context=["代码重构", "API 设计"],
        )
        assert activation.role == Role.BUILDER

    def test_low_confidence_downgrades_to_default(self, role_graph):
        """当最高分未显著领先时，应降级为 default。"""
        # 使用 builder / parent / learner 各一次，使最高分占比低于 0.4
        activation = role_graph.infer_role("孩子学习项目")
        assert activation.role == Role.DEFAULT
        assert activation.confidence < 0.4


class TestGetDominantRole:
    """RoleGraph.get_dominant_role 测试"""

    def test_dominant_role_over_window(self):
        """最近 window 次激活中次数最多的角色应被返回。"""
        rg = RoleGraph()
        rg.infer_role("帮我写代码")  # builder
        rg.infer_role("今天吃什么")  # default
        rg.infer_role("设计数据库")  # builder
        rg.infer_role("孩子学习")  # parent
        rg.infer_role("实现功能")  # builder

        dominant = rg.get_dominant_role(window=5)
        assert dominant == Role.BUILDER

    def test_window_ignores_older_entries(self):
        """window 应只统计最近 N 次激活，忽略更早的记录。"""
        rg = RoleGraph()
        rg.infer_role("帮我写代码")  # builder
        rg.infer_role("设计数据库")  # builder
        rg.infer_role("孩子学习")  # parent
        rg.infer_role("家庭教育")  # parent

        # 最近 2 次都是 parent
        assert rg.get_dominant_role(window=2) == Role.PARENT
        # 全部 4 次则 builder/parent 各 2，取先出现的 max
        # 由于 Python dict 保留插入顺序，builder 先达到 2
        assert rg.get_dominant_role(window=4) == Role.BUILDER

    def test_empty_history_returns_default(self):
        """无历史记录时应返回 Role.DEFAULT。"""
        rg = RoleGraph()
        assert rg.get_dominant_role(window=10) == Role.DEFAULT
