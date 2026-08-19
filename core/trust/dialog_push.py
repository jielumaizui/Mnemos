"""Dialog-facing decision cards for trusted push proposals."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Protocol

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionRequest,
    ProjectContractDecisionContext,
    ProjectContractMaterialActionResolver,
    build_exact_project_contract_evaluator,
)
from core.cognitive.state_contract import sha256_json as cognitive_sha256_json
from core.trust.config import TrustedPushConfig, load_trusted_push_config
from core.trust.knowledge_vault_writer import (
    KNOWLEDGE_VAULT_ACTION_TYPE,
    KNOWLEDGE_VAULT_EXECUTOR,
    KNOWLEDGE_VAULT_OWNER,
    KnowledgeVaultWriter,
    knowledge_vault_material_action_binding,
)
from core.trust.models import JournalEventInput, Proposal, UserDecision, new_id, sha256_text, utc_now_iso
from core.trust.proposal_queue import ProposalQueue
from core.trust.write_journal import WriteJournal


DIALOG_APPROVAL_DECISION_CONTRACT_ID = (
    "project-contract:trusted-proposal-user-approval"
)
DIALOG_APPROVAL_DECISION_CONTRACT_REVISION = (
    "mnemos.trusted_proposal_user_approval.v1"
)
DIALOG_APPROVAL_DECISION_CONTRACT_TEXT = (
    "A user-triggered approval may commit only the exact current trusted-push "
    "proposal revision, target, content, gate result, and confirmed risk scope."
)
DIALOG_APPROVAL_DECISION_PRODUCER_HASH = cognitive_sha256_json(
    {
        "module": "core.trust.dialog_push",
        "producer": "DialogDecisionPush.decide",
        "version": DIALOG_APPROVAL_DECISION_CONTRACT_REVISION,
    }
)


class DialogDecisionAdapter(Protocol):
    """Optional host adapter that can show cards outside the whitebox surface."""

    def deliver(self, cards: list["DecisionCard"]) -> None:
        """Deliver cards or raise on timeout/unavailable adapter."""


@dataclass(frozen=True)
class DecisionCard:
    card_id: str
    proposal_id: str
    title: str
    summary: str
    source: str
    target_uri: str
    risk_level: str
    status: str
    surface: str
    actions: List[dict[str, str]]
    merged_proposal_ids: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DialogDecisionPush:
    """Build and deliver structured decision cards without touching native chat history."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        wiki_base: Path,
        config: TrustedPushConfig | None = None,
        max_daily: int = 20,
        quiet_start_hour: int = 22,
        quiet_end_hour: int = 7,
    ):
        self._config = config or load_trusted_push_config(wiki_base=wiki_base)
        self.db_path = Path(db_path or self._config.db_path)
        self._wiki_base = wiki_base
        self._queue = ProposalQueue(self.db_path, wiki_base=wiki_base, config=self._config)
        self._journal = WriteJournal(self.db_path, config=self._config)
        self._max_daily = max_daily
        self._quiet_start_hour = quiet_start_hour
        self._quiet_end_hour = quiet_end_hour
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dialog_push_events (
                    event_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    surface TEXT NOT NULL,
                    card_json TEXT NOT NULL,
                    snooze_until TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def push(
        self,
        *,
        limit: int = 5,
        surface: str = "whitebox",
        agent_adapter: DialogDecisionAdapter | None = None,
        now: datetime | None = None,
        respect_quiet_hours: bool = False,
    ) -> dict[str, Any]:
        requested_surface = "agent" if agent_adapter is not None else surface
        cards = self._next_cards(
            limit=limit,
            surface=requested_surface,
            now=now,
            respect_quiet_hours=respect_quiet_hours,
        )
        fallback_reason = ""
        if not cards:
            return {
                "surface": "none",
                "fallback_reason": fallback_reason,
                "cards": [],
            }
        if agent_adapter is not None and cards:
            try:
                agent_adapter.deliver(cards)
            except (TimeoutError, RuntimeError, OSError, ValueError) as exc:
                fallback_reason = f"{type(exc).__name__}: {exc}"
                self._fallback_cards(cards, fallback_reason)
                cards = [
                    DecisionCard(**{**card.to_dict(), "surface": "whitebox"})
                    for card in cards
                ]
        return {
            "surface": "whitebox" if fallback_reason else requested_surface,
            "fallback_reason": fallback_reason,
            "cards": [card.to_dict() for card in cards],
        }

    def decide(
        self,
        proposal_id: str,
        action: str,
        *,
        content: str | None = None,
        reason: str = "",
        actor: str = "user",
        allow_high_risk: bool = False,
        snooze_hours: int = 24,
        material_action: MaterialActionAuthorization | None = None,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        supersedes_event_id: str = "",
    ) -> dict[str, Any]:
        if principal is None or narrowing is None:
            raise PermissionError(
                "dialog decision requires an authenticated principal and exact scope"
            )
        proposal = self._queue.get(proposal_id)
        source_session_id = str(proposal.candidate.source_session_id or "").strip()
        requested_session_id = str(narrowing.session_id or "").strip()
        if not source_session_id:
            raise PermissionError(
                "dialog decision proposal source session is unavailable"
            )
        if requested_session_id != source_session_id:
            raise PermissionError(
                "dialog decision session scope does not match proposal source"
            )
        source_agent = str(proposal.candidate.source_agent or "").strip().lower()
        principal_agent = str(principal.agent or "").strip().lower()
        if not source_agent or source_agent != principal_agent:
            raise PermissionError(
                "dialog decision principal agent does not match proposal source"
            )
        from core.cognitive.feedback_entrypoints import (
            record_dialog_decision_feedback,
        )

        proposal_snapshot = {
            "proposal_id": proposal.proposal_id,
            "revision": proposal.revision,
            "candidate_hash": cognitive_sha256_json(
                proposal.candidate.to_dict()
            ),
            "gate_decision": proposal.gate_decision,
            "risk_level": proposal.risk_level,
            "target_uri": str(proposal.candidate.target_path or ""),
            "source_agent": source_agent,
            "source_session_id": source_session_id,
        }
        canonical_feedback = record_dialog_decision_feedback(
            database_dir=self.db_path.parent,
            proposal_snapshot=proposal_snapshot,
            action=action,
            reason=reason,
            principal=principal,
            narrowing=narrowing,
            supersedes_event_id=supersedes_event_id,
        )

        def with_feedback(result: dict[str, Any]) -> dict[str, Any]:
            return {**result, "canonical_feedback": canonical_feedback}

        if action == "approve":
            if proposal.status == "committed":
                return with_feedback(
                    {
                        "status": "duplicate",
                        "proposal_id": proposal_id,
                        "action": action,
                    }
                )
            if material_action is None:
                material_action = self._approval_material_action(
                    proposal,
                    actor=actor,
                    allow_high_risk=allow_high_risk,
                    created_at=utc_now_iso(),
                )
            result = KnowledgeVaultWriter(
                wiki_base=self._wiki_base,
                db_path=self.db_path,
                config=self._config,
            ).write_proposal(
                proposal_id,
                actor=actor,
                allow_high_risk=allow_high_risk,
                material_action=material_action,
            )
            self._mark_acted(proposal_id)
            return with_feedback({"action": action, **result})
        if action == "reject":
            if proposal.status == "rejected":
                return with_feedback(
                    {
                        "status": "duplicate",
                        "proposal_id": proposal_id,
                        "action": action,
                    }
                )
            from core.trust.vault_mutation_service import (
                bind_trusted_markdown_candidate_action,
                record_trusted_markdown_no_effect_terminal,
            )

            origin_material_action = bind_trusted_markdown_candidate_action(
                proposal.candidate,
                state_db_path=self.db_path.parent / "producer_consumer_ledger.db",
            )
            self._queue.record_decision(
                UserDecision(proposal_id=proposal_id, decision="reject", actor=actor, reason=reason)
            )
            self._queue.update_status(proposal_id, "rejected")
            self._append_decision_event(proposal, "reject", actor, {"reason": reason})
            if origin_material_action is not None:
                record_trusted_markdown_no_effect_terminal(
                    origin_material_action,
                    target_path=Path(proposal.candidate.target_path or ""),
                    status="rejected",
                    reason_code="trusted_proposal_rejected",
                    evidence_ref=f"target-journal:trusted-reject:{proposal_id}",
                )
            self._mark_acted(proposal_id)
            return with_feedback({"status": "rejected", "proposal_id": proposal_id})
        if action == "snooze":
            snooze_until = (
                datetime.now(timezone.utc) + timedelta(hours=max(1, snooze_hours))
            ).replace(microsecond=0).isoformat()
            self._queue.record_decision(
                UserDecision(proposal_id=proposal_id, decision="snooze", actor=actor, reason=reason)
            )
            self._append_decision_event(
                proposal,
                "snooze",
                actor,
                {"reason": reason, "snooze_until": snooze_until},
            )
            self._record_snooze(proposal, snooze_until)
            return with_feedback(
                {
                    "status": "snoozed",
                    "proposal_id": proposal_id,
                    "snooze_until": snooze_until,
                }
            )
        if action == "edit":
            if content is None:
                raise ValueError("edit action requires content")
            from core.trust.vault_mutation_service import (
                bind_trusted_markdown_candidate_action,
                record_trusted_markdown_no_effect_terminal,
            )

            origin_material_action = bind_trusted_markdown_candidate_action(
                proposal.candidate,
                state_db_path=self.db_path.parent / "producer_consumer_ledger.db",
            )
            payload = dict(proposal.candidate.payload)
            payload["content"] = content
            payload.pop("material_action", None)
            updated = self._queue.revise_payload(proposal_id, payload)
            self._queue.record_decision(
                UserDecision(proposal_id=proposal_id, decision="edit", actor=actor, reason=reason)
            )
            self._append_decision_event(
                updated,
                "edit",
                actor,
                {
                    "reason": reason,
                    "revision": updated.revision,
                    "gate_decision": updated.gate_decision,
                },
            )
            if origin_material_action is not None:
                record_trusted_markdown_no_effect_terminal(
                    origin_material_action,
                    target_path=Path(proposal.candidate.target_path or ""),
                    status="revoked",
                    reason_code="trusted_proposal_content_superseded",
                    evidence_ref=f"target-journal:trusted-edit:{proposal_id}:{updated.revision}",
                )
            self._mark_acted(proposal_id)
            return with_feedback(
                {"status": updated.status, "proposal": updated.to_dict()}
            )
        raise ValueError(f"unsupported dialog decision action: {action}")

    def _approval_material_action(
        self,
        proposal: Proposal,
        *,
        actor: str,
        allow_high_risk: bool,
        created_at: str,
    ) -> MaterialActionAuthorization:
        """Seal the exact user-triggered approval before the writer runs."""

        if proposal.status not in {"validated", "needs_manual_review"}:
            raise ValueError(
                f"proposal status cannot be approved: {proposal.status}"
            )
        if proposal.risk_level == "high" and not allow_high_risk:
            raise ValueError("high risk proposal requires allow_high_risk")
        target_uri = str(proposal.candidate.target_path or "")
        content = str(proposal.candidate.payload.get("content", ""))
        expected_existing_hash = proposal.candidate.payload.get(
            "expected_existing_hash"
        )
        binding = knowledge_vault_material_action_binding(
            proposal_id=proposal.proposal_id,
            target_uri=target_uri,
            content=content,
            expected_existing_hash=expected_existing_hash,
        )
        state_db_path = (
            self.db_path.parent / "producer_consumer_ledger.db"
        ).resolve(strict=False)
        expected_request = MaterialActionRequest(
            owner=KNOWLEDGE_VAULT_OWNER,
            executor_id=KNOWLEDGE_VAULT_EXECUTOR,
            action_type=KNOWLEDGE_VAULT_ACTION_TYPE,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=str(state_db_path),
        )
        source_facts = {
            "schema_version": "mnemos.trusted_proposal_approval_facts.v1",
            "proposal_id": proposal.proposal_id,
            "proposal_revision": proposal.revision,
            "proposal_status": proposal.status,
            "gate_decision": proposal.gate_decision,
            "gate_reasons": list(proposal.gate_reasons),
            "risk_level": proposal.risk_level,
            "allow_high_risk": bool(allow_high_risk),
            "actor": str(actor or ""),
            "candidate_id": proposal.candidate.candidate_id,
            "candidate_payload_hash": proposal.candidate.payload_hash,
            "candidate_source": proposal.candidate.source,
            "source_agent": str(proposal.candidate.source_agent or ""),
            "source_session_id": str(
                proposal.candidate.source_session_id or ""
            ),
            "target_kind": proposal.candidate.target_kind,
            "target_uri": str(
                Path(target_uri).expanduser().resolve(strict=False)
            ),
            "content_hash": sha256_text(content),
            "expected_existing_hash": str(expected_existing_hash or ""),
            "evidence_refs": list(proposal.candidate.evidence_refs),
            "proposed_actions": list(proposal.candidate.proposed_actions),
        }
        source_facts_hash, evaluator = build_exact_project_contract_evaluator(
            expected_request=expected_request,
            source_facts=source_facts,
            decision_checks={
                "proposal_status_is_approvable": proposal.status
                in {"validated", "needs_manual_review"},
                "risk_confirmation_is_sufficient": (
                    proposal.risk_level != "high" or bool(allow_high_risk)
                ),
                "candidate_identity_is_bound": bool(
                    proposal.candidate.candidate_id
                    and proposal.candidate.payload_hash
                ),
                "target_is_bound": bool(target_uri),
                "actor_is_bound": bool(str(actor).strip()),
            },
            approved_candidate_key="commit_current_user_approved_proposal",
            approved_candidate_summary=(
                "Commit the exact current proposal revision approved by the user."
            ),
            rejected_candidate_key="reject_stale_or_unapproved_proposal",
            rejected_candidate_summary=(
                "Reject a proposal whose revision, target, content, gate, or risk "
                "approval is not the current user-approved request."
            ),
            approved_reason_code="trusted_proposal_approval_binding_verified",
            rejected_reason_code="trusted_proposal_approval_binding_rejected",
            committed_metric="trusted_proposal_commit_receipt",
            rejected_metric="unbound_trusted_proposal_commit_count",
        )
        source_digest = source_facts_hash.split(":", 1)[1]
        resolver = ProjectContractMaterialActionResolver(
            ProjectContractDecisionContext(
                state_db_path=state_db_path,
                contract_id=DIALOG_APPROVAL_DECISION_CONTRACT_ID,
                contract_revision_id=(
                    DIALOG_APPROVAL_DECISION_CONTRACT_REVISION
                ),
                contract_text=DIALOG_APPROVAL_DECISION_CONTRACT_TEXT,
                contract_evidence_ref=(
                    f"{DIALOG_APPROVAL_DECISION_CONTRACT_ID}"
                    f"#{DIALOG_APPROVAL_DECISION_CONTRACT_REVISION}"
                ),
                source_id=f"trusted-proposal-approval:{source_digest[:40]}",
                source_revision_id=(
                    f"trusted-proposal-approval:{source_digest}"
                ),
                source_content_hash=source_facts_hash,
                source_uri=(
                    f"trusted-proposal-approval://{proposal.proposal_id}"
                    f"/{proposal.revision}/{source_digest[:24]}"
                ),
                evidence_refs=(
                    f"trusted-proposal:{proposal.proposal_id}",
                    f"candidate-payload:{proposal.candidate.payload_hash}",
                    f"user-approval-actor:{str(actor or 'unknown')}",
                ),
                task=f"Approve trusted proposal {proposal.proposal_id}",
                goal=(
                    "Commit only the exact proposal revision selected by the "
                    "user-facing decision surface."
                ),
                constraints=(
                    "The proposal revision and candidate payload must remain exact.",
                    "High-risk content requires explicit risk confirmation.",
                    "The target, content, executor, and cognitive store cannot drift.",
                ),
                created_at=created_at,
                scope_prefix="trusted-proposal-approval",
                producer="dialog-decision-push",
                producer_version=DIALOG_APPROVAL_DECISION_CONTRACT_REVISION,
                producer_code_hash=DIALOG_APPROVAL_DECISION_PRODUCER_HASH,
                evaluator_id="trusted-proposal-user-approval-evaluator",
                evaluator=evaluator,
            )
        )
        return resolver(expected_request)

    def _next_cards(
        self,
        *,
        limit: int,
        surface: str,
        now: datetime | None,
        respect_quiet_hours: bool,
    ) -> list[DecisionCard]:
        current = now or datetime.now(timezone.utc)
        cards: list[DecisionCard] = []
        proposals = self._queue.list(statuses=["validated", "needs_manual_review"], limit=100)
        seen_keys: set[str] = set()
        for proposal in proposals:
            if len(cards) >= limit:
                break
            if self._is_snoozed(proposal.proposal_id, current):
                continue
            existing = self._existing_active_card(proposal.proposal_id)
            if existing is not None:
                cards.append(existing)
                continue
            if respect_quiet_hours and self._quiet_now(current) and proposal.risk_level != "high":
                continue
            if self._daily_delivery_count(current) >= self._max_daily and proposal.risk_level != "high":
                break
            dedupe_key = _dedupe_key(proposal)
            if dedupe_key in seen_keys and proposal.risk_level != "high":
                self._record_merged(proposal, surface)
                continue
            seen_keys.add(dedupe_key)
            merged = [
                item.proposal_id
                for item in proposals
                if item.proposal_id != proposal.proposal_id
                and item.risk_level != "high"
                and _dedupe_key(item) == dedupe_key
            ]
            card = self._create_card(proposal, surface, merged)
            self._record_delivered(proposal, card, dedupe_key)
            cards.append(card)
        return cards

    def _create_card(
        self,
        proposal: Proposal,
        surface: str,
        merged_proposal_ids: list[str],
    ) -> DecisionCard:
        title = _title_for(proposal)
        return DecisionCard(
            card_id=new_id("card"),
            proposal_id=proposal.proposal_id,
            title=title,
            summary=_summary_for(proposal),
            source=proposal.candidate.source,
            target_uri=proposal.candidate.target_path or "",
            risk_level=proposal.risk_level,
            status=proposal.status,
            surface=surface,
            actions=_actions_for(proposal.proposal_id),
            merged_proposal_ids=merged_proposal_ids,
        )

    def _record_delivered(self, proposal: Proposal, card: DecisionCard, dedupe_key: str) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO dialog_push_events (
                    event_id, proposal_id, dedupe_key, status, surface, card_json,
                    snooze_until, error_message, created_at, updated_at
                ) VALUES (?, ?, ?, 'delivered', ?, ?, '', '', ?, ?)
                """,
                (
                    card.card_id,
                    proposal.proposal_id,
                    dedupe_key,
                    card.surface,
                    json.dumps(card.to_dict(), ensure_ascii=False),
                    now,
                    now,
                ),
            )

    def _record_merged(self, proposal: Proposal, surface: str) -> None:
        now = utc_now_iso()
        card = self._create_card(proposal, surface, [])
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO dialog_push_events (
                    event_id, proposal_id, dedupe_key, status, surface, card_json,
                    snooze_until, error_message, created_at, updated_at
                ) VALUES (?, ?, ?, 'merged', ?, ?, '', '', ?, ?)
                """,
                (
                    card.card_id,
                    proposal.proposal_id,
                    _dedupe_key(proposal),
                    surface,
                    json.dumps(card.to_dict(), ensure_ascii=False),
                    now,
                    now,
                ),
            )

    def _record_snooze(self, proposal: Proposal, snooze_until: str) -> None:
        now = utc_now_iso()
        card = self._create_card(proposal, "whitebox", [])
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO dialog_push_events (
                    event_id, proposal_id, dedupe_key, status, surface, card_json,
                    snooze_until, error_message, created_at, updated_at
                ) VALUES (?, ?, ?, 'snoozed', 'whitebox', ?, ?, '', ?, ?)
                """,
                (
                    card.card_id,
                    proposal.proposal_id,
                    _dedupe_key(proposal),
                    json.dumps(card.to_dict(), ensure_ascii=False),
                    snooze_until,
                    now,
                    now,
                ),
            )

    def _fallback_cards(self, cards: list[DecisionCard], error_message: str) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            for card in cards:
                payload = {**card.to_dict(), "surface": "whitebox"}
                conn.execute(
                    """
                    UPDATE dialog_push_events
                    SET surface = 'whitebox', card_json = ?, error_message = ?, updated_at = ?
                    WHERE event_id = ?
                    """,
                    (
                        json.dumps(payload, ensure_ascii=False),
                        error_message,
                        now,
                        card.card_id,
                    ),
                )

    def _mark_acted(self, proposal_id: str) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE dialog_push_events
                SET status = 'acted', updated_at = ?
                WHERE proposal_id = ? AND status IN ('delivered', 'snoozed')
                """,
                (now, proposal_id),
            )

    def _existing_active_card(self, proposal_id: str) -> DecisionCard | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT card_json
                FROM dialog_push_events
                WHERE proposal_id = ? AND status = 'delivered'
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (proposal_id,),
            ).fetchone()
        if row is None:
            return None
        return DecisionCard(**json.loads(row["card_json"]))

    def _is_snoozed(self, proposal_id: str, current: datetime) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT snooze_until
                FROM dialog_push_events
                WHERE proposal_id = ? AND status = 'snoozed'
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (proposal_id,),
            ).fetchone()
        if row is None or not row["snooze_until"]:
            return False
        try:
            snooze_until = datetime.fromisoformat(row["snooze_until"])
        except ValueError:
            return False
        return snooze_until > current

    def _daily_delivery_count(self, current: datetime) -> int:
        day = current.date().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM dialog_push_events
                WHERE status = 'delivered' AND created_at >= ? AND created_at < ?
                """,
                (f"{day}T00:00:00", f"{day}T23:59:59"),
            ).fetchone()
        return int(row["count"] if row else 0)

    def _quiet_now(self, current: datetime) -> bool:
        hour = current.hour
        if self._quiet_start_hour < self._quiet_end_hour:
            return self._quiet_start_hour <= hour < self._quiet_end_hour
        return hour >= self._quiet_start_hour or hour < self._quiet_end_hour

    def _append_decision_event(
        self,
        proposal: Proposal,
        event_type: str,
        actor: str,
        metadata: dict[str, Any],
    ) -> None:
        content = str(proposal.candidate.payload.get("content", ""))
        self._journal.append_event(
            JournalEventInput(
                proposal_id=proposal.proposal_id,
                event_type=event_type,
                target_uri=proposal.candidate.target_path or "",
                content_hash=sha256_text(content),
                actor=actor,
                metadata=metadata,
            )
        )


def _dedupe_key(proposal: Proposal) -> str:
    return "|".join(
        [
            proposal.candidate.source,
            proposal.candidate.target_path or "",
            proposal.risk_level,
        ]
    )


def _actions_for(proposal_id: str) -> list[dict[str, str]]:
    return [
        {
            "id": "approve",
            "label": "Approve",
            "command": f"mnemos proposal decide {proposal_id} approve --yes --json",
        },
        {
            "id": "reject",
            "label": "Reject",
            "command": f"mnemos proposal decide {proposal_id} reject --reason <reason> --json",
        },
        {
            "id": "snooze",
            "label": "Snooze",
            "command": f"mnemos proposal decide {proposal_id} snooze --snooze-hours 24 --json",
        },
        {
            "id": "edit",
            "label": "Edit",
            "command": f"mnemos proposal decide {proposal_id} edit --content-file <path> --json",
        },
    ]


def _title_for(proposal: Proposal) -> str:
    payload = proposal.candidate.payload
    title = str(payload.get("title") or payload.get("page_id") or "").strip()
    if title:
        return title[:120]
    content = str(payload.get("content", ""))
    for line in content.splitlines():
        if line.startswith("#"):
            return line.lstrip("# ").strip()[:120]
    target = proposal.candidate.target_path or proposal.proposal_id
    return Path(target).stem[:120]


def _summary_for(proposal: Proposal) -> str:
    content = str(proposal.candidate.payload.get("content", ""))
    normalized = " ".join(content.replace("#", " ").split())
    if len(normalized) > 180:
        return normalized[:177] + "..."
    return normalized
