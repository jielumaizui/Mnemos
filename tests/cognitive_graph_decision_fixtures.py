"""Canonical DecisionTrace helpers for Cognitive Graph tests."""

from __future__ import annotations

from typing import Any, Mapping

from core.cognitive.decision_trace import MaterialActionAuthorization
from core.cognitive_graph.store import (
    COGNITIVE_CANONICAL_NODE_ACTION,
    COGNITIVE_RELATION_ACTION,
    COGNITIVE_RELATION_DELETE_ACTION,
    COGNITIVE_RELATION_EXECUTOR,
    COGNITIVE_RELATION_OWNER,
    COGNITIVE_RELATION_STALE_ACTION,
    CognitiveGraphStore,
    cognitive_canonical_node_material_action_binding,
    cognitive_relation_delete_material_action_binding,
    cognitive_relation_material_action_binding,
    cognitive_relation_stale_material_action_binding,
)
from tests.cognitive_decision_fixtures import material_action_authorization


def cognitive_relation_authorization(
    store: CognitiveGraphStore,
    binding: Mapping[str, str],
) -> MaterialActionAuthorization:
    """Seal a real canonical decision for one exact graph relation effect."""

    return material_action_authorization(
        store.db_path.parent,
        action_type=COGNITIVE_RELATION_ACTION,
        owner=COGNITIVE_RELATION_OWNER,
        executor=COGNITIVE_RELATION_EXECUTOR,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
    )


def cognitive_relation_resolver(
    store: CognitiveGraphStore,
):
    """Return an updater resolver backed by canonical decisions."""

    def resolve(
        _payload: Mapping[str, Any],
        binding: Mapping[str, str],
    ) -> MaterialActionAuthorization:
        return cognitive_relation_authorization(store, binding)

    return resolve


class AuthorizedCognitiveGraphStore(CognitiveGraphStore):
    """Test store that creates canonical commands instead of bypassing guards."""

    def add_relation(self, *args: Any, **kwargs: Any):
        if kwargs.get("material_action") is None:
            names = (
                "source",
                "target",
                "relation_type",
                "strength",
                "confidence",
                "source_layer",
                "target_layer",
                "access_control",
            )
            defaults: dict[str, Any] = {
                "strength": 0.5,
                "confidence": 0.5,
                "source_layer": "",
                "target_layer": "",
                "access_control": None,
            }
            values = dict(defaults)
            values.update(
                {
                    name: value
                    for name, value in zip(names, args)
                }
            )
            values.update(
                {
                    name: kwargs[name]
                    for name in names
                    if name in kwargs
                }
            )
            binding = cognitive_relation_material_action_binding(**values)
            kwargs["material_action"] = cognitive_relation_authorization(
                self,
                binding,
            )
        return super().add_relation(*args, **kwargs)

    def add_canonical_node(self, *args: Any, **kwargs: Any):
        if kwargs.get("material_action") is None:
            names = (
                "canonical_name",
                "canonical_id",
                "aliases",
                "source_ids",
                "embedding",
                "access_control",
            )
            defaults: dict[str, Any] = {
                "canonical_id": None,
                "aliases": None,
                "source_ids": None,
                "embedding": None,
                "access_control": None,
            }
            values = dict(defaults)
            values.update({name: value for name, value in zip(names, args)})
            values.update(
                {name: kwargs[name] for name in names if name in kwargs}
            )
            canonical_name = str(values["canonical_name"])
            canonical_id = str(
                values["canonical_id"] or self._canonical_id(canonical_name)
            )
            binding = cognitive_canonical_node_material_action_binding(
                canonical_name=canonical_name,
                canonical_id=canonical_id,
                aliases=values["aliases"],
                source_ids=values["source_ids"],
                embedding=values["embedding"],
                access_control=values["access_control"],
            )
            kwargs["material_action"] = material_action_authorization(
                self.db_path.parent,
                action_type=COGNITIVE_CANONICAL_NODE_ACTION,
                owner=COGNITIVE_RELATION_OWNER,
                executor=COGNITIVE_RELATION_EXECUTOR,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
            )
        return super().add_canonical_node(*args, **kwargs)

    def mark_stale(self, rel_id: str, **kwargs: Any):
        if kwargs.get("material_action") is None:
            binding = cognitive_relation_stale_material_action_binding(rel_id)
            kwargs["material_action"] = material_action_authorization(
                self.db_path.parent,
                action_type=COGNITIVE_RELATION_STALE_ACTION,
                owner=COGNITIVE_RELATION_OWNER,
                executor=COGNITIVE_RELATION_EXECUTOR,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
            )
        return super().mark_stale(rel_id, **kwargs)

    def delete_relation(self, rel_id: str, **kwargs: Any):
        if kwargs.get("material_action") is None:
            binding = cognitive_relation_delete_material_action_binding(rel_id)
            kwargs["material_action"] = material_action_authorization(
                self.db_path.parent,
                action_type=COGNITIVE_RELATION_DELETE_ACTION,
                owner=COGNITIVE_RELATION_OWNER,
                executor=COGNITIVE_RELATION_EXECUTOR,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
            )
        return super().delete_relation(rel_id, **kwargs)
