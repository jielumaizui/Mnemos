"""Artifact ownership and failure projection support for ``SyncEngine``."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from core.evidence.artifact_capture import (
    build_reasoning_artifact_content,
    managed_capture_artifact_sha256,
    read_historical_capture_artifact_bytes,
    require_capture_turn_number,
    write_managed_capture_artifact,
)
from core.sync_framework.agent_source import (
    AgentSource,
    SessionInfo,
    SyncResult,
    Turn,
)
from core.sync_framework.storage_backend import (
    StorageAuthError,
    StorageRateLimitError,
    StorageServerError,
)

logger = logging.getLogger(__name__)


class CanonicalRawCommitError(RuntimeError):
    """Raised when a turn cannot obtain its required canonical Raw receipt."""


def _configured_database_dir(config: Any) -> Path | None:
    """Resolve only an explicit database owner from config-like inputs."""

    for name in ("database_dir", "mnemos_dir", "data_dir"):
        value = getattr(config, name, None)
        if isinstance(value, (str, Path)):
            return Path(value).expanduser()
    if isinstance(config, Mapping):
        for name in ("database_dir", "mnemos_dir", "data_dir"):
            value = config.get(name)
            if isinstance(value, (str, Path)):
                return Path(value).expanduser()
    return None


class SyncEngineArtifactSupportMixin:
    """Own capture/reasoning artifacts and typed failure projections."""

    config: Any
    db_path: Path
    _record_sync: Callable[..., None]

    def _ensure_reasoning_artifact(
        self,
        turn: Turn,
        source_agent: str,
        session_id: str,
    ) -> None:
        """Own and verify every local artifact before it reaches Raw or Wiki."""

        require_capture_turn_number(turn.turn_number)
        database_dir = _configured_database_dir(self.config) or self.db_path.parent
        metadata = dict(turn.metadata or {})
        metadata.pop("capture_artifact_sha256", None)
        metadata.pop("reasoning_sha256", None)
        capture_path = str(metadata.get("artifact_path") or "")
        if capture_path:
            managed_capture_digest = managed_capture_artifact_sha256(
                database_dir=database_dir,
                source_agent=source_agent,
                session_id=session_id,
                turn_number=turn.turn_number,
                artifact_type="capture",
                path=capture_path,
            )
            if not managed_capture_digest:
                legacy_content = read_historical_capture_artifact_bytes(
                    database_dir=database_dir,
                    session_id=session_id,
                    turn_number=turn.turn_number,
                    artifact_type="capture",
                    path=capture_path,
                )
                if legacy_content is None:
                    raise CanonicalRawCommitError("capture_artifact_reference_untrusted")
                try:
                    legacy_text = legacy_content.decode("utf-8")
                except UnicodeError:
                    raise CanonicalRawCommitError("capture_artifact_reference_unreadable") from None
                metadata["artifact_path"] = str(
                    write_managed_capture_artifact(
                        database_dir=database_dir,
                        source_agent=source_agent,
                        session_id=session_id,
                        turn_number=turn.turn_number,
                        artifact_type="capture",
                        content=legacy_text,
                    )
                )
                managed_capture_digest = hashlib.sha256(legacy_content).hexdigest()
                supplied_refs = metadata.get("artifact_refs")
                if isinstance(supplied_refs, list):
                    metadata["artifact_refs"] = [
                        ref
                        for ref in supplied_refs
                        if not (
                            isinstance(ref, dict) and ref.get("artifact_type") == "capture_artifact"
                        )
                    ]
            metadata["capture_artifact_sha256"] = managed_capture_digest

        mode = self.config.get(
            "capture.reasoning_mode",
            "artifact_summary",
        )
        if mode != "artifact_summary":
            if metadata.get("reasoning_artifact_path"):
                raise CanonicalRawCommitError("reasoning_artifact_reference_unexpected_for_mode")
            turn.metadata = metadata
            return

        if not turn.reasoning:
            reasoning_path = str(metadata.get("reasoning_artifact_path") or "")
            managed_reasoning_digest = ""
            if reasoning_path:
                managed_reasoning_digest = managed_capture_artifact_sha256(
                    database_dir=database_dir,
                    source_agent=source_agent,
                    session_id=session_id,
                    turn_number=turn.turn_number,
                    artifact_type="reasoning",
                    path=reasoning_path,
                )
            if reasoning_path and not managed_reasoning_digest:
                legacy_content = read_historical_capture_artifact_bytes(
                    database_dir=database_dir,
                    session_id=session_id,
                    turn_number=turn.turn_number,
                    artifact_type="reasoning",
                    path=reasoning_path,
                )
                if legacy_content is None:
                    raise CanonicalRawCommitError("reasoning_artifact_reference_untrusted")
                try:
                    legacy_text = legacy_content.decode("utf-8")
                except UnicodeError:
                    raise CanonicalRawCommitError(
                        "reasoning_artifact_reference_unreadable"
                    ) from None
                metadata["reasoning_artifact_path"] = str(
                    write_managed_capture_artifact(
                        database_dir=database_dir,
                        source_agent=source_agent,
                        session_id=session_id,
                        turn_number=turn.turn_number,
                        artifact_type="reasoning",
                        content=legacy_text,
                    )
                )
                managed_reasoning_digest = hashlib.sha256(legacy_content).hexdigest()
                supplied_refs = metadata.get("artifact_refs")
                if isinstance(supplied_refs, list):
                    metadata["artifact_refs"] = [
                        ref
                        for ref in supplied_refs
                        if not (isinstance(ref, dict) and ref.get("artifact_type") == "reasoning")
                    ]
            if managed_reasoning_digest:
                metadata["reasoning_sha256"] = managed_reasoning_digest
            turn.metadata = metadata
            return

        metadata["reasoning_sha256"] = hashlib.sha256(turn.reasoning.encode("utf-8")).hexdigest()
        content = build_reasoning_artifact_content(
            source_agent=source_agent,
            session_id=session_id,
            turn_number=turn.turn_number,
            reasoning=turn.reasoning,
        )
        path = write_managed_capture_artifact(
            database_dir=database_dir,
            source_agent=source_agent,
            session_id=session_id,
            turn_number=turn.turn_number,
            artifact_type="reasoning",
            content=content,
        )
        metadata["reasoning_artifact_path"] = str(path)
        turn.metadata = metadata
        if turn.completeness is not None:
            turn.completeness["reasoning"] = "artifact"

    def _make_failure_result(
        self,
        exc: Exception,
        source: AgentSource,
        session_info: SessionInfo,
        turn: Turn,
        content_hash: str,
        artifact_path: str,
        raw_event_id: Optional[str] = None,
    ) -> SyncResult:
        """Record and return one typed turn-level synchronization failure."""

        if isinstance(exc, StorageRateLimitError):
            error_message = f"rate_limit: {exc}"
            logger.warning("[SyncEngine] rate limited: %s", exc, exc_info=True)
        elif isinstance(exc, StorageAuthError):
            error_message = f"auth_error: {exc}"
            logger.error("[SyncEngine] authentication failed: %s", exc, exc_info=True)
        elif isinstance(exc, StorageServerError):
            error_message = f"server_error: {exc}"
            logger.warning("[SyncEngine] server failed: %s", exc, exc_info=True)
        else:
            error_message = str(exc)
            logger.error("[SyncEngine] synchronization failed: %s", exc, exc_info=True)
        self._record_sync(
            source.name,
            session_info.session_id,
            turn.turn_number,
            content_hash,
            [],
            "failed",
            error=error_message,
            artifact_path=artifact_path,
        )
        return SyncResult(
            session_id=session_info.session_id,
            turn_number=turn.turn_number,
            action="failed",
            content_hash=content_hash,
            error=error_message,
            raw_event_id=raw_event_id,
        )
