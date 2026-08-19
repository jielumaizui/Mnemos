# -*- coding: utf-8 -*-
"""Shared models for distillation action routing."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.hephaestus.distillation_models import KnowledgeFragment


def is_ordered_identity_subsequence(
    candidates: Sequence[KnowledgeFragment],
    admitted: Sequence[KnowledgeFragment],
) -> bool:
    """Accept only an ordered, duplicate-free subset of admitted instances."""

    admitted_index = 0
    seen_identities: set[int] = set()
    for candidate in candidates:
        identity = id(candidate)
        if identity in seen_identities:
            return False
        seen_identities.add(identity)
        while (
            admitted_index < len(admitted)
            and candidate is not admitted[admitted_index]
        ):
            admitted_index += 1
        if admitted_index == len(admitted):
            return False
        admitted_index += 1
    return True


@dataclass(frozen=True)
class MergeDecisionCard:
    """Evidence recorded before an automatic merge/update touches a target page."""

    claim_id: str
    action: str
    target_page: str
    confidence: float
    relation_type: str
    match_signals: list[str] = field(default_factory=list)
    conflicting_signals: list[str] = field(default_factory=list)
    rollback_path: str = ""
    safe_to_apply: bool = False
    decision_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "action": self.action,
            "target_page": self.target_page,
            "confidence": self.confidence,
            "relation_type": self.relation_type,
            "match_signals": list(self.match_signals),
            "conflicting_signals": list(self.conflicting_signals),
            "rollback_path": self.rollback_path,
            "safe_to_apply": self.safe_to_apply,
            "decision_reason": self.decision_reason,
        }


@dataclass
class DistillActionRouteResult:
    """Result returned to DistillationEngine after routing all actions."""

    written: list[str] = field(default_factory=list)
    file_fragments: list[tuple[Path, KnowledgeFragment]] = field(default_factory=list)
    page_raw_event_refs: list[
        tuple[Path, Sequence[Mapping[str, Any]]]
    ] = field(default_factory=list)
    action_ids: list[str] = field(default_factory=list)
    action_receipts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_layer_detail(self) -> dict[str, Any]:
        return {
            "action_ids": self.action_ids,
            "action_receipts": self.action_receipts,
            "errors": self.errors,
        }


def action_receipt_from_row(row: Mapping[str, Any]) -> dict[str, str]:
    try:
        detail = json.loads(str(row.get("result_detail") or "{}"))
    except json.JSONDecodeError:
        detail = {}
    trusted_push = detail.get("trusted_push") if isinstance(detail, Mapping) else None
    proposal_id = ""
    if row.get("result_status") == "proposed" and isinstance(trusted_push, Mapping):
        proposal_id = str(trusted_push.get("proposal_id") or "")
    return {
        "action_id": str(row.get("action_id") or ""),
        "action": str(row.get("action") or ""),
        "status": str(row.get("result_status") or ""),
        "target_page": str(row.get("target_page") or ""),
        "target_kind": str(row.get("target_kind") or ""),
        "proposal_id": proposal_id,
        "error": str(row.get("error") or ""),
    }
