import pytest

from core.scoring.adaptive_scorer_v2 import ScoreCardV2
from core.scoring.scorers import SCORER_DIMENSIONS, dimension_catalog, score_domain


@pytest.mark.parametrize(
    "domain,content,metadata,expected_dimension",
    [
        ("sync", "production error crash with traceback ```py\nraise RuntimeError()\n```", {}, "urgency_score"),
        ("raw_memory", "- decision\n- reason\n- verification\n" * 20, {}, "quality_score"),
        ("kg", "[[Obsidian]] uses [[Raw Vault]] and related KnowledgeGraph", {}, "relation_confidence"),
        ("profile", "I always prefer TDD and ask why/how when unknown???", {}, "blind_spot_score"),
        ("ops", "daemon healthy ok success", {}, "health_score"),
    ],
)
def test_domain_scorers_return_stable_scorecards(domain, content, metadata, expected_dimension):
    card = score_domain(domain, content, metadata)

    assert isinstance(card, ScoreCardV2)
    assert set(card.scores) == set(SCORER_DIMENSIONS[domain])
    assert expected_dimension in card.scores
    assert 0.0 <= card.scores[expected_dimension] <= 1.0
    assert card.model_version.endswith("-rules-v2")


@pytest.mark.parametrize(
    "domain,content,metadata,dimension",
    [
        ("sync", "ok", {}, "noise_score"),
        ("raw_memory", "token" + "=secret-value", {}, "sensitivity_score"),
        ("kg", "no named entities here", {"age_days": 400}, "knowledge_freshness"),
        ("profile", "what why how???", {}, "blind_spot_score"),
        ("ops", "error timeout fail queue disk", {}, "anomaly_score"),
    ],
)
def test_domain_scorers_detect_reject_or_risk_signals(domain, content, metadata, dimension):
    card = score_domain(domain, content, metadata)

    assert card.scores[dimension] >= 0.4 or (
        domain == "kg" and dimension == "knowledge_freshness" and card.scores[dimension] < 0.1
    )


@pytest.mark.parametrize(
    "domain,content,metadata",
    [
        ("sync", "normal conversation with a few useful details", {}),
        ("raw_memory", "short but useful decision note", {}),
        ("kg", "Mnemos related architecture note", {"age_days": 90}),
        ("profile", "I sometimes prefer concise answers", {}),
        ("ops", "queue depth is visible but system is ok", {}),
    ],
)
def test_domain_scorers_cover_review_band_without_optional_ml(domain, content, metadata):
    card = score_domain(domain, content, metadata)
    average = sum(card.scores.values()) / len(card.scores)

    assert 0.0 <= average <= 1.0


def test_dimension_catalog_uses_raw_memory_not_memos_name():
    catalog = dimension_catalog()

    assert "raw_memory" in catalog
    assert "memos" not in catalog
    assert catalog["raw_memory"] == SCORER_DIMENSIONS["raw_memory"]
