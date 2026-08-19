"""Manifest-bound metadata construction for native AgentSource captures."""

from __future__ import annotations

from typing import Any, Dict

from core.agent_kit.source_support_manifest import bind_source_support_metadata

from .agent_source import (
    TURN_STRUCTURED_METADATA_KEYS,
    AgentSource,
    SessionInfo,
    Turn,
    canonicalize_session_info,
)
from .native_event_identity import resolve_native_event_identity


SOURCE_CAPABILITY_RECOVERABLE_ERRORS = (
    OSError,
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
    RuntimeError,
)


def build_native_raw_metadata(
    source: AgentSource,
    session_info: SessionInfo,
    turn: Turn,
) -> Dict[str, Any]:
    """Build and bind the native-source Raw contract before any upsert."""
    metadata = {
        key: value
        for key, value in dict(session_info.metadata or {}).items()
        if key not in TURN_STRUCTURED_METADATA_KEYS
    }
    metadata.update(
        {
            key: value
            for key, value in dict(turn.metadata or {}).items()
            if key not in TURN_STRUCTURED_METADATA_KEYS
        }
    )
    if turn.native_event_id:
        metadata.setdefault("native_event_id", turn.native_event_id)
    identity = resolve_native_event_identity(
        metadata=metadata,
        raw_event_refs=turn.raw_event_refs,
        turn_number=turn.turn_number,
    )
    if identity.is_explicit:
        metadata.setdefault("native_event_id", identity.value)
    canonical_id = canonicalize_session_info(session_info).session_id
    metadata["canonical_session_id"] = canonical_id
    metadata["source_session_id"] = session_info.session_id
    aliases = list(session_info.session_aliases or [])
    if session_info.canonical_session_id and session_info.session_id != canonical_id:
        aliases.insert(0, session_info.session_id)
    if aliases:
        metadata["session_aliases"] = list(dict.fromkeys(aliases))
    if session_info.source_kind:
        metadata["source_kind"] = session_info.source_kind
    if session_info.working_dir:
        metadata.setdefault("working_dir", session_info.working_dir)
    try:
        capabilities = source.completeness_capabilities()
    except SOURCE_CAPABILITY_RECOVERABLE_ERRORS:
        capabilities = {}
    if capabilities:
        metadata.setdefault("source_capabilities", capabilities)
        fidelity = capabilities.get("source_fidelity")
        if fidelity in ("full", True):
            metadata.setdefault("source_fidelity", "full")
        elif fidelity in ("derived", "experimental"):
            metadata.setdefault("source_fidelity", fidelity)
        elif fidelity is False:
            metadata.setdefault("source_fidelity", "unknown")
    return bind_source_support_metadata(
        metadata,
        source.name,
        require_declared=True,
    )
