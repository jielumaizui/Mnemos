# -*- coding: utf-8 -*-
"""V1 scorer 下线后的评分层测试。

覆盖：
- core.scoring.rule_helpers 中的规则函数
- AdaptiveScorerV2._rule_score() 不再依赖 V1 scorer 类
- DistillScorerV2 桥接层基本行为
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import core.scoring.rule_helpers as rh


@dataclass
class FakeScoreCardV2:
    scores: dict
    confidences: dict = None
    features: dict = None
    model_version: str = "v2-mock"
    timestamp: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        if self.confidences is None:
            self.confidences = {k: 0.9 for k in self.scores}
        if self.features is None:
            self.features = {}


class TestRuleHelpersDistill:
    def test_distill_value_score_with_code(self):
        score = rh.distill_value_score("```python\nprint(1)\n```")
        assert 0.0 <= score <= 1.0

    def test_distill_value_score_empty(self):
        assert rh.distill_value_score("") == 0.0

    def test_falsifiability_score_with_metrics(self):
        score = rh.falsifiability_score("延迟必须小于 100ms，成功率 99.9%")
        assert score > 0.4

    def test_evolution_score_with_architecture(self):
        score = rh.evolution_score("架构选型对比：微服务 vs 单体")
        assert score > 0.4

    def test_heat_score_with_code(self):
        score = rh.heat_score("方案决定使用 Python", has_code=True)
        assert score > 0.4


class TestRuleHelpersSync:
    def test_sync_urgency_score_with_crash(self):
        score = rh.sync_urgency_score("生产环境崩溃，error 很多")
        assert score > 0.3

    def test_sync_noise_score_empty(self):
        assert rh.sync_noise_score("") == 0.0

    def test_sync_priority_score_high(self):
        score = rh.sync_priority_score("x" * 250, has_code=True, has_list=True)
        assert score > 0.8


class TestRuleHelpersKG:
    def test_entity_quality_score_with_code(self):
        score = rh.entity_quality_score("```java\nFooBar\n```")
        assert 0.0 <= score <= 1.0

    def test_relation_confidence_score_with_wiki_ref(self):
        score = rh.relation_confidence_score("参考 [[Kubernetes]] 的使用")
        assert score > 0.4

    def test_knowledge_freshness_score_decay(self):
        old = datetime.now() - timedelta(days=30)
        score = rh.knowledge_freshness_score(old, half_life_days=30)
        assert score == pytest.approx(0.5, abs=1e-3)

    def test_update_knowledge_freshness_confirm(self):
        score = rh.update_knowledge_freshness(0.8, "confirm", days_since_last=0)
        assert score == pytest.approx(1.0)

    def test_update_knowledge_freshness_contradict(self):
        score = rh.update_knowledge_freshness(0.8, "contradict", days_since_last=0)
        assert score == pytest.approx(0.4)

    def test_entity_decision(self):
        assert rh.entity_decision(0.6) == "accept"
        assert rh.entity_decision(0.4, threshold=0.3) == "tentative"
        assert rh.entity_decision(0.2, threshold=0.3) == "reject"

    def test_relation_level(self):
        assert rh.relation_level(0.8) == "strong"
        assert rh.relation_level(0.5) == "weak"
        assert rh.relation_level(0.2) == "suspect"


class TestRuleHelpersOps:
    def test_ops_anomaly_score_with_errors(self):
        score = rh.ops_anomaly_score("timeout 异常，连接失败")
        assert score > 0.3

    def test_ops_health_score_healthy(self):
        score = rh.ops_health_score("系统 healthy，任务成功完成")
        assert score > 0.8

    def test_ops_capacity_risk_score_disk(self):
        score = rh.ops_capacity_risk_score("磁盘使用率达到 95%")
        assert score > 0.6

    def test_score_system_reads_log(self, tmp_path):
        log = tmp_path / "daemon.log"
        log.write_text("system healthy\nsuccess\n", encoding="utf-8")
        result = rh.score_system(log)
        assert result["health_score"] > 0.8
        assert result["anomaly_score"] < 0.2

    def test_score_system_defaults_when_log_missing(self, tmp_path):
        result = rh.score_system(tmp_path / "nonexistent.log")
        assert result["health_score"] == pytest.approx(1.0)
        assert result["anomaly_score"] == pytest.approx(0.0)
        assert result["capacity_risk"] == pytest.approx(0.0)


class TestRuleHelpersProfile:
    def test_profile_behavior_score_working_hour(self):
        score = rh.profile_behavior_score("代码", has_code=True, hour_of_day=10)
        assert score > 0.4

    def test_profile_blind_spot_score_with_questions(self):
        score = rh.profile_blind_spot_score("怎么？为什么？如何做？")
        assert score > 0.4

    def test_profile_stability_score_repeat(self):
        score = rh.profile_stability_score(["python", "python", "go", "python"])
        assert score > 0.5

    def test_profile_stability_score_no_history(self):
        assert rh.profile_stability_score([]) == pytest.approx(0.5)


class TestAdaptiveScorerV2Bridge:
    """确认 AdaptiveScorerV2 不再依赖 V1 scorer 类，且规则分支可用。"""

    def test_scorer_map_has_only_dimension_names(self):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        for dim in AdaptiveScorerV2._SCORER_MAP:
            assert isinstance(dim, str)
        # V1 的五个域应继续存在，但 value 不再是类
        assert "distill" in AdaptiveScorerV2._SCORER_MAP
        assert "kg" in AdaptiveScorerV2._SCORER_MAP
        assert "ops" in AdaptiveScorerV2._SCORER_MAP
        assert "profile" in AdaptiveScorerV2._SCORER_MAP
        assert "sync" in AdaptiveScorerV2._SCORER_MAP

    def test_rule_score_dimensions(self, tmp_path, patched_get_config):
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        db = tmp_path / "scorer.db"
        scorer = AdaptiveScorerV2(domain="test", db_path=str(db))
        features = {
            "content": "架构选型对比，代码示例 ```python\n1\n```",
            "has_code_block": True,
            "link_count": 2,
            "_frontmatter": {"tags": ["python", "architecture"]},
        }
        dims = list(scorer._SCORER_MAP.keys()) + ["l1_storage"]
        for dim in dims:
            score, weight = scorer._rule_score(dim, None, features)
            assert isinstance(score, float)
            assert 0.0 <= score <= 1.0
            assert 0.0 <= weight <= 1.0


class TestDistillScorerV2:
    @patch("core.scoring.scorers.distill_scorer_v2.AdaptiveScorerV2")
    def test_should_distill_returns_true_when_score_exceeds_threshold(
        self, mock_cls, patched_get_config
    ):
        scorer = self._make_scorer(mock_cls, {"distill": 0.75})
        assert scorer.should_distill("some content")
        scorer._scorer.score.assert_called_once()
        _, kwargs = scorer._scorer.score.call_args
        assert kwargs.get("dimensions") == ["distill"]

    @patch("core.scoring.scorers.distill_scorer_v2.AdaptiveScorerV2")
    def test_should_distill_returns_false_when_score_below_threshold(
        self, mock_cls, patched_get_config
    ):
        scorer = self._make_scorer(mock_cls, {"distill": 0.55})
        assert not scorer.should_distill("some content")

    @patch("core.scoring.scorers.distill_scorer_v2.AdaptiveScorerV2")
    def test_should_distill_uses_custom_threshold(self, mock_cls, patched_get_config):
        scorer = self._make_scorer(mock_cls, {"distill": 0.5})
        assert not scorer.should_distill("content", threshold=0.6)
        assert scorer.should_distill("content", threshold=0.4)

    def _make_scorer(self, mock_cls, scores):
        from core.scoring.scorers.distill_scorer_v2 import DistillScorerV2

        instance = MagicMock()
        instance.score.return_value = FakeScoreCardV2(scores=scores)
        mock_cls.return_value = instance

        scorer = DistillScorerV2(config={"trigger_threshold": 0.6})
        return scorer
