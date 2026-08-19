# -*- coding: utf-8 -*-
"""Unit tests for core.scoring.lightweight_nb."""

import pytest

from core.scoring.lightweight_nb import LightweightComplementNB


@pytest.fixture
def clf():
    return LightweightComplementNB(alpha=1.0)


class TestLightweightComplementNB:
    def test_fit_trains_and_sets_fitted(self, clf):
        X = [
            {"python": 5, "hello": 0},
            {"python": 4, "hello": 1},
            {"python": 0, "hello": 5},
            {"python": 1, "hello": 4},
        ]
        y = [1, 1, 0, 0]
        clf.fit(X, y)
        assert clf.is_fitted
        assert clf._class_count[1] == pytest.approx(2.0)
        assert clf._class_count[0] == pytest.approx(2.0)

    def test_partial_fit_requires_classes_on_first_call(self, clf):
        with pytest.raises(ValueError, match="classes must be provided"):
            clf.partial_fit([{"a": 1}], [1])

    def test_partial_fit_incremental_accumulates_counts(self, clf):
        clf.partial_fit([{"a": 1}, {"b": 1}], [1, 0], classes=[0, 1])
        clf.partial_fit([{"a": 2}, {"b": 2}], [1, 0])
        assert clf.is_fitted
        assert clf._class_count[1] == pytest.approx(2.0)
        assert clf._class_count[0] == pytest.approx(2.0)

    def test_predict_proba_unfitted_returns_uniform(self, clf):
        probs = clf.predict_proba([{"x": 1}])[0]
        assert probs[0] == pytest.approx(0.5)
        assert probs[1] == pytest.approx(0.5)

    def test_predict_proba_probabilities_sum_to_one(self, clf):
        X = [{"x": 1}, {"y": 1}]
        y = [1, 0]
        clf.fit(X, y)
        for probs in clf.predict_proba([{"x": 1}, {"y": 1}, {"z": 1}]):
            assert sum(probs.values()) == pytest.approx(1.0, abs=1e-6)
            assert 0.0 <= probs[0] <= 1.0
            assert 0.0 <= probs[1] <= 1.0

    def test_predict_on_separable_features(self, clf):
        X = [{"a": 5}, {"a": 4}, {"b": 5}, {"b": 4}]
        y = [1, 1, 0, 0]
        clf.fit(X, y)
        preds = clf.predict([{"a": 1}, {"b": 1}])
        # 两类应被分开（预测标签不同即可）
        assert preds[0] != preds[1]
        assert set(preds) == {0, 1}

        # 相同特征应得到相同预测
        repeat_preds = clf.predict([{"a": 1}, {"a": 1}])
        assert repeat_preds[0] == repeat_preds[1]

    def test_to_dict_from_dict_roundtrip_preserves_predictions(self, clf):
        X = [{"a": 5}, {"b": 5}]
        y = [1, 0]
        clf.fit(X, y)
        pred_before = clf.predict([{"a": 1}])
        proba_before = clf.predict_proba([{"a": 1}])[0]

        data = clf.to_dict()
        clf2 = LightweightComplementNB.from_dict(data)

        assert clf2.is_fitted
        assert clf2.alpha == clf.alpha
        assert clf2._n_features == clf._n_features
        assert clf2._classes == clf._classes
        pred_after = clf2.predict([{"a": 1}])
        proba_after = clf2.predict_proba([{"a": 1}])[0]
        assert pred_before == pred_after
        assert proba_before[0] == pytest.approx(proba_after[0], abs=1e-9)
        assert proba_before[1] == pytest.approx(proba_after[1], abs=1e-9)

    def test_empty_features_use_bias(self, clf):
        clf.fit([{}, {}], [1, 0])
        probs = clf.predict_proba([{}])[0]
        assert sum(probs.values()) == pytest.approx(1.0, abs=1e-6)
