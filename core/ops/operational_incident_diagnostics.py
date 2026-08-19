"""Registered domain reproducers for operational incident diagnosis."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


REGISTERED_DIAGNOSTIC_ROOT_CAUSE_CODES = {
    "distillation_fragment_contract.v1": "schema_contract_mismatch",
}


def _sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def diagnostic_issue_codes(issues: list[str]) -> list[str]:
    observed = "\n".join(str(issue).lower() for issue in issues)
    rules = {
        "title_contract": ("title", "标题"),
        "core_content_contract": ("core_content", "核心内容"),
        "summary_contract": ("summary", "摘要"),
        "domain_contract": ("domain", "领域"),
        "structure_contract": ("structure", "结构化"),
    }
    return sorted(
        code for code, needles in rules.items() if any(needle in observed for needle in needles)
    )


def _distillation_fragment_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Run the production fragment constructor and hard validator."""

    from core.hephaestus.distillation_models import KnowledgeFragment
    from core.hephaestus.distillation_quality import _validate_fragment

    try:
        fragment = KnowledgeFragment(**dict(payload))
    except (TypeError, ValueError, KeyError) as exc:
        return {
            "status": "failed",
            "result_type": type(exc).__name__,
            "result_hash": _sha256({"constructor_error": type(exc).__name__}),
            "issue_codes": ["fragment_constructor_contract"],
        }
    issues = list(_validate_fragment(fragment))
    return {
        "status": "failed" if issues else "passed",
        "result_type": "fragment_contract_issues" if issues else "valid_fragment",
        "result_hash": _sha256({"issues": issues}),
        "issue_codes": diagnostic_issue_codes(issues),
    }


def execute_registered_diagnostic_reproducer(
    reproducer_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Execute one exact allowlisted production-domain reproducer."""

    if reproducer_id != "distillation_fragment_contract.v1":
        raise ValueError(f"unknown diagnostic reproducer: {reproducer_id}")
    if not isinstance(payload, Mapping):
        raise TypeError("diagnostic reproducer input must be a mapping")
    return _distillation_fragment_contract(payload)


def root_cause_code_for_reproducer(reproducer_id: str) -> str:
    """Return the only root-cause code a registered reproducer can confirm."""

    try:
        return REGISTERED_DIAGNOSTIC_ROOT_CAUSE_CODES[reproducer_id]
    except KeyError as exc:
        raise ValueError(f"unknown diagnostic reproducer: {reproducer_id}") from exc


__all__ = [
    "diagnostic_issue_codes",
    "execute_registered_diagnostic_reproducer",
    "root_cause_code_for_reproducer",
]
