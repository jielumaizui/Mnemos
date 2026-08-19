# -*- coding: utf-8 -*-
"""Layer 5 HephaestusCalibrationConsumer 输出被蒸馏/评分系统消费的测试。"""

from core.reflection.consumers import HephaestusCalibrationConsumer
from core.reflection.models import FeedbackType, ReflectionRecord, UserFeedback


class TestLayer5ConsumerWired:
    @staticmethod
    def _record(dimensions, feedback_type=None):
        record = ReflectionRecord(mirror_dimensions=list(dimensions))
        if feedback_type is not None:
            record.user_feedback = UserFeedback(feedback_type=feedback_type)
        return record

    def test_raw_reflection_feedback_does_not_write_dimension_weights(self, tmp_path):
        consumer = HephaestusCalibrationConsumer()
        # 超过最小反馈样本数，使维度权重生效
        for _ in range(11):
            consumer.on_feedback_collected(self._record(["distill"], FeedbackType.ACCURATE))
        consumer.flush()

        assert not (tmp_path / "rule_weights.db").exists()

    def test_raw_reflection_feedback_writes_neither_json_nor_shared_store(self, tmp_path):
        consumer = HephaestusCalibrationConsumer()
        consumer.on_feedback_collected(self._record(["distill"], FeedbackType.ACCURATE))
        consumer.flush()

        # 不再生成旧的 hephaestus_layer5_weights.json
        json_path = tmp_path / "hephaestus_layer5_weights.json"
        assert not json_path.exists()
        assert not (tmp_path / "rule_weights.db").exists()

    def test_distill_scorer_starts_without_layer5_dimension_weights(self):
        from core.scoring.scorers.distill_scorer_v2 import DistillScorerV2

        scorer = DistillScorerV2()

        assert scorer._dimension_weights == {}
