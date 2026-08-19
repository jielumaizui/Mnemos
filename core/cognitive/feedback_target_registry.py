"""Shallow registry for the seven domain-owned feedback proposal journals."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.cognitive.feedback_contract import (
    FEEDBACK_TARGET_JOURNAL_CONTRACTS,
    FEEDBACK_TARGETS,
)
from core.cognitive.feedback_domain_proposal import DomainFeedbackProposalStore
from core.cognitive.feedback_models import FeedbackProposalGateFactory


@dataclass(frozen=True)
class FeedbackTargetJournalSpec:
    """Whitelisted database, owner, tables, and gate for one target."""

    db_file: str
    owner_id: str
    proposal_table: str
    action_table: str
    receipt_table: str
    gate_contract_id: str


FEEDBACK_TARGET_JOURNALS = {
    target_id: FeedbackTargetJournalSpec(**contract)
    for target_id, contract in FEEDBACK_TARGET_JOURNAL_CONTRACTS.items()
}

if tuple(sorted(FEEDBACK_TARGET_JOURNALS)) != FEEDBACK_TARGETS:
    raise RuntimeError("feedback target journal registry drift")


TARGET_DB_FILE_BY_ID = {
    target_id: spec.db_file
    for target_id, spec in FEEDBACK_TARGET_JOURNALS.items()
}
TARGET_DOMAIN_TABLES = {
    target_id: (spec.proposal_table, spec.action_table, spec.receipt_table)
    for target_id, spec in FEEDBACK_TARGET_JOURNALS.items()
}


def build_registered_feedback_proposal_owner(
    database_dir: Path,
    target_id: str,
    *,
    proposal_gate_factory: FeedbackProposalGateFactory | None = None,
) -> DomainFeedbackProposalStore:
    """Build the exact journal selected by a domain-owned public factory."""

    normalized = str(target_id or "").strip()
    try:
        spec = FEEDBACK_TARGET_JOURNALS[normalized]
    except KeyError as exc:
        raise ValueError("unknown feedback target journal") from exc
    return DomainFeedbackProposalStore(
        database_dir=Path(database_dir),
        db_file=spec.db_file,
        target_id=normalized,
        owner_id=spec.owner_id,
        proposal_table=spec.proposal_table,
        action_table=spec.action_table,
        receipt_table=spec.receipt_table,
        gate_contract_id=spec.gate_contract_id,
        proposal_gate_factory=proposal_gate_factory,
    )
