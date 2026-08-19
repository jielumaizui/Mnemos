"""Build hash-verifiable capture references for every ingestion entrypoint."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.evidence.artifact_catalog import (
    INLINE_HASH_VERIFICATION,
    normalize_sha256,
    sha256_file,
)
from core.evidence.artifact_uri import build_artifact_ref
from core.ops.durable_io import (
    DurableIOError,
    secure_publish_immutable_text,
    secure_read_bytes,
)


CAPTURE_ARTIFACT_STORAGE_SCHEMA = "mnemos.capture_artifact_storage.v1"
_MANAGED_ARTIFACT_TYPES = frozenset({"capture", "reasoning"})


def require_capture_turn_number(value: Any) -> int:
    """Return the canonical non-negative turn identity used by every capture path."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("turn_number must be a non-negative integer")
    return value


def _required_identity_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _managed_session_component(source_agent: str, session_id: str) -> str:
    material = json.dumps(
        {
            "schema_version": CAPTURE_ARTIFACT_STORAGE_SCHEMA,
            "source_agent": _required_identity_text(source_agent, field="source_agent"),
            "session_id": _required_identity_text(session_id, field="session_id"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "session-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def _managed_turn_component(turn_number: int) -> str:
    canonical = require_capture_turn_number(turn_number)
    return hashlib.sha256(
        f"{CAPTURE_ARTIFACT_STORAGE_SCHEMA}\0turn\0{canonical}".encode("utf-8")
    ).hexdigest()[:16]


def managed_capture_artifact_relative_path(
    *,
    source_agent: str,
    session_id: str,
    turn_number: int,
    artifact_type: str,
    content: str,
) -> Path:
    if artifact_type not in _MANAGED_ARTIFACT_TYPES:
        raise ValueError("capture artifact type is invalid")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return (
        Path(_managed_session_component(source_agent, session_id))
        / f"{artifact_type}-{_managed_turn_component(turn_number)}-{digest}.md"
    )


def write_managed_capture_artifact(
    *,
    database_dir: Path,
    source_agent: str,
    session_id: str,
    turn_number: int,
    artifact_type: str,
    content: str,
) -> Path:
    """Publish one immutable source/session/turn/content-bound capture artifact."""

    root = Path(database_dir) / "capture_artifacts"
    relative = managed_capture_artifact_relative_path(
        source_agent=source_agent,
        session_id=session_id,
        turn_number=turn_number,
        artifact_type=artifact_type,
        content=content,
    )
    # trusted-scan: artifact owner=evidence target=managed_capture_artifact expires=never
    return secure_publish_immutable_text(root, relative, content)


def managed_capture_artifact_sha256(
    *,
    database_dir: Path,
    source_agent: str,
    session_id: str,
    turn_number: int,
    artifact_type: str,
    path: str | Path,
) -> str:
    """Verify a managed artifact's lexical scope, identity and content-address."""

    content = read_managed_capture_artifact_bytes(
        database_dir=database_dir,
        source_agent=source_agent,
        session_id=session_id,
        turn_number=turn_number,
        artifact_type=artifact_type,
        path=path,
    )
    return hashlib.sha256(content).hexdigest() if content is not None else ""


def read_managed_capture_artifact_bytes(
    *,
    database_dir: Path,
    source_agent: str,
    session_id: str,
    turn_number: int,
    artifact_type: str,
    path: str | Path,
) -> bytes | None:
    """Read one exact managed artifact through the same no-follow validation."""

    if artifact_type not in _MANAGED_ARTIFACT_TYPES:
        return None
    root = Path(os.path.abspath(Path(database_dir) / "capture_artifacts"))
    candidate = Path(os.path.abspath(Path(path).expanduser()))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    if len(relative.parts) != 2:
        return None
    if relative.parts[0] != _managed_session_component(source_agent, session_id):
        return None
    prefix = f"{artifact_type}-{_managed_turn_component(turn_number)}-"
    name = relative.parts[1]
    if not name.startswith(prefix) or not name.endswith(".md"):
        return None
    claimed = name[len(prefix) : -3]
    if len(claimed) != 64 or any(ch not in "0123456789abcdef" for ch in claimed):
        return None
    try:
        content = secure_read_bytes(root, relative)
    except DurableIOError:
        return None
    if content is None:
        return None
    actual = hashlib.sha256(content).hexdigest()
    return content if actual == claimed else None


def read_historical_capture_artifact_bytes(
    *,
    database_dir: Path,
    session_id: str,
    turn_number: int,
    artifact_type: str,
    path: str | Path,
) -> bytes | None:
    """Read only the exact pre-v1 path for a session/turn, without following links."""

    if artifact_type not in _MANAGED_ARTIFACT_TYPES:
        return None
    try:
        session = _required_identity_text(session_id, field="session_id")
        turn = require_capture_turn_number(turn_number)
    except ValueError:
        return None
    filename = (
        f"turn_{turn}.md"
        if artifact_type == "capture"
        else f"turn_{turn}_reasoning.md"
    )
    root = Path(os.path.abspath(Path(database_dir) / "capture_artifacts"))
    relative = Path(session) / filename
    candidate = Path(os.path.abspath(Path(path).expanduser()))
    if candidate != Path(os.path.abspath(root / relative)):
        return None
    try:
        return secure_read_bytes(root, relative)
    except DurableIOError:
        return None


def build_reasoning_artifact_content(
    *,
    source_agent: str,
    session_id: str,
    turn_number: int,
    reasoning: str,
) -> str:
    return "\n".join(
        [
            "# Reasoning Artifact",
            "",
            f"- storage_schema: {CAPTURE_ARTIFACT_STORAGE_SCHEMA}",
            f"- source_agent: {json.dumps(source_agent, ensure_ascii=False)}",
            f"- session_id: {json.dumps(session_id, ensure_ascii=False)}",
            f"- turn_number: {require_capture_turn_number(turn_number)}",
            "",
            "---",
            "",
            reasoning,
            "",
        ]
    )


def build_full_capture_artifact_content(
    *,
    source_agent: str,
    session_id: str,
    turn_number: int,
    user_content: str,
    assistant_content: str,
    structured: Mapping[str, Any],
) -> str:
    return f"""# Capture Artifact

- storage_schema: {CAPTURE_ARTIFACT_STORAGE_SCHEMA}
- source_agent: {json.dumps(source_agent, ensure_ascii=False)}
- session_id: {json.dumps(session_id, ensure_ascii=False)}
- turn_number: {require_capture_turn_number(turn_number)}

---

## User

{user_content}

---

## Assistant

{assistant_content}

---

## Structured Capture

````json
{json.dumps(dict(structured), ensure_ascii=False, indent=2, sort_keys=True, default=str)}
````
"""


def _append_ref(
    refs: list[dict[str, Any]],
    *,
    source_agent: str,
    session_id: str,
    turn_number: int,
    artifact_type: str,
    summary: str,
    sha256: str,
    source_event_id: str,
    index: int | None = None,
    path: str = "",
    mime_type: str = "",
    hash_verification: str = "",
    inline_payload: Any = None,
) -> None:
    digest = normalize_sha256(sha256)
    metadata: dict[str, Any] = {}
    if hash_verification:
        metadata["hash_verification"] = hash_verification
    if inline_payload is not None:
        metadata["inline_payload"] = inline_payload
    refs.append(
        build_artifact_ref(
            source_agent=source_agent,
            session_id=session_id,
            turn_number=turn_number,
            artifact_type=artifact_type,
            summary=summary,
            index=index,
            source_event_id=source_event_id,
            path=path,
            mime_type=mime_type,
            sha256=digest,
            metadata=metadata or None,
        ).to_dict()
    )


def build_capture_artifact_refs(
    *,
    source_agent: str,
    session_id: str,
    turn_number: int,
    source_event_id: str = "",
    capture_artifact_path: str | Path = "",
    reasoning_artifact_path: str | Path = "",
    reasoning_sha256: str = "",
    tool_results: Sequence[Mapping[str, Any]] = (),
    attachments: Sequence[Mapping[str, Any]] = (),
    managed_database_dir: str | Path = "",
) -> list[dict[str, Any]]:
    """Return only references with a complete, system-verifiable SHA-256."""
    refs: list[dict[str, Any]] = []
    capture_path = str(capture_artifact_path or "")
    if capture_path:
        if not str(managed_database_dir or ""):
            raise DurableIOError("capture_artifact_owner_root_required")
        capture_digest = managed_capture_artifact_sha256(
            database_dir=Path(managed_database_dir),
            source_agent=source_agent,
            session_id=session_id,
            turn_number=turn_number,
            artifact_type="capture",
            path=capture_path,
        )
        if not capture_digest:
            raise DurableIOError("capture_artifact_verification_failed")
        _append_ref(
            refs,
            source_agent=source_agent,
            session_id=session_id,
            turn_number=turn_number,
            artifact_type="capture_artifact",
            summary="完整 capture payload",
            sha256=capture_digest,
            source_event_id=source_event_id,
            path=capture_path,
            mime_type="text/markdown",
        )

    reasoning_path = str(reasoning_artifact_path or "")
    if reasoning_path:
        if not str(managed_database_dir or ""):
            raise DurableIOError("reasoning_artifact_owner_root_required")
        reasoning_digest = managed_capture_artifact_sha256(
            database_dir=Path(managed_database_dir),
            source_agent=source_agent,
            session_id=session_id,
            turn_number=turn_number,
            artifact_type="reasoning",
            path=reasoning_path,
        )
        if not reasoning_digest:
            raise DurableIOError("reasoning_artifact_verification_failed")
        _append_ref(
            refs,
            source_agent=source_agent,
            session_id=session_id,
            turn_number=turn_number,
            artifact_type="reasoning",
            summary="宿主暴露的 reasoning artifact",
            sha256=reasoning_digest or reasoning_sha256,
            source_event_id=source_event_id,
            path=reasoning_path,
            mime_type="text/markdown",
        )

    for index, result in enumerate(tool_results):
        inline_payload = dict(result)
        summary = str(
            result.get("name")
            or result.get("tool_name")
            or result.get("tool_use_id")
            or f"tool_result[{index}]"
        )
        digest = hashlib.sha256(
            json.dumps(
                inline_payload,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        _append_ref(
            refs,
            source_agent=source_agent,
            session_id=session_id,
            turn_number=turn_number,
            artifact_type="tool_result",
            summary=summary,
            sha256=digest,
            source_event_id=source_event_id,
            index=index,
            hash_verification=INLINE_HASH_VERIFICATION,
            inline_payload=inline_payload,
        )

    for index, attachment in enumerate(attachments):
        path = str(attachment.get("path") or attachment.get("file") or "")
        summary = str(
            attachment.get("name")
            or attachment.get("title")
            or Path(path).name
            or f"attachment[{index}]"
        )
        digest = sha256_file(path) or normalize_sha256(attachment.get("sha256"))
        _append_ref(
            refs,
            source_agent=source_agent,
            session_id=session_id,
            turn_number=turn_number,
            artifact_type="attachment",
            summary=summary,
            sha256=digest,
            source_event_id=source_event_id,
            index=index,
            path=path,
            mime_type=str(attachment.get("mime_type") or attachment.get("type") or ""),
        )
    return refs
