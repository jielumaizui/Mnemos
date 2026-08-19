# -*- coding: utf-8 -*-
"""Durable, privacy-safe coverage state for continuous AgentSource capture.

The daemon owns this state rather than individual parsers.  It records only
source identities, bounded counters, and cursor metadata; native paths and
captured content never enter the heartbeat or the sidecar state file.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core.ops.durable_io import read_native_bytes

SOURCE_COVERAGE_SCHEMA_VERSION = "mnemos.agent_source_coverage.v2"
SOURCE_COVERAGE_FILE_NAME = "agent_source_coverage.json"

_SAFE_CURSOR_KEYS = (
    "kind",
    "tail_sessions_per_source",
    "reconciliation_sessions_per_source",
    "turns_per_session",
    "discovered_sessions",
    "reconciliation_selected_sessions",
    "tail_selected_sessions",
    "raw_committed_turns",
    "advanced_sessions",
    "denominator_complete",
    "denominator_observed_sessions",
    "denominator_turns",
    "denominator_completed_at",
    "capture_generation_id",
    "capture_roster_hash",
    "capture_generation_eligible",
    "capture_expected_turn_count",
    "capture_receipt_count",
    "capture_exact_receipt_count",
    "capture_pending_turn_count",
    "capture_orphan_receipt_count",
    "capture_denominator_session_set_hash",
    "capture_expected_turn_fingerprint_set_hash",
    "capture_receipt_binding_set_hash",
)
_HEARTBEAT_ENTRY_KEYS = (
    "owner",
    "owner_service",
    "trigger",
    "poll_interval_seconds",
    "max_latency_seconds",
    "last_discovery_at",
    "last_capture_at",
    "cursor",
    "status",
    "gap",
    "error",
    "native_sessions",
    "native_turns",
    "captured_sessions",
    "native_source_snapshot_hash",
)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _safe_cursor(cursor: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep cursor identity/counts while excluding paths, content, and tokens."""
    source = _mapping(cursor)
    result: dict[str, Any] = {}
    for key in _SAFE_CURSOR_KEYS:
        value = source.get(key)
        if value is None or isinstance(value, (bool, int, float, str)):
            if value is not None:
                result[key] = value
    return result


def coverage_state_path(database_dir: Path) -> Path:
    """Return the daemon-owned durable state path for source coverage."""
    return Path(database_dir) / SOURCE_COVERAGE_FILE_NAME


def _previous_sources(
    previous: Mapping[str, Any] | None,
    *,
    manifest_hash: str,
) -> Mapping[str, Any]:
    payload = _mapping(previous)
    if (
        payload.get("schema_version") != SOURCE_COVERAGE_SCHEMA_VERSION
        or payload.get("support_manifest_hash") != manifest_hash
    ):
        return {}
    return _mapping(payload.get("sources"))


def _continuity(spec: Any) -> dict[str, Any]:
    continuous = _mapping(getattr(spec, "continuous", {}))
    return {
        "owner": _text(continuous.get("owner")),
        "owner_service": _text(continuous.get("service")),
        "trigger": _text(continuous.get("trigger")),
        "poll_interval_seconds": _nonnegative_int(continuous.get("poll_interval_seconds")),
        "max_latency_seconds": _nonnegative_int(continuous.get("max_latency_seconds")),
    }


def initialize_source_coverage(
    manifest: Any,
    *,
    previous: Mapping[str, Any] | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Seed one bounded coverage entry for every active manifest source."""
    timestamp = observed_at or datetime.now(timezone.utc).isoformat()
    previous_sources = _previous_sources(previous, manifest_hash=manifest.manifest_hash)
    sources: dict[str, dict[str, Any]] = {}
    for name in manifest.active_source_names:
        spec = manifest.require_active_source(name)
        old = _mapping(previous_sources.get(name))
        entry = {
            **_continuity(spec),
            "last_discovery_at": "",
            "last_capture_at": _text(old.get("last_capture_at")),
            "cursor": _safe_cursor(_mapping(old.get("cursor"))),
            "status": "awaiting_discovery",
            "gap": "not_observed_this_cycle",
            "error": "",
            "native_sessions": 0,
            "native_turns": 0,
            "captured_sessions": 0,
            "native_source_snapshot_hash": "",
        }
        sources[name] = entry
    return {
        "schema_version": SOURCE_COVERAGE_SCHEMA_VERSION,
        "support_manifest_hash": manifest.manifest_hash,
        "owner_service": "raw_sync",
        "observed_at": timestamp,
        "sources": sources,
    }


def mark_source_not_detected(
    coverage: Mapping[str, Any],
    source_name: str,
    *,
    observed_at: str,
) -> None:
    """Record that the scheduled owner checked but found no native root."""
    sources = _mapping(coverage.get("sources"))
    entry = sources.get(source_name)
    if not isinstance(entry, dict):
        return
    entry.update(
        {
            "last_discovery_at": observed_at,
            "status": "not_detected",
            "gap": "native_root_not_detected",
            "error": "",
            "native_sessions": 0,
            "native_turns": 0,
            "captured_sessions": 0,
            "native_source_snapshot_hash": "",
        }
    )


def record_source_observation(
    coverage: Mapping[str, Any],
    source_name: str,
    *,
    observed_at: str,
    cursor: Mapping[str, Any],
    native_sessions: int,
    native_turns: int,
    captured_sessions: int,
    native_source_snapshot_hash: str = "",
    error: Exception | None = None,
) -> None:
    """Record one source scan outcome without storing native path/content."""
    sources = _mapping(coverage.get("sources"))
    entry = sources.get(source_name)
    if not isinstance(entry, dict):
        return
    entry.update(
        {
            "last_discovery_at": observed_at,
            "cursor": _safe_cursor(cursor),
            "native_sessions": _nonnegative_int(native_sessions),
            "native_turns": _nonnegative_int(native_turns),
            "captured_sessions": _nonnegative_int(captured_sessions),
            "native_source_snapshot_hash": _text(native_source_snapshot_hash),
        }
    )
    if error is not None:
        entry.update({"status": "error", "gap": "source_error", "error": error.__class__.__name__})
        return
    if captured_sessions:
        entry.update(
            {
                "last_capture_at": observed_at,
                "status": "captured",
                "gap": "none",
                "error": "",
            }
        )
        return
    gap = "no_native_turns" if native_turns == 0 else "no_eligible_turns"
    entry.update({"status": "observed_empty", "gap": gap, "error": ""})


class SourceCoverageStateError(RuntimeError):
    """Existing durable coverage state could not be interpreted safely."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def load_source_coverage_state(path: Path) -> dict[str, Any]:
    """Read durable coverage state without provisioning paths or mutating state."""
    target = Path(path)
    try:
        metadata = target.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise SourceCoverageStateError("source_coverage_state_symlink")
        if not stat.S_ISREG(metadata.st_mode):
            raise SourceCoverageStateError("source_coverage_state_unreadable")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SourceCoverageStateError("source_coverage_state_unreadable") from exc
    try:
        payload = json.loads(read_native_bytes(target).decode("utf-8"))
    except OSError as exc:
        raise SourceCoverageStateError("source_coverage_state_unreadable") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise SourceCoverageStateError("source_coverage_state_malformed") from exc
    if not isinstance(payload, dict):
        raise SourceCoverageStateError("source_coverage_state_not_object")
    if payload.get("schema_version") != SOURCE_COVERAGE_SCHEMA_VERSION:
        raise SourceCoverageStateError("source_coverage_schema_unsupported")
    if not isinstance(payload.get("sources"), dict):
        raise SourceCoverageStateError("source_coverage_sources_invalid")
    return payload


def write_source_coverage_state(path: Path, coverage: Mapping[str, Any]) -> None:
    """Atomically persist a sanitized coverage state after a daemon scan."""
    from core.utils import atomic_write_text

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        target,
        json.dumps(coverage, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.chmod(target, 0o600)


def source_coverage_for_heartbeat(coverage: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project coverage into the daemon heartbeat using an explicit safe schema."""
    if coverage is None:
        return {}
    payload = _mapping(coverage)
    if payload.get("schema_version") != SOURCE_COVERAGE_SCHEMA_VERSION:
        raise SourceCoverageStateError("source_coverage_heartbeat_schema_unsupported")
    if not isinstance(payload.get("sources"), Mapping):
        raise SourceCoverageStateError("source_coverage_heartbeat_sources_invalid")
    source_entries = _mapping(payload.get("sources"))
    sources: dict[str, dict[str, Any]] = {}
    for source_name, value in sorted(source_entries.items()):
        if not isinstance(source_name, str) or not source_name:
            raise SourceCoverageStateError("source_coverage_heartbeat_source_name_invalid")
        if not isinstance(value, Mapping):
            raise SourceCoverageStateError("source_coverage_heartbeat_entry_invalid")
        entry = value
        safe_entry: dict[str, Any] = {}
        for key in _HEARTBEAT_ENTRY_KEYS:
            if key == "cursor":
                cursor = entry.get(key)
                if cursor is not None and not isinstance(cursor, Mapping):
                    raise SourceCoverageStateError("source_coverage_heartbeat_cursor_invalid")
                cursor_mapping = _mapping(cursor)
                if any(
                    cursor_key in cursor_mapping
                    and not isinstance(
                        cursor_mapping[cursor_key],
                        (bool, int, float, str),
                    )
                    for cursor_key in _SAFE_CURSOR_KEYS
                ):
                    raise SourceCoverageStateError("source_coverage_heartbeat_cursor_invalid")
                safe_entry[key] = _safe_cursor(cursor_mapping)
            elif key in {
                "native_sessions",
                "native_turns",
                "captured_sessions",
                "poll_interval_seconds",
                "max_latency_seconds",
            }:
                count = entry.get(key)
                if count is not None and (
                    not isinstance(count, int) or isinstance(count, bool) or count < 0
                ):
                    raise SourceCoverageStateError("source_coverage_heartbeat_count_invalid")
                safe_entry[key] = _nonnegative_int(count)
            else:
                text = entry.get(key)
                if text is not None and not isinstance(text, str):
                    raise SourceCoverageStateError("source_coverage_heartbeat_text_invalid")
                safe_entry[key] = _text(text)
        sources[str(source_name)] = safe_entry
    for key in ("support_manifest_hash", "owner_service", "observed_at"):
        value = payload.get(key)
        if value is not None and not isinstance(value, str):
            raise SourceCoverageStateError("source_coverage_heartbeat_header_invalid")
    from core.agent_kit.source_support_manifest import (
        get_agent_source_support_manifest,
    )

    manifest = get_agent_source_support_manifest()
    if payload.get("support_manifest_hash") != manifest.manifest_hash:
        raise SourceCoverageStateError("source_coverage_heartbeat_manifest_mismatch")
    if set(sources) != set(manifest.active_source_names):
        raise SourceCoverageStateError("source_coverage_heartbeat_denominator_incomplete")
    return {
        "schema_version": SOURCE_COVERAGE_SCHEMA_VERSION,
        "support_manifest_hash": _text(payload.get("support_manifest_hash")),
        "owner_service": _text(payload.get("owner_service")),
        "observed_at": _text(payload.get("observed_at")),
        "sources": sources,
    }
