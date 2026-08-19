# -*- coding: utf-8 -*-
"""rule_helpers cold code-owned weight tests."""

import pytest

from core.kia.rule_scorer import RuleScorer
import core.scoring.rule_helpers as rh


class TestRuleHelpersColdWeights:
    def test_distill_value_score_uses_code_owned_weights(self, monkeypatch):
        from core.kia import rule_scorer as mod

        content = "这是一个关于 Redis 连接池配置的技术讨论。"
        scorer = RuleScorer(load_shared_weights=False)
        monkeypatch.setattr(mod, "get_shared_rule_scorer", lambda: scorer)

        first = rh.distill_value_score(content)
        second = rh.distill_value_score(content)

        assert first == pytest.approx(second)
        assert scorer.weight_store is None
