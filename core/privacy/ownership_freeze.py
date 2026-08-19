"""Read-only privacy freeze guard shared by cognitive storage owners.

The ownership orchestrator may import individual storage owners while applying
deletion.  Keeping the write barrier in this dependency-neutral module lets
those owners enforce an existing freeze without importing the orchestrator and
forming a cycle.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _configured_data_dir(config: Any) -> Path:
    for name in ("mnemos_dir", "data_dir"):
        value = config.get(name) if isinstance(config, Mapping) else getattr(config, name, None)
        if isinstance(value, (str, os.PathLike)):
            return Path(value).expanduser()
    return Path.home() / ".mnemos"


def cognitive_write_is_frozen(
    config: Any,
    *,
    session_id: str = "",
    project: str = "",
    agent: str = "",
    source_event_ids: tuple[str, ...] = (),
) -> bool:
    """Return whether an existing ownership freeze blocks a cognitive write.

    This is deliberately read-only: a missing ownership ledger means no
    freeze has been registered, while an existing but unreadable ledger fails
    closed instead of silently bypassing a privacy barrier.
    """

    db_path = _configured_data_dir(config) / "data_ownership.db"
    if not db_path.is_file():
        return False
    candidates = {("all", "all")}
    if session_id:
        candidates.add(("session", str(session_id)))
    if project:
        candidates.add(("project", str(project).lower()))
    if agent:
        candidates.add(("agent", str(agent).lower()))
    for source_event_id in source_event_ids:
        if source_event_id:
            candidates.add(("raw_event_id", str(source_event_id)))
    try:
        with sqlite3.connect(
            db_path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=5,
        ) as conn:
            for kind, value in candidates:
                values = {value}
                if kind in {"project", "agent"}:
                    values.add(value.lower())
                hashes = tuple(sorted(_hash_text(candidate) for candidate in values))
                placeholders = ",".join("?" for _ in hashes)
                row = conn.execute(
                    f"""
                    SELECT 1
                    FROM data_ownership_requests
                    WHERE request_type='freeze'
                      AND scope_kind=?
                      AND scope_value_hash IN ({placeholders})
                      AND status='frozen'
                    LIMIT 1
                    """,  # nosec B608 - placeholders are generated from local hashes
                    (kind, *hashes),
                ).fetchone()
                if row is not None:
                    return True
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise PermissionError(
            "cognitive state write blocked because data ownership freeze state is unavailable"
        ) from exc
    return False


def assert_cognitive_write_not_frozen(
    config: Any,
    access_control: Mapping[str, Any],
    *,
    domain: str,
    source_event_ids: tuple[str, ...] = (),
) -> None:
    """Raise before commit when a typed object matches a durable freeze."""

    from core.cognitive.access_control import validate_cognitive_access_envelope

    normalized = validate_cognitive_access_envelope(access_control)
    scope = normalized["scope"]
    if cognitive_write_is_frozen(
        config,
        session_id=str(scope.get("session_id") or ""),
        project=str(scope.get("project") or ""),
        agent=str(normalized["owner"].get("agent") or ""),
        source_event_ids=source_event_ids,
    ):
        raise PermissionError(
            f"{str(domain or 'cognitive')} write is blocked by a matching "
            "frozen data ownership scope"
        )
