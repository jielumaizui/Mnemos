"""P3 audit unit tests for core.kia.premise_validator private methods."""

import pytest

from core.kia.premise_validator import PremiseValidator


@pytest.fixture
def validator():
    return PremiseValidator(similarity_threshold=0.3)


# ---------------------------------------------------------------------------
# core/kia/premise_validator.py::PremiseValidator._check_semantic_match
# ---------------------------------------------------------------------------


class TestCheckSemanticMatch:
    def test_matching_premise(self, validator):
        score, reason = validator._check_semantic_match(
            "Python 是强大的编程语言",
            "Python 作为编程语言非常强大",
        )
        assert score >= validator.similarity_threshold
        assert "语义匹配度" in reason

    def test_unrelated_premise(self, validator):
        score, reason = validator._check_semantic_match(
            "Python 是强大的编程语言",
            "香蕉是黄色的水果",
        )
        assert score < validator.similarity_threshold
        assert "不足" in reason

    def test_empty_context(self, validator):
        score, reason = validator._check_semantic_match("Python", "")
        assert score == 0.0


# ---------------------------------------------------------------------------
# core/kia/premise_validator.py::PremiseValidator._check_obsolete_signals
# ---------------------------------------------------------------------------


class TestCheckObsoleteSignals:
    def test_obsolete_keyword_in_premise(self, validator):
        score, reason = validator._check_obsolete_signals(
            "使用旧版方法部署",
            "当前使用新版方法",
        )
        assert score == pytest.approx(0.7)
        assert "过时信号词" in reason

    def test_replacement_in_context(self, validator):
        score, reason = validator._check_obsolete_signals(
            "使用开发框架",
            "已经用新方案替代使用开发框架",
        )
        assert score == pytest.approx(0.4)
        assert "替代方案" in reason

    def test_no_obsolete_signal(self, validator):
        score, reason = validator._check_obsolete_signals(
            "使用 Python 框架",
            "Python 框架运行良好",
        )
        assert score == 1.0
        assert "无过时信号" in reason
