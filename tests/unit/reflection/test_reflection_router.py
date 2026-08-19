# -*- coding: utf-8 -*-
"""Unit tests for core.reflection.reflection_router."""

from __future__ import annotations

from unittest.mock import MagicMock


from core.reflection.reflection_router import ReflectionRoute, ReflectionRouter
from core.role.role_graph import Role, RoleActivation, RoleGraph


class TestReflectionRouter:
    def test_route_default_scene_does_not_reflect(self):
        router = ReflectionRouter()
        route = router.route("今天天气不错")
        assert isinstance(route, ReflectionRoute)
        assert route.should_reflect is False
        assert route.scene == "default"
        assert route.role == Role.DEFAULT

    def test_route_new_project_scene(self):
        router = ReflectionRouter()
        route = router.route("我要启动一个新项目")
        assert route.should_reflect is True
        assert route.scene == "new_project"
        assert route.role == Role.BUILDER
        assert "new_project" in route.reason

    def test_route_major_decision_scene_english(self):
        router = ReflectionRouter()
        route = router.route("I need to decide whether to accept the promotion")
        assert route.should_reflect is True
        assert route.scene == "major_decision"
        assert route.role == Role.CAREER_EXPLORER

    def test_route_role_shift_scene_uses_context(self):
        router = ReflectionRouter()
        route = router.route(
            "最近有些想法",
            recent_context=["我在考虑职业规划", "晋升机会出现了"],
        )
        assert route.should_reflect is True
        assert route.scene == "role_shift"

    def test_route_repeated_stuck(self):
        router = ReflectionRouter()
        route = router.route("again I am stuck with the same issue")
        assert route.should_reflect is True
        assert route.scene == "repeated_stuck"

    def test_route_to_dict(self):
        activation = RoleActivation(role=Role.LEARNER, confidence=0.8)
        route = ReflectionRoute(
            should_reflect=True,
            scene="new_project",
            role=Role.LEARNER,
            role_activation=activation,
            reason="r",
        )
        d = route.to_dict()
        assert d["should_reflect"] is True
        assert d["scene"] == "new_project"
        assert d["role"] == "learner"
        assert d["role_confidence"] == 0.8
        assert d["reason"] == "r"

    def test_route_with_custom_role_graph(self):
        role_graph = MagicMock(spec=RoleGraph)
        role_graph.infer_role.return_value = RoleActivation(role=Role.PARENT, confidence=0.9)
        router = ReflectionRouter(role_graph=role_graph)
        route = router.route("孩子在学习上总是拖拉")
        assert route.role == Role.PARENT
        role_graph.infer_role.assert_called_once()

    def test_get_role_graph(self):
        role_graph = RoleGraph()
        router = ReflectionRouter(role_graph=role_graph)
        assert router.get_role_graph() is role_graph
