"""COG-048 historical RuleWeightStore boundary tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.kia.rule_scorer import (
    RuleScorer,
    RuleWeightStore,
    get_shared_rule_scorer,
)


ERROR = "training_admission_receipt_required"


def test_rule_weight_store_never_creates_or_reads_historical_database(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "rule_weights.db"
    store = RuleWeightStore(db_path)

    for call in (
        store.load_rules,
        lambda: store.save_rules({"noise_penalty": 0.9}),
        store.load_dimensions,
        lambda: store.save_dimensions({"distill": 1.2}),
    ):
        with pytest.raises(PermissionError, match=ERROR):
            call()

    assert not db_path.exists()


def test_rule_scorer_uses_code_owned_default_weights() -> None:
    scorer = RuleScorer(load_shared_weights=False)
    weights = {function.__name__: weight for function, weight, _enabled in scorer.rules}

    assert weights["noise_penalty"] == pytest.approx(0.15)
    assert weights["quality_score"] == pytest.approx(0.30)


def test_shared_rule_scorer_is_cold_singleton() -> None:
    first = get_shared_rule_scorer()
    second = get_shared_rule_scorer()

    assert first is second
    assert first.optimizer is None
    assert first.weight_store is None


def test_loading_shared_weights_fails_closed_without_mutating_rules(
    tmp_path: Path,
) -> None:
    store = RuleWeightStore(tmp_path / "rule_weights.db")

    with pytest.raises(PermissionError, match=ERROR):
        RuleScorer(load_shared_weights=True, weight_store=store)

    assert not (tmp_path / "rule_weights.db").exists()
