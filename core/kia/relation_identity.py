"""Canonical opaque identities for KnowledgeGraph relation effects."""

from __future__ import annotations

from core.cognitive.state_contract import sha256_json

from .relation_schema import Relation


def relation_target_ref(relation: Relation) -> str:
    """Return the stable non-display identity of one logical relation row."""

    identity = {
        "source": relation.source,
        "target": relation.target,
        "relation_type": relation.relation_type.value,
    }
    return f"kg-relation:{sha256_json(identity)}"
