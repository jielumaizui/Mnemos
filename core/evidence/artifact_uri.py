"""Artifact URI helpers for raw, tool, attachment, and report evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

SCHEME = "mnemos-artifact"

ALLOWED_ARTIFACT_TYPES: frozenset[str] = frozenset(
    {
        "capture_artifact",
        "tool_call",
        "tool_result",
        "attachment",
        "reasoning",
        "terminal",
        "screenshot",
        "test_report",
        "file",
        "diff",
    }
)


@dataclass(frozen=True)
class ArtifactRef:
    uri: str
    artifact_type: str
    summary: str
    source_event_id: str = ""
    mime_type: str = ""
    sha256: str = ""
    path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "uri": self.uri,
            "artifact_type": self.artifact_type,
            "summary": self.summary,
        }
        for key, value in (
            ("source_event_id", self.source_event_id),
            ("mime_type", self.mime_type),
            ("sha256", self.sha256),
            ("path", self.path),
        ):
            if value:
                payload[key] = value
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True)
class ArtifactUriIdentity:
    """Canonical identity encoded by a validated artifact URI."""

    source_agent: str
    session_id: str
    turn_number: str
    artifact_type: str
    index: str = ""
    identity_kind: str = "turn"
    sha256: str = ""


def build_artifact_uri(
    source_agent: str,
    session_id: str,
    turn_number: int | str,
    artifact_type: str,
    index: int | str | None = None,
) -> str:
    """Build a stable non-local URI for evidence artifacts."""
    for field_name, value in (
        ("source_agent", source_agent),
        ("session_id", session_id),
        ("turn_number", turn_number),
    ):
        if not str(value).strip():
            raise ValueError(f"{field_name} is required")
    artifact_type = str(artifact_type)
    if artifact_type not in ALLOWED_ARTIFACT_TYPES:
        raise ValueError(f"unsupported artifact type: {artifact_type}")
    parts = [
        quote(str(source_agent), safe=""),
        quote(str(session_id), safe=""),
        "turn",
        quote(str(turn_number), safe=""),
        quote(artifact_type, safe=""),
    ]
    if index is not None:
        parts.append(quote(str(index), safe=""))
    return f"{SCHEME}://{'/'.join(parts)}"


def build_content_artifact_uri(artifact_type: str, sha256: str) -> str:
    """Build a canonical URI whose identity is the artifact bytes, not a path or turn."""
    artifact_type = str(artifact_type)
    if artifact_type not in ALLOWED_ARTIFACT_TYPES:
        raise ValueError(f"unsupported artifact type: {artifact_type}")
    digest = str(sha256 or "").removeprefix("sha256:").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("artifact sha256 must be 64 lowercase hex characters")
    return f"{SCHEME}://content/sha256/{digest}/{artifact_type}"


def build_artifact_ref(
    *,
    source_agent: str,
    session_id: str,
    turn_number: int | str,
    artifact_type: str,
    summary: str,
    index: int | str | None = None,
    source_event_id: str = "",
    path: str | Path = "",
    mime_type: str = "",
    sha256: str = "",
    metadata: dict[str, Any] | None = None,
) -> ArtifactRef:
    """Build a serializable artifact reference with a validated URI."""
    uri = build_artifact_uri(source_agent, session_id, turn_number, artifact_type, index)
    if not str(summary).strip():
        raise ValueError("artifact summary is required")
    return ArtifactRef(
        uri=uri,
        artifact_type=str(artifact_type),
        summary=str(summary).strip(),
        source_event_id=str(source_event_id or ""),
        mime_type=str(mime_type or ""),
        sha256=str(sha256 or ""),
        path=str(path or ""),
        metadata=dict(metadata or {}),
    )


def artifact_uri_error(uri: Any) -> str:
    """Return an error string when an artifact URI violates the Mnemos contract."""
    if not isinstance(uri, str) or not uri.strip():
        return "artifact_uri must be a non-empty string"
    value = uri.strip()
    if any(ord(ch) < 32 for ch in value) or any(ch.isspace() for ch in value):
        return "artifact_uri must not contain whitespace or control characters"
    if len(value) > 1024:
        return "artifact_uri is too long"
    parsed = urlparse(value)
    if parsed.scheme != SCHEME:
        return f"artifact_uri scheme must be {SCHEME}"
    if not parsed.netloc:
        return "artifact_uri must include source agent"
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc == "content":
        if len(parts) != 3 or parts[0] != "sha256":
            return "content artifact_uri path must be /sha256/<digest>/<artifact_type>"
        digest = parts[1]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            return "content artifact sha256 must be 64 lowercase hex characters"
        artifact_type = parts[2]
        if artifact_type not in ALLOWED_ARTIFACT_TYPES:
            return f"artifact_type must be one of {sorted(ALLOWED_ARTIFACT_TYPES)}"
        return ""
    if len(parts) < 4:
        return "artifact_uri path must include session, turn, turn_number, and artifact type"
    if len(parts) > 5:
        return "artifact_uri path must not contain extra segments"
    if parts[1] != "turn":
        return "artifact_uri path must contain /turn/<turn_number>/"
    artifact_type = parts[3]
    if artifact_type not in ALLOWED_ARTIFACT_TYPES:
        return f"artifact_type must be one of {sorted(ALLOWED_ARTIFACT_TYPES)}"
    if any(part in {".", ".."} for part in parts):
        return "artifact_uri path must not contain relative path segments"
    return ""


def parse_artifact_uri(uri: Any) -> ArtifactUriIdentity:
    """Parse a validated URI without guessing missing identity fields."""

    error = artifact_uri_error(uri)
    if error:
        raise ValueError(error)
    parsed = urlparse(str(uri).strip())
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if parsed.netloc == "content":
        return ArtifactUriIdentity(
            source_agent="",
            session_id="",
            turn_number="",
            artifact_type=parts[2],
            identity_kind="content",
            sha256=parts[1],
        )
    return ArtifactUriIdentity(
        source_agent=unquote(parsed.netloc),
        session_id=parts[0],
        turn_number=parts[2],
        artifact_type=parts[3],
        index=parts[4] if len(parts) == 5 else "",
    )


def is_valid_artifact_uri(uri: Any) -> bool:
    return artifact_uri_error(uri) == ""
