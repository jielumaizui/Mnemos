from __future__ import annotations

import pytest

from core.hephaestus.distillation_models import KnowledgeFragment
from core.hephaestus.distillation_wiki_page import (
    _map_form_to_type,
    _usage_hints_for_fragment,
)
from core.knowledge_form import display_knowledge_form, normalize_knowledge_form


@pytest.mark.parametrize(
    ("raw_form", "normalized", "display"),
    [
        ("问题-解决", "problem-solution", "问题-解决"),
        (" PROBLEM-SOLUTION ", "problem-solution", "问题-解决"),
        ("决策记录", "decision", "决策记录"),
        (" Decision-Log ", "decision", "决策记录"),
        ("经验法则", "heuristic", "经验法则"),
        (" HEURISTIC ", "heuristic", "经验法则"),
        ("反模式", "anti-pattern", "反模式"),
        (" Pitfall ", "anti-pattern", "反模式"),
        ("方法论", "methodology", "方法论"),
        (" METHODOLOGY ", "methodology", "方法论"),
        ("洞察关联", "insight", "洞察关联"),
        ("洞察", "insight", "洞察关联"),
        (" INSIGHT ", "insight", "洞察关联"),
        (" ＩＮＳＩＧＨＴ ", "insight", "洞察关联"),
    ],
)
def test_canonical_knowledge_form_corpus(raw_form, normalized, display):
    assert normalize_knowledge_form(raw_form) == normalized
    assert display_knowledge_form(raw_form) == display


@pytest.mark.parametrize("raw_form", ["决策记录", " decision ", " DECISION-LOG "])
def test_renderer_entity_type_uses_canonical_normalization(raw_form):
    assert _map_form_to_type(raw_form) == "project"


@pytest.mark.parametrize("raw_form", ["洞察关联", "洞察", " INSIGHT "])
def test_renderer_usage_hint_uses_canonical_normalization(raw_form):
    fragment = KnowledgeFragment(
        form=raw_form,
        title="Canonical insight fixture",
        frontmatter={},
        background="",
        core_content="",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )

    hints = _usage_hints_for_fragment(fragment)

    assert any("解释模型或判断视角" in hint for hint in hints)
