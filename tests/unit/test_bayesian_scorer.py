"""COG-048 cold/stateless Bayesian fusion contract tests."""

from __future__ import annotations

import math
import unittest
from datetime import datetime
from pathlib import Path

import pytest

from core.scoring.bayesian_scorer import (
    BetaDimensionScorer,
    BayesianScorer,
    DimensionPrior,
    DimensionScore,
)


ERROR = "training_admission_receipt_required"


class TestBetaDimensionScorer(unittest.TestCase):
    """The standalone in-memory math helper remains available."""

    def test_posterior_moves_with_observations(self) -> None:
        positive = BetaDimensionScorer("positive")
        negative = BetaDimensionScorer("negative")

        positive.observe(True)
        negative.observe(False)

        self.assertGreater(positive.posterior_mean(), 0.5)
        self.assertLess(negative.posterior_mean(), 0.5)

    def test_confidence_increases_with_samples_and_reset_restores_prior(self) -> None:
        scorer = BetaDimensionScorer("test")
        cold = scorer.posterior_confidence()
        for _ in range(20):
            scorer.observe(True)
        self.assertGreater(scorer.posterior_confidence(), cold)

        scorer.reset()
        self.assertAlmostEqual(scorer.posterior_mean(), 0.5)

    def test_pseudo_observations_clamp_inputs(self) -> None:
        rule = BetaDimensionScorer("rule")
        rule.observe_rule_prior(1.5, weight=2.0)
        self.assertEqual((rule.alpha, rule.beta), (4.0, 2.0))

        likelihood = BetaDimensionScorer("likelihood")
        likelihood.observe_likelihood(-0.5, weight=2.0)
        self.assertEqual((likelihood.alpha, likelihood.beta), (2.0, 4.0))


class TestBayesianScorerColdBoundary:
    def test_score_and_multi_score_remain_stateless(self, tmp_path: Path) -> None:
        db_path = tmp_path / "historical-bayesian.db"
        scorer = BayesianScorer(dimensions=["quality"], db_path=db_path, persistent=True)

        result = scorer.score("quality", rule_prior=0.6, ml_likelihood=0.8)
        card = scorer.score_multi(
            dimensions=["quality", "noise"],
            rule_priors={"quality": 0.6, "noise": 0.2},
        )

        assert isinstance(result, DimensionScore)
        assert result.dimension == "quality"
        assert result.sample_count == 0
        assert set(card.scores) == {"quality", "noise"}
        assert scorer.get_dimension_status("quality")["samples"] == 0
        assert not db_path.exists()

    def test_unknown_dimension_falls_back_to_ml_input(self) -> None:
        scorer = BayesianScorer(dimensions=["known"])

        score, confidence = scorer.fuse(
            "unknown",
            rule_prior=0.5,
            ml_likelihood=0.9,
            ml_confidence=0.8,
        )

        assert score == pytest.approx(0.9)
        assert confidence == pytest.approx(0.8)

    def test_all_legacy_state_mutations_fail_closed_without_writes(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "bayesian.db"
        scorer = BayesianScorer(dimensions=["kg"], db_path=db_path, persistent=True)
        before = scorer.state_to_dict()

        calls = (
            lambda: scorer.update_from_ground_truth("kg", 1),
            lambda: scorer.batch_update("kg", [1, 0]),
            lambda: scorer.set_neg_likelihood("kg", 0.1),
            lambda: scorer.restore_state({"kg": {"alpha": 9.0}}),
            lambda: scorer.feedback("kg", is_positive=True),
            lambda: scorer.reset_dimension("kg"),
        )
        for call in calls:
            with pytest.raises(PermissionError, match=ERROR):
                call()

        assert scorer.state_to_dict() == before
        assert not db_path.exists()

    def test_dimension_prior_records_local_helper_update_time(self) -> None:
        prior = DimensionPrior()
        before = datetime.now().isoformat()
        prior.update(1)
        after = datetime.now().isoformat()

        assert before <= prior.last_updated <= after


def _reference_fuse(
    prior: DimensionPrior,
    rule_prior: float,
    ml_likelihood: float,
    ml_confidence: float,
    neg_likelihood: float,
) -> tuple[float, float]:
    def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
        return max(lo, min(hi, value))

    if prior.total_samples <= 0:
        rule_weight = 3.0
    else:
        rule_weight = round(
            0.5 + 2.5 * math.exp(-prior.total_samples / 30.0),
            2,
        )
    pseudo_alpha = rule_prior * rule_weight
    pseudo_beta = (1.0 - rule_prior) * rule_weight
    p_h = clamp(rule_prior, 0.01, 0.99)
    p_e_h = clamp(ml_likelihood, 0.01, 0.99)
    p_e_not_h = clamp(neg_likelihood, 0.01, 0.99)
    evidence = p_e_h * p_h + p_e_not_h * (1.0 - p_h)
    evidence_posterior = clamp((p_e_h * p_h) / evidence if evidence > 1e-12 else p_h)
    ml_weight = ml_confidence * 2.0
    if evidence_posterior >= 0.5:
        obs_alpha = evidence_posterior * ml_weight
        obs_beta = (1.0 - evidence_posterior) * ml_weight * 0.5
    else:
        obs_alpha = evidence_posterior * ml_weight * 0.5
        obs_beta = (1.0 - evidence_posterior) * ml_weight
    fused_alpha = prior.alpha + pseudo_alpha + obs_alpha
    fused_beta = prior.beta + pseudo_beta + obs_beta
    posterior = fused_alpha / (fused_alpha + fused_beta)
    variance = (fused_alpha * fused_beta) / (
        (fused_alpha + fused_beta) ** 2 * (fused_alpha + fused_beta + 1.0)
    )
    confidence = clamp((1.0 - variance * 4.0) * 0.5 + ml_confidence * 0.5)
    return posterior, confidence


@pytest.mark.parametrize(
    "samples, rule_prior, ml_like, ml_conf, neg",
    [
        (0, 0.5, 0.8, 0.5, 0.3),
        (5, 0.3, 0.9, 0.7, 0.3),
        (10, 0.6, 0.4, 0.6, 0.1),
        (30, 0.7, 0.2, 0.5, 0.9),
        (100, 0.2, 0.5, 0.4, 0.5),
    ],
)
def test_fuse_matches_reference_implementation(
    samples: int,
    rule_prior: float,
    ml_like: float,
    ml_conf: float,
    neg: float,
) -> None:
    scorer = BayesianScorer(dimensions=["x"], neg_likelihood=neg)
    prior = DimensionPrior(alpha=1.0, beta=1.0, total_samples=samples)
    scorer.priors["x"] = prior

    expected = _reference_fuse(prior, rule_prior, ml_like, ml_conf, neg)
    actual = scorer.fuse("x", rule_prior, ml_like, ml_conf)

    assert actual[0] == pytest.approx(expected[0], abs=1e-9)
    assert actual[1] == pytest.approx(expected[1], abs=1e-9)
