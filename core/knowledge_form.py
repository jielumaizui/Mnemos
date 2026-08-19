"""Canonical vocabulary for distilled knowledge forms."""

from __future__ import annotations

import unicodedata
from typing import Any

FORM_ALIASES = {
    "问题-解决": "problem-solution",
    "problem-solution": "problem-solution",
    "决策记录": "decision",
    "decision-log": "decision",
    "decision": "decision",
    "经验法则": "heuristic",
    "heuristic": "heuristic",
    "反模式": "anti-pattern",
    "anti-pattern": "anti-pattern",
    "pitfall": "anti-pattern",
    "方法论": "methodology",
    "methodology": "methodology",
    "洞察关联": "insight",
    "洞察": "insight",
    "insight": "insight",
}

CANONICAL_DISPLAY_FORMS = {
    "problem-solution": "问题-解决",
    "decision": "决策记录",
    "heuristic": "经验法则",
    "anti-pattern": "反模式",
    "methodology": "方法论",
    "insight": "洞察关联",
}

CANONICAL_KNOWLEDGE_FORMS = tuple(CANONICAL_DISPLAY_FORMS.values())

KNOWLEDGE_FORM_ENTITY_TYPES = {
    "problem-solution": "concept",
    "decision": "project",
    "heuristic": "concept",
    "anti-pattern": "concept",
    "methodology": "concept",
    "insight": "concept",
}


def normalize_knowledge_form(value: Any) -> str:
    """Return the consumer vocabulary for a producer/display form."""

    lookup = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return FORM_ALIASES.get(lookup, "")


def display_knowledge_form(value: Any) -> str:
    """Return the canonical persisted display value, or an empty string."""

    return CANONICAL_DISPLAY_FORMS.get(normalize_knowledge_form(value), "")


def knowledge_form_entity_type(value: Any) -> str:
    """Return the canonical Wiki entity type for a recognized knowledge form."""

    return KNOWLEDGE_FORM_ENTITY_TYPES.get(normalize_knowledge_form(value), "")


__all__ = [
    "CANONICAL_DISPLAY_FORMS",
    "CANONICAL_KNOWLEDGE_FORMS",
    "FORM_ALIASES",
    "KNOWLEDGE_FORM_ENTITY_TYPES",
    "display_knowledge_form",
    "knowledge_form_entity_type",
    "normalize_knowledge_form",
]
