from __future__ import annotations

from pathlib import Path

import pytest

import core.kia.rule_scorer as rule_module
from core.kia.rule_scorer import RuleScorer, RuleWeightOptimizer, RuleWeightStore


ERROR = "training_admission_receipt_required"


def test_rule_optimizer_constructor_is_read_only_and_all_learning_fails_closed(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "rule_weight_optimizer.db"
    optimizer = RuleWeightOptimizer(db_path)

    assert not db_path.exists()
    calls = (
        lambda: optimizer.record_outcome("quality", 0.9, True),
        lambda: optimizer.get_rule_accuracy("quality"),
        lambda: optimizer.get_total_samples(),
        lambda: optimizer.optimize_weights({"quality": 1.0}),
        lambda: optimizer.get_weight_history(),
        lambda: optimizer.get_recent_outcomes("quality"),
        lambda: optimizer.detect_misalignment({"quality": 1.0}),
        lambda: optimizer.get_stats(),
    )
    for call in calls:
        with pytest.raises(PermissionError, match=ERROR):
            call()
    assert not db_path.exists()


def test_rule_scorer_is_cold_and_rejects_all_adaptive_effects() -> None:
    scorer = RuleScorer(load_shared_weights=False)

    assert scorer.score("完整的技术说明和具体步骤") >= 0
    calls = (
        lambda: scorer.record_rule_outcome("quality", 0.9, True),
        lambda: scorer.record_full_outcome(True, "完整内容"),
        lambda: scorer.auto_optimize(),
        lambda: scorer.get_optimizer_stats(),
        lambda: scorer.apply_pattern_adjustments(),
        lambda: scorer.refresh_weights(),
    )
    for call in calls:
        with pytest.raises(PermissionError, match=ERROR):
            call()


def test_shared_rule_scorer_does_not_attach_legacy_optimizer_or_weight_store() -> None:
    rule_module._shared_rule_scorer_instance = None  # noqa: SLF001

    scorer = rule_module.get_shared_rule_scorer()

    assert scorer.optimizer is None
    assert scorer.weight_store is None


def test_rule_weight_store_is_read_only_and_all_legacy_effects_fail_closed(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "rule_weights.db"
    store = RuleWeightStore(db_path)

    assert not db_path.exists()
    calls = (
        store.load_rules,
        lambda: store.save_rules({"quality": 0.9}),
        store.load_dimensions,
        lambda: store.save_dimensions({"distill": 1.2}),
    )
    for call in calls:
        with pytest.raises(PermissionError, match=ERROR):
            call()
    assert not db_path.exists()
