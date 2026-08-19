from core.hephaestus.cognitive_value_gate import CognitiveValueGate


def test_cognitive_value_gate_rejects_polished_generic_content():
    content = """
# Architecture Overview

- This document describes components.
- It is well formatted and easy to read.
- It contains headings and bullet points.
"""

    decision = CognitiveValueGate().evaluate(content)

    assert decision.disposition == "reject"
    assert decision.reason == "missing_cognitive_contribution"
    assert decision.contribution_types == ()


def test_cognitive_value_gate_accepts_decision_preference_and_evidence():
    content = """
## 决策记录

用户要求以后修复 Mnemos 问题时必须测试、同步文档并本地提交。
决定把这个偏好写入 policy/guard 可消费的认知资产，而不是只放普通 Wiki。
验证证据：pytest tests/unit/test_cognitive_value_gate.py。
"""

    decision = CognitiveValueGate().evaluate(
        content,
        frontmatter={"source_event_ids": ["evt-1"]},
        lifecycle_signals={"search_hits": 2, "ref_count": 1},
    )

    assert decision.disposition == "accept"
    assert "decision" in decision.contribution_types
    assert "preference" in decision.contribution_types
    assert "evidence" in decision.contribution_types
    assert "preflight_guard" in decision.consumers


def test_cognitive_value_gate_routes_uncertain_high_value_to_review():
    content = "踩坑：旧导入入口会失败。下次如果看到 database is locked，先检查 raw_projection。"

    decision = CognitiveValueGate(review_margin=0.25).evaluate(content)

    assert decision.disposition == "review"
    assert decision.reason == "cognitive_contribution_needs_review"
    assert "anti_pattern" in decision.contribution_types
    assert "future_trigger" in decision.contribution_types
