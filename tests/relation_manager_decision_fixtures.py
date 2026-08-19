"""Canonical DecisionTrace helpers for RelationManager tests."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from core.cognitive.decision_trace import MaterialActionAuthorization
from core.kia.relation_manager import (
    KG_RELATION_ACTION,
    KG_RELATION_EXECUTOR,
    KG_RELATION_OWNER,
    RelationManager,
)
from tests.cognitive_decision_fixtures import material_action_authorization


def authorized_relation_manager(db_path: str | Path) -> RelationManager:
    """Create a manager whose sink receives real canonical authorizations."""

    resolved_path = Path(db_path)

    def resolve(binding: Mapping[str, str]) -> MaterialActionAuthorization:
        return material_action_authorization(
            resolved_path.parent,
            action_type=KG_RELATION_ACTION,
            owner=KG_RELATION_OWNER,
            executor=KG_RELATION_EXECUTOR,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
        )

    return RelationManager(
        str(resolved_path),
        material_action_resolver=resolve,
    )
