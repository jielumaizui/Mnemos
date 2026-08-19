# -*- coding: utf-8 -*-
"""Structured storage for recap skip feedback."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

from core.app.retrospective_consumption_router import RetrospectiveConsumptionRouter
from core.app.retrospective_models import (
    SKIP_POLICY_MAP,
    VALID_SKIP_REASONS,
    SkipEvent,
)
from core.config import get_config


class RetrospectiveSkipEventStore:
    """Record skip events and apply their scheduling/scoring semantics."""

    def __init__(self, db_path: str | Path | None = None):
        config = get_config()
        self.db_path = Path(db_path).expanduser() if db_path else config.database_dir / "recap_tasks.db"
        self._init_db()

    def record_skip(
        self,
        recap_id: str,
        task_id: str,
        skip_reason: str,
        owner_agent: str,
        user_note: str = "",
        source_agent: str = "",
        source_agents: List[str] | None = None,
        project: str = "",
        task_type: str = "",
        trigger_reason: List[str] | None = None,
    ) -> SkipEvent:
        """Persist a structured skip event and update the source task."""
        if skip_reason not in VALID_SKIP_REASONS:
            raise ValueError(f"skip_reason must be one of {sorted(VALID_SKIP_REASONS)}")
        skip_status, next_policy, write_to_wiki = SKIP_POLICY_MAP[skip_reason]
        event = SkipEvent(
            event_id=f"skip-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
            recap_id=recap_id,
            task_id=task_id,
            skip_reason=skip_reason,
            skip_status=skip_status,
            next_policy=next_policy,
            source_agent=source_agent,
            owner_agent=owner_agent,
            source_agents=list(source_agents or ([source_agent] if source_agent else [])),
            project=project,
            task_type=task_type,
            trigger_reason=list(trigger_reason or []),
            defer_until=self._defer_until(skip_reason),
            user_note=user_note,
            consumption_targets=self._targets_for(skip_reason),
            write_to_wiki=write_to_wiki,
        )
        self._insert_event(event)
        plan = RetrospectiveConsumptionRouter(db_path=self.db_path).route_skip_event(event)
        event.consumption_plan = plan.to_dict()
        return event

    def derive_next_policy(self, event: SkipEvent) -> str:
        """Return the policy derived for an event."""
        return event.next_policy

    def route_skip_event(self, event: SkipEvent):
        """Route a skip event into downstream consumers."""
        return RetrospectiveConsumptionRouter(db_path=self.db_path).route_skip_event(event)

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recap_skip_events (
                    event_id TEXT PRIMARY KEY,
                    recap_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    skip_reason TEXT NOT NULL,
                    skip_status TEXT NOT NULL,
                    next_policy TEXT NOT NULL,
                    source_agent TEXT DEFAULT '',
                    owner_agent TEXT DEFAULT '',
                    source_agents TEXT DEFAULT '[]',
                    project TEXT DEFAULT '',
                    task_type TEXT DEFAULT '',
                    trigger_reason TEXT DEFAULT '[]',
                    selected_at TEXT NOT NULL,
                    defer_until TEXT DEFAULT '',
                    user_note TEXT DEFAULT '',
                    consumption_targets TEXT DEFAULT '[]',
                    write_to_wiki INTEGER DEFAULT 0
                )
                """
            )

    def _insert_event(self, event: SkipEvent) -> None:
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute(
                """
                INSERT INTO recap_skip_events (
                    event_id, recap_id, task_id, skip_reason, skip_status,
                    next_policy, source_agent, owner_agent, source_agents,
                    project, task_type, trigger_reason, selected_at, defer_until,
                    user_note, consumption_targets, write_to_wiki
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.recap_id,
                    event.task_id,
                    event.skip_reason,
                    event.skip_status,
                    event.next_policy,
                    event.source_agent,
                    event.owner_agent,
                    json.dumps(event.source_agents, ensure_ascii=False),
                    event.project,
                    event.task_type,
                    json.dumps(event.trigger_reason, ensure_ascii=False),
                    event.selected_at,
                    event.defer_until,
                    event.user_note,
                    json.dumps(event.consumption_targets, ensure_ascii=False),
                    1 if event.write_to_wiki else 0,
                ),
            )

    @staticmethod
    def _defer_until(skip_reason: str) -> str:
        if skip_reason == "no_time":
            return (datetime.now() + timedelta(hours=24)).isoformat()
        if skip_reason == "no_response":
            return (datetime.now() + timedelta(hours=12)).isoformat()
        return ""

    @staticmethod
    def _targets_for(skip_reason: str) -> List[str]:
        # A skip is an operational state transition, not authenticated
        # cognitive feedback.  Keep it inside the scheduler domain; scoring and
        # persona changes require the canonical recap_feedback seam.
        if skip_reason == "no_time":
            return ["scheduler"]
        if skip_reason == "low_value":
            return ["scheduler"]
        if skip_reason == "false_positive":
            return ["scheduler"]
        if skip_reason == "already_handled":
            return ["scheduler"]
        return ["scheduler"]
