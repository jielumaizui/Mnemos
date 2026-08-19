# -*- coding: utf-8 -*-
"""Canonical Raw-only engine for controlled AgentSource reconciliation.

This adapter is deliberately narrower than :class:`SyncEngine`: it writes the
native transcript into canonical Raw and returns durable revision receipts, but
does not touch the semantic backend, sync log, persona signals, Amphora, or
distillation.  It exists for bounded recovery/reconciliation runs where a
complete Native-to-Raw denominator must be rebuilt without accidentally
creating downstream work.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from core.sync_framework.agent_source import (
    AgentSource,
    SessionInfo,
    SyncResult,
    Turn,
    canonicalize_session_info,
)
from core.sync_framework.raw_event_store import RawEventStore
from core.sync_framework.source_support import build_native_raw_metadata


class RawOnlySyncEngine:
    """Commit exact native turns to canonical Raw with no downstream effects."""

    def __init__(
        self,
        *,
        raw_store: RawEventStore | None = None,
        db_path: Path | None = None,
        config: Any | None = None,
    ) -> None:
        self.raw_store = raw_store or RawEventStore(db_path=db_path, config=config)
        # ``daemon.raw_sync`` uses this path solely to locate its durable
        # cursor ledger.  Raw and cursor state intentionally share the
        # configured database directory in production.
        self.db_path = Path(self.raw_store.db_path)

    @staticmethod
    def _source_files(session_info: SessionInfo, turn: Turn) -> list[str]:
        files = [str(path) for path in (turn.source_files or [])]
        if not files and session_info.source_path:
            files.append(str(session_info.source_path))
        return files

    def sync_turns(
        self,
        source: AgentSource,
        session_info: SessionInfo,
        turns: Iterable[Turn],
        *,
        incremental: bool,
        enqueue_distillation: bool,
    ) -> list[SyncResult]:
        """Write a selected batch and reject any semantic-pipeline request."""
        if incremental or enqueue_distillation:
            raise ValueError("RawOnlySyncEngine requires full raw-only batches")
        canonical_session = canonicalize_session_info(session_info)
        results: list[SyncResult] = []
        for turn in turns:
            metadata = build_native_raw_metadata(source, canonical_session, turn)
            revision_id = self.raw_store.upsert_turn(
                source_agent=source.name,
                session_id=canonical_session.session_id,
                turn_number=int(turn.turn_number),
                user_content=turn.user_content,
                assistant_content=turn.assistant_content,
                model_tag=str(getattr(source, "model_tag", "") or ""),
                timestamp=turn.timestamp,
                metadata=metadata,
                tool_calls=turn.tool_calls,
                tool_results=turn.tool_results,
                reasoning=turn.reasoning,
                attachments=turn.attachments,
                raw_event_refs=turn.raw_event_refs,
                source_files=self._source_files(canonical_session, turn),
                source_path=(
                    str(canonical_session.source_path)
                    if canonical_session.source_path
                    else None
                ),
                completeness=dict(turn.completeness or {}),
                full_content_hash=(turn.metadata or {}).get("full_content_hash"),
                # Keep the canonical quality ordering identical to the formal
                # SyncEngine Raw write path.  The *engine boundary*, not an
                # invented origin value, is what proves this was raw-only.
                origin="sync_engine",
            )
            if not revision_id:
                raise RuntimeError("canonical_raw_commit_missing")
            results.append(
                SyncResult(
                    session_id=canonical_session.session_id,
                    turn_number=int(turn.turn_number),
                    action="raw_committed",
                    raw_event_id=str(revision_id),
                )
            )
        return results

    def close(self) -> None:
        """Close only the canonical Raw store owned by this adapter."""
        self.raw_store.close()
