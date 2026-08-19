# -*- coding: utf-8 -*-
"""
阶段二测试 — 蒸馏层与评分层 V2 的桥接整合

覆盖：
  1. DistillScorerV2 基本接口
  2. layer2_value_prejudge() 的 V2 评分融合
  3. ValuePrejudgment.judge() 的 V2 路径
  4. DistillFeedbackLoop.evaluate() 的 V2 ground_truth 写入
"""

# ==================== 1. DistillScorerV2 ====================


class TestDistillScorerV2:
    def test_score_returns_scorecardv2(self):
        from core.scoring.scorers.distill_scorer_v2 import DistillScorerV2

        scorer = DistillScorerV2()
        card = scorer.score("Redis Cluster 方案选择：采用三主三从架构。")

        assert isinstance(card.scores, dict)
        assert "distill" in card.scores
        assert 0.0 <= card.scores["distill"] <= 1.0
        assert card.features["content_len"] > 0

    def test_default_dimensions_include_l1_alias(self):
        from core.scoring.scorers.distill_scorer_v2 import DistillScorerV2

        scorer = DistillScorerV2()
        card = scorer.score("Redis Cluster 方案选择：采用三主三从架构。")

        # DEFAULT_DIMENSIONS 中的 "l1" 是 "l1_storage" 别名，必须命中规则先验。
        assert "l1" in card.scores
        # 默认 frontmatter 为空，规则先验给出 0.735 左右；若未命中规则则 fusion 返回 0.5/0.0。
        assert card.scores["l1"] > 0.55
        assert card.confidences["l1"] > 0.0

    def test_should_distill_above_threshold(self):
        from core.scoring.scorers.distill_scorer_v2 import DistillScorerV2

        scorer = DistillScorerV2()
        # 使用有明显知识信号的内容
        content = (
            "决定采用 Kafka 而非 RabbitMQ，原因是吞吐量需求 100k msg/s，"
            "RabbitMQ 在集群模式下无法满足。"
        )
        card = scorer.score(content)
        should = scorer.should_distill(content, threshold=0.3)
        # 阈值 0.3 应该触发蒸馏（内容有决策信号）
        assert isinstance(should, bool)
        # 验证 score 和 should_distill 一致
        assert should == (card.scores.get("distill", 0.0) > 0.3)

    def test_should_distill_below_threshold(self):
        from core.scoring.scorers.distill_scorer_v2 import DistillScorerV2

        scorer = DistillScorerV2()
        # 低价值内容
        content = "好的，收到。"
        should = scorer.should_distill(content, threshold=0.9)
        assert should is False

    def test_trigger_threshold_from_effective_policy(self, monkeypatch):
        """触发阈值应从 EffectivePolicy 读取。"""

        class _FakePolicy:
            def __init__(self, values):
                self._values = values

            def get(self, key, default=None):
                return self._values.get(key, default)

        from core.scoring.scorers.distill_scorer_v2 import DistillScorerV2
        from core.scoring.scorers import distill_scorer_v2 as mod

        monkeypatch.setattr(
            mod,
            "get_effective_policy",
            lambda: _FakePolicy({"distill.trigger_threshold": 0.95}),
        )
        scorer = DistillScorerV2()
        assert scorer._trigger_threshold == 0.95


# ==================== 2. layer2_value_prejudge V2 融合 ====================


class TestLayer2ValuePrejudgeV2:
    def test_rule_only_backward_compatible(self):
        """不传 V2 时行为与之前一致。"""
        from core.kia.ingest_helpers import layer2_value_prejudge

        result = layer2_value_prejudge(
            content="test",
            rule_score={"total_score": 80.0},
        )
        assert result["decision"] == "direct_distill"
        assert result["score"] == 80.0
        assert result["sources"] == {"rule": 80.0}

    def test_v2_fusion_increases_score(self):
        """V2 评分与规则评分融合后取平均。"""
        from core.kia.ingest_helpers import layer2_value_prejudge
        from core.scoring.adaptive_scorer_v2 import ScoreCardV2

        v2_card = ScoreCardV2(
            scores={"distill": 0.9},  # 映射到 90 分
            confidences={"distill": 0.8},
            features={"content_len": 100},
            model_version="v2-test",
        )
        result = layer2_value_prejudge(
            content="test",
            rule_score={"total_score": 70.0},
            v2_score=v2_card,
        )
        # (70 + 90) / 2 = 80 → direct_distill
        assert result["decision"] == "direct_distill"
        assert result["score"] == 80.0
        assert result["sources"] == {"rule": 70.0, "v2_distill": 90.0}

    def test_v2_fusion_decreases_score(self):
        """V2 评分低时拉低总分。"""
        from core.kia.ingest_helpers import layer2_value_prejudge
        from core.scoring.adaptive_scorer_v2 import ScoreCardV2

        v2_card = ScoreCardV2(
            scores={"distill": 0.2},  # 映射到 20 分
            confidences={"distill": 0.5},
            features={},
            model_version="v2-test",
        )
        result = layer2_value_prejudge(
            content="test",
            rule_score={"total_score": 50.0},
            v2_score=v2_card,
        )
        # (50 + 20) / 2 = 35 → llm_judge
        assert result["decision"] == "llm_judge"
        assert result["score"] == 35.0
        assert "V2 distill=0.20" in result["reason"]

    def test_v2_missing_distill_dimension_falls_back(self):
        """V2 ScoreCard 没有 distill 维度时回退到 rule-only。"""
        from core.kia.ingest_helpers import layer2_value_prejudge
        from core.scoring.adaptive_scorer_v2 import ScoreCardV2

        v2_card = ScoreCardV2(
            scores={"backend": 0.8},  # 没有 distill
            confidences={"backend": 0.7},
            features={},
            model_version="v2-test",
        )
        result = layer2_value_prejudge(
            content="test",
            rule_score={"total_score": 75.0},
            v2_score=v2_card,
        )
        assert result["decision"] == "direct_distill"
        assert result["score"] == 75.0  # 只用 rule


# ==================== 3. ValuePrejudgment V2 路径 ====================


class TestValuePrejudgmentV2:
    def test_judge_falls_back_when_v2_unavailable(self, monkeypatch):
        """V2 scorer 初始化失败时回退到纯规则评估。"""
        from core.hephaestus.distillation_engine import ValuePrejudgment

        vp = ValuePrejudgment()
        # 强制 V2 失败，走纯规则路径（避免初始化耗时）
        monkeypatch.setattr(vp, "_distill_scorer_v2", None)
        monkeypatch.setattr(
            ValuePrejudgment,
            "_get_scorer_v2",
            lambda self: None,
        )

        verdict, conf = vp.judge(
            [
                {"role": "user", "content": "原来 Redis Cluster 的选举机制是这样的..."},
            ]
        )
        assert verdict in (
            ValuePrejudgment.CERTAINLY_YES,
            ValuePrejudgment.CERTAINLY_NO,
            ValuePrejudgment.MAYBE,
        )
        assert 0.0 <= conf <= 1.0


# ==================== 4. DistillFeedbackLoop operational signals ====================


class TestDistillFeedbackLoopV2:
    @staticmethod
    def _reject_legacy_training_sink(monkeypatch):
        """Install a tripwire proving operational signals never become labels."""
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        def _unexpected_legacy_write(*_args, **_kwargs):
            raise AssertionError("distillation feedback reached legacy training sink")

        monkeypatch.setattr(
            AdaptiveScorerV2,
            "insert_ground_truth",
            staticmethod(_unexpected_legacy_write),
        )

    def test_evaluate_emits_prejudgment_signal_without_legacy_training(self, monkeypatch):
        from core.hephaestus.distillation_engine import (
            DistillFeedbackLoop,
            DistillationResult,
            ValuePrejudgment,
        )

        self._reject_legacy_training_sink(monkeypatch)

        result = DistillationResult(
            session_id="sess-001",
            prejudgment=ValuePrejudgment.CERTAINLY_NO,
            judgment="knowledge",  # 与预判不一致 → 产生信号
            fragments=[],
        )

        loop = DistillFeedbackLoop()
        signals = loop.evaluate(result)

        # 应生成 prejudgment_mismatch 信号
        assert any(s["type"] == "prejudgment_mismatch" for s in signals)

        mismatch = next(s for s in signals if s["type"] == "prejudgment_mismatch")
        assert mismatch["expected"] == 0.3
        assert mismatch["actual"] == 0.7

    def test_evaluate_emits_self_check_signal_without_legacy_training(self, monkeypatch):
        from core.hephaestus.distillation_engine import (
            DistillFeedbackLoop,
            DistillationResult,
            KnowledgeFragment,
        )

        self._reject_legacy_training_sink(monkeypatch)

        # 构造自检失败的 fragment
        frag = KnowledgeFragment(
            form="decision",
            title="x",
            frontmatter={},
            background="",
            core_content="bad",
            boundaries={},
            anti_patterns=[],
            related_concepts=[],
        )
        frag.self_check_passed = False
        frag.self_check_issues = ["标题过短"]

        result = DistillationResult(
            session_id="sess-002",
            judgment="knowledge",
            fragments=[frag, frag],  # 2/2 失败 → 失败率 100% > 50%
        )

        loop = DistillFeedbackLoop()
        signals = loop.evaluate(result)

        assert any(s["type"] == "self_check_failure" for s in signals)

        failure = next(s for s in signals if s["type"] == "self_check_failure")
        assert failure["actual"] == 0.0

    def test_evaluate_zero_extraction_signal(self, monkeypatch):
        from core.hephaestus.distillation_engine import (
            DistillFeedbackLoop,
            DistillationResult,
        )

        self._reject_legacy_training_sink(monkeypatch)

        result = DistillationResult(
            session_id="sess-003",
            judgment="knowledge",
            fragments=[],  # 零提取
        )

        loop = DistillFeedbackLoop()
        signals = loop.evaluate(result)

        assert any(s["type"] == "zero_extraction" for s in signals)


# ==================== 5. DistillFeedbackLoop 关系置信度更新（P1-12） ====================


def _make_fragment_with_relation(relations):
    from core.hephaestus.distillation_engine import KnowledgeFragment

    return KnowledgeFragment(
        form="concept",
        title="关系测试",
        frontmatter={"领域": "test", "摘要": "测试关系置信度更新。"},
        background="",
        core_content="## 核心内容\n\n这是为了保证内容长度超过阈值而添加的说明文字。",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
        relations=relations,
    )


def test_evaluate_updates_relation_confidence_on_success(monkeypatch):
    """高质量蒸馏结果应对片段关系给出正向置信度反馈。"""
    from core.hephaestus.distillation_engine import (
        DistillFeedbackLoop,
        DistillationResult,
    )
    from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

    monkeypatch.setattr(
        AdaptiveScorerV2, "enqueue_training_sample", staticmethod(lambda **kwargs: None)
    )
    monkeypatch.setattr(
        AdaptiveScorerV2, "insert_ground_truth", staticmethod(lambda **kwargs: None)
    )

    updated = []

    def _fake_update_confidence(self, source, target, relation_type, feedback):
        updated.append((source, target, relation_type, feedback))

    monkeypatch.setattr(
        "core.kia.relation_manager.RelationManager.update_confidence",
        _fake_update_confidence,
    )

    frag = _make_fragment_with_relation(
        [
            {"source": "Redis", "target": "[[Docker]]", "type": "related_to", "context": ""},
        ]
    )
    result = DistillationResult(
        session_id="sess-rel-ok",
        judgment="knowledge",
        fragments=[frag],
    )

    loop = DistillFeedbackLoop()
    loop.evaluate(result)

    assert len(updated) == 1
    assert updated[0] == ("Redis", "Docker", "related_to", 1.0)


def test_evaluate_updates_relation_confidence_on_failure(monkeypatch):
    """低质量/跳过结果应对片段关系给出负向置信度反馈。"""
    from core.hephaestus.distillation_engine import (
        DistillFeedbackLoop,
        DistillationResult,
    )
    from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

    monkeypatch.setattr(
        AdaptiveScorerV2, "enqueue_training_sample", staticmethod(lambda **kwargs: None)
    )
    monkeypatch.setattr(
        AdaptiveScorerV2, "insert_ground_truth", staticmethod(lambda **kwargs: None)
    )

    updated = []

    def _fake_update_confidence(self, source, target, relation_type, feedback):
        updated.append((source, target, relation_type, feedback))

    monkeypatch.setattr(
        "core.kia.relation_manager.RelationManager.update_confidence",
        _fake_update_confidence,
    )

    frag = _make_fragment_with_relation(
        [
            {"source": "A", "target": "B", "type": "depends_on", "context": ""},
        ]
    )
    result = DistillationResult(
        session_id="sess-rel-bad",
        judgment="skip",
        fragments=[frag],
    )

    loop = DistillFeedbackLoop()
    loop.evaluate(result)

    assert len(updated) == 1
    assert updated[0][3] == 0.0
