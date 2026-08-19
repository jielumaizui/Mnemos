from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from core.cognitive.access_control import make_cognitive_access_envelope


def _provenance(
    *,
    owner: str = "principal:scoring-test",
    scope_id: str = "session-scoring-delete",
) -> dict[str, object]:
    return make_cognitive_access_envelope(
        owner_principal_id=owner,
        owner_agent="codex",
        scope_type="session",
        scope_id=scope_id,
        session_id=scope_id,
        project="mnemos",
        purposes=("score_training",),
        consent_provenance_refs=("sha256:" + "a" * 64,),
        sensitivity="sensitive",
        retention_policy="test",
        source_acl_lineage=("sha256:" + "b" * 64,),
    )


def _create_historical_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE scorer_training_queue (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE ground_truth_signals (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE scorer_models (
            id INTEGER PRIMARY KEY,
            dimension TEXT NOT NULL
        )
        """
    )


def _seed_tracked_history(db_path) -> tuple[int, int, int]:
    from core.scoring.subject_provenance import (
        record_scoring_derived_object,
        record_scoring_subject_provenance,
    )

    access = _provenance()
    with sqlite3.connect(db_path) as conn:
        _create_historical_tables(conn)
        queue_id = int(
            conn.execute(
                "INSERT INTO scorer_training_queue(session_id) VALUES (?)",
                ("historical-queue",),
            ).lastrowid
        )
        truth_id = int(
            conn.execute(
                "INSERT INTO ground_truth_signals(session_id) VALUES (?)",
                ("historical-truth",),
            ).lastrowid
        )
        model_id = int(conn.execute("INSERT INTO scorer_models(dimension) VALUES ('kg')").lastrowid)
        record_scoring_subject_provenance(
            conn,
            object_type="training_queue",
            object_id=str(queue_id),
            subject_provenance=access,
        )
        record_scoring_subject_provenance(
            conn,
            object_type="ground_truth",
            object_id=str(truth_id),
            subject_provenance=access,
        )
        record_scoring_derived_object(
            conn,
            object_type="model",
            object_id=str(model_id),
            source_refs=(
                ("training_queue", str(queue_id)),
                ("ground_truth", str(truth_id)),
            ),
        )
        conn.commit()
    return queue_id, truth_id, model_id


def test_historical_scoring_delete_removes_exact_objects_and_derived_model(
    tmp_path,
):
    from core.scoring.subject_provenance import delete_scoring_subject_scope

    db_path = tmp_path / "mnemos.db"
    _seed_tracked_history(db_path)

    result = delete_scoring_subject_scope(
        db_path=db_path,
        request_id="delete-scoring-subject",
        scope_kind="session",
        scope_value="session-scoring-delete",
    )

    assert result["status"] == "applied"
    assert result["verified"] is True
    assert result["training_samples_deleted"] == 1
    assert result["ground_truth_deleted"] == 1
    assert result["models_invalidated"] == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM scorer_training_queue").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ground_truth_signals").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM scorer_models").fetchone()[0] == 0


def test_historical_delete_never_guesses_unattributed_rows(tmp_path):
    from core.scoring.subject_provenance import delete_scoring_subject_scope

    db_path = tmp_path / "mnemos.db"
    with sqlite3.connect(db_path) as conn:
        _create_historical_tables(conn)
        conn.execute("INSERT INTO scorer_training_queue(session_id) VALUES ('unattributed')")
        conn.commit()

    result = delete_scoring_subject_scope(
        db_path=db_path,
        request_id="delete-scoring-unattributed",
        scope_kind="session",
        scope_value="someone-else",
    )

    assert result["status"] == "applied"
    assert result["target_count"] == 0
    assert result["unresolved_legacy_count"] == 1
    assert result["verified"] is False
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM scorer_training_queue").fetchone()[0] == 1


def test_historical_delete_resume_rechecks_tombstoned_bodies(tmp_path):
    from core.scoring.subject_provenance import (
        RECEIPT_TABLE,
        delete_scoring_subject_scope,
    )

    db_path = tmp_path / "mnemos.db"
    queue_id, _, _ = _seed_tracked_history(db_path)
    initial = delete_scoring_subject_scope(
        db_path=db_path,
        request_id="delete-scoring-resume",
        scope_kind="session",
        scope_value="session-scoring-delete",
    )
    assert initial["status"] == "applied"

    with sqlite3.connect(db_path) as conn:
        conn.execute(f"UPDATE {RECEIPT_TABLE} SET status='flushed', applied_at=''")
        conn.execute(
            """
            INSERT INTO scorer_training_queue(id, session_id)
            VALUES (?, 'resurrected-history')
            """,
            (queue_id,),
        )
        conn.commit()

    resumed = delete_scoring_subject_scope(
        db_path=db_path,
        request_id="delete-scoring-resume-retry",
        scope_kind="session",
        scope_value="session-scoring-delete",
    )
    assert resumed["status"] == "blocked"
    assert resumed["error"] == "scoring_subject_after_oracle_nonzero"


def test_historical_provenance_is_immutable(tmp_path):
    from core.scoring.subject_provenance import record_scoring_subject_provenance

    db_path = tmp_path / "mnemos.db"
    with sqlite3.connect(db_path) as conn:
        _create_historical_tables(conn)
        object_id = str(
            conn.execute(
                "INSERT INTO scorer_training_queue(session_id) VALUES ('history')"
            ).lastrowid
        )
        record_scoring_subject_provenance(
            conn,
            object_type="training_queue",
            object_id=object_id,
            subject_provenance=_provenance(),
        )
        with pytest.raises(ValueError, match="immutable scoring provenance conflict"):
            record_scoring_subject_provenance(
                conn,
                object_type="training_queue",
                object_id=object_id,
                subject_provenance=_provenance(
                    owner="principal:other",
                    scope_id="other-session",
                ),
            )


def test_all_retired_scoring_feedback_writers_are_fail_closed(tmp_path):
    from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2
    from core.scoring.bayesian_scorer import BayesianScorer
    from core.scoring.feedback_persistence import persist_identified_feedback

    db_path = tmp_path / "retired.db"
    with pytest.raises(
        PermissionError,
        match="training_admission_receipt_required:enqueue_training_sample",
    ):
        AdaptiveScorerV2.enqueue_training_sample(
            session_id="legacy",
            dimension="kg",
            features={},
            expected_score=1.0,
            source="test",
            db_path=str(db_path),
            subject_provenance=_provenance(),
        )
    with pytest.raises(
        PermissionError,
        match="training_admission_receipt_required:bayesian_feedback",
    ):
        BayesianScorer(dimensions=["kg"], db_path=db_path).feedback(
            "kg",
            True,
            subject_provenance=_provenance(),
        )
    feedback = SimpleNamespace(
        features={},
        dimension="kg",
        session_id="legacy",
        subject_provenance=_provenance(),
    )
    with pytest.raises(
        PermissionError,
        match="training_admission_receipt_required:identified_feedback",
    ):
        persist_identified_feedback(
            db_path,
            feedback,
            feedback_event_id="feedback-1",
            dimension="kg",
            label=1,
            confidence=1.0,
        )
    assert not db_path.exists()
