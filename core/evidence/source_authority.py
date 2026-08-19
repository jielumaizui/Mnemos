"""System-owned source authority for distillation and cognitive writes.

Visible content is kept lossless in canonical Raw.  This module does not
classify text by prompt-injection keywords and never deletes source bytes.
Instead it binds role-local spans and artifact summaries to opaque references
that a model may select but cannot upgrade.  The resolved authority is then
consumed by the cognitive write gate.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

from core.cognition_episode_contract import iter_cognition_episode_evidence
from core.evidence.artifact_catalog import ArtifactCatalog


SOURCE_AUTHORITY_SCHEMA_VERSION = "mnemos.source_authority_catalog.v1"


class SourceAuthorityCatalogRejectedError(ValueError):
    """Raised before a model call when role/span authority cannot be proven."""

    def __init__(self, rejection_codes: Sequence[str]):
        self.rejection_codes = tuple(str(code) for code in rejection_codes)
        super().__init__(
            "source authority catalog rejected input: " + ", ".join(self.rejection_codes)
        )


class SourceAuthority(str, Enum):
    SYSTEM_POLICY = "system_policy"
    EXPLICIT_USER = "explicit_user"
    PROJECT_CONTRACT = "project_contract"
    ASSISTANT_INFERENCE = "assistant_inference"
    TOOL_OBSERVATION = "tool_observation"
    EXTERNAL_CONTENT = "external_content"
    QUOTED_CONTENT = "quoted_content"


COGNITIVE_UPDATE_AUTHORITIES = frozenset(
    {
        SourceAuthority.SYSTEM_POLICY,
        SourceAuthority.EXPLICIT_USER,
        SourceAuthority.PROJECT_CONTRACT,
    }
)

SYSTEM_SOURCE_AUTHORITY_FIELDS = frozenset(
    {
        "source_authority",
        "authority_purpose",
        "authority_allows_cognitive_update",
        "authority_content_sha256",
        "authority_role",
        "authority_span_start",
        "authority_span_end",
        "authority_span_status",
        "authority_source_revision_sha256",
        "authority_artifact_ref_id",
    }
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _authority_ref_id(payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json(dict(payload)).encode("utf-8")).hexdigest()
    return f"source-authority:{digest[:32]}"


def _default_authority(role: str) -> SourceAuthority:
    normalized = str(role or "").strip().lower()
    if normalized == "user":
        return SourceAuthority.EXPLICIT_USER
    if normalized == "system":
        return SourceAuthority.SYSTEM_POLICY
    if normalized in {"tool", "tool_result", "function"}:
        return SourceAuthority.TOOL_OBSERVATION
    return SourceAuthority.ASSISTANT_INFERENCE


def _authority_from_message(
    message: Mapping[str, Any],
    *,
    default_authority: str = "",
) -> SourceAuthority:
    role = str(message.get("role") or "unknown").strip().lower()
    # Role is the outer trust boundary.  Metadata can lower a user/system
    # message to external/quoted or identify a project contract, but it can
    # never turn assistant/tool text into explicit-user/system authority.
    if role in {"assistant", "model"}:
        return SourceAuthority.ASSISTANT_INFERENCE
    if role in {"tool", "tool_result", "function"}:
        return SourceAuthority.TOOL_OBSERVATION
    asset_kind = str(message.get("asset_kind") or "")
    content_source = str(message.get("content_source") or message.get("source_type") or "")
    # A low-authority source classification is monotonic.  Caller-controlled
    # metadata cannot relabel an external document or quoted payload as a
    # project contract or explicit user statement.
    if asset_kind == "trusted_user_document" or content_source == "external_file":
        return SourceAuthority.EXTERNAL_CONTENT
    if content_source in {"likely_pasted", "quoted_content", "external_quoted"}:
        return SourceAuthority.QUOTED_CONTENT
    supplied = str(message.get("source_authority") or "").strip()
    if not supplied and role in {"user", "system"}:
        supplied = str(default_authority or "").strip()
    if supplied:
        try:
            candidate = SourceAuthority(supplied)
        except ValueError:
            candidate = None
        if candidate is not None:
            if candidate == SourceAuthority.SYSTEM_POLICY and role != "system":
                candidate = None
            if candidate == SourceAuthority.EXPLICIT_USER and role != "user":
                candidate = None
            if candidate == SourceAuthority.PROJECT_CONTRACT and role != "system":
                candidate = None
            if candidate is not None:
                return candidate
    return _default_authority(role)


def _default_purpose(authority: SourceAuthority) -> str:
    if authority in COGNITIVE_UPDATE_AUTHORITIES:
        return "authoritative_instruction_or_user_statement"
    if authority == SourceAuthority.TOOL_OBSERVATION:
        return "searchable_observation"
    if authority == SourceAuthority.ASSISTANT_INFERENCE:
        return "searchable_assistant_inference"
    return "searchable_reference_or_pending_hypothesis"


def _merge_spans(spans: Iterable[tuple[int, int]], content_length: int) -> tuple[tuple[int, int], ...]:
    normalized = sorted(
        (max(0, int(start)), min(content_length, int(end)))
        for start, end in spans
        if int(end) > int(start)
    )
    merged: list[tuple[int, int]] = []
    for start, end in normalized:
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _paired_spans(content: str, opener: str, closer: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(content):
        start = content.find(opener, cursor)
        if start < 0:
            break
        if start and content[start - 1] == "\\":
            cursor = start + len(opener)
            continue
        end = content.find(closer, start + len(opener))
        if end < 0:
            break
        if end and content[end - 1] == "\\":
            cursor = end + len(closer)
            continue
        spans.append((start, end + len(closer)))
        cursor = end + len(closer)
    return spans


def _ascii_single_quote_spans(content: str) -> list[tuple[int, int]]:
    """Find paired straight quotes without treating apostrophes as delimiters."""

    spans: list[tuple[int, int]] = []
    opener: int | None = None
    opening_context = "([{<—–-"
    closing_context = ".,!?;:)]}>—–-"
    for index, char in enumerate(content):
        if char != "'" or (index and content[index - 1] == "\\"):
            continue
        previous = content[index - 1] if index else ""
        following = content[index + 1] if index + 1 < len(content) else ""
        if previous.isalnum() and following.isalnum():
            continue
        can_open = (
            (not previous or previous.isspace() or previous in opening_context)
            and bool(following)
            and not following.isspace()
        )
        can_close = (
            bool(previous)
            and not previous.isspace()
            and (not following or following.isspace() or following in closing_context)
        )
        if opener is None:
            if can_open:
                opener = index
        elif can_close:
            spans.append((opener, index + 1))
            opener = None
    return spans


def _structured_quoted_spans(content: str) -> tuple[tuple[int, int], ...]:
    """Find syntax-owned quoted/code spans without interpreting their words."""

    spans: list[tuple[int, int]] = []
    line_offset = 0
    fence_start: int | None = None
    fence_marker = ""
    for line in content.splitlines(keepends=True):
        line_end = line_offset + len(line)
        stripped = line.lstrip()
        marker = "```" if stripped.startswith("```") else "~~~" if stripped.startswith("~~~") else ""
        if marker:
            if fence_start is None:
                fence_start = line_offset
                fence_marker = marker
            elif marker == fence_marker:
                spans.append((fence_start, line_end))
                fence_start = None
                fence_marker = ""
        elif fence_start is None and stripped.startswith(">"):
            spans.append((line_offset, line_end))
        line_offset = line_end
    if fence_start is not None:
        spans.append((fence_start, len(content)))

    for opener, closer in (
        ("“", "”"),
        ("「", "」"),
        ("『", "』"),
        ("‘", "’"),
        ('"', '"'),
        ("`", "`"),
    ):
        spans.extend(_paired_spans(content, opener, closer))
    spans.extend(_ascii_single_quote_spans(content))
    return _merge_spans(spans, len(content))


def _authority_segments(
    content: str,
    authority: SourceAuthority,
) -> tuple[tuple[int, int, SourceAuthority], ...]:
    if authority not in COGNITIVE_UPDATE_AUTHORITIES:
        return ((0, len(content), authority),)
    quoted_spans = _structured_quoted_spans(content)
    if not quoted_spans:
        return ((0, len(content), authority),)

    segments: list[tuple[int, int, SourceAuthority]] = []
    cursor = 0
    for start, end in quoted_spans:
        if cursor < start and content[cursor:start].strip():
            segments.append((cursor, start, authority))
        if content[start:end].strip():
            segments.append((start, end, SourceAuthority.QUOTED_CONTENT))
        cursor = end
    if cursor < len(content) and content[cursor:].strip():
        segments.append((cursor, len(content), authority))
    return tuple(segments) or ((0, len(content), SourceAuthority.QUOTED_CONTENT),)


@dataclass(frozen=True)
class SourceAuthorityEntry:
    source_authority_id: str
    authority: SourceAuthority
    source_event_id: str
    role: str
    purpose: str
    content_sha256: str
    span_start: int
    span_end: int
    span_status: str
    source_revision_sha256: str = ""
    artifact_ref_id: str = ""
    _verifiable_text: str = field(default="", repr=False, compare=False)

    @property
    def allows_cognitive_update(self) -> bool:
        return self.authority in COGNITIVE_UPDATE_AUTHORITIES

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "source_authority_id": self.source_authority_id,
            "source_authority": self.authority.value,
            "source_event_id": self.source_event_id,
            "role": self.role,
            "purpose": self.purpose,
            "content_sha256": self.content_sha256,
            "span_start": self.span_start,
            "span_end": self.span_end,
            "span_status": self.span_status,
            "source_revision_sha256": self.source_revision_sha256,
            "artifact_ref_id": self.artifact_ref_id,
            "allows_cognitive_update": self.allows_cognitive_update,
        }

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "source_authority_id": self.source_authority_id,
            "source_authority": self.authority.value,
            "source_event_id": self.source_event_id,
            "role": self.role,
            "purpose": self.purpose,
            "artifact_ref_id": self.artifact_ref_id,
            "allows_cognitive_update": self.allows_cognitive_update,
        }

    def resolved_evidence_payload(self) -> dict[str, Any]:
        return {
            "source_authority_id": self.source_authority_id,
            "source_authority": self.authority.value,
            "authority_purpose": self.purpose,
            "authority_allows_cognitive_update": self.allows_cognitive_update,
            "authority_content_sha256": self.content_sha256,
            "authority_role": self.role,
            "authority_span_start": self.span_start,
            "authority_span_end": self.span_end,
            "authority_span_status": self.span_status,
            "authority_source_revision_sha256": self.source_revision_sha256,
            "authority_artifact_ref_id": self.artifact_ref_id,
        }

    def matches_quote(self, quote: Any) -> bool:
        candidate = str(quote or "").strip()
        return bool(candidate) and candidate in self._verifiable_text


@dataclass(frozen=True)
class SourceAuthorityCatalog:
    entries: tuple[SourceAuthorityEntry, ...] = ()
    rejected_count: int = 0
    rejection_codes: tuple[str, ...] = ()
    schema_version: str = SOURCE_AUTHORITY_SCHEMA_VERSION

    @classmethod
    def from_messages(
        cls,
        messages: Iterable[Mapping[str, Any]] | None,
        *,
        allowed_source_event_ids: Sequence[str],
        artifact_catalog: ArtifactCatalog | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> "SourceAuthorityCatalog":
        allowed = tuple(dict.fromkeys(str(value) for value in allowed_source_event_ids if str(value)))
        allowed_set = set(allowed)
        context = dict(context or {})
        default_authority = str(context.get("source_authority") or "")
        default_purpose = str(context.get("source_authority_purpose") or "")
        entries: list[SourceAuthorityEntry] = []
        rejections: list[str] = []

        for ordinal, raw_message in enumerate(messages or (), start=1):
            if not isinstance(raw_message, Mapping):
                rejections.append("source_authority_message_invalid")
                continue
            content = str(raw_message.get("content") or "")
            if not content:
                continue
            role = str(raw_message.get("role") or "unknown").strip().lower() or "unknown"
            source_span = raw_message.get("source_span")
            source_event_ids: tuple[str, ...] = ()
            message_span_start = 0
            message_span_end = len(content)
            span_status = "session_bound"
            revision_hash = ""
            if isinstance(source_span, Mapping):
                try:
                    source_event_id = str(source_span.get("revision_id") or "")
                    message_span_start = int(source_span["span_start"])
                    message_span_end = int(source_span["span_end"])
                except (KeyError, TypeError, ValueError):
                    rejections.append("source_authority_span_invalid")
                    continue
                source_role = str(source_span.get("role") or role).strip().lower()
                revision_hash = str(source_span.get("content_hash") or "")
                if (
                    source_role != role
                    or message_span_start < 0
                    or message_span_end <= message_span_start
                    or message_span_end - message_span_start != len(content)
                    or not revision_hash
                ):
                    rejections.append("source_authority_span_invalid")
                    continue
                span_status = "exact"
                source_event_ids = (source_event_id,)
            elif allowed:
                # Callers without role-local Raw spans are bound to the
                # session event set, never to a guessed single event.  Quote
                # matching still distinguishes user/assistant authority and
                # rejects identical ambiguous quotes.
                source_event_ids = allowed
            else:
                rejections.append("source_authority_span_missing")
                continue
            if not source_event_ids or any(
                source_event_id not in allowed_set for source_event_id in source_event_ids
            ):
                rejections.append("source_authority_source_outside_input")
                continue
            authority = _authority_from_message(
                raw_message,
                default_authority=default_authority,
            )
            purpose = str(raw_message.get("source_authority_purpose") or default_purpose).strip()
            purpose = purpose or _default_purpose(authority)
            for source_event_id in source_event_ids:
                for segment_ordinal, (local_start, local_end, segment_authority) in enumerate(
                    _authority_segments(content, authority),
                    start=1,
                ):
                    segment_text = content[local_start:local_end]
                    segment_start = message_span_start + local_start
                    segment_end = message_span_start + local_end
                    segment_hash = _sha256(segment_text)
                    identity = {
                        "source_event_id": source_event_id,
                        "role": role,
                        "authority": segment_authority.value,
                        "span_start": segment_start,
                        "span_end": segment_end,
                        "content_sha256": segment_hash,
                        "ordinal": ordinal,
                        "segment_ordinal": segment_ordinal,
                    }
                    entries.append(
                        SourceAuthorityEntry(
                            source_authority_id=_authority_ref_id(identity),
                            authority=segment_authority,
                            source_event_id=source_event_id,
                            role=role,
                            purpose=(
                                purpose
                                if segment_authority == authority
                                else _default_purpose(segment_authority)
                            ),
                            content_sha256=segment_hash,
                            span_start=segment_start,
                            span_end=segment_end,
                            span_status=span_status,
                            source_revision_sha256=revision_hash,
                            _verifiable_text=segment_text,
                        )
                    )

        for artifact in (artifact_catalog or ArtifactCatalog()).entries:
            if artifact.artifact_type in {"tool_call", "tool_result"}:
                authority = SourceAuthority.TOOL_OBSERVATION
                role = "tool"
            elif artifact.artifact_type == "reasoning":
                authority = SourceAuthority.ASSISTANT_INFERENCE
                role = "assistant"
            else:
                authority = SourceAuthority.EXTERNAL_CONTENT
                role = "artifact"
            content_sha256 = _sha256(artifact.summary)
            for source_event_id in artifact.source_event_ids:
                identity = {
                    "source_event_id": source_event_id,
                    "role": role,
                    "authority": authority.value,
                    "artifact_ref_id": artifact.artifact_ref_id,
                    "content_sha256": content_sha256,
                }
                entries.append(
                    SourceAuthorityEntry(
                        source_authority_id=_authority_ref_id(identity),
                        authority=authority,
                        source_event_id=source_event_id,
                        role=role,
                        purpose=_default_purpose(authority),
                        content_sha256=content_sha256,
                        span_start=-1,
                        span_end=-1,
                        span_status="artifact_summary",
                        artifact_ref_id=artifact.artifact_ref_id,
                        _verifiable_text=artifact.summary,
                    )
                )

        deduplicated = {entry.source_authority_id: entry for entry in entries}
        return cls(
            entries=tuple(deduplicated[key] for key in sorted(deduplicated)),
            rejected_count=len(rejections),
            rejection_codes=tuple(rejections),
        )

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "entries": [entry.canonical_payload() for entry in self.entries],
            "rejected_count": self.rejected_count,
            "rejection_codes": list(self.rejection_codes),
        }

    @property
    def catalog_hash(self) -> str:
        return _sha256(_canonical_json(self.canonical_payload()))

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "catalog_hash": self.catalog_hash,
            "entries": [entry.prompt_payload() for entry in self.entries],
            "rejected_count": self.rejected_count,
        }

    def get(self, source_authority_id: Any) -> SourceAuthorityEntry | None:
        value = str(source_authority_id or "")
        return next((entry for entry in self.entries if entry.source_authority_id == value), None)

    def require_admissible(self) -> None:
        if self.rejected_count:
            raise SourceAuthorityCatalogRejectedError(self.rejection_codes)
        if not self.entries:
            raise SourceAuthorityCatalogRejectedError(("source_authority_catalog_empty",))

    def matching_entries(
        self,
        *,
        source_event_id: Any,
        quote: Any,
        artifact_ref_id: Any = "",
    ) -> tuple[SourceAuthorityEntry, ...]:
        event_id = str(source_event_id or "")
        artifact_id = str(artifact_ref_id or "")
        return tuple(
            entry
            for entry in self.entries
            if entry.source_event_id == event_id
            and (not artifact_id or entry.artifact_ref_id == artifact_id)
            and entry.matches_quote(quote)
        )


def verify_source_authority_raw_span(
    entry: SourceAuthorityEntry,
    raw_db_path: Path,
) -> bool:
    """Verify one exact catalog span against immutable canonical Raw bytes."""

    return load_source_authority_raw_snapshot(entry, raw_db_path) is not None


def load_source_authority_raw_snapshot(
    entry: SourceAuthorityEntry,
    raw_db_path: Path,
) -> Mapping[str, Any] | None:
    """Return canonical Raw only when the selected authority span verifies exactly."""

    # Import lazily: importing ``core.sync_framework`` executes its package
    # initializer, which reaches trust/cognitive modules that themselves
    # consume SourceAuthority.  Keeping the read-only decoder at the use site
    # preserves direct-import purity for distillation entrypoints.
    from core.sync_framework.raw_event_reader import decode_raw_revision_snapshot

    path = Path(raw_db_path).expanduser().resolve(strict=True)
    uri = "file:" + quote(str(path), safe="/") + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        row = conn.execute(
            "SELECT content_hash, snapshot_blob FROM raw_turn_revisions "
            "WHERE revision_id=?",
            (entry.source_event_id,),
        ).fetchone()
    if row is None or _normalized_sha256(str(row[0])) != entry.source_revision_sha256:
        return None
    snapshot = decode_raw_revision_snapshot(row[1])
    role_text = _raw_role_text(snapshot, entry.role)
    if role_text is None or not (
        0 <= entry.span_start < entry.span_end <= len(role_text)
    ):
        return None
    if _sha256(role_text[entry.span_start : entry.span_end]) != entry.content_sha256:
        return None
    return snapshot


def _raw_role_text(snapshot: Mapping[str, Any], role: str) -> str | None:
    if role == "user":
        return str(snapshot.get("user_content") or "")
    if role == "assistant":
        return str(snapshot.get("assistant_content") or "")
    if role == "tool":
        return _canonical_json(snapshot.get("tool_results") or [])
    return None


def _normalized_sha256(value: str) -> str:
    return value if value.startswith("sha256:") else "sha256:" + value


@dataclass(frozen=True)
class SourceAuthorityResolutionIssue:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class SourceAuthorityResolution:
    payload: Any
    issues: tuple[SourceAuthorityResolutionIssue, ...]


def _structured_output(payload: Any) -> Mapping[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    nested = payload.get("structured_output")
    return nested if isinstance(nested, Mapping) else payload


def _evidence_items(payload: Any) -> list[tuple[str, dict[str, Any]]]:
    structured = _structured_output(payload)
    if not isinstance(structured, Mapping):
        return []
    found: list[tuple[str, dict[str, Any]]] = []
    behavior = structured.get("user_behavior_intent")
    if isinstance(behavior, Mapping):
        for field_name in ("intent_evidence", "intent_verification_events"):
            values = behavior.get(field_name)
            if not isinstance(values, list):
                continue
            for index, item in enumerate(values):
                if isinstance(item, dict):
                    found.append((f"structured_output.user_behavior_intent.{field_name}[{index}]", item))
    claims = structured.get("claims")
    if isinstance(claims, list):
        for claim_index, claim in enumerate(claims):
            if not isinstance(claim, Mapping) or not isinstance(claim.get("evidence"), list):
                continue
            for evidence_index, item in enumerate(claim["evidence"]):
                if isinstance(item, dict):
                    found.append(
                        (
                            f"structured_output.claims[{claim_index}].evidence[{evidence_index}]",
                            item,
                        )
                    )
    found.extend(iter_cognition_episode_evidence(payload))
    return found


def _select_entry(
    evidence: Mapping[str, Any],
    catalog: SourceAuthorityCatalog,
) -> tuple[SourceAuthorityEntry | None, str]:
    supplied = str(evidence.get("source_authority_id") or "")
    matches = catalog.matching_entries(
        source_event_id=evidence.get("source_event_id"),
        quote=evidence.get("quote"),
        artifact_ref_id=evidence.get("artifact_ref_id"),
    )
    if supplied:
        entry = catalog.get(supplied)
        if entry is None:
            return None, "source_authority_unknown"
        if entry.source_event_id != str(evidence.get("source_event_id") or ""):
            return None, "source_authority_source_mismatch"
        artifact_ref_id = str(evidence.get("artifact_ref_id") or "")
        if artifact_ref_id and entry.artifact_ref_id != artifact_ref_id:
            return None, "source_authority_artifact_mismatch"
        if not entry.matches_quote(evidence.get("quote")):
            return None, "source_authority_quote_mismatch"
        if len(matches) > 1:
            return None, "source_authority_ambiguous"
        if matches != (entry,):
            return None, "source_authority_quote_mismatch"
        return entry, ""
    if len(matches) == 1:
        return matches[0], ""
    if not matches:
        return None, "source_authority_quote_mismatch"
    return None, "source_authority_ambiguous"


def resolve_model_source_authority_selections(
    payload: Any,
    catalog: SourceAuthorityCatalog,
) -> SourceAuthorityResolution:
    """Resolve model-selected refs and reject model-owned authority fields."""
    resolved = deepcopy(payload)
    issues: list[SourceAuthorityResolutionIssue] = []
    for path, evidence in _evidence_items(resolved):
        supplied_system_fields = sorted(SYSTEM_SOURCE_AUTHORITY_FIELDS.intersection(evidence))
        if supplied_system_fields:
            issues.append(
                SourceAuthorityResolutionIssue(
                    "model_owned_source_authority",
                    path,
                    "model output may select source_authority_id only; system fields supplied: "
                    + ", ".join(supplied_system_fields),
                )
            )
            continue
        entry, error = _select_entry(evidence, catalog)
        if entry is None:
            issues.append(
                SourceAuthorityResolutionIssue(
                    error or "source_authority_unresolved",
                    f"{path}.source_authority_id",
                    "evidence must select one system-owned authority whose exact span contains the quote",
                )
            )
            continue
        evidence.update(entry.resolved_evidence_payload())
    return SourceAuthorityResolution(payload=resolved, issues=tuple(issues))


def model_source_authority_projection(payload: Any) -> Any:
    """Remove resolved system fields before validating the model-facing schema."""
    projected = deepcopy(payload)
    for _, evidence in _evidence_items(projected):
        for field_name in SYSTEM_SOURCE_AUTHORITY_FIELDS:
            evidence.pop(field_name, None)
    return projected


@dataclass(frozen=True)
class CognitiveAuthorityDecision:
    authorized: bool
    source_authority_ids: tuple[str, ...]
    authorities: tuple[str, ...]
    reason: str


def claim_cognitive_authority(
    claim: Mapping[str, Any],
    catalog: SourceAuthorityCatalog,
) -> CognitiveAuthorityDecision:
    evidence_items = claim.get("evidence")
    if not isinstance(evidence_items, list) or not evidence_items:
        return CognitiveAuthorityDecision(False, (), (), "claim_evidence_missing")
    entries: list[SourceAuthorityEntry] = []
    for evidence in evidence_items:
        if not isinstance(evidence, Mapping):
            return CognitiveAuthorityDecision(False, (), (), "claim_evidence_invalid")
        entry, error = _select_entry(evidence, catalog)
        if entry is None:
            return CognitiveAuthorityDecision(False, (), (), error or "source_authority_unresolved")
        entries.append(entry)
    authority_ids = tuple(dict.fromkeys(entry.source_authority_id for entry in entries))
    authorities = tuple(dict.fromkeys(entry.authority.value for entry in entries))
    authorized = any(entry.allows_cognitive_update for entry in entries)
    return CognitiveAuthorityDecision(
        authorized,
        authority_ids,
        authorities,
        "high_authority_evidence_present" if authorized else "low_authority_evidence_only",
    )


def output_allows_cognitive_derivative(
    structured_output: Mapping[str, Any] | None,
    catalog: SourceAuthorityCatalog,
) -> bool:
    if not isinstance(structured_output, Mapping):
        return False
    claims = structured_output.get("claims")
    if not isinstance(claims, list) or not claims:
        return False
    candidates = [
        claim
        for claim in claims
        if isinstance(claim, Mapping) and claim.get("cognitive_actions")
    ]
    if not candidates:
        candidates = [claim for claim in claims if isinstance(claim, Mapping)]
    return bool(candidates) and all(
        claim_cognitive_authority(claim, catalog).authorized for claim in candidates
    )
