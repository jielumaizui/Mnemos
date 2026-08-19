from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.scoring.adaptive_scorer_support import FeedbackV2
from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2


ERROR = "training_admission_receipt_required"


def test_fresh_adaptive_scorer_does_not_create_legacy_training_tables(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mnemos.db"

    AdaptiveScorerV2.ensure_tables(str(db_path))

    with sqlite3.connect(db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "search_sessions" in tables
    assert (
        not {
            "ground_truth_signals",
            "scorer_training_queue",
            "scorer_feedback_events",
            "scorer_models",
            "bayesian_scorer_state",
            "bayesian_feedback",
        }
        & tables
    )


def test_all_legacy_training_and_model_entrypoints_fail_closed(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mnemos.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE safety_sentinel(value TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO safety_sentinel VALUES ('unchanged')")
        conn.commit()
    scorer = AdaptiveScorerV2(db_path=str(db_path))
    before = db_path.read_bytes()
    feedback = FeedbackV2(
        session_id="legacy-feedback",
        dimension="kg",
        expected=1.0,
        actual=1.0,
        features={"caller_label": 1},
    )

    calls = (
        lambda: scorer.feedback(feedback),
        lambda: AdaptiveScorerV2.enqueue_training_sample(
            "legacy-queue",
            "kg",
            {"caller_feature": 1},
            1.0,
            "legacy-test",
            str(db_path),
        ),
        lambda: AdaptiveScorerV2.insert_ground_truth(
            "legacy-ground-truth",
            "kg",
            1,
            db_path=db_path,
        ),
        lambda: scorer.process_training_queue(),
        lambda: scorer.save_model("kg"),
        lambda: scorer.load_model("kg"),
        lambda: scorer.refresh_bayesian_priors_from_ground_truth(),
        lambda: scorer.rollback_model("kg", "legacy-version"),
    )
    for call in calls:
        with pytest.raises(PermissionError, match=ERROR):
            call()

    assert db_path.read_bytes() == before
    assert not (tmp_path / "mnemos.db-wal").exists()


def test_constructor_ignores_historical_active_model_and_bayesian_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mnemos.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE scorer_models ("
            "id INTEGER PRIMARY KEY, dimension TEXT, model_version TEXT, "
            "model_type TEXT, model_blob BLOB, model_hash TEXT, "
            "train_samples INTEGER, is_active INTEGER, created_at TEXT, meta_json TEXT)"
        )
        conn.execute(
            "INSERT INTO scorer_models VALUES "
            "(1, 'kg', 'legacy', 'lightweight_nb_json', X'7B7D', '', 100, 1, "
            "'2026-07-19T00:00:00+00:00', '{}')"
        )
        conn.execute(
            "CREATE TABLE bayesian_scorer_state ("
            "dimension TEXT PRIMARY KEY, alpha REAL, beta REAL, prior_alpha REAL, "
            "prior_beta REAL, total_samples INTEGER, neg_likelihood REAL, "
            "last_updated TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO bayesian_scorer_state VALUES " "('kg', 99, 1, 1, 1, 98, 0.3, '', '')"
        )

    scorer = AdaptiveScorerV2(db_path=str(db_path))

    assert scorer._models == {}  # noqa: SLF001
    assert scorer._bayesian.priors["kg"].total_samples == 0  # noqa: SLF001
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT is_active FROM scorer_models WHERE id=1").fetchone() == (1,)
        assert conn.execute(
            "SELECT alpha, beta FROM bayesian_scorer_state WHERE dimension='kg'"
        ).fetchone() == (99.0, 1.0)
