"""Dependency-neutral constants and path validation for Raw projection."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any

PROJECTION_CONTRACT = "lossless-visible-v1"
PROJECTION_JOURNAL_NAME = ".mnemos_raw_projection_journal.json"
PROJECTION_TRANSACTION_DIR = ".mnemos_raw_projection_transaction"
PROJECTION_TRANSACTION_TOMBSTONE_PREFIX = (
    f"{PROJECTION_TRANSACTION_DIR}.removing."
)
PROJECTION_TRANSACTION_SCHEMA = "mnemos.raw_projection_transaction.v1"
PROJECTION_TRANSACTION_STATE_SCHEMA = (
    "mnemos.raw_projection_transaction_state.v1"
)
_REVISION_ID_PATTERN = re.compile(r"rawrev-[0-9a-f]{40}")
_SOURCE_PATTERN = re.compile(r"[\w:.$/-]{1,64}")
_SESSION_PATTERN = re.compile(r"[\w:.$/-]{1,256}")
_COMPLETENESS_PATTERN = re.compile(r"[\w-]{0,64}")
_EVENT_HEADER_PATTERN = re.compile(
    r"\A## Turn (?P<turn_number>[1-9][0-9]*)\n\n"
    r"- event_id: `(?P<event_id>rawrev-[0-9a-f]{40})`\n"
    r"- captured_at: (?P<captured_at>[^\n]+)\n"
    r"- conversation_at: (?P<conversation_at>[^\n]+)\n"
    r"- completeness: (?P<completeness_status>[^\n]+)\n"
    r"- survival_score: `(?P<survival_score>[0-9]+(?:\.[0-9]{2})?)`\n"
    r"- search/result/hit/reference: "
    r"`(?P<search_count>[0-9]+)/(?P<result_count>[0-9]+)/"
    r"(?P<hit_count>[0-9]+)/(?P<reference_count>[0-9]+)`\n\Z"
)


def _header_scalar(value: object) -> str:
    """Encode an opaque scalar without giving its bytes structural meaning."""
    return json.dumps(
        str(value or ""),
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _decode_compat_header_scalar(value: str) -> str:
    """Decode current JSON scalars and safe pre-v2 single-backtick scalars."""
    if value.startswith("`") and value.endswith("`"):
        legacy = value[1:-1]
        if "`" in legacy or "\n" in legacy or "\r" in legacy:
            raise ValueError("Raw projection event header scalar is malformed")
        return legacy
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Raw projection event header scalar is malformed") from exc
    if not isinstance(decoded, str) or _header_scalar(decoded) != value:
        raise ValueError("Raw projection event header scalar is noncanonical")
    return decoded


def validate_projection_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate identity fields and preserve delimiter-hostile timestamp bytes."""
    source_agent = payload.get("source_agent")
    session_id = payload.get("session_id")
    turn_number = payload.get("turn_number")
    captured_at = str(payload.get("captured_at") or "")
    conversation_at = str(payload.get("conversation_at") or "")
    completeness_status = str(payload.get("completeness_status") or "")
    if (
        not isinstance(source_agent, str)
        or _SOURCE_PATTERN.fullmatch(source_agent) is None
        or not isinstance(session_id, str)
        or _SESSION_PATTERN.fullmatch(session_id) is None
        or type(turn_number) is not int
        or turn_number < 0
        or len(captured_at) > 64
        or len(conversation_at) > 64
        or _COMPLETENESS_PATTERN.fullmatch(completeness_status) is None
    ):
        raise ValueError("raw revision has invalid projection provenance metadata")
    return {
        "source_agent": source_agent,
        "session_id": session_id,
        "turn_number": turn_number + 1,
        "captured_at": captured_at,
        "conversation_at": conversation_at,
        "completeness_status": completeness_status,
    }


def projection_timestamp_path_segment(value: object) -> str:
    """Return a traversal-safe, deterministic segment without losing header bytes."""
    timestamp = str(value or "")
    candidate = (timestamp[:10] if timestamp else "unknown-date").replace(
        ":",
        "-",
    )
    allowed = [
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in candidate
    ]
    segment = "".join(allowed).strip("-") or "unknown"
    while "--" in segment:
        segment = segment.replace("--", "-")
    return segment[:16]


def render_projection_event_header(
    *,
    event_id: str,
    projection_metadata: dict[str, Any],
    survival_score: float,
    search_count: int,
    result_count: int,
    hit_count: int,
    reference_count: int,
) -> str:
    """Render the canonical delimiter-safe event header."""
    if _REVISION_ID_PATTERN.fullmatch(event_id) is None:
        raise ValueError("Raw projection event header has an invalid event id")
    turn_number = projection_metadata.get("turn_number")
    if type(turn_number) is not int or turn_number < 1:
        raise ValueError("Raw projection event header has an invalid turn number")
    counts = (search_count, result_count, hit_count, reference_count)
    if any(type(value) is not int or value < 0 for value in counts):
        raise ValueError("Raw projection event header has invalid reference counts")
    if (
        isinstance(survival_score, bool)
        or not isinstance(survival_score, (int, float))
        or not math.isfinite(float(survival_score))
        or float(survival_score) < 0
    ):
        raise ValueError("Raw projection event header has an invalid survival score")
    header = (
        f"## Turn {turn_number}\n\n"
        f"- event_id: `{event_id}`\n"
        f"- captured_at: {_header_scalar(projection_metadata.get('captured_at'))}\n"
        "- conversation_at: "
        f"{_header_scalar(projection_metadata.get('conversation_at'))}\n"
        "- completeness: "
        f"{_header_scalar(projection_metadata.get('completeness_status'))}\n"
        f"- survival_score: `{float(survival_score):.2f}`\n"
        "- search/result/hit/reference: "
        f"`{search_count}/{result_count}/{hit_count}/{reference_count}`\n"
    )
    observed = parse_projection_event_header(header.encode("utf-8"))
    expected = {
        "event_id": event_id,
        "turn_number": turn_number,
        "captured_at": str(projection_metadata.get("captured_at") or ""),
        "conversation_at": str(projection_metadata.get("conversation_at") or ""),
        "completeness_status": str(
            projection_metadata.get("completeness_status") or ""
        ),
        "survival_score": float(f"{float(survival_score):.2f}"),
        "search_count": search_count,
        "result_count": result_count,
        "hit_count": hit_count,
        "reference_count": reference_count,
    }
    if observed != expected:
        raise RuntimeError("Raw projection event header is not reversibly encoded")
    return header


def parse_projection_event_header(raw: bytes) -> dict[str, Any]:
    """Parse one exact event header without trusting Markdown delimiters."""
    if len(raw) > 4096:
        raise ValueError("Raw projection event header is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Raw projection event header is not UTF-8") from exc
    match = _EVENT_HEADER_PATTERN.fullmatch(text)
    if match is None:
        raise ValueError("Raw projection event header is malformed")
    return {
        "event_id": match.group("event_id"),
        "turn_number": int(match.group("turn_number")),
        "captured_at": _decode_compat_header_scalar(match.group("captured_at")),
        "conversation_at": _decode_compat_header_scalar(
            match.group("conversation_at")
        ),
        "completeness_status": _decode_compat_header_scalar(
            match.group("completeness_status")
        ),
        "survival_score": float(match.group("survival_score")),
        "search_count": int(match.group("search_count")),
        "result_count": int(match.group("result_count")),
        "hit_count": int(match.group("hit_count")),
        "reference_count": int(match.group("reference_count")),
    }


def _projection_path_kind(path: Path) -> str:
    """Inspect one physical projection path without following links."""

    try:
        metadata = Path(path).lstat()
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        raise ValueError(
            "Raw projection path inspection is unavailable"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    return "other"


def safe_projection_target(
    raw_dir: Path,
    relative_path: str | Path,
) -> Path:
    """Resolve a relative projection path without crossing a symlink boundary."""
    root = Path(raw_dir)
    relative = Path(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("Raw projection path is unsafe")
    absolute_root = Path(os.path.abspath(root))
    inspected = Path(absolute_root.anchor)
    for part in absolute_root.parts[1:]:
        inspected = inspected / part
        kind = _projection_path_kind(inspected)
        if kind == "symlink":
            raise ValueError("Raw projection vault root is unsafe")
        if inspected != absolute_root and kind not in {
            "missing",
            "directory",
        }:
            raise ValueError("Raw projection vault root is unsafe")
        if inspected == absolute_root and kind not in {
            "missing",
            "directory",
        }:
            raise ValueError("Raw projection vault root is unsafe")

    inspected = absolute_root
    for index, part in enumerate(relative.parts):
        inspected = inspected / part
        kind = _projection_path_kind(inspected)
        if kind == "symlink":
            raise ValueError(
                "Raw projection path has a symlinked component and is unsafe"
            )
        if index < len(relative.parts) - 1 and kind not in {
            "missing",
            "directory",
        }:
            raise ValueError("Raw projection path is unsafe")
    current = root / relative
    try:
        resolved_root = absolute_root.resolve(strict=False)
        resolved_target = Path(os.path.abspath(current)).resolve(strict=False)
        resolved_target.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(
            "Raw projection path escapes its vault and is unsafe"
        ) from exc
    return current


__all__ = [
    "PROJECTION_CONTRACT",
    "PROJECTION_JOURNAL_NAME",
    "PROJECTION_TRANSACTION_DIR",
    "PROJECTION_TRANSACTION_SCHEMA",
    "PROJECTION_TRANSACTION_STATE_SCHEMA",
    "PROJECTION_TRANSACTION_TOMBSTONE_PREFIX",
    "parse_projection_event_header",
    "projection_timestamp_path_segment",
    "render_projection_event_header",
    "safe_projection_target",
    "validate_projection_metadata",
]
