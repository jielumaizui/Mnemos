"""Unit tests for core.cognitive.dimension_extractors."""

import pytest

from core.cognitive.dimension_extractors import (
    AttentionExtractor,
    DecisionsExtractor,
    GrowthExtractor,
    RelationshipsExtractor,
    StressExtractor,
)
from core.cognitive.models import ObservationType
from core.cognitive.sources import ContentSource, ContentTier, SourceItem, UserIntent


def _item(
    content,
    session_id="s1",
    content_source=ContentSource.NATIVE_DIALOGUE,
    content_tier=ContentTier.USER_GENERATED,
    user_intent=UserIntent.UNKNOWN,
):
    """Create a minimal SourceItem for extractor tests."""
    return SourceItem(
        source_type="wiki",
        file_path=f"/wiki/{session_id}.md",
        content=content,
        frontmatter={"session_id": session_id},
        content_source=content_source,
        content_tier=content_tier,
        user_intent=user_intent,
    )


# ───────────────────────────────────────────────
# Empty / single-item inputs
# ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "extractor_cls",
    [AttentionExtractor, DecisionsExtractor, StressExtractor, GrowthExtractor],
)
def test_extractor_returns_empty_for_empty_input(extractor_cls):
    assert list(extractor_cls().extract([])) == []


def test_attention_extractor_single_item():
    obs = list(AttentionExtractor().extract([_item("我们今天讨论了人工智能和编程。")]))
    assert len(obs) >= 1
    concepts_obs = [
        o for o in obs if "concepts" in o.value and o.value.get("source") == "native"
    ][0]
    assert concepts_obs.value["concepts"]["ai"] == 1
    assert concepts_obs.value["concepts"]["coding"] == 1


def test_decisions_extractor_single_item():
    obs = list(DecisionsExtractor().extract([_item("我决定采用这个方案。")]))
    assert len(obs) == 1
    assert obs[0].value["decision_signals"] == 2


def test_stress_extractor_single_item():
    obs = list(StressExtractor().extract([_item("最近压力很大。")]))
    assert len(obs) == 1
    assert obs[0].value["stress_signals"] == 1
    assert obs[0].value["affected_sessions"] == 1


def test_growth_extractor_single_item():
    obs = list(GrowthExtractor().extract([_item("我学会了一项新方法。")]))
    assert len(obs) == 1
    assert obs[0].value["growth_signals"] == 2


def test_relationships_ignores_non_scalar_frontmatter_source_identity():
    """Malformed Wiki metadata must not crash a real extraction page."""
    item = SourceItem(
        source_type="wiki",
        file_path="/wiki/non-scalar-source.md",
        content="我们讨论了协作方式。",
        frontmatter={"session_id": "s1", "source": ["not", "an", "agent"]},
    )

    observations = list(RelationshipsExtractor().extract([item]))

    assert item.source_agent is None
    assert observations[0].value == {"讨论": 1}


# ───────────────────────────────────────────────
# Snippet deduplication and clean-snippet filtering
# ───────────────────────────────────────────────


def test_decisions_snippet_deduplication_and_clean_filter():
    """Repeated clean snippets are deduped; numbered-list snippets are filtered."""
    items = [
        _item("经过充分讨论，我们最终决定采用这个方案。", session_id="s1"),
        _item("经过充分讨论，我们最终决定采用这个方案。", session_id="s2"),
        _item("1. 最终决定采用这个方案。", session_id="s3"),  # dirty list marker
    ]
    obs = list(DecisionsExtractor().extract(items))
    freq = [o for o in obs if o.observation_type == ObservationType.FREQUENCY][0]
    assert freq.value["decision_signals"] == 9
    assert freq.value["snippets_count"] == 1


def test_decisions_clean_snippet_filters_date_content():
    """Snippets containing date patterns are rejected by _is_clean_snippet."""
    items = [_item("在2026-06-04这一天，我决定采用这个方案。")]
    obs = list(DecisionsExtractor().extract(items))
    freq = [o for o in obs if o.observation_type == ObservationType.FREQUENCY][0]
    assert freq.value["decision_signals"] == 2
    assert freq.value["snippets_count"] == 0


def test_growth_snippet_deduplication():
    items = [
        _item(
            "经过几周练习，我终于完全掌握了这项新技能。",
            session_id="s1",
        ),
        _item(
            "经过几周练习，我终于完全掌握了这项新技能。",
            session_id="s2",
        ),
    ]
    obs = list(GrowthExtractor().extract(items))
    freq = [o for o in obs if o.observation_type == ObservationType.FREQUENCY][0]
    assert freq.value["growth_signals"] == 4
    assert freq.value["unique_snippets"] == 1


# ───────────────────────────────────────────────
# Confidence downgrade for pasted content
# ───────────────────────────────────────────────


def test_attention_confidence_downgrade_with_pasted_content():
    items = [
        _item(
            "我们今天讨论了人工智能和编程。",
            content_tier=ContentTier.LIKELY_PASTED,
        )
    ]
    obs = list(AttentionExtractor().extract(items))
    concepts_obs = [o for o in obs if "concepts" in o.value][0]
    assert concepts_obs.confidence == 0.35  # 0.5 base × 0.7


def test_decisions_confidence_downgrade_with_pasted_content():
    items = [
        _item(
            "我决定采用这个方案。",
            content_tier=ContentTier.LIKELY_PASTED,
        )
    ]
    obs = list(DecisionsExtractor().extract(items))
    freq = [o for o in obs if o.observation_type == ObservationType.FREQUENCY][0]
    assert freq.confidence == 0.35  # 0.5 base × 0.7 (no clean snippets)


def test_stress_confidence_downgrade_with_pasted_content():
    items = [
        _item(
            "最近压力很大。",
            content_tier=ContentTier.LIKELY_PASTED,
        )
    ]
    obs = list(StressExtractor().extract(items))
    freq = obs[0]
    assert freq.confidence == 0.32  # 0.45 base × 0.7


def test_growth_confidence_downgrade_with_pasted_content():
    items = [
        _item(
            "我学会了一项新方法。",
            content_tier=ContentTier.LIKELY_PASTED,
        )
    ]
    obs = list(GrowthExtractor().extract(items))
    freq = [o for o in obs if o.observation_type == ObservationType.FREQUENCY][0]
    assert freq.confidence == 0.35  # 0.5 base × 0.7


# ───────────────────────────────────────────────
# Attention concept counts and stop-word filtering
# ───────────────────────────────────────────────


def test_attention_concept_counts():
    items = [
        _item("我在学习编程和重构技术债。"),
        _item("我们讨论了系统架构和设计模式。"),
    ]
    obs = list(AttentionExtractor().extract(items))
    concepts_obs = [
        o for o in obs if "concepts" in o.value and o.value.get("source") == "native"
    ][0]
    assert concepts_obs.value["concepts"]["coding"] == 5
    assert concepts_obs.value["total_mentions"] == 7
    assert concepts_obs.value["dominant"] == "coding"


def test_attention_top_words_filter_stop_words():
    items = [_item("Machine learning and artificial intelligence are great topics.")]
    obs = list(AttentionExtractor().extract(items))
    top_words_obs = [o for o in obs if "top_words" in o.value][0]
    top_words = top_words_obs.value["top_words"]
    assert "and" not in top_words
    assert "are" not in top_words
    assert "machine" in top_words
    assert "learning" in top_words
    assert "intelligence" in top_words
    assert "topics" in top_words


# ───────────────────────────────────────────────
# Stress affected sessions
# ───────────────────────────────────────────────


def test_stress_affected_sessions_count():
    items = [
        _item("最近压力很大。", session_id="s1"),
        _item("工作很紧急。", session_id="s2"),
        _item("没什么特别的。", session_id="s3"),
    ]
    obs = list(StressExtractor().extract(items))
    freq = obs[0]
    assert freq.value["stress_signals"] == 2
    assert freq.value["affected_sessions"] == 2


# ───────────────────────────────────────────────
# Growth role mentions
# ───────────────────────────────────────────────


def test_growth_role_mentions():
    items = [_item("我作为管理者和架构师参与了项目。")]
    obs = list(GrowthExtractor().extract(items))
    role_obs = [o for o in obs if o.observation_type == ObservationType.PATTERN][0]
    assert role_obs.value["管理者"] == 1
    assert role_obs.value["架构师"] == 1
