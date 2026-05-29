# -*- coding: utf-8 -*-
"""
P0-1 长链路测试 — 评分 V2 训练闭环

链路：insert_ground_truth → scorer_training_queue → process_training_queue
      → partial_fit → save_model → 新实例 load_model → score

策略：临时 SQLite + LightweightNB（不依赖 sklearn），只 mock 外部网络。
断言目标：DB 记录、模型版本、预测结果合理性。
"""

import json
import sqlite3
from pathlib import Path

import pytest


def _create_schema(db: Path) -> None:
    """创建评分层所需的最小表结构。"""
    with sqlite3.connect(str(db)) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ground_truth_signals (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                signal_value INTEGER NOT NULL,
                confidence REAL DEFAULT 1.0,
                latency_hours INTEGER DEFAULT 0,
                created_at TEXT,
                UNIQUE(session_id, signal_type) ON CONFLICT REPLACE
            );

            CREATE TABLE IF NOT EXISTS scorer_training_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                dimension TEXT NOT NULL,
                features_json TEXT NOT NULL,
                priority INTEGER DEFAULT 0,
                earliest_train_at TEXT,
                status TEXT DEFAULT 'pending',
                retry_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS scorer_models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dimension TEXT NOT NULL,
                model_version TEXT NOT NULL,
                model_type TEXT,
                model_blob BLOB,
                model_hash TEXT,
                train_samples INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                meta_json TEXT
            );
        """)


def _insert_training_queue(conn, session_id: str, dimension: str,
                           features: dict, status: str = "pending") -> None:
    conn.execute("""
        INSERT INTO scorer_training_queue
        (session_id, dimension, features_json, status, earliest_train_at)
        VALUES (?, ?, ?, ?, datetime('now'))
    """, (session_id, dimension, json.dumps(features), status))
    conn.commit()


class TestScorerV2TrainingLoop:
    """评分 V2 完整训练闭环 — 使用真实 SQLite 和 LightweightNB。"""

    @pytest.fixture
    def db(self, tmp_path):
        db = tmp_path / "scorer_v2_loop.db"
        _create_schema(db)
        return db

    def test_full_loop_lightweight_nb(self, monkeypatch, db):
        """
        完整闭环：
        1. 写入 ground_truth + training_queue
        2. process_training_queue → fit → save_model
        3. 新实例 load_model → score
        """
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        # 强制使用 lightweight 后端（不依赖 sklearn）
        monkeypatch.setattr(
            "core.scoring.adaptive_scorer_v2._SKLEARN_AVAILABLE", False,
        )

        scorer = AdaptiveScorerV2(
            domain="mnemos",
            config={"backend": "lightweight"},
            db_path=str(db),
        )

        # ── 步骤 1：构造并写入训练数据 ──
        samples = [
            ("sess-001", {"content_len": 100, "content_words": 20,
                           "has_code_block": 1, "fm_heat": 0.9}, 1),
            ("sess-002", {"content_len": 50, "content_words": 10,
                           "has_code_block": 0, "fm_heat": 0.2}, 0),
            ("sess-003", {"content_len": 200, "content_words": 40,
                           "has_code_block": 1, "fm_heat": 0.8}, 1),
            ("sess-004", {"content_len": 30, "content_words": 5,
                           "has_code_block": 0, "fm_heat": 0.1}, 0),
            ("sess-005", {"content_len": 150, "content_words": 30,
                           "has_code_block": 1, "fm_heat": 0.85}, 1),
            ("sess-006", {"content_len": 40, "content_words": 8,
                           "has_code_block": 0, "fm_heat": 0.15}, 0),
            ("sess-007", {"content_len": 180, "content_words": 35,
                           "has_code_block": 1, "fm_heat": 0.75}, 1),
            ("sess-008", {"content_len": 25, "content_words": 4,
                           "has_code_block": 0, "fm_heat": 0.05}, 0),
            ("sess-009", {"content_len": 120, "content_words": 25,
                           "has_code_block": 1, "fm_heat": 0.9}, 1),
            ("sess-010", {"content_len": 60, "content_words": 12,
                           "has_code_block": 0, "fm_heat": 0.2}, 0),
            # 再补 10 条让 memos 维度超过 min_samples=20
            ("sess-011", {"content_len": 110, "content_words": 22,
                           "has_code_block": 1, "fm_heat": 0.88}, 1),
            ("sess-012", {"content_len": 55, "content_words": 11,
                           "has_code_block": 0, "fm_heat": 0.18}, 0),
            ("sess-013", {"content_len": 190, "content_words": 38,
                           "has_code_block": 1, "fm_heat": 0.82}, 1),
            ("sess-014", {"content_len": 35, "content_words": 7,
                           "has_code_block": 0, "fm_heat": 0.12}, 0),
            ("sess-015", {"content_len": 130, "content_words": 26,
                           "has_code_block": 1, "fm_heat": 0.92}, 1),
            ("sess-016", {"content_len": 45, "content_words": 9,
                           "has_code_block": 0, "fm_heat": 0.22}, 0),
            ("sess-017", {"content_len": 170, "content_words": 34,
                           "has_code_block": 1, "fm_heat": 0.78}, 1),
            ("sess-018", {"content_len": 28, "content_words": 6,
                           "has_code_block": 0, "fm_heat": 0.08}, 0),
            ("sess-019", {"content_len": 140, "content_words": 28,
                           "has_code_block": 1, "fm_heat": 0.86}, 1),
            ("sess-020", {"content_len": 52, "content_words": 10,
                           "has_code_block": 0, "fm_heat": 0.16}, 0),
            ("sess-021", {"content_len": 160, "content_words": 32,
                           "has_code_block": 1, "fm_heat": 0.8}, 1),
            ("sess-022", {"content_len": 33, "content_words": 6,
                           "has_code_block": 0, "fm_heat": 0.14}, 0),
            ("sess-023", {"content_len": 125, "content_words": 25,
                           "has_code_block": 1, "fm_heat": 0.9}, 1),
            ("sess-024", {"content_len": 48, "content_words": 9,
                           "has_code_block": 0, "fm_heat": 0.19}, 0),
        ]

        with sqlite3.connect(str(db)) as conn:
            for session_id, features, label in samples:
                conn.execute("""
                    INSERT INTO ground_truth_signals
                    (session_id, signal_type, signal_value, confidence, created_at)
                    VALUES (?, ?, ?, 1.0, datetime('now'))
                """, (session_id, "user_feedback", label))
                _insert_training_queue(conn, session_id, "memos", features)

        # ── 步骤 2：训练 ──
        trained = scorer.process_training_queue(dimension="memos")
        assert trained > 0, "训练应消耗至少部分样本"

        # ── 步骤 3：验证模型已保存到 DB ──
        with sqlite3.connect(str(db)) as conn:
            rows = conn.execute(
                "SELECT dimension, model_version, model_type, model_hash, meta_json "
                "FROM scorer_models WHERE dimension='memos'"
            ).fetchall()
            assert len(rows) >= 1, "模型应已持久化到 scorer_models"
            dim, version, mtype, mhash, meta = rows[0]
            assert dim == "memos"
            assert version, "应有版本号"
            assert mhash, "应有 hash"
            assert meta, "应有 meta_json"
            meta_dict = json.loads(meta)
            assert meta_dict.get("schema_version")
            assert meta_dict.get("python_version")

        # ── 步骤 4：新实例加载模型并预测 ──
        scorer2 = AdaptiveScorerV2(
            domain="mnemos",
            config={"backend": "lightweight"},
            db_path=str(db),
        )
        loaded = scorer2.load_model("memos")
        assert loaded is not None, "load_model 应成功加载已保存的模型"

        # 高价值内容应得高分
        high_value = scorer2.score(
            {"content": "Python asyncio best practices", "frontmatter": {"heat": 0.9}},
            dimensions=["memos"],
        )
        # 低价值内容应得低分
        low_value = scorer2.score(
            {"content": "ok", "frontmatter": {"heat": 0.1}},
            dimensions=["memos"],
        )

        # 训练后的模型应能区分高低价值（不要求硬阈值，但顺序应正确）
        assert high_value.scores["memos"] > low_value.scores["memos"], \
            f"高价值应高于低价值: {high_value.scores['memos']} vs {low_value.scores['memos']}"

    def test_ground_truth_insert_idempotent(self, db):
        """同一 session_id + signal_type 重复插入应更新而非重复。"""
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

        AdaptiveScorerV2.insert_ground_truth(
            session_id="sess-dup",
            signal_type="search_hit",
            label=1,
            db_path=db,
        )
        AdaptiveScorerV2.insert_ground_truth(
            session_id="sess-dup",
            signal_type="search_hit",
            label=0,
            db_path=db,
        )

        with sqlite3.connect(str(db)) as conn:
            rows = conn.execute(
                "SELECT signal_value FROM ground_truth_signals WHERE session_id='sess-dup'"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0][0] == 0  # 第二次更新覆盖了第一次

    def test_save_and_load_roundtrip_with_hash_validation(self, monkeypatch, db):
        """模型保存后，篡改 blob 应导致加载失败（hash 校验）。"""
        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2, _SCHEMA_VERSION
        import pickle
        import hashlib

        monkeypatch.setattr(
            "core.scoring.adaptive_scorer_v2._SKLEARN_AVAILABLE", False,
        )

        scorer = AdaptiveScorerV2(
            domain="mnemos", config={"backend": "lightweight"}, db_path=str(db),
        )

        # 手动训练并保存
        from core.scoring.lightweight_nb import LightweightComplementNB
        model = LightweightComplementNB()
        model.fit(
            [{"a": 1}, {"b": 1}, {"a": 2}, {"b": 2}],
            [1, 0, 1, 0],
        )
        scorer._models["memos"] = model
        version = scorer.save_model("memos")
        assert version

        # 篡改 blob
        with sqlite3.connect(str(db)) as conn:
            row = conn.execute(
                "SELECT model_blob FROM scorer_models WHERE dimension='memos'"
            ).fetchone()
            corrupted = row[0][:10] + b"CORRUPTED" + row[0][20:]
            conn.execute(
                "UPDATE scorer_models SET model_blob = ? WHERE dimension='memos'",
                (corrupted,),
            )
            conn.commit()

        # 加载应失败并返回 None
        loaded = scorer.load_model("memos")
        assert loaded is None, "hash 不匹配时应拒绝加载"
