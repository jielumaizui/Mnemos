# -*- coding: utf-8 -*-
"""State machine for structured forced retrospective sessions."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.app.forced_retrospective import ForcedRetrospective, RecapTask
from core.app.retrospective_builder import RetrospectiveBuilder
from core.app.retrospective_models import (
    RECAP_QUESTIONS,
    RECAP_STATE_ORDER,
    RecapSession,
    RetrospectiveActionItem,
    RetrospectiveDraft,
)
from core.config import get_config


class RetrospectiveSessionManager:
    """Enforce recap ownership and the three-question state chain."""

    FINAL_STATES = {"finalized", "consumed"}
    LOCKED_STATES = {
        "user_confirmed",
        "proposal_pending",
        "consumption_pending",
        "finalized",
        "consumed",
    }

    def __init__(
        self,
        db_path: str | Path | None = None,
        forced: ForcedRetrospective | None = None,
        builder: RetrospectiveBuilder | None = None,
    ):
        config = get_config()
        self.db_path = (
            Path(db_path).expanduser() if db_path else config.database_dir / "recap_tasks.db"
        )
        self.forced = forced or ForcedRetrospective(db_path=str(self.db_path))
        self.builder = builder or RetrospectiveBuilder()
        self._init_db()

    def start(
        self,
        task_id: str = "",
        owner_agent: str = "",
        mode: str = "minimal",
        topic: str = "",
        source_agent: str = "",
        source_agents: List[str] | None = None,
        session_id: str = "",
        context: Dict | None = None,
        project: str = "",
        task_type: str = "",
        subtype: str = "",
        evidence_refs: List[str] | None = None,
    ) -> RecapSession:
        """Start or resume a recap session for a task."""
        context = context or {}
        evidence_refs = list(evidence_refs or [])
        if not task_id:
            if not topic:
                raise ValueError("task_id or topic is required")
            task_id = self.forced.create_system_recap(
                topic=topic,
                severity=str(context.get("severity") or "medium"),
                context=str(context.get("context") or ""),
                suggested_points=str(context.get("suggested_points") or ""),
            )

        existing = self._find_active_by_task(task_id)
        if existing:
            if owner_agent and existing.owner_agent and existing.owner_agent != owner_agent:
                return existing
            if owner_agent and not existing.owner_agent:
                existing.owner_agent = owner_agent
                self._save_session(existing)
            return existing

        task = self._require_task(task_id)
        now = datetime.now().isoformat()
        recap_id = f"retro-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        agents = list(dict.fromkeys(source_agents or ([source_agent] if source_agent else [])))
        if owner_agent and owner_agent not in agents:
            agents.append(owner_agent)
        session = RecapSession(
            recap_id=recap_id,
            task_id=task_id,
            state="q1_goal_actual",
            owner_agent=owner_agent,
            source_agent=source_agent,
            source_agents=agents,
            session_id=session_id,
            mode=mode,
            topic=task.topic,
            project=project,
            task_type=task_type,
            subtype=subtype,
            evidence_refs=evidence_refs,
            created_at=now,
            updated_at=now,
        )
        self._save_session(session)
        return session

    def submit_answer(self, recap_id: str, question_id: str, answer: str) -> RecapSession:
        """Submit one answer and advance the state machine."""
        session = self._require_session(recap_id)
        if session.state in self.LOCKED_STATES:
            raise ValueError("cannot submit answers after confirmation or finalize")
        valid_ids = {question["id"] for question in RECAP_QUESTIONS}
        if question_id not in valid_ids:
            raise ValueError(f"question_id must be one of {sorted(valid_ids)}")
        session.answers[question_id] = answer.strip()
        self._advance_or_build(session)
        self._save_session(session)
        return session

    def submit_answers(self, recap_id: str, answers: Dict[str, str]) -> RecapSession:
        """Submit multiple answers, typically from recap_submit."""
        session = self._require_session(recap_id)
        if session.state in self.LOCKED_STATES:
            raise ValueError("cannot submit answers after confirmation or finalize")
        answers = self.builder.normalize_answers(answers)
        for question in RECAP_QUESTIONS:
            question_id = question["id"]
            if question_id in answers:
                session.answers[question_id] = str(answers.get(question_id) or "").strip()
        self._advance_or_build(session)
        self._save_session(session)
        return session

    def confirm(self, recap_id: str) -> RecapSession:
        """Mark a generated draft as confirmed by the user."""
        session = self._require_session(recap_id)
        if session.state in self.FINAL_STATES:
            raise ValueError("cannot confirm finalized recap")
        validation = self.validate_ready_to_finalize(recap_id)
        if not validation["can_finalize"]:
            raise ValueError(validation["message"])
        if session.state == "user_confirmed":
            return session
        session.state = "user_confirmed"
        session.updated_at = datetime.now().isoformat()
        self._save_session(session)
        return session

    def mark_finalized(self, recap_id: str, page_path: str) -> RecapSession:
        """Mark a confirmed session as finalized."""
        session = self._require_session(recap_id)
        session.state = "finalized"
        session.finalized_page = page_path
        session.updated_at = datetime.now().isoformat()
        self._save_session(session)
        return session

    def mark_pipeline_state(
        self,
        recap_id: str,
        state: str,
        *,
        page_path: str = "",
        completion_receipt: Dict[str, Any] | None = None,
    ) -> RecapSession:
        """Persist a non-terminal recap handoff state and its recovery receipt."""
        if state not in {"proposal_pending", "consumption_pending", "retryable_failed"}:
            raise ValueError(f"invalid recap pipeline state: {state}")
        session = self._require_session(recap_id)
        session.state = state
        if page_path:
            session.finalized_page = page_path
        if completion_receipt is not None:
            session.completion_receipt = dict(completion_receipt)
        session.updated_at = datetime.now().isoformat()
        self._save_session(session)
        return session

    def mark_consumed(self, recap_id: str) -> RecapSession:
        """Mark a finalized session as consumed only after its page commit receipt."""
        session = self._require_session(recap_id)
        if session.state != "finalized":
            raise ValueError("only a finalized recap can be consumed")
        receipt = session.completion_receipt
        page_committed = receipt.get("status") == "committed" and receipt.get("terminal") is True
        required_count = int(receipt.get("required_receipt_count") or 0)
        terminal_count = int(receipt.get("terminal_receipt_count") or 0)
        consumption_committed = (
            receipt.get("consumption_plan_status") == "consumed"
            and required_count > 0
            and terminal_count == required_count
        )
        if not session.finalized_page or not page_committed or not consumption_committed:
            raise ValueError(
                "recap consumption requires committed page and complete target receipts"
            )
        session.state = "consumed"
        session.updated_at = datetime.now().isoformat()
        self._save_session(session)
        return session

    def validate_ready_to_finalize(self, recap_id: str) -> Dict[str, Any]:
        """Validate that a recap has a complete structured draft."""
        session = self._require_session(recap_id)
        if session.state in self.FINAL_STATES:
            return {
                "can_finalize": False,
                "missing_fields": [],
                "message": f"复盘已关闭：当前状态为 {session.state}。",
            }
        if not session.draft:
            return {
                "can_finalize": False,
                "missing_fields": ["draft"],
                "message": "复盘未完成：缺少结构化草稿，不能 finalize。",
            }
        missing = self.builder.validate_draft(session.draft)
        if missing:
            return {
                "can_finalize": False,
                "missing_fields": missing,
                "message": f"复盘未完成：缺少 {', '.join(missing)}，不能 finalize。",
            }
        return {
            "can_finalize": True,
            "missing_fields": [],
            "message": "ready",
        }

    def claim_owner(self, recap_id: str, owner_agent: str, current_session_id: str = "") -> bool:
        """Claim ownership if the recap has no owner or already belongs to this agent."""
        if not owner_agent:
            raise ValueError("owner_agent is required")
        session = self._require_session(recap_id)
        if session.owner_agent and session.owner_agent != owner_agent:
            return False
        session.owner_agent = owner_agent
        if current_session_id:
            session.session_id = current_session_id
        if owner_agent not in session.source_agents:
            session.source_agents.append(owner_agent)
        session.updated_at = datetime.now().isoformat()
        self._save_session(session)
        return True

    def get_session(self, recap_id: str = "", task_id: str = "") -> Optional[RecapSession]:
        """Fetch a session by recap_id or task_id."""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            if recap_id:
                row = conn.execute(
                    "SELECT * FROM retrospective_sessions WHERE recap_id = ?",
                    (recap_id,),
                ).fetchone()
            elif task_id:
                row = conn.execute(
                    "SELECT * FROM retrospective_sessions WHERE task_id = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
            else:
                return None
        return self._row_to_session(row) if row else None

    def _advance_or_build(self, session: RecapSession) -> None:
        if all(session.answers.get(question["id"]) for question in RECAP_QUESTIONS):
            task = self._require_task(session.task_id)
            session.draft = self.builder.build_draft(
                task,
                session.answers,
                session.evidence_refs or self._default_evidence(task),
                recap_id=session.recap_id,
                owner_agent=session.owner_agent,
            )
            session.state = "draft_generated"
        else:
            next_question = session.next_question()
            session.state = {
                "goal_actual": "q1_goal_actual",
                "cause_lesson": "q2_cause_lesson",
                "next_handling": "q3_next_handling",
            }.get(next_question, "draft_generated")
        session.updated_at = datetime.now().isoformat()

    def _default_evidence(self, task: RecapTask) -> List[str]:
        refs = [f"wiki://{task.target_page}"] if task.target_page else []
        if task.context:
            refs.append(f"recap_task://{task.task_id}/context")
        return refs

    def _find_active_by_task(self, task_id: str) -> Optional[RecapSession]:
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM retrospective_sessions WHERE task_id = ? "
                "AND state NOT IN ('finalized', 'consumed') "
                "ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return self._row_to_session(row) if row else None

    def _require_task(self, task_id: str) -> RecapTask:
        task = self.forced.get_recap_task(task_id)
        if not task:
            raise ValueError(f"recap task not found: {task_id}")
        return task

    def _require_session(self, recap_id: str) -> RecapSession:
        session = self.get_session(recap_id=recap_id)
        if not session:
            raise ValueError(f"recap session not found: {recap_id}")
        return session

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS retrospective_sessions (
                    recap_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    owner_agent TEXT DEFAULT '',
                    source_agent TEXT DEFAULT '',
                    source_agents TEXT DEFAULT '[]',
                    session_id TEXT DEFAULT '',
                    mode TEXT DEFAULT 'minimal',
                    topic TEXT DEFAULT '',
                    project TEXT DEFAULT '',
                    task_type TEXT DEFAULT '',
                    subtype TEXT DEFAULT '',
                    answers TEXT DEFAULT '{}',
                    draft TEXT DEFAULT '',
                    finalized_page TEXT DEFAULT '',
                    completion_receipt TEXT DEFAULT '{}',
                    evidence_refs TEXT DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(retrospective_sessions)")}
            if "completion_receipt" not in columns:
                conn.execute(
                    "ALTER TABLE retrospective_sessions ADD COLUMN completion_receipt TEXT DEFAULT '{}'"
                )
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_retrospective_sessions_task
                ON retrospective_sessions(task_id)
                """)

    def _save_session(self, session: RecapSession) -> None:
        if session.state not in RECAP_STATE_ORDER:
            raise ValueError(f"invalid recap state: {session.state}")
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute(
                """
                INSERT INTO retrospective_sessions (
                    recap_id, task_id, state, owner_agent, source_agent,
                    source_agents, session_id, mode, topic, project, task_type,
                    subtype, answers, draft, finalized_page, completion_receipt,
                    evidence_refs, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recap_id) DO UPDATE SET
                    task_id = excluded.task_id,
                    state = excluded.state,
                    owner_agent = excluded.owner_agent,
                    source_agent = excluded.source_agent,
                    source_agents = excluded.source_agents,
                    session_id = excluded.session_id,
                    mode = excluded.mode,
                    topic = excluded.topic,
                    project = excluded.project,
                    task_type = excluded.task_type,
                    subtype = excluded.subtype,
                    answers = excluded.answers,
                    draft = excluded.draft,
                    finalized_page = excluded.finalized_page,
                    completion_receipt = excluded.completion_receipt,
                    evidence_refs = excluded.evidence_refs,
                    updated_at = excluded.updated_at
                """,
                (
                    session.recap_id,
                    session.task_id,
                    session.state,
                    session.owner_agent,
                    session.source_agent,
                    json.dumps(session.source_agents, ensure_ascii=False),
                    session.session_id,
                    session.mode,
                    session.topic,
                    session.project,
                    session.task_type,
                    session.subtype,
                    json.dumps(session.answers, ensure_ascii=False),
                    (
                        json.dumps(session.draft.to_dict(), ensure_ascii=False)
                        if session.draft
                        else ""
                    ),
                    session.finalized_page,
                    json.dumps(session.completion_receipt, ensure_ascii=False),
                    json.dumps(session.evidence_refs, ensure_ascii=False),
                    session.created_at,
                    session.updated_at,
                ),
            )

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> RecapSession:
        draft = RetrospectiveSessionManager._draft_from_json(row["draft"] or "")
        return RecapSession(
            recap_id=row["recap_id"],
            task_id=row["task_id"],
            state=row["state"],
            owner_agent=row["owner_agent"] or "",
            source_agent=row["source_agent"] or "",
            source_agents=json.loads(row["source_agents"] or "[]"),
            session_id=row["session_id"] or "",
            mode=row["mode"] or "minimal",
            topic=row["topic"] or "",
            project=row["project"] or "",
            task_type=row["task_type"] or "",
            subtype=row["subtype"] or "",
            answers=json.loads(row["answers"] or "{}"),
            draft=draft,
            finalized_page=row["finalized_page"] or "",
            completion_receipt=json.loads(row["completion_receipt"] or "{}"),
            evidence_refs=json.loads(row["evidence_refs"] or "[]"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _draft_from_json(raw: str) -> Optional[RetrospectiveDraft]:
        if not raw:
            return None
        data = json.loads(raw)
        actions = [
            RetrospectiveActionItem(
                action_id=item.get("action_id", ""),
                action=item.get("action", ""),
                owner=item.get("owner", ""),
                deadline=item.get("deadline", ""),
                metric=item.get("metric", ""),
                follow_up_at=item.get("follow_up_at", ""),
                status=item.get("status", "open"),
            )
            for item in data.get("action_items", [])
        ]
        return RetrospectiveDraft(
            recap_id=data.get("recap_id", ""),
            task_id=data.get("task_id", ""),
            title=data.get("title", ""),
            lesson=data.get("lesson", ""),
            goal=data.get("goal", ""),
            actual=data.get("actual", ""),
            delta=data.get("delta", ""),
            root_type=list(data.get("root_type", [])),
            root_cause=data.get("root_cause", ""),
            next_handling=data.get("next_handling", "specific_action"),
            no_action_reason=data.get("no_action_reason", ""),
            action_items=actions,
            activation_rules=dict(data.get("activation_rules", {})),
            consumption_targets=list(data.get("consumption_targets", [])),
            evidence_refs=list(data.get("evidence_refs", [])),
            missing_fields=list(data.get("missing_fields", [])),
            created_at=data.get("created_at", datetime.now().isoformat()),
        )
