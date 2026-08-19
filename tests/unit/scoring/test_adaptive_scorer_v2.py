# -*- coding: utf-8 -*-
"""AdaptiveScorerV2 控制面参数测试。"""

from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2


class TestAdaptiveScorerV2Config:
    def test_min_samples_per_dimension_is_configurable(self, tmp_path, patched_get_config):
        db = tmp_path / "scorer.db"
        scorer = AdaptiveScorerV2(
            domain="test",
            db_path=str(db),
            config={"training": {"min_samples_per_dimension": 7}},
        )
        assert scorer.config["training"]["min_samples_per_dimension"] == 7
