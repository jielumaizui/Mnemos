# -*- coding: utf-8 -*-
"""Tests for daemon.scoring_signals."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from daemon import scoring_signals


class FakeScorer:
    inserted = []

    @classmethod
    def ensure_tables(cls, db_path):
        return None

    @classmethod
    def insert_ground_truth(cls, **kwargs):
        cls.inserted.append(kwargs)


def test_search_ignore_detection_does_not_turn_silence_into_ground_truth(tmp_path):
    FakeScorer.inserted = []
    db_path = tmp_path / "mnemos.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE search_sessions "
            "(session_id TEXT, query TEXT, result_paths TEXT, created_at TEXT, clicked_path TEXT)"
        )
        conn.execute(
            "INSERT INTO search_sessions VALUES "
            "('s1', 'q', '[]', '2026-06-24T09:00:00', '')"
        )

    cfg = SimpleNamespace(database_dir=tmp_path)
    fake_scorer_module = SimpleNamespace(AdaptiveScorerV2=FakeScorer)
    with (
        patch("core.config.get_config", return_value=cfg),
        patch.dict(sys.modules, {"core.scoring.adaptive_scorer_v2": fake_scorer_module}),
    ):
        result = scoring_signals.run_search_ignore_detection(
            lambda service_name, exc: None,
            now_func=lambda: datetime(2026, 6, 24, 10, 0, 0),
        )

    assert result == {"ignored": 0}
    assert FakeScorer.inserted == []
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(search_sessions)")}
    assert "ignored_at" not in columns
    assert "outcome_status" not in columns


def test_user_correction_detection_does_not_infer_feedback_from_mtime(tmp_path):
    FakeScorer.inserted = []
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    page = wiki_dir / "note.md"
    page.write_text(
        "---\nsource: distill\ndistilled_at: '2000-01-01 00:00:00'\n---\nbody",
        encoding="utf-8",
    )
    cfg = SimpleNamespace(wiki_dir=wiki_dir, database_dir=tmp_path)
    fake_scorer_module = SimpleNamespace(AdaptiveScorerV2=FakeScorer)

    with (
        patch("core.config.get_config", return_value=cfg),
        patch.dict(sys.modules, {"core.scoring.adaptive_scorer_v2": fake_scorer_module}),
    ):
        result = scoring_signals.run_user_correction_detection(
            lambda service_name, exc: None,
        )

    assert result == {"corrections": 0}
    assert FakeScorer.inserted == []
