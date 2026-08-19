"""Canonical DecisionTrace helpers for KnowledgeGraph tests."""

from __future__ import annotations

from typing import Any, Mapping

from core.cognitive.decision_trace import MaterialActionAuthorization
from core.kia.knowledge_graph import (
    KG_GRAPH_RELATION_ACTION,
    KG_GRAPH_RELATION_EXECUTOR,
    KG_GRAPH_RELATION_OWNER,
    KnowledgeGraph,
)
from tests.cognitive_decision_fixtures import material_action_authorization


def authorized_knowledge_graph(*args: Any, **kwargs: Any) -> KnowledgeGraph:
    """Build a graph whose material sink receives real canonical commands."""

    holder: dict[str, KnowledgeGraph] = {}

    def resolve(binding: Mapping[str, str]) -> MaterialActionAuthorization:
        graph = holder["graph"]
        return material_action_authorization(
            graph.db_path.parent,
            action_type=KG_GRAPH_RELATION_ACTION,
            owner=KG_GRAPH_RELATION_OWNER,
            executor=KG_GRAPH_RELATION_EXECUTOR,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
        )

    graph = KnowledgeGraph(
        *args,
        material_action_resolver=resolve,
        **kwargs,
    )
    holder["graph"] = graph
    return graph
