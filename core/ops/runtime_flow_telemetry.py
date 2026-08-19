"""Best-effort runtime flow receipts with a durable local outbox."""

from __future__ import annotations

from contextlib import contextmanager
import json
import hashlib
import logging
import os
import sqlite3
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from core.ops.cognitive_data_contract import CognitiveDataEvent
from core.ops.durable_io import (
    DurableIOError,
    fsync_directory,
    inspect_path_kind,
)
from core.ops.producer_consumer_ledger import ProducerConsumerLedger

OUTBOX_NAME = "runtime_flow_outbox.jsonl"
OUTBOX_LOCK_NAME = "runtime_flow_outbox.lock"
DEAD_LETTER_OUTBOX_NAME = "runtime_flow_outbox.dead_letter.jsonl"
DEAD_LETTER_LOCK_NAME = "runtime_flow_outbox.dead_letter.lock"
DEAD_LETTER_SCHEMA_VERSION = "mnemos.runtime_flow_outbox_dead_letter.v1"
_OUTBOX_LOCK = threading.RLock()
logger = logging.getLogger(__name__)


def _runtime_outbox_kind(path: Path) -> str:
    """Inspect a durable outbox without treating unavailable as absent."""

    try:
        kind = inspect_path_kind(path)
    except DurableIOError:
        raise DurableIOError(
            "runtime_flow_outbox_inspection_failed"
        ) from None
    if kind not in {"missing", "file"}:
        raise DurableIOError("runtime_flow_outbox_not_regular")
    return kind


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError("runtime outbox write made no progress")
        offset += written


@contextmanager
def _outbox_process_lock(database_dir: Path):
    """Serialize outbox read/replace/append across runtime processes."""
    with _runtime_file_lock(database_dir, OUTBOX_LOCK_NAME):
        yield


@contextmanager
def _dead_letter_process_lock(database_dir: Path):
    """Serialize dead-letter JSONL appends across runtime processes."""
    with _runtime_file_lock(database_dir, DEAD_LETTER_LOCK_NAME):
        yield


@contextmanager
def _runtime_file_lock(database_dir: Path, lock_name: str):
    database_dir.mkdir(parents=True, exist_ok=True)
    lock_path = database_dir / lock_name
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt  # type: ignore[import]

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(  # type: ignore[attr-defined]
                descriptor,
                msvcrt.LK_LOCK,  # type: ignore[attr-defined]
                1,
            )
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt  # type: ignore[import]

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(  # type: ignore[attr-defined]
                    descriptor,
                    msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
                    1,
                )
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _atomic_replace_outbox(path: Path, content: str) -> None:
    """Replace an outbox with mode 0600 and no partially visible state."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        data = content.encode("utf-8")
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temporary, path)
        fsync_directory(path.parent)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def runtime_item_id(namespace: str, *parts: Any) -> str:
    """Build a correlation id without persisting raw user content in the ledger."""
    material = "|".join([namespace, *(str(part) for part in parts)])
    return f"{namespace}:{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


class RuntimeFlowTelemetry:
    """Write real business transitions without making telemetry a business dependency."""

    def __init__(self, config: Any):
        database_dir = (
            config.get("database_dir")
            if isinstance(config, Mapping)
            else getattr(config, "database_dir", None)
        )
        if not isinstance(database_dir, (str, Path)):
            raise ValueError("runtime telemetry requires an explicit database_dir")
        self.config = config
        self.database_dir = Path(database_dir)
        self.outbox_path = self.database_dir / OUTBOX_NAME
        self.dead_letter_path = self.database_dir / DEAD_LETTER_OUTBOX_NAME

    def _ledger(self) -> ProducerConsumerLedger:
        return ProducerConsumerLedger(self.config, initialize=False)

    def produced(
        self,
        flow_id: str,
        *,
        source: str,
        item_id: str,
        intended_consumers: list[str] | tuple[str, ...],
        metadata: Mapping[str, Any] | None = None,
        generation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str | None:
        payload = {
            "operation": "produced",
            "flow_id": flow_id,
            "source": source,
            "item_id": item_id,
            "intended_consumers": list(intended_consumers),
            "metadata": dict(metadata or {}),
            "generation_id": generation_id,
            "idempotency_key": idempotency_key,
        }
        return self._record_or_spool(payload)

    def consumed(
        self,
        flow_id: str,
        *,
        source: str,
        item_id: str,
        production_event_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        generation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str | None:
        payload = {
            "operation": "consumed",
            "flow_id": flow_id,
            "source": source,
            "item_id": item_id,
            "production_event_id": production_event_id,
            "metadata": dict(metadata or {}),
            "generation_id": generation_id,
            "idempotency_key": idempotency_key,
        }
        return self._record_or_spool(payload)

    def stage(
        self,
        flow_id: str,
        *,
        source: str,
        item_id: str,
        production_event_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        generation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str | None:
        payload = {
            "operation": "stage",
            "flow_id": flow_id,
            "source": source,
            "item_id": item_id,
            "production_event_id": production_event_id,
            "metadata": dict(metadata or {}),
            "generation_id": generation_id,
            "idempotency_key": idempotency_key,
        }
        return self._record_or_spool(payload)

    def dead_letter(
        self,
        flow_id: str,
        *,
        source: str,
        item_id: str,
        production_event_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        generation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str | None:
        payload = {
            "operation": "dead_letter",
            "flow_id": flow_id,
            "source": source,
            "item_id": item_id,
            "production_event_id": production_event_id,
            "metadata": dict(metadata or {}),
            "generation_id": generation_id,
            "idempotency_key": idempotency_key,
        }
        return self._record_or_spool(payload)

    def skipped(
        self,
        flow_id: str,
        *,
        source: str,
        consumer_id: str | None = None,
        item_id: str,
        production_event_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        generation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str | None:
        payload = {
            "operation": "skipped",
            "flow_id": flow_id,
            "source": source,
            "consumer_id": consumer_id,
            "item_id": item_id,
            "production_event_id": production_event_id,
            "metadata": dict(metadata or {}),
            "generation_id": generation_id,
            "idempotency_key": idempotency_key,
        }
        return self._record_or_spool(payload)

    def cognitive_event(
        self,
        event: CognitiveDataEvent,
        *,
        lifecycle_status: str = "produced",
    ) -> str | None:
        payload = {
            "operation": "cognitive_event",
            "event": event.as_dict(),
            "lifecycle_status": lifecycle_status,
        }
        result, durable = self._record_or_spool_state(payload)
        return result or (event.event_id if durable else None)

    def cognitive_consumed(
        self,
        event_id: str,
        *,
        consumer_id: str,
        action_changed: bool = False,
        outcome: str = "",
        status: str = "consumed",
        metadata: Mapping[str, Any] | None = None,
        supersedes_consumption_id: str = "",
        correction_of_consumption_id: str = "",
    ) -> str | None:
        return self._record_or_spool(
            {
                "operation": "cognitive_consumed",
                "event_id": event_id,
                "consumer_id": consumer_id,
                "action_changed": action_changed,
                "outcome": outcome,
                "status": status,
                "metadata": dict(metadata or {}),
                "supersedes_consumption_id": supersedes_consumption_id,
                "correction_of_consumption_id": correction_of_consumption_id,
            }
        )

    def _record_or_spool(self, payload: dict[str, Any]) -> str | None:
        result, _durable = self._record_or_spool_state(payload)
        return result

    def _record_or_spool_state(
        self,
        payload: dict[str, Any],
    ) -> tuple[str | None, bool]:
        try:
            outbox_kind = _runtime_outbox_kind(self.outbox_path)
        except DurableIOError:
            return None, False
        if outbox_kind == "file":
            try:
                self.drain_outbox()
            except DurableIOError:
                return None, False
            try:
                outbox_kind = _runtime_outbox_kind(self.outbox_path)
            except DurableIOError:
                return None, False
            if outbox_kind == "file":
                self._spool(payload)
                return None, True
        try:
            return self._apply_payload(payload), True
        except (FileNotFoundError, OSError, sqlite3.OperationalError):
            self._spool(payload)
            return None, True
        except (sqlite3.Error, TypeError, ValueError, KeyError) as exc:
            self._quarantine(payload, error_type=type(exc).__name__)
            return None, False

    def _apply_payload(self, payload: Mapping[str, Any]) -> str:
        operation = str(payload["operation"])
        ledger = self._ledger()
        if operation == "cognitive_event":
            raw = dict(payload["event"])
            event = CognitiveDataEvent(
                event_id=str(raw["event_id"]),
                source_kind=str(raw["source_kind"]),
                source_uri=str(raw["source_uri"]),
                content_hash=str(raw["content_hash"]),
                canonical_subject=str(raw["canonical_subject"]),
                data_type=str(raw["data_type"]),
                producer=str(raw["producer"]),
                intended_consumers=tuple(str(value) for value in raw["intended_consumers"]),
                privacy_level=str(raw["privacy_level"]),
                confidence=float(raw["confidence"]),
                evidence_refs=tuple(str(value) for value in raw["evidence_refs"]),
                dedupe_key=str(raw["dedupe_key"]),
                created_at=str(raw["created_at"]),
                source_id=str(raw.get("source_id") or ""),
                asset_id=str(raw.get("asset_id") or ""),
                retention_policy=str(raw.get("retention_policy") or "default"),
                metadata=dict(raw.get("metadata") or {}),
            )
            return ledger.record_data_event(
                event,
                lifecycle_status=str(payload.get("lifecycle_status") or "produced"),
            )
        if operation == "cognitive_consumed":
            return ledger.record_data_consumed(
                str(payload["event_id"]),
                consumer_id=str(payload["consumer_id"]),
                action_changed=bool(payload.get("action_changed")),
                outcome=str(payload.get("outcome") or ""),
                status=str(payload.get("status") or "consumed"),
                metadata=dict(payload.get("metadata") or {}),
                supersedes_consumption_id=str(
                    payload.get("supersedes_consumption_id") or ""
                ),
                correction_of_consumption_id=str(
                    payload.get("correction_of_consumption_id") or ""
                ),
            )
        source = str(payload["source"])
        item_id = str(payload.get("item_id") or "")
        metadata = dict(payload.get("metadata") or {})
        generation_id = str(payload["generation_id"]) if payload.get("generation_id") else None
        idempotency_key = (
            str(payload["idempotency_key"]) if payload.get("idempotency_key") else None
        )
        if operation == "produced":
            return ledger.record_produced(
                str(payload["flow_id"]),
                intended_consumers=[
                    str(value) for value in payload.get("intended_consumers") or []
                ],
                source=source,
                item_id=item_id,
                metadata=metadata,
                generation_id=generation_id,
                idempotency_key=idempotency_key,
            )
        production_event_id = (
            str(payload["production_event_id"]) if payload.get("production_event_id") else None
        )
        if operation == "stage":
            return ledger.record_stage(
                str(payload["flow_id"]),
                source=source,
                item_id=item_id,
                metadata=metadata,
                generation_id=generation_id,
                production_event_id=production_event_id,
                idempotency_key=idempotency_key,
            )
        if operation == "consumed":
            return ledger.record_consumed(
                str(payload["flow_id"]),
                source=source,
                item_id=item_id,
                metadata=metadata,
                generation_id=generation_id,
                production_event_id=production_event_id,
                idempotency_key=idempotency_key,
            )
        if operation == "dead_letter":
            return ledger.record_dead_letter(
                str(payload["flow_id"]),
                source=source,
                item_id=item_id,
                metadata=metadata,
                generation_id=generation_id,
                production_event_id=production_event_id,
                idempotency_key=idempotency_key,
            )
        if operation == "skipped":
            return ledger.record_skipped(
                str(payload["flow_id"]),
                source=source,
                consumer_id=str(payload.get("consumer_id") or source),
                item_id=item_id,
                metadata=metadata,
                generation_id=generation_id,
                production_event_id=production_event_id,
                idempotency_key=idempotency_key,
            )
        raise ValueError(f"unsupported runtime telemetry operation: {operation}")

    def _spool(self, payload: Mapping[str, Any]) -> None:
        self.database_dir.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        with _OUTBOX_LOCK:
            with _outbox_process_lock(self.database_dir):
                fd = os.open(
                    self.outbox_path,
                    os.O_APPEND | os.O_CREAT | os.O_RDWR,
                    0o600,
                )
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    chunks: list[bytes] = []
                    while chunk := os.read(fd, 64 * 1024):
                        chunks.append(chunk)
                    if line.rstrip(b"\n") in b"".join(chunks).splitlines():
                        return
                    os.lseek(fd, 0, os.SEEK_END)
                    _write_all(fd, line)
                    os.fsync(fd)
                    fsync_directory(self.database_dir)
                finally:
                    os.close(fd)

    def _quarantine(
        self,
        payload: Mapping[str, Any],
        *,
        error_type: str,
    ) -> None:
        """Persist a non-retryable receipt failure without retaining content."""

        self.database_dir.mkdir(parents=True, exist_ok=True)
        canonical = json.dumps(
            dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        record = {
            "schema_version": DEAD_LETTER_SCHEMA_VERSION,
            "reason": "permanent_validation_failure",
            "error_type": str(error_type),
            "payload_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "operation": str(payload.get("operation") or ""),
            "flow_id": str(payload.get("flow_id") or ""),
            "event_id": str(payload.get("event_id") or ""),
            "item_id": str(payload.get("item_id") or ""),
            "consumer_id": str(payload.get("consumer_id") or payload.get("source") or ""),
        }
        line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        with _OUTBOX_LOCK:
            with _dead_letter_process_lock(self.database_dir):
                fd = os.open(
                    self.dead_letter_path,
                    os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                    0o600,
                )
                try:
                    _write_all(fd, line)
                    os.fsync(fd)
                    fsync_directory(self.database_dir)
                finally:
                    os.close(fd)

    def drain_outbox(self) -> int:
        """Replay durable transitions in order; retain the uncommitted suffix on failure."""
        if _runtime_outbox_kind(self.outbox_path) == "missing":
            return 0
        with _OUTBOX_LOCK:
            with _outbox_process_lock(self.database_dir):
                if _runtime_outbox_kind(self.outbox_path) == "missing":
                    return 0
                fd = os.open(self.outbox_path, os.O_RDONLY)
                try:
                    chunks: list[bytes] = []
                    while chunk := os.read(fd, 64 * 1024):
                        chunks.append(chunk)
                    lines = b"".join(chunks).decode("utf-8").splitlines()
                finally:
                    os.close(fd)
                replayed = 0
                for index, line in enumerate(lines):
                    try:
                        payload = json.loads(line)
                        if not isinstance(payload, Mapping):
                            raise TypeError(
                                "runtime outbox entry must be an object"
                            )
                    except (
                        json.JSONDecodeError,
                        TypeError,
                        ValueError,
                    ) as exc:
                        self._quarantine(
                            {
                                "operation": "unparseable_outbox_line",
                                "line_sha256": hashlib.sha256(
                                    line.encode("utf-8")
                                ).hexdigest(),
                            },
                            error_type=type(exc).__name__,
                        )
                        continue
                    try:
                        self._apply_payload(payload)
                    except (
                        FileNotFoundError,
                        OSError,
                        sqlite3.OperationalError,
                    ):
                        remainder = "\n".join(lines[index:]) + "\n"
                        _atomic_replace_outbox(
                            self.outbox_path,
                            remainder,
                        )
                        return replayed
                    except (
                        sqlite3.Error,
                        TypeError,
                        ValueError,
                        KeyError,
                    ) as exc:
                        self._quarantine(
                            payload,
                            error_type=type(exc).__name__,
                        )
                        continue
                    replayed += 1
                self.outbox_path.unlink(missing_ok=True)
                fsync_directory(self.database_dir)
                return replayed


def _telemetry_for(config_or_path: Any) -> RuntimeFlowTelemetry | None:
    if isinstance(config_or_path, (str, Path)):
        return RuntimeFlowTelemetry(SimpleNamespace(database_dir=Path(config_or_path)))
    try:
        return RuntimeFlowTelemetry(config_or_path)
    except (TypeError, ValueError):
        logger.debug("runtime telemetry skipped: invalid database_dir", exc_info=True)
        return None


def record_runtime_produced(
    flow_id: str,
    *,
    source: str,
    item_id: str,
    intended_consumers: list[str] | tuple[str, ...],
    metadata: Mapping[str, Any] | None = None,
    generation_id: str | None = None,
    idempotency_key: str | None = None,
    config_or_path: Any,
) -> str | None:
    telemetry = _telemetry_for(config_or_path)
    if telemetry is None:
        return None
    return telemetry.produced(
        flow_id,
        source=source,
        item_id=item_id,
        intended_consumers=intended_consumers,
        metadata=metadata,
        generation_id=generation_id,
        idempotency_key=idempotency_key,
    )


def record_runtime_consumed(
    flow_id: str,
    *,
    source: str,
    item_id: str,
    production_event_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    generation_id: str | None = None,
    idempotency_key: str | None = None,
    config_or_path: Any,
) -> str | None:
    telemetry = _telemetry_for(config_or_path)
    if telemetry is None:
        return None
    return telemetry.consumed(
        flow_id,
        source=source,
        item_id=item_id,
        production_event_id=production_event_id,
        metadata=metadata,
        generation_id=generation_id,
        idempotency_key=idempotency_key,
    )


def record_runtime_stage(
    flow_id: str,
    *,
    source: str,
    item_id: str,
    production_event_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    generation_id: str | None = None,
    idempotency_key: str | None = None,
    config_or_path: Any,
) -> str | None:
    """Record a nonterminal consumer stage event for an existing producer."""

    telemetry = _telemetry_for(config_or_path)
    if telemetry is None:
        return None
    return telemetry.stage(
        flow_id,
        source=source,
        item_id=item_id,
        production_event_id=production_event_id,
        metadata=metadata,
        generation_id=generation_id,
        idempotency_key=idempotency_key,
    )


def record_runtime_dead_letter(
    flow_id: str,
    *,
    source: str,
    item_id: str,
    production_event_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    generation_id: str | None = None,
    idempotency_key: str | None = None,
    config_or_path: Any,
) -> str | None:
    telemetry = _telemetry_for(config_or_path)
    if telemetry is None:
        return None
    return telemetry.dead_letter(
        flow_id,
        source=source,
        item_id=item_id,
        production_event_id=production_event_id,
        metadata=metadata,
        generation_id=generation_id,
        idempotency_key=idempotency_key,
    )


def record_runtime_skipped(
    flow_id: str,
    *,
    source: str,
    consumer_id: str | None = None,
    item_id: str,
    production_event_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    generation_id: str | None = None,
    idempotency_key: str | None = None,
    config_or_path: Any,
) -> str | None:
    """Record a reviewed terminal no-effect receipt for an existing producer."""

    telemetry = _telemetry_for(config_or_path)
    if telemetry is None:
        return None
    return telemetry.skipped(
        flow_id,
        source=source,
        consumer_id=consumer_id,
        item_id=item_id,
        production_event_id=production_event_id,
        metadata=metadata,
        generation_id=generation_id,
        idempotency_key=idempotency_key,
    )


def record_cognitive_data_event(
    event: CognitiveDataEvent,
    *,
    lifecycle_status: str = "produced",
    config_or_path: Any,
) -> str | None:
    telemetry = _telemetry_for(config_or_path)
    if telemetry is None:
        return None
    return telemetry.cognitive_event(
        event,
        lifecycle_status=lifecycle_status,
    )


def record_cognitive_data_consumed(
    event_id: str,
    *,
    consumer_id: str,
    action_changed: bool = False,
    outcome: str = "",
    status: str = "consumed",
    metadata: Mapping[str, Any] | None = None,
    supersedes_consumption_id: str = "",
    correction_of_consumption_id: str = "",
    config_or_path: Any,
) -> str | None:
    telemetry = _telemetry_for(config_or_path)
    if telemetry is None:
        return None
    return telemetry.cognitive_consumed(
        event_id,
        consumer_id=consumer_id,
        action_changed=action_changed,
        outcome=outcome,
        status=status,
        metadata=metadata,
        supersedes_consumption_id=supersedes_consumption_id,
        correction_of_consumption_id=correction_of_consumption_id,
    )
