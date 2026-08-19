# -*- coding: utf-8 -*-
"""Canonical decision inbox for proposals, delivery, recaps, and cognitive actions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.config import get_config
from core.trust.config import load_trusted_push_config


DECISION_INBOX_SCHEMA_VERSION = "mnemos.decision_inbox.v1"


@dataclass(frozen=True)
class DecisionInboxPaths:
    wiki_base: Path
    database_dir: Path
    proposal_db: Path
    distill_actions_db: Path
    recap_db: Path
    delivery_db: Path


class DecisionInboxService:
    """Read and act on user-facing decision items through one application API."""

    def __init__(self, *, config: Any = None, paths: DecisionInboxPaths | None = None):
        self.config = config or get_config()
        self.paths = paths or self._paths_from_config(self.config)

    @staticmethod
    def _paths_from_config(config: Any) -> DecisionInboxPaths:
        wiki_dir = getattr(config, "wiki_dir", None)
        database_root = getattr(config, "database_dir", None)
        if not wiki_dir or not database_root:
            raise ValueError(
                "DecisionInboxService requires configured wiki_dir and database_dir "
                "or explicit DecisionInboxPaths"
            )
        wiki_base = Path(wiki_dir).expanduser()
        database_dir = Path(database_root).expanduser()
        trusted = load_trusted_push_config(config, wiki_base=wiki_base)
        configured_delivery_db = (
            config.get("delivery.db_path", None) if hasattr(config, "get") else None
        )
        delivery_db = Path(configured_delivery_db or database_dir / "delivery_events.db")
        return DecisionInboxPaths(
            wiki_base=wiki_base,
            database_dir=database_dir,
            proposal_db=Path(trusted.db_path),
            distill_actions_db=database_dir / "distill_actions.db",
            recap_db=database_dir / "recap_tasks.db",
            delivery_db=delivery_db,
        )

    def list_items(self, *, limit: int = 50) -> Dict[str, Any]:
        safe_limit = max(1, min(int(limit or 50), 500))
        items: List[Dict[str, Any]] = []
        collectors = (
            self._proposal_items,
            self._delivery_items,
            self._cognitive_action_items,
            self._recap_items,
        )
        for collect in collectors:
            items.extend(collect(safe_limit))
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        items = items[:safe_limit]
        counts: Dict[str, int] = {}
        for item in items:
            source = str(item.get("source") or "unknown")
            counts[source] = counts.get(source, 0) + 1
        return {
            "schema_version": DECISION_INBOX_SCHEMA_VERSION,
            "count": len(items),
            "counts": counts,
            "items": items,
        }

    def act(
        self,
        item_id: str,
        action: str,
        *,
        reason: str = "",
        allow_high_risk: bool = False,
        snooze_hours: int = 24,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        supersedes_event_id: str = "",
    ) -> Dict[str, Any]:
        source, raw_id = _split_item_id(item_id)
        if source == "proposal":
            return self._act_proposal(
                raw_id,
                action,
                reason=reason,
                allow_high_risk=allow_high_risk,
                snooze_hours=snooze_hours,
                principal=principal,
                narrowing=narrowing,
                supersedes_event_id=supersedes_event_id,
            )
        if source == "recap":
            return self._act_recap(raw_id, action, reason=reason)
        if source == "delivery":
            return self._act_delivery(
                raw_id,
                action,
                reason=reason,
                principal=principal,
                narrowing=narrowing,
                supersedes_event_id=supersedes_event_id,
            )
        if source == "cognitive_action":
            return self._act_cognitive_action(raw_id, action)
        return {"success": False, "error": f"unsupported decision inbox source: {source}"}

    def _proposal_items(self, limit: int) -> List[Dict[str, Any]]:
        if not self.paths.proposal_db.exists():
            return []
        from core.trust.proposal_queue import ProposalQueue

        proposals = ProposalQueue(
            self.paths.proposal_db,
            wiki_base=self.paths.wiki_base,
        ).list(statuses=("validated", "needs_manual_review", "snoozed"), limit=limit)
        items = []
        for proposal in proposals:
            data = proposal.to_dict()
            items.append(
                {
                    "item_id": f"proposal:{proposal.proposal_id}",
                    "source": "proposal",
                    "status": proposal.status,
                    "severity": proposal.risk_level,
                    "title": proposal.candidate.payload.get("title") or proposal.candidate.target_path,
                    "target": proposal.candidate.target_path,
                    "created_at": data.get("created_at", ""),
                    "actions": ["approve", "reject", "snooze"],
                    "payload": data,
                }
            )
        return items

    def _delivery_items(self, limit: int) -> List[Dict[str, Any]]:
        if not self.paths.delivery_db.exists():
            return []
        rows = self._select_rows(
            self.paths.delivery_db,
            """
            SELECT * FROM delivery_events
            WHERE decision = 'deliver'
              AND delivered_level IN ('hint', 'warn', 'force_open')
            ORDER BY created_at DESC, event_id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = [row for row in rows if not str(row.get("feedback") or "")]
        state_db = self.paths.database_dir / "producer_consumer_ledger.db"
        reacted: set[str] = set()
        if state_db.is_file():
            from core.cognitive.state_store import CognitiveStateStore

            reacted = {
                str(revision.payload["delivery_ref"]["event_id"])
                for revision in CognitiveStateStore(state_db).current_revisions(
                    object_type="user_reaction_event"
                )
                if revision.payload["delivery_ref"]["state"] == "available"
            }
        return [
            {
                "item_id": f"delivery:{row['event_id']}",
                "source": "delivery",
                "status": "awaiting_feedback",
                "severity": row["delivered_level"],
                "title": row["subject"],
                "target": row["target"],
                "created_at": row["created_at"],
                "actions": ["accept", "ignore", "dismiss"],
                "payload": row,
            }
            for row in rows
            if str(row["event_id"]) not in reacted
        ]

    def _cognitive_action_items(self, limit: int) -> List[Dict[str, Any]]:
        if not self.paths.distill_actions_db.exists():
            return []
        if not _table_exists(self.paths.distill_actions_db, "cognitive_action_log"):
            return []
        rows = self._select_rows(
            self.paths.distill_actions_db,
            """
            SELECT * FROM cognitive_action_log
            WHERE status = 'queued'
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                "item_id": f"cognitive_action:{row['cognitive_action_id']}",
                "source": "cognitive_action",
                "status": row["status"],
                "severity": "medium",
                "title": row["cognitive_action"],
                "target": row["target_kind"],
                "created_at": row["created_at"],
                "actions": ["process"],
                "payload": row,
            }
            for row in rows
        ]

    def _recap_items(self, limit: int) -> List[Dict[str, Any]]:
        if not self.paths.recap_db.exists():
            return []
        from core.app.forced_retrospective import ForcedRetrospective

        tasks = ForcedRetrospective(str(self.paths.recap_db)).list_recap_tasks(
            status="pending",
            limit=limit,
        )
        return [
            {
                "item_id": f"recap:{task.task_id}",
                "source": "recap",
                "status": task.status,
                "severity": task.severity,
                "title": task.topic,
                "target": task.target_page,
                "created_at": task.created_at,
                "actions": ["resolve", "dismiss"],
                "payload": task.__dict__,
            }
            for task in tasks
        ]

    def _act_proposal(
        self,
        proposal_id: str,
        action: str,
        *,
        reason: str,
        allow_high_risk: bool,
        snooze_hours: int,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        supersedes_event_id: str,
    ) -> Dict[str, Any]:
        if principal is None or narrowing is None:
            return {
                "success": False,
                "source": "proposal",
                "reason": "canonical_feedback_entrypoint_required",
            }
        from core.trust.dialog_push import DialogDecisionPush

        result = DialogDecisionPush(
            wiki_base=self.paths.wiki_base,
            db_path=self.paths.proposal_db,
        ).decide(
            proposal_id,
            action,
            reason=reason,
            allow_high_risk=allow_high_risk,
            snooze_hours=snooze_hours,
            principal=principal,
            narrowing=narrowing,
            supersedes_event_id=supersedes_event_id,
        )
        return {"success": result.get("status") != "failed", "source": "proposal", **result}

    def _act_recap(self, task_id: str, action: str, *, reason: str) -> Dict[str, Any]:
        if action not in {"resolve", "dismiss"}:
            return {"success": False, "source": "recap", "error": f"unsupported action: {action}"}
        from core.app.forced_retrospective import ForcedRetrospective

        status = "resolved" if action == "resolve" else "dismissed"
        changed = ForcedRetrospective(str(self.paths.recap_db)).mark_recap_status(
            task_id,
            status,
            reason=reason or f"decision-inbox {action}",
            actor="decision-inbox",
        )
        return {"success": bool(changed), "source": "recap", "status": status, "changed": changed}

    def _act_delivery(
        self,
        event_id: str,
        action: str,
        *,
        reason: str,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        supersedes_event_id: str,
    ) -> Dict[str, Any]:
        if principal is None or narrowing is None:
            return {
                "success": False,
                "source": "delivery",
                "reason": "canonical_feedback_entrypoint_required",
            }
        rows = self._select_rows(
            self.paths.delivery_db,
            "SELECT subject FROM delivery_events WHERE event_id=?",
            (event_id,),
        )
        if len(rows) != 1:
            return {"success": False, "source": "delivery", "reason": "not_found"}
        from core.cognitive.feedback_entrypoints import record_predictive_feedback

        result = record_predictive_feedback(
            database_dir=self.paths.database_dir,
            topic=str(rows[0]["subject"]),
            action=action,
            delivery_event_id=event_id,
            principal=principal,
            narrowing=narrowing,
            supersedes_event_id=supersedes_event_id,
            correction_target_ref=(f"delivery:{event_id}" if reason else ""),
            correction_reason=reason,
        )
        return {"source": "delivery", **result}

    def _act_cognitive_action(self, action_id: str, action: str) -> Dict[str, Any]:
        if action != "process":
            return {
                "success": False,
                "source": "cognitive_action",
                "error": f"unsupported action: {action}",
            }
        from core.hephaestus.distill_cognitive_action_worker import (
            DistillCognitiveActionWorker,
        )

        worker = DistillCognitiveActionWorker(
            self.paths.distill_actions_db,
            database_dir=self.paths.database_dir,
        )
        result = worker.process_queued(limit=1, action_id=action_id)
        return {"success": True, "source": "cognitive_action", "requested_id": action_id, **result}

    @staticmethod
    def _select_rows(
        db_path: Path,
        query: str,
        params: tuple[Any, ...],
    ) -> List[Dict[str, Any]]:
        with sqlite3.connect(str(db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(row) for row in rows]


def _split_item_id(item_id: str) -> tuple[str, str]:
    if ":" not in item_id:
        return "", item_id
    source, raw_id = item_id.split(":", 1)
    return source, raw_id


def _table_exists(db_path: Path, table: str) -> bool:
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    return bool(row)


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    for key in ("metadata_json", "detail", "result_detail"):
        if key in data:
            data[key] = _loads_json(data.get(key))
    return data


def _loads_json(value: Any) -> Any:
    if not value:
        return {}
    try:
        return json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return {}
