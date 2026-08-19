"""System-owned catalog and resolution for distillation artifact evidence.

Capture producers may supply local paths and legacy turn-addressed URIs.  The
catalog converts only hash-verifiable, locally authorized references into
content-addressed identities.  The model sees opaque reference IDs and can
select them; it never owns the canonical URI, type, digest, MIME type, or ACL.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core.cognition_episode_contract import iter_cognition_episode_evidence
from core.evidence.artifact_uri import (
    ALLOWED_ARTIFACT_TYPES,
    artifact_uri_error,
    build_content_artifact_uri,
    parse_artifact_uri,
)
from core.privacy.content_redaction import redact_persistence_value

ARTIFACT_CATALOG_SCHEMA_VERSION = "mnemos.artifact_catalog.v1"
LOCAL_USER_ACL = "local_user"
INLINE_HASH_VERIFICATION = "inline_payload_sha256_v1"
INLINE_ARTIFACT_TYPES = frozenset({"tool_call", "tool_result"})
SYSTEM_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_uri",
        "artifact_type",
        "artifact_summary",
        "artifact_sha256",
        "artifact_mime_type",
        "artifact_acl",
    }
)


class ArtifactCatalogRejectedError(ValueError):
    """Raised before model execution when any source ref failed admission."""

    def __init__(self, rejection_codes: Sequence[str]):
        self.rejection_codes = tuple(str(code) for code in rejection_codes)
        super().__init__(
            "artifact catalog rejected source refs: " + ", ".join(self.rejection_codes)
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    """Return a full hexadecimal SHA-256 for a regular local file, or empty."""
    candidate = Path(path).expanduser() if str(path or "").strip() else None
    if candidate is None or not candidate.is_file():
        return ""
    digest = hashlib.sha256()
    try:
        with candidate.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def require_sha256_file(path: str | Path) -> str:
    """Return a file SHA-256 or fail closed when the source cannot be read."""

    digest = sha256_file(path)
    if not digest:
        raise OSError(f"cannot hash required file: {Path(path)}")
    return digest


def normalize_sha256(value: Any) -> str:
    digest = str(value or "").strip().lower().removeprefix("sha256:")
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        return ""
    return digest


def _artifact_ref_id(artifact_type: str, digest: str) -> str:
    token = hashlib.sha256(
        f"artifact-ref-v1\0{artifact_type}\0{digest}".encode("utf-8")
    ).hexdigest()[:32]
    return f"artifact-ref:{token}"


@dataclass(frozen=True)
class ArtifactCatalogEntry:
    artifact_ref_id: str
    uri: str
    artifact_type: str
    summary: str
    source_event_ids: tuple[str, ...]
    sha256: str
    mime_type: str = ""
    acl: str = LOCAL_USER_ACL

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "artifact_ref_id": self.artifact_ref_id,
            "uri": self.uri,
            "artifact_type": self.artifact_type,
            "summary": self.summary,
            "source_event_ids": list(self.source_event_ids),
            "sha256": self.sha256,
            "mime_type": self.mime_type,
            "acl": self.acl,
        }

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "artifact_ref_id": self.artifact_ref_id,
            "artifact_type": self.artifact_type,
            "summary": self.summary,
            "source_event_ids": list(self.source_event_ids),
        }

    def resolved_evidence_payload(self) -> dict[str, str]:
        return {
            "artifact_ref_id": self.artifact_ref_id,
            "artifact_uri": self.uri,
            "artifact_type": self.artifact_type,
            "artifact_summary": self.summary,
            "artifact_sha256": self.sha256,
            "artifact_mime_type": self.mime_type,
            "artifact_acl": self.acl,
        }


@dataclass(frozen=True)
class ArtifactCatalog:
    entries: tuple[ArtifactCatalogEntry, ...] = ()
    rejected_count: int = 0
    rejection_codes: tuple[str, ...] = ()
    schema_version: str = ARTIFACT_CATALOG_SCHEMA_VERSION

    @classmethod
    def from_refs(
        cls,
        refs: Iterable[Mapping[str, Any]] | None,
        *,
        allowed_source_event_ids: Sequence[str] | None = None,
    ) -> "ArtifactCatalog":
        allowed = (
            {str(value) for value in allowed_source_event_ids if str(value)}
            if allowed_source_event_ids is not None
            else None
        )
        grouped: dict[str, dict[str, Any]] = {}
        rejection_codes: list[str] = []
        for raw_ref in refs or ():
            normalized, error = _normalize_ref(raw_ref, allowed)
            if error:
                rejection_codes.append(error)
                continue
            assert normalized is not None
            ref_id = str(normalized["artifact_ref_id"])
            group = grouped.setdefault(
                ref_id,
                {
                    **normalized,
                    "summaries": set(),
                    "mime_types": set(),
                    "source_event_ids": set(),
                },
            )
            group["summaries"].add(str(normalized["summary"]))
            if normalized["mime_type"]:
                group["mime_types"].add(str(normalized["mime_type"]))
            group["source_event_ids"].update(normalized["source_event_ids"])

        entries = []
        for ref_id in sorted(grouped):
            group = grouped[ref_id]
            summaries = sorted(value for value in group["summaries"] if value)
            mime_types = sorted(value for value in group["mime_types"] if value)
            entries.append(
                ArtifactCatalogEntry(
                    artifact_ref_id=ref_id,
                    uri=str(group["uri"]),
                    artifact_type=str(group["artifact_type"]),
                    summary=summaries[0],
                    source_event_ids=tuple(sorted(group["source_event_ids"])),
                    sha256=str(group["sha256"]),
                    mime_type=mime_types[0] if mime_types else "",
                )
            )
        return cls(
            entries=tuple(entries),
            rejected_count=len(rejection_codes),
            rejection_codes=tuple(rejection_codes),
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
        return _sha256_text(_canonical_json(self.canonical_payload()))

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "catalog_hash": self.catalog_hash,
            "entries": [entry.prompt_payload() for entry in self.entries],
            "rejected_count": self.rejected_count,
        }

    def get(self, artifact_ref_id: str) -> ArtifactCatalogEntry | None:
        return next(
            (
                entry
                for entry in self.entries
                if entry.artifact_ref_id == str(artifact_ref_id or "")
            ),
            None,
        )

    def require_admissible(self) -> None:
        """Fail before prompt rendering if capture supplied any invalid ref."""
        if self.rejected_count:
            raise ArtifactCatalogRejectedError(self.rejection_codes)


def _source_event_ids(
    ref: Mapping[str, Any],
    allowed: set[str] | None,
) -> tuple[str, ...]:
    supplied = ref.get("source_event_ids")
    if isinstance(supplied, Sequence) and not isinstance(supplied, (str, bytes)):
        values = [str(value) for value in supplied if str(value)]
    else:
        value = str(ref.get("source_event_id") or "")
        values = [value] if value else []
    if allowed is not None:
        values = [value for value in values if value in allowed]
    return tuple(sorted(set(values)))


def _normalize_ref(
    raw_ref: Mapping[str, Any] | Any,
    allowed: set[str] | None,
) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(raw_ref, Mapping):
        return None, "artifact_ref_invalid"
    artifact_type = str(raw_ref.get("artifact_type") or "")
    if artifact_type not in ALLOWED_ARTIFACT_TYPES:
        return None, "artifact_type_invalid"
    uri = str(raw_ref.get("uri") or "")
    if artifact_uri_error(uri):
        return None, "artifact_uri_invalid"
    if parse_artifact_uri(uri).artifact_type != artifact_type:
        return None, "artifact_type_mismatch"
    source_event_ids = _source_event_ids(raw_ref, allowed)
    if not source_event_ids:
        return None, "artifact_ref_outside_input"
    acl = str(raw_ref.get("acl") or raw_ref.get("artifact_acl") or LOCAL_USER_ACL)
    if acl != LOCAL_USER_ACL:
        return None, "artifact_ref_unauthorized"
    summary = str(
        redact_persistence_value(
            str(raw_ref.get("summary") or ""),
            key="artifact_summary",
        ).value
    ).strip()
    if not summary:
        return None, "artifact_summary_missing"

    supplied_digest = normalize_sha256(raw_ref.get("sha256"))
    path_value = str(raw_ref.get("path") or "").strip()
    path_digest = sha256_file(path_value)
    if path_value:
        if not path_digest:
            return None, "artifact_content_missing"
        if supplied_digest and supplied_digest != path_digest:
            return None, "artifact_hash_mismatch"
        digest = path_digest
    else:
        metadata = raw_ref.get("metadata")
        verification = (
            str(metadata.get("hash_verification") or "")
            if isinstance(metadata, Mapping)
            else ""
        )
        if (
            artifact_type not in INLINE_ARTIFACT_TYPES
            or verification != INLINE_HASH_VERIFICATION
            or not isinstance(metadata, Mapping)
            or "inline_payload" not in metadata
        ):
            return None, "artifact_hash_unverifiable"
        try:
            inline_payload = _canonical_json(metadata["inline_payload"])
        except (TypeError, ValueError):
            return None, "artifact_hash_unverifiable"
        digest = hashlib.sha256(inline_payload.encode("utf-8")).hexdigest()
        if supplied_digest and supplied_digest != digest:
            return None, "artifact_hash_mismatch"
    if not digest:
        return None, "artifact_hash_unverifiable"

    return {
        "artifact_ref_id": _artifact_ref_id(artifact_type, digest),
        "uri": build_content_artifact_uri(artifact_type, digest),
        "artifact_type": artifact_type,
        "summary": summary,
        "source_event_ids": source_event_ids,
        "sha256": f"sha256:{digest}",
        "mime_type": str(raw_ref.get("mime_type") or "").strip(),
        "acl": LOCAL_USER_ACL,
    }, ""


@dataclass(frozen=True)
class ArtifactSelectionIssue:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class ArtifactSelectionResolution:
    payload: Any
    issues: tuple[ArtifactSelectionIssue, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.issues


def _evidence_items(payload: Any) -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(payload, dict):
        return []
    structured = payload.get("structured_output")
    if not isinstance(structured, dict):
        return []
    claims = structured.get("claims")
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(claims, list):
        for claim_index, claim in enumerate(claims):
            if not isinstance(claim, dict) or not isinstance(claim.get("evidence"), list):
                continue
            for evidence_index, evidence in enumerate(claim["evidence"]):
                if isinstance(evidence, dict):
                    found.append(
                        (
                            "structured_output.claims"
                            f"[{claim_index}].evidence[{evidence_index}]",
                            evidence,
                        )
                    )
    found.extend(iter_cognition_episode_evidence(payload))
    return found


def resolve_model_artifact_selections(
    payload: Any,
    catalog: ArtifactCatalog,
) -> ArtifactSelectionResolution:
    """Resolve model-selected IDs while rejecting model-owned identity fields."""
    resolved = deepcopy(payload)
    issues: list[ArtifactSelectionIssue] = []
    for path, evidence in _evidence_items(resolved):
        supplied_system_fields = sorted(SYSTEM_ARTIFACT_FIELDS.intersection(evidence))
        if supplied_system_fields:
            issues.append(
                ArtifactSelectionIssue(
                    "model_owned_artifact_identity",
                    path,
                    "model output must select artifact_ref_id only; system fields supplied: "
                    + ", ".join(supplied_system_fields),
                )
            )
            continue
        if "artifact_ref_id" not in evidence:
            continue
        ref_id = str(evidence.get("artifact_ref_id") or "")
        entry = catalog.get(ref_id)
        if entry is None:
            issues.append(
                ArtifactSelectionIssue(
                    "artifact_ref_unknown",
                    f"{path}.artifact_ref_id",
                    "artifact_ref_id is not present in the immutable input catalog",
                )
            )
            continue
        source_event_id = str(evidence.get("source_event_id") or "")
        if source_event_id not in entry.source_event_ids:
            issues.append(
                ArtifactSelectionIssue(
                    "artifact_ref_source_mismatch",
                    f"{path}.source_event_id",
                    "artifact_ref_id is not authorized for this source_event_id",
                )
            )
            continue
        evidence.update(entry.resolved_evidence_payload())
    return ArtifactSelectionResolution(payload=resolved, issues=tuple(issues))


def model_artifact_projection(payload: Any) -> Any:
    """Project a system-resolved root back to the schema shown to the model."""
    projected = deepcopy(payload)
    for _, evidence in _evidence_items(projected):
        for field in SYSTEM_ARTIFACT_FIELDS:
            evidence.pop(field, None)
    return projected
