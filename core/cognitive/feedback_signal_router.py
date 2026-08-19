"""Read-only view of the retired pre-COG-038 feedback signal ledger."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.config import get_config


SCHEMA_VERSION = "mnemos.feedback_signal.v1"


@dataclass(frozen=True)
class FeedbackSignal:
    """Historical row shape retained solely for migration and audit reads."""

    signal_id: str
    created_at: str
    source: str
    subject: str
    action: str
    polarity: str
    scope_type: str = "topic"
    scope_value: str = ""
    target_ref: str = ""
    source_event_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FeedbackSignalRouter:
    """Compatibility reader; new writes belong to FeedbackAttributionStore."""

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        database_dir: Path | None = None,
        config: Any | None = None,
        trust_scorer: Any | None = None,
        ensure_db: bool = False,
    ) -> None:
        del trust_scorer, ensure_db
        cfg = config or get_config()
        base_dir = Path(
            database_dir
            or getattr(cfg, "database_dir", "")
            or Path.home() / ".mnemos"
        )
        configured_db = _cfg_get(cfg, "feedback_signal.db_path", None)
        self.db_path = Path(
            db_path or configured_db or (base_dir / "feedback_signals.db")
        ).expanduser()

    def record_signal(self, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(
            "legacy_feedback_signal_write_retired; use FeedbackAttributionStore"
        )

    def list_signals(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.db_path.is_file():
            return []
        safe_limit = max(1, min(int(limit or 50), 500))
        with sqlite3.connect(
            f"file:{self.db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            conn.row_factory = sqlite3.Row
            if not _table_exists(conn, "feedback_signals"):
                return []
            rows = conn.execute(
                "SELECT * FROM feedback_signals "
                "ORDER BY created_at DESC, signal_id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [_row_to_signal(row).to_dict() for row in rows]


def _row_to_signal(row: sqlite3.Row) -> FeedbackSignal:
    try:
        metadata = json.loads(str(row["metadata_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    return FeedbackSignal(
        signal_id=str(row["signal_id"]),
        created_at=str(row["created_at"]),
        source=str(row["source"]),
        subject=str(row["subject"]),
        action=str(row["action"]),
        polarity=str(row["polarity"]),
        scope_type=str(row["scope_type"]),
        scope_value=str(row["scope_value"]),
        target_ref=str(row["target_ref"]),
        source_event_id=str(row["source_event_id"]),
        metadata=metadata if isinstance(metadata, dict) else {},
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _cfg_get(cfg: Any, path: str, default: Any = None) -> Any:
    if hasattr(cfg, "get"):
        try:
            return cfg.get(path, default)
        except (TypeError, KeyError, AttributeError):
            return default
    return default
