"""Event value contract and config-path resolution for the event bus."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
from typing import Any, Dict, Optional
import uuid
from unittest.mock import Mock as _Mock


logger = logging.getLogger(__name__)


def _coerce_config_path(value: Any) -> Optional[Path]:
    if isinstance(value, _Mock):
        return None
    if isinstance(value, (str, os.PathLike)):
        return Path(value).expanduser()
    return None


def _path_from_config(config: Any, *names: str) -> Optional[Path]:
    for name in names:
        value = getattr(config, name, None)
        path = _coerce_config_path(value)
        if path is not None:
            return path
    return None


def _resolve_event_db_dir(config: Any) -> Path:
    return (
        _path_from_config(config, "mnemos_dir", "database_dir", "data_dir")
        or Path.home() / ".mnemos"
    )


def _resolve_events_root(config: Any) -> Path:
    base = (
        _path_from_config(config, "database_dir", "mnemos_dir", "data_dir")
        or Path.home() / ".mnemos"
    )
    return base / "events"


@dataclass
class Event:
    """标准事件格式"""

    event_type: str
    source: str
    payload: Dict[str, Any]
    trace_id: str = ""
    timestamp: str = ""
    chain_depth: int = 0
    subject_provenance: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if not self.trace_id:
            self.trace_id = str(uuid.uuid4())[:16]
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Optional["Event"]:
        """从 SQLite 行反序列化"""
        try:
            return cls(
                event_type=row["event_type"],
                source=row["source"],
                payload=json.loads(row["payload_json"]),
                trace_id=row["trace_id"],
                timestamp=row["timestamp"],
            )
        except (json.JSONDecodeError, ValueError, TypeError, KeyError) as e:
            logger.warning("从数据库行反序列化事件失败: %s", e, exc_info=True)
            return None

    def to_dict(self) -> Dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
