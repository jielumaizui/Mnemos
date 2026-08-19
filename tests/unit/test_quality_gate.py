from core.hephaestus.quality_gate import QualityGate


def test_quality_gate_rejects_low_quality_content():
    decision = QualityGate().evaluate("ok", uncertainty=0.1)

    assert decision.disposition == "reject"
    assert decision.accepted is False
    assert decision.reason == "score_below_threshold"
    assert "clarity" in decision.dimension_scores


def test_quality_gate_sends_high_uncertainty_to_review():
    decision = QualityGate().evaluate(
        "Short but maybe useful",
        uncertainty=0.9,
        dimension_scores={"length": 0.35, "structure": 0.35, "clarity": 0.35},
    )

    assert decision.disposition == "review"
    assert decision.accepted is False
    assert decision.reason == "uncertain_or_near_threshold"


def test_quality_gate_accepts_high_quality_content():
    content = """
# Deployment Decision

- 原因: Obsidian raw vault is the default storage path.
- 配置: LLM, embedding, and reranker keys are independent.
- 验证: run health --json and pytest.
"""

    decision = QualityGate().evaluate(content, uncertainty=0.1)

    assert decision.disposition == "accept"
    assert decision.accepted is True
    assert decision.score >= decision.threshold
