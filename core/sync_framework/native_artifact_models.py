"""Typed evidence and immutable adapters for native artifact generations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping

from core.sync_framework.agent_source import (
    SessionInfo,
    SessionParseResult,
    Turn,
    canonicalize_session_info,
)

INVENTORY_SCHEMA_VERSION = "mnemos.native_artifact_inventory.v1"
DEFAULT_SNAPSHOT_PARSE_INFRASTRUCTURE_ATTEMPTS = 2
SNAPSHOT_PARSE_WORKER_RETRYABLE_ERROR_CODES = frozenset(
    {
        "native_freeze_worker_failed",
        "native_freeze_worker_setup_failed",
        "native_freeze_worker_signaled",
    }
)
SNAPSHOT_PARSE_STORAGE_RETRYABLE_ERROR_CODES = frozenset(
    {"native_session_parser_retryable_exception"}
)
SNAPSHOT_PARSE_RETRYABLE_ERROR_CODES = (
    SNAPSHOT_PARSE_WORKER_RETRYABLE_ERROR_CODES | SNAPSHOT_PARSE_STORAGE_RETRYABLE_ERROR_CODES
)
SNAPSHOT_PARSE_CHILD_REPORTED_ERROR_CODES = frozenset(
    {
        "native_artifact_drift_during_freeze",
        "native_freeze_budget_exceeded",
        "native_session_parser_exception",
        "native_session_parser_retryable_exception",
    }
)
SNAPSHOT_PARSE_TERMINAL_ERROR_CODES = (
    frozenset(
        {
            "native_freeze_worker_budget_exceeded",
            "native_freeze_worker_unavailable",
            "native_parse_recovery_evidence_invalid",
            "native_parse_terminal_error_unregistered",
            "native_snapshot_registry_unavailable",
            "snapshot_session_identity_missing",
        }
    )
    | SNAPSHOT_PARSE_RETRYABLE_ERROR_CODES
    | SNAPSHOT_PARSE_CHILD_REPORTED_ERROR_CODES
)
SNAPSHOT_PARSE_RECOVERY_EVIDENCE_KEYS = frozenset(
    {
        "error_code",
        "exception_type",
        "failure_class",
        "os_errno",
        "reason_code",
        "signal",
        "sqlite_errorcode",
        "sqlite_errorname",
    }
)
SNAPSHOT_PARSE_TERMINAL_EVIDENCE_KEYS = SNAPSHOT_PARSE_RECOVERY_EVIDENCE_KEYS | frozenset(
    {
        "attempt_count",
        "session_id_hash",
        "source_name",
    }
)


def _snapshot_parse_evidence_fields_are_valid(
    evidence: Mapping[str, Any],
    *,
    allowed_error_codes: frozenset[str],
) -> bool:
    error_code = evidence.get("error_code")
    failure_class = evidence.get("failure_class")
    signal_value = evidence.get("signal")
    return not (
        error_code not in allowed_error_codes
        or any(
            not isinstance(evidence.get(key), str)
            or re.fullmatch(r"[a-z][a-z0-9_]{2,127}", evidence[key]) is None
            for key in ("error_code", "reason_code")
            if key in evidence
        )
        or (
            "exception_type" in evidence
            and (
                not isinstance(evidence["exception_type"], str)
                or re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_.]{0,127}",
                    evidence["exception_type"],
                )
                is None
            )
        )
        or (
            failure_class is not None
            and failure_class
            not in {
                "os_nontransient",
                "os_transient",
                "sqlite_nontransient",
                "sqlite_transient",
                "storage_untyped",
            }
        )
        or (
            error_code in SNAPSHOT_PARSE_STORAGE_RETRYABLE_ERROR_CODES
            and failure_class not in {"os_transient", "sqlite_transient"}
        )
        or (
            "sqlite_errorname" in evidence
            and (
                not isinstance(evidence["sqlite_errorname"], str)
                or re.fullmatch(
                    r"SQLITE_[A-Z0-9_]{1,96}",
                    evidence["sqlite_errorname"],
                )
                is None
            )
        )
        or any(
            isinstance(evidence.get(key), bool) or not isinstance(evidence.get(key), int)
            for key in ("os_errno", "signal", "sqlite_errorcode")
            if key in evidence
        )
        or (
            error_code == "native_freeze_worker_signaled"
            and (
                not isinstance(signal_value, int)
                or isinstance(signal_value, bool)
                or not 1 <= signal_value <= 127
            )
        )
        or (error_code != "native_freeze_worker_signaled" and signal_value is not None)
    )


def snapshot_parse_recovery_evidence_is_valid(
    value: Mapping[str, Any],
) -> bool:
    """Validate the exact typed evidence emitted after one recovered retry."""

    evidence = dict(value)
    return not (
        not set(evidence).issubset(SNAPSHOT_PARSE_RECOVERY_EVIDENCE_KEYS)
        or not _snapshot_parse_evidence_fields_are_valid(
            evidence,
            allowed_error_codes=SNAPSHOT_PARSE_RETRYABLE_ERROR_CODES,
        )
    )


def snapshot_parse_terminal_evidence_is_valid(
    value: Mapping[str, Any],
) -> bool:
    """Validate one content-free terminal parse failure from the challenger."""

    evidence = dict(value)
    attempt_count = evidence.get("attempt_count")
    source_name = evidence.get("source_name")
    session_id_hash = evidence.get("session_id_hash")
    details = {
        key: item
        for key, item in evidence.items()
        if key not in {"attempt_count", "session_id_hash", "source_name"}
    }
    return not (
        not set(evidence).issubset(SNAPSHOT_PARSE_TERMINAL_EVIDENCE_KEYS)
        or not isinstance(attempt_count, int)
        or isinstance(attempt_count, bool)
        or not 1 <= attempt_count <= DEFAULT_SNAPSHOT_PARSE_INFRASTRUCTURE_ATTEMPTS
        or (
            details.get("error_code") in SNAPSHOT_PARSE_RETRYABLE_ERROR_CODES
            and attempt_count != DEFAULT_SNAPSHOT_PARSE_INFRASTRUCTURE_ATTEMPTS
        )
        or (
            details.get("error_code") == "native_parse_recovery_evidence_invalid"
            and attempt_count != 1
        )
        or (
            details.get("error_code")
            in {
                "native_parse_recovery_evidence_invalid",
                "native_parse_terminal_error_unregistered",
            }
            and "reason_code" not in details
        )
        or not isinstance(source_name, str)
        or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", source_name) is None
        or not isinstance(session_id_hash, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", session_id_hash) is None
        or not _snapshot_parse_evidence_fields_are_valid(
            details,
            allowed_error_codes=SNAPSHOT_PARSE_TERMINAL_ERROR_CODES,
        )
    )


class NativeArtifactInventoryError(RuntimeError):
    """Fail-closed native inventory construction error."""

    def __init__(
        self,
        code: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = str(code)
        self.details = dict(details or {})
        super().__init__(self.code)


@dataclass(frozen=True)
class NativeArtifactEvidence:
    """One parser input with opaque identity and content binding."""

    source_name: str
    canonical_session_id: str
    artifact_identity_hash: str
    content_hash: str
    logical_size_bytes: int
    hash_contract: str

    def to_dict(self) -> dict[str, Any]:
        """Return content-free evidence safe for plans and receipts."""
        return {
            "source_name": self.source_name,
            "canonical_session_id": self.canonical_session_id,
            "artifact_identity_hash": self.artifact_identity_hash,
            "content_hash": self.content_hash,
            "logical_size_bytes": self.logical_size_bytes,
            "hash_contract": self.hash_contract,
        }


@dataclass(frozen=True)
class NativeSourceEvidence:
    """Content-free source/root roster evidence, including zero-session sources."""

    source_name: str
    root_identity_hashes: tuple[str, ...]
    session_count: int
    artifact_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "root_identity_hashes": list(self.root_identity_hashes),
            "session_count": self.session_count,
            "artifact_count": self.artifact_count,
        }


@dataclass(frozen=True)
class NativeArtifactInventory:
    """Canonical inventory shared by planning, freezing, and verification."""

    entries: tuple[NativeArtifactEvidence, ...]
    sources: tuple[NativeSourceEvidence, ...]
    inventory_hash: str

    @property
    def path_hashes(self) -> tuple[str, ...]:
        """Return only opaque artifact identities, never native paths."""
        return tuple(entry.artifact_identity_hash for entry in self.entries)

    def to_evidence(self) -> dict[str, Any]:
        """Return the complete content-free plan binding."""
        return {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "inventory_hash": self.inventory_hash,
            "source_count": len(self.sources),
            "artifact_count": len(self.entries),
            "sources": [source.to_dict() for source in self.sources],
            "entries": [entry.to_dict() for entry in self.entries],
        }


class FrozenAgentSource:
    """In-memory adapter that cannot observe later native artifact changes."""

    def __init__(
        self,
        source: Any,
        sessions: list[SessionInfo],
        results: Mapping[str, SessionParseResult],
        roots: tuple[Path, ...],
    ):
        self._source = source
        self._sessions = tuple(sessions)
        self._results = dict(results)
        self._roots = roots
        self.name = str(source.name)
        self.model_tag = str(source.model_tag)

    def discover_sessions(self) -> list[SessionInfo]:
        return list(self._sessions)

    def parse_session(self, session_info: SessionInfo) -> list[Turn]:
        return list(self.parse_session_result(session_info).turns)

    def parse_session_result(
        self,
        session_info: SessionInfo,
    ) -> SessionParseResult:
        canonical = canonicalize_session_info(session_info)
        result = self._results.get(canonical.session_id)
        if result is None:
            raise NativeArtifactInventoryError("frozen_session_identity_required")
        return result

    def _framework_bound_session_artifact_evidence_hash(
        self,
        session_info: SessionInfo,
    ) -> str:
        """Return only framework-produced evidence for this generation."""
        canonical = canonicalize_session_info(session_info)
        result = self._results.get(canonical.session_id)
        if result is None:
            raise NativeArtifactInventoryError("frozen_session_identity_required")
        return str(result.artifact_evidence_hash)

    def parse_turns(self, session_path: Path) -> list[Turn]:
        matches = [session for session in self._sessions if session.source_path == session_path]
        if len(matches) != 1:
            raise NativeArtifactInventoryError("frozen_session_identity_required")
        return self.parse_session(matches[0])

    @property
    def data_dir(self) -> Path:
        if not self._roots:
            raise NativeArtifactInventoryError("native_root_unresolvable")
        return self._roots[0]

    def observed_roots(self) -> list[Path]:
        return list(self._roots)

    def completeness_capabilities(self) -> dict[str, Any]:
        return dict(self._source.completeness_capabilities())

    def build_extra_tags(self, turn: Turn) -> list[str]:
        return list(self._source.build_extra_tags(turn))


@dataclass(frozen=True)
class FrozenNativeSourceSet:
    """Parsed immutable source adapters plus the exact input inventory."""

    sources: tuple[Any, ...]
    inventory: NativeArtifactInventory
    frozen_turn_count: int
    estimated_bytes: int
    max_bytes: int
    max_turns: int
    preparse_logical_bytes: int

    def freeze_evidence(self) -> dict[str, int | str]:
        return {
            "schema_version": "mnemos.native_source_freeze_budget.v1",
            "frozen_turn_count": self.frozen_turn_count,
            "estimated_bytes": self.estimated_bytes,
            "max_bytes": self.max_bytes,
            "max_turns": self.max_turns,
            "preparse_logical_bytes": self.preparse_logical_bytes,
            "parser_isolation": "fork-rss-monitored-private-spool-v1",
        }


@dataclass(frozen=True)
class SnapshotNativeSourceSet:
    """Disk-backed immutable parser inputs for large recovery generations."""

    sources: tuple[Any, ...]
    inventory: NativeArtifactInventory
    snapshot_logical_bytes: int
    snapshot_artifact_count: int
    max_session_logical_bytes: int
    max_session_parse_bytes: int
    max_session_turns: int
    stale_snapshot_dirs_cleaned: int
    stabilization_attempts: int = 1

    def snapshot_evidence(self) -> dict[str, int | str]:
        return {
            "schema_version": "mnemos.native_source_artifact_snapshot.v4",
            "parser_isolation": "plan-bound-private-artifact-snapshot-v4",
            "snapshot_logical_bytes": self.snapshot_logical_bytes,
            "snapshot_artifact_count": self.snapshot_artifact_count,
            "snapshot_permissions": "directory-0700-files-0600",
            "sqlite_snapshot_journal_mode": "delete",
            "sqlite_snapshot_sidecar_count": 0,
            "max_session_logical_bytes": self.max_session_logical_bytes,
            "max_session_parse_bytes": self.max_session_parse_bytes,
            "max_session_turns": self.max_session_turns,
            "parse_materialization": ("fork-rss-monitored-turn-stream-private-spool-v3"),
            "parser_private_temp_contract": (
                "generic-temp-bound-to-private-parse-spool-sqlite-memory-v2"
            ),
            "native_sqlite_temp_store": ("connection-local-memory-with-session-rss-budget-v1"),
            "challenger_identity_materialization": (
                "per-session-exit-reclaimed-content-free-pipe-v1"
            ),
            "parse_infrastructure_attempts": (DEFAULT_SNAPSHOT_PARSE_INFRASTRUCTURE_ATTEMPTS),
            "crash_cleanup": "owner-identity-registry-next-run-reap-v1",
            "stale_snapshot_dirs_cleaned": self.stale_snapshot_dirs_cleaned,
            "stabilization_attempts": self.stabilization_attempts,
        }
