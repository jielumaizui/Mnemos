# -*- coding: utf-8 -*-
"""
阶段二测试 — 蒸馏层与评分层 V2 的桥接整合

覆盖：
  1. DistillScorerV2 基本接口
  2. layer2_value_prejudge() 的 V2 评分融合
  3. ValuePrejudgment.judge() 的 V2 路径
  4. DistillFeedbackLoop.evaluate() 的 V2 ground_truth 写入
"""

import sqlite3
from dataclasses import field
from typing import Dict, List

import pytest


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

    def test_score_with_sources(self):
        from core.scoring.scorers.distill_scorer_v2 import DistillScorerV2

        scorer = DistillScorerV2()
        result = scorer.score_with_sources("测试内容")
        assert "scores" in result
        assert "confidences" in result
        assert "features" in result
        assert "model_version" in result
        assert "should_distill" in result
        assert isinstance(result["should_distill"], bool)


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
            scores={"distill": 0.9},      # 映射到 90 分
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
            scores={"distill": 0.2},      # 映射到 20 分
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
            scores={"memos": 0.8},  # 没有 distill
            confidences={"memos": 0.7},
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
        """V2 scorer 初始化失败时回退到规则+V1。"""
        from core.hephaestus.distillation_engine import ValuePrejudgment

        vp = ValuePrejudgment()
        # 强制 V2 和 V1 都失败，走纯规则路径（避免初始化耗时）
        monkeypatch.setattr(vp, "_distill_scorer_v2", None)
        monkeypatch.setattr(vp, "_distill_scorer", None)
        monkeypatch.setattr(
            ValuePrejudgment, "_get_scorer_v2",
            lambda self: None,
        )
        monkeypatch.setattr(
            ValuePrejudgment, "_get_scorer",
            lambda self: None,
        )

        verdict, conf = vp.judge([
            {"role": "user", "content": "原来 Redis Cluster 的选举机制是这样的..."},
        ])
        assert verdict in (ValuePrejudgment.CERTAINLY_YES,
                           ValuePrejudgment.CERTAINLY_NO,
                           ValuePrejudgment.MAYBE)
        assert 0.0 <= conf <= 1.0


# ==================== 4. DistillFeedbackLoop V2 ground_truth 写入 ====================

class TestDistillFeedbackLoopV2:
    @pytest.fixture
    def db_with_gt(self, tmp_path):
        db = tmp_path / "feedback.db"
        with sqlite3.connect(str(db)) as conn:
            conn.execute("""
                CREATE TABLE ground_truth_signals (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT,
                    signal_type TEXT,
                    signal_value INTEGER,
                    confidence REAL,
                    latency_hours INTEGER,
                    created_at TEXT,
                    UNIQUE(session_id, signal_type) ON CONFLICT REPLACE
                )
            """)
        return db

    def _patch_v2_db(self, monkeypatch, db_with_gt):
        """辅助：将 V2 insert_ground_truth 重定向到临时库（避免递归）。"""
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        def _mock_insert(session_id, signal_type, label, confidence=1.0,
                         latency_hours=0, db_path=None):
            db = db_path or db_with_gt
            try:
                with sqlite3.connect(str(db)) as conn:
                    conn.execute("""
                        INSERT INTO ground_truth_signals
                        (session_id, signal_type, signal_value, confidence, latency_hours, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(session_id, signal_type) DO UPDATE SET
                            signal_value = excluded.signal_value,
                            confidence = excluded.confidence,
                            created_at = excluded.created_at
                    """, (session_id, signal_type, label, confidence, latency_hours,
                          __import__("datetime").datetime.now().isoformat()))
                    conn.commit()
            except Exception:
                pass

        monkeypatch.setattr(AdaptiveScorerV2, "insert_ground_truth", staticmethod(_mock_insert))

    def test_evaluate_writes_v2_ground_truth(self, monkeypatch, db_with_gt):
        from core.hephaestus.distillation_engine import (
            DistillFeedbackLoop,
            DistillationResult,
            ValuePrejudgment,
        )

        self._patch_v2_db(monkeypatch, db_with_gt)

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

        # V2 ground_truth 应写入
        with sqlite3.connect(str(db_with_gt)) as conn:
            rows = conn.execute(
                "SELECT signal_type, signal_value, confidence FROM ground_truth_signals WHERE session_id='sess-001'"
            ).fetchall()
            assert len(rows) >= 1
            # 预判 NO 但 judgment knowledge → expected(0.3) < actual(0.7) → label=0
            assert any(r[0] == "prejudgment_mismatch" for r in rows)

    def test_evaluate_self_check_failure_signal(self, monkeypatch, db_with_gt):
        from core.hephaestus.distillation_engine import (
            DistillFeedbackLoop,
            DistillationResult,
            KnowledgeFragment,
        )

        self._patch_v2_db(monkeypatch, db_with_gt)

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

        with sqlite3.connect(str(db_with_gt)) as conn:
            rows = conn.execute(
                "SELECT signal_type FROM ground_truth_signals WHERE session_id='sess-002'"
            ).fetchall()
            assert any(r[0] == "self_check_failure" for r in rows)

    def test_evaluate_zero_extraction_signal(self, monkeypatch, db_with_gt):
        from core.hephaestus.distillation_engine import (
            DistillFeedbackLoop,
            DistillationResult,
        )

        self._patch_v2_db(monkeypatch, db_with_gt)

        result = DistillationResult(
            session_id="sess-003",
            judgment="knowledge",
            fragments=[],  # 零提取
        )

        loop = DistillFeedbackLoop()
        signals = loop.evaluate(result)

        assert any(s["type"] == "zero_extraction" for s in signals)
