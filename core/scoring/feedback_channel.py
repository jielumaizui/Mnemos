"""Small V2 feedback channel for AdaptiveScorerV2."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


@dataclass(frozen=True)
class FeedbackSignal:
    """Historical input shape retained for migration/audit reads only."""

    subject: str
    action: str
    dimension: str
    confidence: float = 0.8
    metadata: Dict[str, Any] = field(default_factory=dict)
    source: str = "feedback_channel"
    subject_provenance: Mapping[str, Any] | None = None


class FeedbackFatigueGuard:
    """Persistent cooldown guard for user-facing feedback prompts."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            from core.config import get_config

            db_path = get_config().database_dir / "feedback_channel.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS feedback_prompt_state (
                    subject TEXT PRIMARY KEY,
                    last_prompt_at TEXT NOT NULL,
                    prompt_count INTEGER NOT NULL DEFAULT 0
                )
            """)

    def allow_prompt(self, subject: str, cooldown: timedelta = timedelta(hours=24)) -> bool:
        key = self._key(subject)
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT last_prompt_at FROM feedback_prompt_state WHERE subject = ?",
                (key,),
            ).fetchone()
        if not row:
            return True
        try:
            last = datetime.fromisoformat(row[0])
        except ValueError:
            return True
        return datetime.now() - last >= cooldown

    def record_prompt(
        self,
        subject: str,
        *,
        subject_provenance: Mapping[str, Any] | None = None,
    ) -> None:
        key = self._key(subject)
        now = datetime.now().isoformat()
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """
                INSERT INTO feedback_prompt_state(subject, last_prompt_at, prompt_count)
                VALUES (?, ?, 1)
                ON CONFLICT(subject) DO UPDATE SET
                    last_prompt_at = excluded.last_prompt_at,
                    prompt_count = feedback_prompt_state.prompt_count + 1
                """,
                (key, now),
            )
            from core.scoring.subject_provenance import (
                ensure_scoring_subject_provenance_schema,
                record_scoring_subject_provenance,
            )

            ensure_scoring_subject_provenance_schema(conn)
            record_scoring_subject_provenance(
                conn,
                object_type="feedback_prompt",
                object_id=key,
                subject_provenance=subject_provenance,
            )

    @staticmethod
    def _key(subject: str) -> str:
        # The cooldown table is a persistent operational store, not a place
        # to duplicate a caller's potentially sensitive subject literal.
        # Provenance/deletion sidecars can still join on this deterministic,
        # non-reversible key without inferring ownership from the text.
        normalized = (subject or "").strip().lower().encode("utf-8")
        return "feedback-prompt:" + hashlib.sha256(normalized).hexdigest()


def record_feedback_signal(
    signal: FeedbackSignal,
    scorer: Any = None,
) -> Dict[str, Any]:
    """Reject the retired reaction-as-training-label bridge."""

    del signal, scorer
    raise RuntimeError(
        "direct_feedback_training_retired; consume a canonical "
        "training_evidence target command"
    )
