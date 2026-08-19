"""Canonical native-event identity resolution for Raw and Capture paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


_EXPLICIT_ID_KEYS = (
    "native_event_id",
    "native_message_id",
    "message_id",
    "messageId",
    "event_id",
    "eventId",
    "uuid",
    "prompt_message_id",
)


@dataclass(frozen=True)
class NativeEventIdentity:
    """One lossless logical-event identity with an auditable fallback."""

    value: str
    kind: str
    parser: str = ""
    parser_version: str = ""
    source_artifact_id: str = ""
    artifact_offset: str = ""

    @property
    def is_explicit(self) -> bool:
        """Whether the native producer supplied a message/event identifier."""
        return self.kind == "native_event_id"

    @property
    def has_auditable_fallback(self) -> bool:
        """Whether a fallback is concrete enough to replace historical identity."""
        return (
            self.kind == "parser_artifact_offset"
            and bool(self.parser)
            and bool(self.parser_version)
            and bool(self.source_artifact_id)
            and bool(self.artifact_offset)
        )


def _explicit_id(value: Mapping[str, Any]) -> str:
    for key in _EXPLICIT_ID_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, (str, int)) and str(candidate):
            return str(candidate)
    assistant_ids = value.get("assistant_message_ids")
    if isinstance(assistant_ids, (list, tuple)):
        for candidate in assistant_ids:
            if isinstance(candidate, (str, int)) and str(candidate):
                return str(candidate)
    return ""


def resolve_native_event_identity(
    *,
    metadata: Mapping[str, Any] | None,
    raw_event_refs: Sequence[Mapping[str, Any]] | None,
    turn_number: int,
) -> NativeEventIdentity:
    """Prefer an explicit native ID, otherwise return a parser/artifact offset.

    Generic ``id`` values are deliberately excluded: they are often session
    IDs and treating them as a message identity would collapse real turns.
    """
    meta = metadata or {}
    explicit = _explicit_id(meta)
    if explicit:
        return NativeEventIdentity(explicit, "native_event_id")
    for reference in raw_event_refs or ():
        explicit = _explicit_id(reference)
        if explicit:
            return NativeEventIdentity(explicit, "native_event_id")
        nested = reference.get("raw")
        if isinstance(nested, Mapping):
            explicit = _explicit_id(nested)
            if explicit:
                return NativeEventIdentity(explicit, "native_event_id")

    parser = str(meta.get("support_parser") or meta.get("parser_id") or "")
    parser_version = str(
        meta.get("parser_version")
        or meta.get("support_manifest_hash")
        or meta.get("parser_schema_version")
        or ""
    )
    artifact = str(
        meta.get("source_artifact_id")
        or meta.get("native_artifact_id")
        or meta.get("source_path")
        or ""
    )
    offset = str(
        meta.get("native_event_offset")
        or meta.get("artifact_offset")
        or meta.get("parser_offset")
        or (turn_number if artifact else "")
    )
    if not (parser and parser_version and artifact and offset):
        return NativeEventIdentity("", "legacy_turn_number")
    return NativeEventIdentity(
        "parser="
        f"{parser};version={parser_version};artifact={artifact};offset={offset}",
        "parser_artifact_offset",
        parser=parser,
        parser_version=parser_version,
        source_artifact_id=artifact,
        artifact_offset=offset,
    )
