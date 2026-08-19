"""Typed counterfactual effect receipts for Persona profile consumers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping
from uuid import uuid4

PROFILE_TARGET_EFFECT_SCHEMA_VERSION = "mnemos.persona_profile_target_effect.v1"
_TARGET_WRITE_STATUSES = frozenset({"committed", "failed"})
_TERMINAL_STATUSES = frozenset({"committed", "no_effect", "failed"})


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProfileTargetEffectReceipt:
    """Immutable target-owner proof derived from actual before/after outputs."""

    receipt_id: str
    owner: str
    target_type: str
    target_id: str
    request_id: str
    decision_id: str
    matched_assertion_revisions: Mapping[str, str]
    baseline_hash: str
    persona_enabled_hash: str
    expected_delta: Mapping[str, Any]
    actual_target_delta: Mapping[str, Any]
    terminal_status: str
    action_changed: bool
    created_at: str
    receipt_hash: str
    schema_version: str = PROFILE_TARGET_EFFECT_SCHEMA_VERSION

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "owner": self.owner,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "request_id": self.request_id,
            "decision_id": self.decision_id,
            "matched_assertion_revisions": dict(sorted(self.matched_assertion_revisions.items())),
            "baseline_hash": self.baseline_hash,
            "persona_enabled_hash": self.persona_enabled_hash,
            "expected_delta": dict(self.expected_delta),
            "actual_target_delta": dict(self.actual_target_delta),
            "terminal_status": self.terminal_status,
            "action_changed": self.action_changed,
            "created_at": self.created_at,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.unsigned_payload(), "receipt_hash": self.receipt_hash}


def compare_profile_effect(
    *,
    owner: str,
    target_type: str,
    target_id: str,
    matched_assertion_revisions: Mapping[str, str],
    baseline_output: Any,
    persona_enabled_output: Any,
    expected_delta: Mapping[str, Any],
    target_status: str = "committed",
    receipt_id: str = "",
    request_id: str = "",
    decision_id: str = "",
    created_at: str = "",
) -> ProfileTargetEffectReceipt:
    """Compare real outputs and issue one target-owner reciprocal receipt.

    ``action_changed`` and the actual delta are intentionally absent from the
    input API. They are derived here from canonical before/after hashes and the
    target write status.
    """

    normalized_owner = str(owner or "").strip()
    normalized_type = str(target_type or "").strip()
    normalized_target = str(target_id or "").strip()
    if not normalized_owner or not normalized_type or not normalized_target:
        raise ValueError("profile target receipt requires owner and exact target")
    normalized_revisions = {
        str(assertion_id).strip(): str(revision_id).strip()
        for assertion_id, revision_id in matched_assertion_revisions.items()
    }
    if not normalized_revisions or any(
        not assertion_id or not revision_id
        for assertion_id, revision_id in normalized_revisions.items()
    ):
        raise ValueError("profile target receipt requires exact matched revisions")
    if not expected_delta:
        raise ValueError("profile target receipt requires an expected delta")
    normalized_write_status = str(target_status or "").strip()
    if normalized_write_status not in _TARGET_WRITE_STATUSES:
        raise ValueError("unsupported profile target write status")

    baseline_hash = _sha256_json(baseline_output)
    attempted_enabled_hash = _sha256_json(persona_enabled_output)
    enabled_hash = baseline_hash if normalized_write_status == "failed" else attempted_enabled_hash
    hashes_changed = baseline_hash != enabled_hash
    action_changed = normalized_write_status == "committed" and hashes_changed
    if normalized_write_status == "failed":
        terminal_status = "failed"
    elif hashes_changed:
        terminal_status = "committed"
    else:
        terminal_status = "no_effect"

    resolved_receipt_id = str(receipt_id or f"profile-target:{uuid4().hex}")
    resolved_request_id = str(request_id or resolved_receipt_id)
    resolved_decision_id = str(decision_id or resolved_request_id)
    resolved_created_at = str(created_at or datetime.now(timezone.utc).isoformat())
    actual_delta = {
        "target_type": normalized_type,
        "target_id": normalized_target,
        "before_hash": baseline_hash,
        "after_hash": enabled_hash,
        "changed": action_changed,
    }
    unsigned = {
        "schema_version": PROFILE_TARGET_EFFECT_SCHEMA_VERSION,
        "receipt_id": resolved_receipt_id,
        "owner": normalized_owner,
        "target_type": normalized_type,
        "target_id": normalized_target,
        "request_id": resolved_request_id,
        "decision_id": resolved_decision_id,
        "matched_assertion_revisions": dict(sorted(normalized_revisions.items())),
        "baseline_hash": baseline_hash,
        "persona_enabled_hash": enabled_hash,
        "expected_delta": dict(expected_delta),
        "actual_target_delta": actual_delta,
        "terminal_status": terminal_status,
        "action_changed": action_changed,
        "created_at": resolved_created_at,
    }
    return ProfileTargetEffectReceipt(
        receipt_id=resolved_receipt_id,
        owner=normalized_owner,
        target_type=normalized_type,
        target_id=normalized_target,
        request_id=resolved_request_id,
        decision_id=resolved_decision_id,
        matched_assertion_revisions=normalized_revisions,
        baseline_hash=baseline_hash,
        persona_enabled_hash=enabled_hash,
        expected_delta=dict(expected_delta),
        actual_target_delta=actual_delta,
        terminal_status=terminal_status,
        action_changed=action_changed,
        created_at=resolved_created_at,
        receipt_hash=_sha256_json(unsigned),
    )


def validate_profile_target_effect_receipt(
    receipt: ProfileTargetEffectReceipt,
) -> ProfileTargetEffectReceipt:
    """Fail closed on forged or internally inconsistent target receipts."""

    if not isinstance(receipt, ProfileTargetEffectReceipt):
        raise TypeError("profile usage requires a typed target receipt")
    if receipt.schema_version != PROFILE_TARGET_EFFECT_SCHEMA_VERSION:
        raise ValueError("unsupported profile target receipt schema")
    if any(
        not str(value or "").strip()
        for value in (
            receipt.receipt_id,
            receipt.owner,
            receipt.target_type,
            receipt.target_id,
            receipt.request_id,
            receipt.decision_id,
            receipt.created_at,
        )
    ):
        raise ValueError("profile target receipt identity is incomplete")
    if not receipt.expected_delta:
        raise ValueError("profile target receipt expected delta is missing")
    if receipt.terminal_status not in _TERMINAL_STATUSES:
        raise ValueError("unsupported profile target terminal status")
    hashes_changed = receipt.baseline_hash != receipt.persona_enabled_hash
    expected_changed = receipt.terminal_status == "committed" and hashes_changed
    if receipt.action_changed is not expected_changed:
        raise ValueError("profile action_changed must be comparator-derived")
    if receipt.terminal_status == "no_effect" and hashes_changed:
        raise ValueError("no_effect receipt has different before and after hashes")
    if receipt.terminal_status == "committed" and not hashes_changed:
        raise ValueError("committed effect has equal before and after hashes")
    expected_delta = {
        "target_type": receipt.target_type,
        "target_id": receipt.target_id,
        "before_hash": receipt.baseline_hash,
        "after_hash": receipt.persona_enabled_hash,
        "changed": expected_changed,
    }
    if dict(receipt.actual_target_delta) != expected_delta:
        raise ValueError("profile target delta does not match comparator hashes")
    if not receipt.matched_assertion_revisions or any(
        not str(assertion_id or "").strip() or not str(revision_id or "").strip()
        for assertion_id, revision_id in receipt.matched_assertion_revisions.items()
    ):
        raise ValueError("profile target receipt requires exact matched revisions")
    if receipt.receipt_hash != _sha256_json(receipt.unsigned_payload()):
        raise ValueError("profile target receipt hash mismatch")
    return receipt


def parse_profile_target_effect_receipt(
    value: Mapping[str, Any],
) -> ProfileTargetEffectReceipt:
    """Rehydrate a persisted receipt and run the same fail-closed validator."""

    try:
        receipt = ProfileTargetEffectReceipt(
            receipt_id=str(value["receipt_id"]),
            owner=str(value["owner"]),
            target_type=str(value["target_type"]),
            target_id=str(value["target_id"]),
            request_id=str(value["request_id"]),
            decision_id=str(value["decision_id"]),
            matched_assertion_revisions=dict(value["matched_assertion_revisions"]),
            baseline_hash=str(value["baseline_hash"]),
            persona_enabled_hash=str(value["persona_enabled_hash"]),
            expected_delta=dict(value["expected_delta"]),
            actual_target_delta=dict(value["actual_target_delta"]),
            terminal_status=str(value["terminal_status"]),
            action_changed=value["action_changed"] is True,
            created_at=str(value["created_at"]),
            receipt_hash=str(value["receipt_hash"]),
            schema_version=str(value["schema_version"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid persisted profile target receipt") from exc
    return validate_profile_target_effect_receipt(receipt)


def profile_usage_idempotency_key(
    *,
    consumer: str,
    read_purpose: str,
    receipt: ProfileTargetEffectReceipt,
) -> str:
    return "profile-usage:" + _sha256_json(
        {
            "consumer": str(consumer),
            "read_purpose": str(read_purpose),
            "target_receipt_id": receipt.receipt_id,
            "target_receipt_hash": receipt.receipt_hash,
        }
    )
