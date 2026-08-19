# -*- coding: utf-8 -*-
"""Route finalized retrospectives and skip events to downstream consumers."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from core.app.recap_consumption import RecapConsumptionLedger
from core.app.recap_feedback import RecapFeedbackOutbox
from core.app.retrospective_models import (
    ConsumptionPlan,
    RetrospectiveActionItem,
    RetrospectiveDraft,
    RetrospectiveRecord,
    SkipEvent,
)
from core.cognitive.state_contract import sha256_json
from core.config import get_config
from core.db_utils import render_sql


RECAP_POLICY_DECISION_CONTRACT_ID = (
    "project-contract:confirmed-recap-policy-patch"
)
RECAP_POLICY_DECISION_CONTRACT_REVISION = (
    "mnemos.confirmed_recap_policy_patch.v1"
)
RECAP_POLICY_DECISION_CONTRACT_TEXT = (
    "A confirmed retrospective may create only the exact bounded policy patch "
    "derived from its validated lesson, scope, triggers, and evidence."
)
RECAP_POLICY_DECISION_PRODUCER_HASH = sha256_json(
    {
        "module": "core.app.retrospective_consumption_router",
        "producer": "RetrospectiveConsumptionRouter._route_policy_patch",
        "version": RECAP_POLICY_DECISION_CONTRACT_REVISION,
    }
)


class RetrospectiveConsumptionRouter:
    """Persist consumption plans so recap assets can affect future behavior."""

    def __init__(self, db_path: str | Path | None = None):
        config = get_config()
        self.db_path = Path(db_path).expanduser() if db_path else config.database_dir / "recap_tasks.db"
        self._init_db()

    def route_after_finalize(
        self,
        record: RetrospectiveRecord,
        *,
        page_path: str = "",
    ) -> ConsumptionPlan:
        """Create a consumption plan for a finalized recap."""
        draft = record.draft
        ledger = RecapConsumptionLedger(self.db_path)
        existing = ledger.latest_plan_for_recap(draft.recap_id)
        if existing:
            source = ledger.plan_source(str(existing["plan_id"]))
            stored_record = self._record_from_dict(dict(source["record"]))
            stored_page_path = str(source["page_path"] or page_path)
            self._drain_plan(
                ledger,
                str(existing["plan_id"]),
                record=stored_record,
                page_path=stored_page_path,
            )
            return self._plan_model(ledger.plan(str(existing["plan_id"])))
        targets = list(dict.fromkeys(draft.consumption_targets or ["wiki_search"]))
        if draft.activation_rules and "preflight" not in targets:
            targets.append("preflight")
        if draft.next_handling in ("specific_action", "rule_update") and "follow_up" not in targets:
            targets.append("follow_up")
        priority = "high" if record.severity in ("critical", "high") else "medium"
        plan_id = ledger.create_plan(
            recap_id=draft.recap_id,
            requested_targets=targets,
            activation_rules=draft.activation_rules,
            consume_priority=priority,
            follow_up_at=record.follow_up_at,
            page_path=page_path,
            record_payload=record.to_dict(),
        )
        self._drain_plan(ledger, plan_id, record=record, page_path=page_path)
        return self._plan_model(ledger.plan(plan_id))

    def route_skip_event(self, event: SkipEvent) -> ConsumptionPlan:
        """Create a consumption plan from structured skip feedback."""
        targets = list(event.consumption_targets)
        if not targets:
            targets = ["scheduler"]
        ledger = RecapConsumptionLedger(self.db_path)
        plan_id = ledger.create_plan(
            recap_id=event.recap_id,
            requested_targets=targets,
            activation_rules={"skip_reason": event.skip_reason, "next_policy": event.next_policy},
            consume_priority="high" if event.skip_reason == "false_positive" else "medium",
            follow_up_at=event.defer_until,
            page_path="",
            record_payload={"skip_event": event.to_dict()},
        )
        self._drain_plan(ledger, plan_id, skip_event=event)
        return self._plan_model(ledger.plan(plan_id))

    def route_feedback(
        self,
        *,
        recap_id: str,
        feedback_type: str,
        comment: str,
        source_agent: str,
        supersedes_ref: str = "",
        canonical_feedback: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Bind recap feedback to canonical attribution before domain bridges."""

        outbox = RecapFeedbackOutbox(self.db_path)
        event_id = outbox.create(
            recap_id=recap_id,
            feedback_type=feedback_type,
            comment=comment,
            source_agent=source_agent,
            supersedes_ref=supersedes_ref,
        )
        if canonical_feedback is not None:
            outbox.bind_canonical_feedback(event_id, canonical_feedback)
        canonical = outbox.canonical_feedback(event_id)
        for command in outbox.claim(event_id):
            try:
                status, effect_ref, evidence = self._correct_effect(
                    command,
                    feedback_type=feedback_type,
                    comment=comment,
                    canonical_feedback=canonical,
                )
            except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
                outbox.finish(
                    str(command["command_id"]),
                    status="retryable_failed",
                    error=str(exc),
                )
            else:
                outbox.finish(
                    str(command["command_id"]),
                    status=status,
                    effect_ref=effect_ref,
                    evidence=evidence,
                )
        return outbox.view(event_id)

    def drain_pending(self, limit: int = 100) -> Dict[str, Any]:
        """Retry durable recap plans and feedback corrections after a crash/restart."""
        ledger = RecapConsumptionLedger(self.db_path)
        plan_results: list[Dict[str, Any]] = self._recover_missing_plans(ledger, limit)
        plan_results.extend(self._recover_orphan_skip_plans(ledger, limit))
        recovered_plan_ids = {str(item["plan_id"]) for item in plan_results}
        for pending in ledger.pending_plans(limit=limit):
            if str(pending["plan_id"]) in recovered_plan_ids:
                continue
            payload = dict(pending["record"])
            skip_payload = payload.get("skip_event")
            if isinstance(skip_payload, dict):
                skip_event = self._skip_event_from_dict(skip_payload)
                self._drain_plan(
                    ledger,
                    str(pending["plan_id"]),
                    skip_event=skip_event,
                )
            else:
                record = self._record_from_dict(payload)
                self._drain_plan(
                    ledger,
                    str(pending["plan_id"]),
                    record=record,
                    page_path=str(pending.get("page_path") or ""),
                )
            plan_result = ledger.plan(str(pending["plan_id"]))
            plan_results.append(plan_result)
            if plan_result["plan_status"] == "consumed" and not isinstance(
                skip_payload, dict
            ):
                self._close_consumed_session(plan_result, str(pending.get("page_path") or ""))

        feedback_outbox = RecapFeedbackOutbox(self.db_path)
        feedback_results: list[Dict[str, Any]] = []
        for event in feedback_outbox.pending_events(limit=limit):
            feedback_results.append(
                self.route_feedback(
                    recap_id=str(event["recap_id"]),
                    feedback_type=str(event["feedback_type"]),
                    comment=str(event["comment"] or ""),
                    source_agent=str(event["source_agent"] or ""),
                    supersedes_ref=str(event["supersedes_ref"] or ""),
                )
            )
        return {
            "schema_version": "mnemos.recap_consumption_drain.v1",
            "plans_processed": len(plan_results),
            "plans_recovered": sum(
                1 for item in plan_results if item.get("recovered_missing_plan") is True
            ),
            "feedback_events_processed": len(feedback_results),
            "plans": plan_results,
            "feedback_events": feedback_results,
            "errors": sum(1 for item in plan_results if item["failed_targets"])
            + sum(1 for item in feedback_results if item["failed_targets"]),
        }

    def _recover_missing_plans(
        self,
        ledger: RecapConsumptionLedger,
        limit: int,
    ) -> list[Dict[str, Any]]:
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            table = conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='retrospective_sessions'
                """
            ).fetchone()
            if not table:
                return []
            rows = conn.execute(
                """
                SELECT recap_id FROM retrospective_sessions
                WHERE state='consumption_pending' ORDER BY updated_at LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        if not rows:
            return []
        from core.app.retrospective_session_manager import RetrospectiveSessionManager

        manager = RetrospectiveSessionManager(db_path=self.db_path)
        recovered: list[Dict[str, Any]] = []
        for row in rows:
            recap_id = str(row[0])
            if ledger.latest_plan_for_recap(recap_id):
                continue
            session = manager.get_session(recap_id=recap_id)
            if not session or not session.draft:
                continue
            task = manager.forced.get_recap_task(session.task_id)
            record = RetrospectiveRecord(
                draft=session.draft,
                status="confirmed",
                completion_state="confirmed",
                source="system",
                source_agent=session.source_agent,
                owner_agent=session.owner_agent,
                source_agents=session.source_agents,
                session_id=session.session_id,
                project=session.project,
                task_type=session.task_type,
                subtype=session.subtype,
                severity=str(getattr(task, "severity", "medium") or "medium"),
                follow_up_at="",
            )
            plan = self.route_after_finalize(
                record,
                page_path=session.finalized_page,
            ).to_dict()
            plan["recovered_missing_plan"] = True
            recovered.append(plan)
            if plan["plan_status"] == "consumed":
                self._close_consumed_session(plan, session.finalized_page)
        return recovered

    def _recover_orphan_skip_plans(
        self,
        ledger: RecapConsumptionLedger,
        limit: int,
    ) -> list[Dict[str, Any]]:
        """Create plans for durable skip events interrupted before plan commit."""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            table = conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='recap_skip_events'
                """
            ).fetchone()
            if not table:
                return []
            rows = conn.execute(
                """
                SELECT s.* FROM recap_skip_events AS s
                WHERE NOT EXISTS (
                    SELECT 1 FROM recap_consumption_plans AS p
                    WHERE p.recap_id=s.recap_id
                )
                ORDER BY s.selected_at LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        recovered: list[Dict[str, Any]] = []
        for row in rows:
            recap_id = str(row["recap_id"])
            if ledger.latest_plan_for_recap(recap_id):
                continue
            event = SkipEvent(
                event_id=str(row["event_id"]),
                recap_id=recap_id,
                task_id=str(row["task_id"]),
                skip_reason=str(row["skip_reason"]),
                skip_status=str(row["skip_status"]),
                next_policy=str(row["next_policy"]),
                source_agent=str(row["source_agent"] or ""),
                owner_agent=str(row["owner_agent"] or ""),
                source_agents=list(json.loads(row["source_agents"] or "[]")),
                project=str(row["project"] or ""),
                task_type=str(row["task_type"] or ""),
                trigger_reason=list(json.loads(row["trigger_reason"] or "[]")),
                selected_at=str(row["selected_at"]),
                defer_until=str(row["defer_until"] or ""),
                user_note=str(row["user_note"] or ""),
                consumption_targets=list(
                    json.loads(row["consumption_targets"] or "[]")
                ),
                write_to_wiki=bool(row["write_to_wiki"]),
            )
            plan = self.route_skip_event(event).to_dict()
            plan["recovered_missing_plan"] = True
            recovered.append(plan)
        return recovered

    def _close_consumed_session(self, plan: Dict[str, Any], page_path: str) -> None:
        from core.app.retrospective_session_manager import RetrospectiveSessionManager

        manager = RetrospectiveSessionManager(db_path=self.db_path)
        session = manager.get_session(recap_id=str(plan["recap_id"]))
        if not session or session.state == "consumed":
            return
        receipt = {
            **dict(session.completion_receipt),
            "status": "committed",
            "terminal": True,
            "consumption_plan_id": plan["plan_id"],
            "consumption_plan_status": plan["plan_status"],
            "required_receipt_count": plan["required_receipt_count"],
            "terminal_receipt_count": plan["terminal_receipt_count"],
        }
        manager.mark_pipeline_state(
            str(plan["recap_id"]),
            "consumption_pending",
            page_path=page_path,
            completion_receipt=receipt,
        )
        manager.mark_finalized(str(plan["recap_id"]), page_path)
        manager.mark_consumed(str(plan["recap_id"]))

    @staticmethod
    def _record_from_dict(data: Dict[str, Any]) -> RetrospectiveRecord:
        draft_data = dict(data.get("draft") or {})
        actions = [
            RetrospectiveActionItem(**item)
            for item in draft_data.pop("action_items", [])
            if isinstance(item, dict)
        ]
        draft = RetrospectiveDraft(**draft_data, action_items=actions)
        record_data = dict(data)
        record_data.pop("draft", None)
        return RetrospectiveRecord(draft=draft, **record_data)

    @staticmethod
    def _skip_event_from_dict(data: Dict[str, Any]) -> SkipEvent:
        payload = dict(data)
        payload.pop("event_type", None)
        payload.pop("schema", None)
        payload.pop("consumption_plan", None)
        return SkipEvent(**payload)

    def _correct_effect(
        self,
        command: Dict[str, Any],
        *,
        feedback_type: str,
        comment: str,
        canonical_feedback: Dict[str, Any],
    ) -> tuple[str, str, Dict[str, Any]]:
        """Consume canonical attribution receipts without replaying the reaction."""

        target = str(command["canonical_target"])
        correction_id = str(command["command_id"])
        recap_id = str(command["recap_id"])
        source_effect_ref = str(command.get("source_effect_ref") or "")
        if target == "feedback_outcome":
            self.mark_consumed(
                recap_id,
                consumer="recap_feedback",
                outcome="accepted",
                evidence=comment,
                source_event_id=correction_id,
            )
            return "committed", f"recap-feedback-outcome:{correction_id}", {
                "outcome": "reaction_recorded",
                "canonical_reaction_event_id": canonical_feedback[
                    "feedback_event_id"
                ],
            }
        target_map = {
            "knowledge_retrieval": "belief_correction_proposal",
            "policy_patch": "policy_proposal",
            "follow_up": "delivery_state",
            "persona": "persona_proposal",
            "scoring": "training_evidence",
            "scheduler": "delivery_state",
        }
        canonical_target = target_map.get(target)
        if canonical_target is None:
            raise ValueError(f"unregistered recap correction handler: {target}")
        receipts = [
            item
            for item in canonical_feedback.get("terminal_receipts", [])
            if item.get("target_id") == canonical_target
        ]
        if len(receipts) != 1:
            raise ValueError("recap correction lacks one canonical target receipt")
        receipt = dict(receipts[0])
        canonical_proof = {
            "feedback_type": feedback_type,
            "source_effect_ref": source_effect_ref,
            "canonical_target_id": canonical_target,
            "canonical_disposition": receipt["disposition"],
            "canonical_effect_receipt_id": receipt["effect_receipt_id"],
            "canonical_attribution_revision_id": canonical_feedback[
                "attribution_revision_id"
            ],
        }
        outbox = RecapFeedbackOutbox(self.db_path)
        if target == "knowledge_retrieval":
            if not source_effect_ref.startswith("wiki:"):
                raise ValueError("retrieval correction lacks its exact page effect")
            outbox.mark_effect_state(
                recap_id=recap_id,
                canonical_target=target,
                status="superseded",
                source_event_id=correction_id,
                effect_ref=source_effect_ref,
            )
            return "committed", f"retrieval-superseded:{recap_id}", {
                **canonical_proof,
                "outcome": "suppressed",
                "target_oracle": f"recap-effect-state:{recap_id}:knowledge_retrieval:superseded",
            }
        if target == "policy_patch":
            if not source_effect_ref:
                raise ValueError("policy correction lacks its exact patch effect")
            from core.cognitive.policy_patch import PolicyPatchStore

            policy_outcome = {
                "inaccurate": "contradicted",
                "irrelevant": "irrelevant",
                "outdated": "outdated",
            }[feedback_type]
            store = PolicyPatchStore()
            evidence = {
                **canonical_proof,
                "recap_id": recap_id,
                "source_event_id": correction_id,
                "comment": comment,
            }
            material_action = store.prepare_feedback_material_action(
                patch_id=source_effect_ref,
                outcome=policy_outcome,
                evidence=evidence,
                source_event_id=correction_id,
                source_facts={
                    "feedback_type": feedback_type,
                    "correction_command": dict(command),
                    "canonical_proof": canonical_proof,
                },
                evidence_refs=(
                    f"recap:{recap_id}",
                    f"recap-feedback-command:{correction_id}",
                    f"cognitive-effect-receipt:{receipt['effect_receipt_id']}",
                ),
                created_at=datetime.now().astimezone().isoformat(),
                producer="retrospective-feedback-correction-router",
            )
            result = store.record_feedback(
                source_effect_ref,
                outcome=policy_outcome,
                evidence=evidence,
                source_event_id=correction_id,
                material_action=material_action,
            )
            if not result.get("feedback_id") or result.get("status") == "active":
                raise RuntimeError("policy correction did not suppress the active patch")
            outbox.mark_effect_state(
                recap_id=recap_id,
                canonical_target=target,
                status="superseded",
                source_event_id=correction_id,
                effect_ref=source_effect_ref,
            )
            return "committed", f"policy-feedback:{result['feedback_id']}", {
                **canonical_proof,
                "outcome": policy_outcome,
                "patch_status": result["status"],
            }
        if target == "follow_up":
            prefix = "dialog-reminder:"
            if not source_effect_ref.startswith(prefix):
                raise ValueError("follow-up correction lacks reminder identity")
            reminder_id = source_effect_ref[len(prefix) :]
            from core.kia.dialog_reminder import DialogReminderQueue

            queue = DialogReminderQueue(
                db_path=str(get_config().database_dir / "dialog_reminder.db")
            )
            if not queue.dismiss(
                reminder_id,
                reason=f"recap_{feedback_type}",
                source_event_id=correction_id,
            ):
                raise RuntimeError("follow-up reminder could not be suppressed")
            outbox.mark_effect_state(
                recap_id=recap_id,
                canonical_target=target,
                status="superseded",
                source_event_id=correction_id,
                effect_ref=source_effect_ref,
            )
            return "committed", f"dismissed:{reminder_id}", {
                **canonical_proof,
                "outcome": "suppressed",
                "target_oracle": f"dialog-reminder:{reminder_id}:dismissed",
            }
        if target == "persona":
            prefix = "reflection-signal:"
            if not source_effect_ref.startswith(prefix):
                raise ValueError("persona correction lacks signal identity")
            signal_id = source_effect_ref[len(prefix) :]
            from core.persona.psyche import suppress_reflection_signal

            suppression = suppress_reflection_signal(
                signal_id=int(signal_id),
                source_event_id=correction_id,
                reason=f"recap_{feedback_type}",
                evidence=canonical_proof,
            )
            outbox.mark_effect_state(
                recap_id=recap_id,
                canonical_target=target,
                status="superseded",
                source_event_id=correction_id,
                effect_ref=source_effect_ref,
            )
            return "committed", str(suppression["receipt_ref"]), {
                **canonical_proof,
                "outcome": "suppressed",
                "target_oracle": suppression["target_oracle"],
            }
        if target == "scoring":
            from core.scoring.feedback_provenance import (
                build_training_feedback_proposal_owner,
            )
            from core.cognitive.state_store import CognitiveStateStore

            owner = build_training_feedback_proposal_owner(get_config().database_dir)
            prior_id = source_effect_ref.rsplit(":", 1)[-1]
            with sqlite3.connect(str(owner.db_path), timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                prior = conn.execute(
                    render_sql(
                        "SELECT after_hash FROM {table} WHERE receipt_id=?",
                        identifiers={"table": owner.receipt_table},
                    ),
                    (prior_id,),
                ).fetchone()
            if prior is None:
                raise ValueError("scoring correction source proposal is missing")
            state = CognitiveStateStore(
                get_config().database_dir / "producer_consumer_ledger.db"
            )
            attribution = state.revision(
                str(canonical_feedback["attribution_revision_id"])
            )
            if attribution is None:
                raise ValueError("scoring correction attribution is missing")
            effect = owner.neutralize(
                {
                    "schema_version": "mnemos.feedback_neutralization_command.v1",
                    "target_id": "training_evidence",
                    "command_key": correction_id,
                    "attribution_revision_id": attribution.revision_id,
                    "attribution_payload_hash": attribution.payload_hash,
                    "prior_target_receipt_ref": source_effect_ref,
                    "prior_after_hash": str(prior["after_hash"]),
                    "neutralization_kind": "suppress",
                }
            )
            if not owner.verify(effect):
                raise RuntimeError("scoring correction lacks reciprocal suppression")
            outbox.mark_effect_state(
                recap_id=recap_id,
                canonical_target=target,
                status="superseded",
                source_event_id=correction_id,
                effect_ref=source_effect_ref,
            )
            return "committed", effect.target_receipt_ref, {
                **canonical_proof,
                "outcome": effect.disposition,
                "target_oracle": effect.target_receipt_ref,
                "training_admitted": False,
            }
        if target == "scheduler":
            source_command_id = str(command.get("source_command_id") or "")
            if not source_command_id:
                raise ValueError("scheduler correction lacks its source command")
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT task_id, corrected_status FROM recap_scheduler_corrections WHERE source_event_id=?",
                    (correction_id,),
                ).fetchone()
                if existing:
                    task_id, corrected_status = str(existing[0]), str(existing[1])
                else:
                    effect = conn.execute(
                        "SELECT task_id FROM recap_scheduler_effects WHERE source_event_id=?",
                        (source_command_id,),
                    ).fetchone()
                    if effect is None:
                        raise ValueError("scheduler source effect is missing")
                    task_id = str(effect[0])
                    task = conn.execute(
                        "SELECT status, due_date FROM recap_tasks WHERE task_id=?",
                        (task_id,),
                    ).fetchone()
                    if task is None:
                        raise ValueError("scheduler source task is missing")
                    corrected_status = "pending"
                    conn.execute(
                        "UPDATE recap_tasks SET status='pending', due_date='' WHERE task_id=?",
                        (task_id,),
                    )
                    conn.execute(
                        """
                        INSERT INTO recap_scheduler_corrections (
                            source_event_id, source_command_id, recap_id, task_id,
                            previous_status, previous_due_date, corrected_status,
                            corrected_due_date, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?)
                        """,
                        (
                            correction_id,
                            source_command_id,
                            recap_id,
                            task_id,
                            str(task[0] or ""),
                            str(task[1] or ""),
                            corrected_status,
                            datetime.now().isoformat(),
                        ),
                    )
                committed = conn.execute(
                    "SELECT status, due_date FROM recap_tasks WHERE task_id=?",
                    (task_id,),
                ).fetchone()
            if not committed or str(committed[0]) != corrected_status:
                raise RuntimeError("scheduler correction is not durably applied")
            outbox.mark_effect_state(
                recap_id=recap_id,
                canonical_target=target,
                status="superseded",
                source_event_id=correction_id,
                effect_ref=source_effect_ref,
            )
            return "committed", f"recap-task:{task_id}:{corrected_status}", {
                **canonical_proof,
                "outcome": "restored_pending",
                "target_oracle": f"recap-task:{task_id}:{corrected_status}",
            }
        raise ValueError(f"unregistered recap correction handler: {target}")

    @staticmethod
    def _plan_model(data: Dict[str, Any]) -> ConsumptionPlan:
        return ConsumptionPlan(
            recap_id=data["recap_id"],
            targets=list(data["targets"]),
            activation_rules=dict(data["activation_rules"]),
            consume_priority=str(data["consume_priority"]),
            follow_up_at=str(data["follow_up_at"]),
            outcomes=list(data["outcomes"]),
            plan_id=str(data["plan_id"]),
            plan_status=str(data["plan_status"]),
            target_statuses=list(data["target_statuses"]),
            required_receipt_count=int(data["required_receipt_count"]),
            terminal_receipt_count=int(data["terminal_receipt_count"]),
            consumed_at=str(data["consumed_at"]),
            retryable=bool(data["retryable"]),
            failed_targets=list(data["failed_targets"]),
            effect_evidence=list(data["effect_evidence"]),
        )

    def _drain_plan(
        self,
        ledger: RecapConsumptionLedger,
        plan_id: str,
        *,
        record: RetrospectiveRecord | None = None,
        skip_event: SkipEvent | None = None,
        page_path: str = "",
    ) -> None:
        for command in ledger.claim(plan_id):
            target = str(command["canonical_target"])
            try:
                status, effect_ref, evidence = self._dispatch_target(
                    target,
                    record=record,
                    skip_event=skip_event,
                    page_path=page_path,
                    command_id=str(command["command_id"]),
                )
            except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
                ledger.finish(
                    str(command["command_id"]),
                    status="retryable_failed",
                    error=str(exc),
                )
            else:
                ledger.finish(
                    str(command["command_id"]),
                    status=status,
                    effect_ref=effect_ref,
                    evidence=evidence,
                )

    def _dispatch_target(
        self,
        target: str,
        *,
        record: RetrospectiveRecord | None,
        skip_event: SkipEvent | None,
        page_path: str,
        command_id: str,
    ) -> tuple[str, str, Dict[str, Any]]:
        if target == "knowledge_retrieval":
            if not record or not page_path:
                raise ValueError("knowledge retrieval requires a finalized recap page")
            page = get_config().wiki_dir / page_path
            if not page.exists():
                raise ValueError("finalized recap page is missing from retrieval source")
            return "committed", f"wiki:{page_path}", {"outcome": "indexed"}
        if target == "policy_patch":
            if not record:
                return "intentional_skip", "", {
                    "outcome": "skipped",
                    "reason": "skip_event_has_no_policy_patch",
                }
            outcome = self._route_policy_patch(
                record,
                "high" if record.severity in ("critical", "high") else "medium",
            )
            if not outcome:
                return "intentional_skip", "", {
                    "outcome": "skipped",
                    "reason": "policy_patch_not_applicable",
                }
            if outcome.get("outcome") == "error":
                raise RuntimeError(str(outcome.get("evidence") or "policy patch consumer failed"))
            status = "committed" if outcome.get("outcome") == "proposed" else "intentional_skip"
            return status, str(outcome.get("evidence") or ""), dict(outcome)
        if target == "follow_up":
            if not record:
                raise ValueError("follow-up target requires a finalized recap")
            from core.kia.dialog_reminder import DialogReminderQueue

            reminder_queue = DialogReminderQueue(
                db_path=str(get_config().database_dir / "dialog_reminder.db")
            )
            reminder_id = reminder_queue.enqueue(
                issue_id=f"recap-follow-up:{record.draft.recap_id}",
                page_path=page_path,
                severity=record.severity,
                content=record.draft.lesson or record.draft.title,
                choices=["已执行", "稍后提醒", "不再提醒"],
            )
            if record.follow_up_at and not reminder_queue.schedule_at(
                reminder_id, record.follow_up_at
            ):
                raise RuntimeError("follow-up reminder could not be scheduled")
            return "committed", f"dialog-reminder:{reminder_id}", {
                "outcome": "scheduled",
                "command_id": command_id,
                "follow_up_at": record.follow_up_at,
            }
        if target == "persona":
            from core.persona.psyche import get_signal_store

            if record:
                value = record.draft.lesson or record.draft.title
                source = f"recap:{record.draft.recap_id}"
            elif skip_event:
                value = f"recap skip: {skip_event.skip_reason}; policy: {skip_event.next_policy}"
                source = f"recap-skip:{skip_event.event_id}"
            else:
                raise ValueError("persona target requires a recap or skip event")
            signal_id = get_signal_store().add_signal(
                dimension="retrospective_lesson",
                value=value,
                confidence=0.9,
                source=source,
                source_event_id=command_id,
            )
            if not signal_id:
                raise RuntimeError("persona signal consumer returned no durable identity")
            return "committed", f"reflection-signal:{signal_id}", {
                "outcome": "persona_signal_recorded",
                "source_event_id": command_id,
            }
        if target == "scheduler":
            if not skip_event:
                raise ValueError("scheduler target requires a recap skip event")
            expected_status = {
                "deferred": "pending",
                "dismissed": "ignored",
                "false_positive": "ignored",
                "already_handled": "resolved",
                "cooldown": "pending",
            }.get(skip_event.skip_status, "pending")
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recap_scheduler_effects (
                        source_event_id TEXT PRIMARY KEY,
                        recap_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        due_date TEXT DEFAULT '',
                        created_at TEXT NOT NULL
                    )
                    """
                )
                inserted = conn.execute(
                    """
                    INSERT OR IGNORE INTO recap_scheduler_effects (
                        source_event_id, recap_id, task_id, status, due_date, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        command_id,
                        skip_event.recap_id,
                        skip_event.task_id,
                        expected_status,
                        skip_event.defer_until,
                        datetime.now().isoformat(),
                    ),
                ).rowcount
                if inserted:
                    note = (
                        "\n\n[recap_skip]"
                        f" reason={skip_event.skip_reason}"
                        f" status={skip_event.skip_status}"
                        f" policy={skip_event.next_policy}"
                        f" event_id={skip_event.event_id}"
                    )
                    conn.execute(
                        """
                        UPDATE recap_tasks
                        SET status=?, due_date=?, context=COALESCE(context, '') || ?
                        WHERE task_id=?
                        """,
                        (
                            expected_status,
                            skip_event.defer_until,
                            note,
                            skip_event.task_id,
                        ),
                    )
                row = conn.execute(
                    "SELECT status, due_date FROM recap_tasks WHERE task_id=?",
                    (skip_event.task_id,),
                ).fetchone()
            if not row or str(row[0]) != expected_status:
                raise RuntimeError("recap scheduler effect is not durably applied")
            return "committed", f"recap-task:{skip_event.task_id}:{expected_status}", {
                "outcome": "scheduled",
                "due_date": str(row[1] or ""),
                "source_event_id": command_id,
            }
        raise ValueError(f"unregistered recap target handler: {target}")

    def match_for_task(
        self,
        task_type: str,
        context_text: str,
        current_file: str = "",
        limit: int = 5,
    ) -> List[Dict]:
        """Return stored plans whose activation rules match the current task."""
        haystack = " ".join([task_type, context_text, current_file]).lower()
        matches: List[Dict] = []
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM recap_consumption_plans ORDER BY updated_at DESC LIMIT 100"
            ).fetchall()
        for row in rows:
            rules = json.loads(row["activation_rules"] or "{}")
            keywords = [str(item).lower() for item in rules.get("keywords", [])]
            file_patterns = [str(item).lower() for item in rules.get("current_file_patterns", [])]
            if any(self._keyword_matches(keyword, haystack) for keyword in keywords) or any(
                pattern and pattern in current_file.lower() for pattern in file_patterns
            ):
                matches.append(
                    {
                        "recap_id": row["recap_id"],
                        "targets": json.loads(row["targets"] or "[]"),
                        "activation_rules": rules,
                        "consume_priority": row["consume_priority"],
                        "follow_up_at": row["follow_up_at"] or "",
                    }
                )
            if len(matches) >= limit:
                break
        return matches

    @staticmethod
    def _keyword_matches(keyword: str, haystack: str) -> bool:
        """Match exact keywords plus conservative Chinese sub-phrases."""
        if not keyword:
            return False
        if keyword in haystack:
            return True
        if any("\u4e00" <= char <= "\u9fff" for char in keyword) and len(keyword) >= 4:
            chunks = [keyword[i : i + 2] for i in range(0, len(keyword) - 1, 2)]
            return any(chunk in haystack for chunk in chunks)
        return False

    def mark_consumed(
        self,
        recap_id: str,
        consumer: str,
        outcome: str,
        evidence: str = "",
        source_event_id: str = "",
    ) -> None:
        """Append an outcome for a consumer using a recap."""
        now = datetime.now().isoformat()
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recap_consumption_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recap_id TEXT NOT NULL,
                    consumer TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    evidence TEXT DEFAULT '',
                    source_event_id TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(recap_consumption_outcomes)")
            }
            if "source_event_id" not in columns:
                conn.execute(
                    "ALTER TABLE recap_consumption_outcomes "
                    "ADD COLUMN source_event_id TEXT DEFAULT ''"
                )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_recap_outcome_source_event
                ON recap_consumption_outcomes(source_event_id)
                WHERE source_event_id <> ''
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO recap_consumption_outcomes
                    (recap_id, consumer, outcome, evidence, source_event_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (recap_id, consumer, outcome, evidence, source_event_id, now),
            )

    def _route_policy_patch(
        self,
        record: RetrospectiveRecord,
        priority: str,
    ) -> Dict[str, Any]:
        """Persist a confirmed recap as a bounded policy patch when it can affect KIA."""
        draft = record.draft
        if draft.next_handling == "no_action_needed":
            return {}
        if not ({*draft.consumption_targets} & {"preflight", "guard"}) and not draft.activation_rules:
            return {}

        content = self._policy_patch_content(record)
        if not content:
            return {}
        triggers = self._policy_patch_triggers(record)
        if not triggers:
            return {
                "consumer": "policy_patch",
                "outcome": "skipped",
                "evidence": "missing_trigger",
            }
        lesson_evidence_refs = self._policy_patch_evidence_refs(record)
        lesson = {
            "source_type": "retrospective",
            "source_id": draft.recap_id,
            "task_type": record.task_type or "general",
            "subtype": record.subtype or "general",
            "scope": record.project or "global",
            "severity": record.severity,
            "summary": content,
            "trigger_keywords": triggers,
            "confidence": 0.9,
            "evidence_refs": lesson_evidence_refs,
            "metadata": {
                "title": draft.title,
                "root_type": draft.root_type,
                "activation_rules": draft.activation_rules,
                "consume_priority": priority,
                "next_handling": draft.next_handling,
            },
        }
        try:
            from core.cognitive.decision_trace import (
                MaterialActionRequest,
                authorize_exact_project_contract_action,
            )
            from core.cognitive.policy_patch import (
                POLICY_PATCH_EXECUTOR,
                POLICY_PATCH_OWNER,
                POLICY_PATCH_PROPOSE_ACTION,
                PolicyPatchStore,
                policy_patch_proposal_binding,
            )

            store = PolicyPatchStore()
            binding = policy_patch_proposal_binding(lesson, store.options)
            if binding is None:
                return {
                    "consumer": "policy_patch",
                    "outcome": "skipped",
                    "evidence": "disabled_or_below_threshold",
                }
            state_db_path = (
                store.options.database_dir / "producer_consumer_ledger.db"
            ).resolve(strict=False)
            request = MaterialActionRequest(
                owner=POLICY_PATCH_OWNER,
                executor_id=POLICY_PATCH_EXECUTOR,
                action_type=POLICY_PATCH_PROPOSE_ACTION,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
                expected_state_db=str(state_db_path),
            )
            material_action = authorize_exact_project_contract_action(
                expected_request=request,
                state_db_path=state_db_path,
                contract_id=RECAP_POLICY_DECISION_CONTRACT_ID,
                contract_revision_id=RECAP_POLICY_DECISION_CONTRACT_REVISION,
                contract_text=RECAP_POLICY_DECISION_CONTRACT_TEXT,
                source_namespace="confirmed-recap-policy-patch",
                source_facts={
                    "schema_version": "mnemos.confirmed_recap_policy_facts.v1",
                    "record": record.to_dict(),
                    "lesson": lesson,
                    "consume_priority": priority,
                },
                decision_checks={
                    "confirmed_recap_is_actionable": (
                        record.status == "confirmed"
                        and draft.next_handling != "no_action_needed"
                    ),
                    "bounded_patch_has_triggers_and_evidence": (
                        bool(triggers) and bool(lesson_evidence_refs)
                    ),
                },
                evidence_refs=tuple(
                    dict.fromkeys(
                        (
                            f"recap:{draft.recap_id}",
                            *(str(ref) for ref in lesson_evidence_refs),
                        )
                    )
                ),
                task=f"Create policy patch from recap {draft.recap_id}",
                goal=(
                    "Persist only the exact bounded policy patch derived from "
                    "the confirmed recap."
                ),
                constraints=(
                    "The recap must remain confirmed and actionable.",
                    "Scope, trigger terms, content, confidence, and evidence cannot drift.",
                ),
                created_at=(
                    datetime.fromisoformat(record.reviewed_at)
                    .astimezone()
                    .isoformat()
                ),
                producer="retrospective-consumption-router",
                producer_version=RECAP_POLICY_DECISION_CONTRACT_REVISION,
                producer_code_hash=RECAP_POLICY_DECISION_PRODUCER_HASH,
                evaluator_id="confirmed-recap-policy-patch-evaluator",
                approved_candidate_key="create_exact_confirmed_recap_patch",
                approved_candidate_summary=(
                    "Create the exact bounded patch derived from the confirmed recap."
                ),
                rejected_candidate_key="retain_policy_without_unbound_patch",
                rejected_candidate_summary=(
                    "Retain current policy when recap scope, triggers, or evidence drift."
                ),
                approved_reason_code="confirmed_recap_patch_binding_verified",
                rejected_reason_code="confirmed_recap_patch_binding_rejected",
                committed_metric="confirmed_recap_policy_patch_receipt",
                rejected_metric="unbound_recap_policy_patch_count",
            )
            patch = store.propose(
                lesson,
                material_action=material_action,
            )
        except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
            return {
                "consumer": "policy_patch",
                "outcome": "error",
                "evidence": str(exc)[:200],
            }
        if not patch:
            return {
                "consumer": "policy_patch",
                "outcome": "skipped",
                "evidence": "disabled_or_below_threshold",
            }
        return {
            "consumer": "policy_patch",
            "outcome": "proposed",
            "evidence": patch.patch_id,
        }

    @staticmethod
    def _policy_patch_content(record: RetrospectiveRecord) -> str:
        draft = record.draft
        content = draft.lesson.strip()
        details = [
            item.action.strip()
            for item in draft.action_items[:2]
            if item.action and item.action.strip() and item.action.strip() not in content
        ]
        if details:
            content = f"{content}；关键动作：{'；'.join(details)}" if content else "；".join(details)
        return str(content)

    @staticmethod
    def _policy_patch_triggers(record: RetrospectiveRecord) -> List[str]:
        draft = record.draft
        rules = draft.activation_rules or {}
        raw_terms: List[str] = []
        raw_terms.extend(str(item) for item in record.trigger_reason if str(item or "").strip())
        raw_terms.extend(str(item) for item in rules.get("keywords", []) if str(item or "").strip())
        raw_terms.extend(
            str(item) for item in rules.get("current_file_patterns", []) if str(item or "").strip()
        )
        for action_item in draft.action_items[:2]:
            raw_terms.extend(_extract_policy_terms(action_item.action))
        raw_terms.extend(_extract_policy_terms(draft.lesson))

        terms: List[str] = []
        for term in raw_terms:
            normalized = term.strip()
            if normalized and normalized not in terms:
                terms.append(normalized)
            if len(terms) >= 12:
                break
        return terms

    @staticmethod
    def _policy_patch_evidence_refs(record: RetrospectiveRecord) -> List[str]:
        refs = list(dict.fromkeys(record.draft.evidence_refs or []))
        refs.append(f"recap://{record.draft.recap_id}")
        if record.session_id:
            refs.append(f"session://{record.session_id}")
        return refs

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        RecapConsumptionLedger(self.db_path)
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recap_consumption_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recap_id TEXT NOT NULL,
                    consumer TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    evidence TEXT DEFAULT '',
                    source_event_id TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(recap_consumption_outcomes)")
            }
            if "source_event_id" not in columns:
                conn.execute(
                    "ALTER TABLE recap_consumption_outcomes "
                    "ADD COLUMN source_event_id TEXT DEFAULT ''"
                )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_recap_outcome_source_event
                ON recap_consumption_outcomes(source_event_id)
                WHERE source_event_id <> ''
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recap_scheduler_corrections (
                    source_event_id TEXT PRIMARY KEY,
                    source_command_id TEXT NOT NULL,
                    recap_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    previous_status TEXT NOT NULL,
                    previous_due_date TEXT DEFAULT '',
                    corrected_status TEXT NOT NULL,
                    corrected_due_date TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )


def _extract_policy_terms(text: str) -> List[str]:
    terms: List[str] = []
    for token in str(text or "").replace("--", " --").split():
        clean = token.strip(" ，,。；;：:()[]{}")
        if not clean:
            continue
        if "." in clean or "/" in clean or "_" in clean or "-" in clean:
            terms.append(clean)
    return terms
