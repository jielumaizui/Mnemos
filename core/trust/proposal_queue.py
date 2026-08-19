"""SQLite-backed proposal queue for trusted push."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, List

from core.trust.config import TrustedPushConfig, load_trusted_push_config
from core.trust.models import CandidateBundle, Proposal, UserDecision, new_id, utc_now_iso
from core.trust.push_decision_gate import GateDecision, GateResult, PushDecisionGate


class ProposalQueue:
    """Persist proposals, revisions, and user decisions."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        wiki_base: Path | None = None,
        config: TrustedPushConfig | None = None,
    ):
        self._config = config or load_trusted_push_config(wiki_base=wiki_base)
        self.db_path = Path(db_path or self._config.db_path)
        self._wiki_base = wiki_base
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
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    target_uri TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL,
                    gate_decision TEXT NOT NULL,
                    gate_reasons_json TEXT NOT NULL,
                    candidate_json TEXT NOT NULL,
                    error_message TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proposal_revisions (
                    revision_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    candidate_json TEXT NOT NULL,
                    gate_decision TEXT NOT NULL,
                    gate_reasons_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_decisions (
                    decision_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence_ledger (
                    evidence_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    evidence_ref TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    expires_at TEXT,
                    created_at TEXT NOT NULL
                );
                """)

    def submit_candidate(
        self,
        candidate: CandidateBundle,
        *,
        shadow: bool = False,
        gate: PushDecisionGate | None = None,
    ) -> Proposal:
        gate_result = (
            gate or PushDecisionGate(wiki_base=self._wiki_base, config=self._config)
        ).evaluate(candidate)
        status = _status_from_gate(gate_result)
        if shadow and status == "validated":
            status = "shadow_validated"
        proposal_id = new_id("prop")
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = self._find_equivalent_active(conn, candidate)
            if existing is not None:
                conn.commit()
                return existing
            conn.execute(
                """
                INSERT INTO proposals (
                    proposal_id, candidate_id, source_ref, target_uri, operation,
                    content_hash, risk_level, confidence, status, gate_decision,
                    gate_reasons_json, candidate_json, error_message, revision,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, ?, ?)
                """,
                (
                    proposal_id,
                    candidate.candidate_id,
                    candidate.source,
                    candidate.target_path or "",
                    ",".join(candidate.proposed_actions),
                    candidate.payload_hash,
                    gate_result.risk_level,
                    float(candidate.confidence_score),
                    status,
                    gate_result.decision,
                    json.dumps(gate_result.reasons, ensure_ascii=False),
                    json.dumps(candidate.to_dict(), ensure_ascii=False),
                    now,
                    now,
                ),
            )
            for ref in candidate.evidence_refs:
                conn.execute(
                    """
                    INSERT INTO evidence_ledger (
                        evidence_id, proposal_id, source_type, evidence_ref,
                        evidence_hash, summary, expires_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        new_id("ev"),
                        proposal_id,
                        candidate.source,
                        ref,
                        candidate.payload_hash,
                        f"Evidence reference for {candidate.source}",
                        now,
                    ),
                )
        return self.get(proposal_id)

    def _find_equivalent_active(
        self,
        conn: sqlite3.Connection,
        candidate: CandidateBundle,
    ) -> Proposal | None:
        """Reuse the durable receipt for an exact retry, but never a rejection/failure."""
        rows = conn.execute(
            """
            SELECT * FROM proposals
            WHERE source_ref=? AND target_uri=? AND content_hash=? AND operation=?
              AND status IN (
                  'validated', 'pending_review', 'needs_manual_review',
                  'shadow_validated', 'committed'
              )
            ORDER BY created_at DESC
            """,
            (
                candidate.source,
                candidate.target_path or "",
                candidate.payload_hash,
                ",".join(candidate.proposed_actions),
            ),
        ).fetchall()
        for row in rows:
            proposal = _proposal_from_row(row)
            prior = proposal.candidate
            if (
                prior.source_agent == candidate.source_agent
                and prior.source_session_id == candidate.source_session_id
                and prior.target_kind == candidate.target_kind
                and (proposal.status != "committed" or self._committed_target_exists(prior))
            ):
                return proposal
        return None

    @staticmethod
    def _committed_target_exists(candidate: CandidateBundle) -> bool:
        if candidate.target_kind != "markdown":
            return True
        return bool(candidate.target_path and Path(candidate.target_path).exists())

    def get(self, proposal_id: str) -> Proposal:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"proposal not found: {proposal_id}")
        return _proposal_from_row(row)

    def find_by_material_command_id(self, command_id: str) -> List[Proposal]:
        """Return every proposal bound to one exact material command, without a cap."""

        normalized = str(command_id or "").strip()
        if not normalized:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM proposals
                WHERE json_extract(
                    candidate_json,
                    '$.payload.material_action.command_id'
                )=?
                ORDER BY created_at, proposal_id
                """,
                (normalized,),
            ).fetchall()
        return [_proposal_from_row(row) for row in rows]

    def list(self, statuses: Iterable[str] | None = None, limit: int = 50) -> List[Proposal]:
        values = list(statuses or [])
        with self._connect() as conn:
            if values:
                all_rows = conn.execute(
                    "SELECT * FROM proposals ORDER BY created_at DESC"
                ).fetchall()
                allowed = set(values)
                rows = [row for row in all_rows if row["status"] in allowed][: int(limit)]
            else:
                rows = conn.execute(
                    "SELECT * FROM proposals ORDER BY created_at DESC LIMIT ?", (int(limit),)
                ).fetchall()
        return [_proposal_from_row(row) for row in rows]

    def update_status(self, proposal_id: str, status: str, error_message: str = "") -> Proposal:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE proposals
                SET status = ?, error_message = ?, updated_at = ?
                WHERE proposal_id = ?
                """,
                (status, error_message, now, proposal_id),
            )
        return self.get(proposal_id)

    def record_decision(self, decision: UserDecision) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_decisions (
                    decision_id, proposal_id, decision, actor, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("decision"),
                    decision.proposal_id,
                    decision.decision,
                    decision.actor,
                    decision.reason,
                    decision.created_at,
                ),
            )

    def revise_payload(
        self,
        proposal_id: str,
        payload: dict[str, Any],
        *,
        gate: PushDecisionGate | None = None,
    ) -> Proposal:
        current = self.get(proposal_id)
        revised = CandidateBundle.from_payload(
            source=current.candidate.source,
            target_kind=current.candidate.target_kind,
            target_path=current.candidate.target_path,
            payload=payload,
            evidence_refs=current.candidate.evidence_refs,
            source_agent=current.candidate.source_agent,
            source_session_id=current.candidate.source_session_id,
            confidence_score=current.candidate.confidence_score,
            risk_level=current.candidate.risk_level,
            proposed_actions=current.candidate.proposed_actions,
        )
        gate_result = (
            gate or PushDecisionGate(wiki_base=self._wiki_base, config=self._config)
        ).evaluate(revised)
        status = _status_from_gate(gate_result)
        revision = current.revision + 1
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO proposal_revisions (
                    revision_id, proposal_id, revision, candidate_json,
                    gate_decision, gate_reasons_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("rev"),
                    proposal_id,
                    revision,
                    json.dumps(revised.to_dict(), ensure_ascii=False),
                    gate_result.decision,
                    json.dumps(gate_result.reasons, ensure_ascii=False),
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE proposals
                SET candidate_id = ?, target_uri = ?, content_hash = ?, risk_level = ?,
                    confidence = ?, status = ?, gate_decision = ?,
                    gate_reasons_json = ?, candidate_json = ?, revision = ?, updated_at = ?
                WHERE proposal_id = ?
                """,
                (
                    revised.candidate_id,
                    revised.target_path or "",
                    revised.payload_hash,
                    gate_result.risk_level,
                    float(revised.confidence_score),
                    status,
                    gate_result.decision,
                    json.dumps(gate_result.reasons, ensure_ascii=False),
                    json.dumps(revised.to_dict(), ensure_ascii=False),
                    revision,
                    now,
                    proposal_id,
                ),
            )
        return self.get(proposal_id)


def _status_from_gate(result: GateResult) -> str:
    if result.decision == GateDecision.REJECT:
        return "rejected"
    if result.decision == GateDecision.NEEDS_MANUAL_REVIEW:
        return "needs_manual_review"
    return "validated"


def _proposal_from_row(row: sqlite3.Row) -> Proposal:
    candidate = CandidateBundle.from_dict(json.loads(row["candidate_json"]))
    return Proposal(
        proposal_id=row["proposal_id"],
        candidate=candidate,
        status=row["status"],
        gate_decision=row["gate_decision"],
        gate_reasons=json.loads(row["gate_reasons_json"]),
        risk_level=row["risk_level"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        revision=int(row["revision"]),
        error_message=row["error_message"],
    )


def build_trust_feedback_proposal_owner(database_dir: Path):
    """Return the trust-owned pending-review journal for feedback commands."""

    from core.cognitive.feedback_target_registry import (
        build_registered_feedback_proposal_owner,
    )

    return build_registered_feedback_proposal_owner(database_dir, "trust_proposal")
