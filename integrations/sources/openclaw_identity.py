"""Stable identity and equivalence rules for OpenClaw native artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from core.sync_framework.agent_source import Turn

NON_EQUIVALENCE_RAW_REF_TYPES = frozenset(
    {
        "normal_message_provenance",
        "normal_event_provenance",
        "normal_data_provenance",
        "trajectory_event_provenance",
        "trajectory_workspace_provenance",
        "corpus_line_provenance",
    }
)
CORPUS_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class OpenClawSessionCandidate:
    """One native artifact considered for canonical session discovery."""

    source_path: Path
    native_session_id: str
    source_kind: str
    artifact_id: str
    content_hash: str
    turn_fingerprints: tuple[str, ...]
    turn_count: int
    mtime: float


def artifact_id(path: Path, source_kind: str) -> str:
    """Return an opaque artifact identity without persisting a raw path."""
    try:
        material = str(path.resolve(strict=False))
    except OSError:
        material = str(path)
    digest = hashlib.sha256(
        f"{source_kind}\0{material}".encode("utf-8")
    ).hexdigest()[:20]
    return f"openclaw-artifact-{digest}"


def turn_fingerprint(turn: Turn) -> str:
    """Hash semantic payload while excluding location-only provenance."""
    semantic_refs = [
        ref
        for ref in turn.raw_event_refs
        if ref.get("event_type") not in NON_EQUIVALENCE_RAW_REF_TYPES
    ]
    semantic_metadata = {
        key: value
        for key, value in (turn.metadata or {}).items()
        if key
        not in {
            "session_id",
            "sessionId",
            "timestamp",
            "workspaceDir",
            "working_dir",
        }
    }
    payload = {
        "user": turn.user_content,
        "assistant": turn.assistant_content,
        "tool_calls": turn.tool_calls,
        "tool_results": turn.tool_results,
        "reasoning": turn.reasoning,
        "attachments": turn.attachments,
        "raw_event_refs": semantic_refs,
        "metadata": semantic_metadata,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def source_kind_for_path(path: Path) -> str:
    """Infer the manifest variant from one already discovered native path."""
    if path.name.endswith(".trajectory.jsonl"):
        return "trajectory"
    if path.suffix == ".txt":
        return "corpus"
    return "normal_jsonl"


def path_session_id(path: Path) -> str:
    """Return the filename fallback for a missing native session id."""
    if path.name.endswith(".trajectory.jsonl"):
        return path.name.removesuffix(".trajectory.jsonl")
    return path.name.removesuffix(".jsonl")


def is_turn_prefix(prefix: tuple[str, ...], sequence: tuple[str, ...]) -> bool:
    """Return whether one semantic turn fingerprint sequence prefixes another."""
    return len(prefix) <= len(sequence) and sequence[: len(prefix)] == prefix


def corpus_timestamp(path: Path) -> str:
    """Return the stable day-bucket timestamp for a corpus artifact."""
    if CORPUS_DATE_RE.match(path.stem):
        return f"{path.stem}T00:00:00"
    return ""
