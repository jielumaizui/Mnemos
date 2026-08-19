"""Admission rules for model-produced cognition episode drafts.

This module owns the episode-specific shape and coverage checks.  The outer
distillation contract remains responsible for validating each evidence object
against the immutable artifact and source-authority catalogs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
from typing import Any, TypeGuard

from core.cognition_episode_contract import (
    COGNITION_EPISODE_FIELDS,
    COGNITION_EPISODE_STATUSES,
)


@dataclass(frozen=True)
class CognitionEpisodeValidationIssue:
    code: str
    path: str
    message: str


EvidenceValidator = Callable[[Any, str], None]


def validate_cognition_episode_draft(
    episode: Any,
    *,
    claim_ids: set[str],
    source_authority_catalog: Any,
    evidence_validator: EvidenceValidator,
) -> tuple[CognitionEpisodeValidationIssue, ...]:
    """Validate complete typed fields and exact evidence/claim coverage."""

    issues: list[CognitionEpisodeValidationIssue] = []
    path = "cognition_episode"
    if not isinstance(episode, Mapping):
        return (
            CognitionEpisodeValidationIssue(
                "missing_cognition_episode",
                path,
                "non-skip output must include a complete cognition_episode object",
            ),
        )

    covered_claim_ids: set[str] = set()
    fields_with_known_evidence: set[str] = set()
    for field_name in COGNITION_EPISODE_FIELDS:
        seen_entries: set[str] = set()
        field_path = f"{path}.{field_name}"
        entries = episode.get(field_name)
        if not _non_empty_sequence(entries):
            issues.append(
                CognitionEpisodeValidationIssue(
                    "missing_cognition_episode_field",
                    field_path,
                    f"{field_name} must be a non-empty typed list",
                )
            )
            continue
        for index, entry in enumerate(entries):
            entry_path = f"{field_path}[{index}]"
            if not isinstance(entry, Mapping):
                issues.append(
                    CognitionEpisodeValidationIssue(
                        "invalid_cognition_episode_entry",
                        entry_path,
                        "cognition episode entry must be an object",
                    )
                )
                continue
            try:
                entry_identity = json.dumps(
                    dict(entry),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError):
                issues.append(
                    CognitionEpisodeValidationIssue(
                        "invalid_cognition_episode_entry",
                        entry_path,
                        "cognition episode entry must be canonical JSON data",
                    )
                )
                continue
            if entry_identity in seen_entries:
                issues.append(
                    CognitionEpisodeValidationIssue(
                        "cognition_episode_duplicate_entry",
                        entry_path,
                        "cognition episode field contains a duplicate typed entry",
                    )
                )
                continue
            seen_entries.add(entry_identity)
            status = _status(issues, entry, entry_path)
            evidence_refs = entry.get("evidence_refs")
            entry_claim_ids = entry.get("claim_ids")
            if not _sequence(evidence_refs):
                issues.append(
                    CognitionEpisodeValidationIssue(
                        "invalid_cognition_episode_evidence",
                        f"{entry_path}.evidence_refs",
                        "evidence_refs must be a list",
                    )
                )
                evidence_refs = []
            if not _sequence(entry_claim_ids) or not all(
                _non_empty_text(value) for value in entry_claim_ids
            ):
                issues.append(
                    CognitionEpisodeValidationIssue(
                        "invalid_cognition_episode_claim_ids",
                        f"{entry_path}.claim_ids",
                        "claim_ids must be a list of non-empty strings",
                    )
                )
                entry_claim_ids = []

            normalized_claim_ids = {str(value) for value in entry_claim_ids}
            unknown_claim_ids = sorted(normalized_claim_ids - claim_ids)
            if unknown_claim_ids:
                issues.append(
                    CognitionEpisodeValidationIssue(
                        "cognition_episode_unknown_claim",
                        f"{entry_path}.claim_ids",
                        "cognition episode references unknown claim_id(s): "
                        + ", ".join(unknown_claim_ids),
                    )
                )

            if status == "known":
                _require_text(issues, entry, "value", entry_path)
                if not _non_empty_sequence(evidence_refs):
                    issues.append(
                        CognitionEpisodeValidationIssue(
                            "cognition_episode_known_without_evidence",
                            f"{entry_path}.evidence_refs",
                            "known cognition must cite at least one exact Raw evidence span",
                        )
                    )
                else:
                    evidence_validator(evidence_refs, entry_path)
                if not normalized_claim_ids:
                    issues.append(
                        CognitionEpisodeValidationIssue(
                            "cognition_episode_known_without_claim",
                            f"{entry_path}.claim_ids",
                            "known cognition must reference an admitted claim_id",
                        )
                    )
                if _has_exact_span(evidence_refs, source_authority_catalog):
                    fields_with_known_evidence.add(field_name)
                else:
                    issues.append(
                        CognitionEpisodeValidationIssue(
                            "cognition_episode_exact_span_missing",
                            f"{entry_path}.evidence_refs",
                            "known cognition must resolve to an exact Raw revision span",
                        )
                    )
                covered_claim_ids.update(normalized_claim_ids & claim_ids)
            elif status in {"unknown", "not_applicable"}:
                _require_text(issues, entry, "reason", entry_path)
                if evidence_refs or normalized_claim_ids or _non_empty_text(entry.get("value")):
                    issues.append(
                        CognitionEpisodeValidationIssue(
                            "cognition_episode_non_known_has_assertion",
                            entry_path,
                            "unknown/not_applicable entries must not carry value, evidence, or claims",
                        )
                    )

    for required_known in ("situation", "facts", "scope"):
        if required_known not in fields_with_known_evidence:
            issues.append(
                CognitionEpisodeValidationIssue(
                    "cognition_episode_required_known_missing",
                    f"{path}.{required_known}",
                    f"{required_known} must contain at least one exact, evidence-bound known entry",
                )
            )
    missing_claim_ids = sorted(claim_ids - covered_claim_ids)
    if missing_claim_ids:
        issues.append(
            CognitionEpisodeValidationIssue(
                "claim_without_cognition_episode_mapping",
                path,
                "admitted claim(s) are absent from cognition_episode: "
                + ", ".join(missing_claim_ids),
            )
        )
    return tuple(issues)


def _has_exact_span(evidence_refs: Any, source_authority_catalog: Any) -> bool:
    for evidence in evidence_refs:
        if not isinstance(evidence, Mapping):
            continue
        try:
            supplied_start = int(evidence.get("authority_span_start", -1))
            supplied_end = int(evidence.get("authority_span_end", -1))
        except (TypeError, ValueError):
            supplied_start = -1
            supplied_end = -1
        if (
            _non_empty_text(evidence.get("source_authority_id"))
            and evidence.get("authority_span_status") == "exact"
            and _non_empty_text(evidence.get("authority_source_revision_sha256"))
            and supplied_start >= 0
            and supplied_end > supplied_start
        ):
            return True
        if source_authority_catalog is None:
            continue
        authority = source_authority_catalog.get(evidence.get("source_authority_id"))
        if (
            authority is not None
            and authority.span_status == "exact"
            and authority.source_revision_sha256
            and authority.span_start >= 0
            and authority.span_end > authority.span_start
        ):
            return True
    return False


def _status(
    issues: list[CognitionEpisodeValidationIssue],
    entry: Mapping[str, Any],
    prefix: str,
) -> str:
    value = entry.get("status")
    if not _non_empty_text(value) or str(value) not in COGNITION_EPISODE_STATUSES:
        issues.append(
            CognitionEpisodeValidationIssue(
                "invalid_status",
                f"{prefix}.status",
                "status must be one of: " + ", ".join(sorted(COGNITION_EPISODE_STATUSES)),
            )
        )
        return ""
    return str(value)


def _require_text(
    issues: list[CognitionEpisodeValidationIssue],
    entry: Mapping[str, Any],
    key: str,
    prefix: str,
) -> None:
    if not _non_empty_text(entry.get(key)):
        issues.append(
            CognitionEpisodeValidationIssue(
                f"missing_{key}",
                f"{prefix}.{key}",
                f"{key} is required",
            )
        )


def _non_empty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _non_empty_sequence(value: Any) -> TypeGuard[Sequence[Any]]:
    return _sequence(value) and bool(value)


def _sequence(value: Any) -> TypeGuard[Sequence[Any]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))
