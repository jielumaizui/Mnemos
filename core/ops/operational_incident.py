"""Canonical operational-incident state for machine failures.

The public interface deliberately separates machine incidents from human
retrospectives.  Callers record immutable occurrences; the store aggregates
them into one active incident and creates exactly one diagnostic task.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from core.file_ops import sha256_file
from core.ops.operational_incident_notification import (
    DialogReminderIncidentNotificationAdapter,
)
from core.ops.operational_incident_auxiliary import OperationalIncidentAuxiliaryMixin
from core.ops.operational_incident_diagnostics import root_cause_code_for_reproducer
from core.ops.operational_incident_identity import canonical_replay_input_binding_hash

SCHEMA_VERSION = "mnemos.operational_incident.v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


_DDL = (
    """
    CREATE TABLE operational_incident_schema_registry (
        component TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        schema_hash TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE operational_incidents (
        incident_id TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        generation INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('investigating','diagnosed','resolved')),
        severity TEXT NOT NULL,
        failure_class TEXT NOT NULL,
        source_family TEXT NOT NULL,
        producer TEXT NOT NULL,
        execution_spec_hash TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        route TEXT NOT NULL,
        schema_hash TEXT NOT NULL,
        parser_hash TEXT NOT NULL,
        validator_hash TEXT NOT NULL,
        occurrence_count INTEGER NOT NULL DEFAULT 0 CHECK(occurrence_count >= 0),
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        resolved_at TEXT NOT NULL DEFAULT '',
        UNIQUE(fingerprint, generation)
    )
    """,
    """
    CREATE UNIQUE INDEX one_active_operational_incident_per_fingerprint
    ON operational_incidents(fingerprint)
    WHERE status != 'resolved'
    """,
    """
    CREATE TABLE incident_occurrences (
        occurrence_id TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        error_codes_json TEXT NOT NULL,
        validation_errors_json TEXT NOT NULL,
        prompt_hash TEXT NOT NULL,
        visible_input_sha256 TEXT NOT NULL,
        response_hash TEXT NOT NULL,
        source_event_refs_json TEXT NOT NULL,
        missing_evidence_json TEXT NOT NULL,
        artifact_path TEXT NOT NULL,
        artifact_hash TEXT NOT NULL,
        artifact_acl TEXT NOT NULL,
        retention_class TEXT NOT NULL,
        raw_response_available INTEGER NOT NULL CHECK(raw_response_available IN (0,1)),
        raw_response_length INTEGER NOT NULL CHECK(raw_response_length >= 0),
        created_at TEXT NOT NULL,
        UNIQUE(artifact_path, artifact_hash),
        FOREIGN KEY(incident_id) REFERENCES operational_incidents(incident_id)
    )
    """,
    """
    CREATE INDEX incident_occurrences_by_incident
    ON incident_occurrences(incident_id, created_at, occurrence_id)
    """,
    """
    CREATE TRIGGER incident_occurrences_no_update
    BEFORE UPDATE ON incident_occurrences
    BEGIN SELECT RAISE(ABORT, 'incident occurrences are append-only'); END
    """,
    """
    CREATE TRIGGER incident_occurrences_no_delete
    BEFORE DELETE ON incident_occurrences
    BEGIN SELECT RAISE(ABORT, 'incident occurrences are append-only'); END
    """,
    """
    CREATE TABLE incident_ingest_receipts (
        receipt_id TEXT PRIMARY KEY,
        occurrence_id TEXT NOT NULL UNIQUE,
        incident_id TEXT NOT NULL,
        artifact_path TEXT NOT NULL,
        artifact_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status='committed'),
        committed_at TEXT NOT NULL,
        FOREIGN KEY(occurrence_id) REFERENCES incident_occurrences(occurrence_id),
        FOREIGN KEY(incident_id) REFERENCES operational_incidents(incident_id)
    )
    """,
    """
    CREATE TRIGGER incident_ingest_receipts_no_update
    BEFORE UPDATE ON incident_ingest_receipts
    BEGIN SELECT RAISE(ABORT, 'incident ingest receipts are append-only'); END
    """,
    """
    CREATE TRIGGER incident_ingest_receipts_no_delete
    BEFORE DELETE ON incident_ingest_receipts
    BEGIN SELECT RAISE(ABORT, 'incident ingest receipts are append-only'); END
    """,
    """
    CREATE TABLE incident_artifact_access_events (
        access_id TEXT PRIMARY KEY,
        occurrence_id TEXT NOT NULL,
        incident_id TEXT NOT NULL,
        principal TEXT NOT NULL,
        purpose TEXT NOT NULL,
        artifact_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(occurrence_id) REFERENCES incident_occurrences(occurrence_id),
        FOREIGN KEY(incident_id) REFERENCES operational_incidents(incident_id)
    )
    """,
    """
    CREATE TRIGGER incident_artifact_access_events_no_update
    BEFORE UPDATE ON incident_artifact_access_events
    BEGIN SELECT RAISE(ABORT, 'artifact access events are append-only'); END
    """,
    """
    CREATE TRIGGER incident_artifact_access_events_no_delete
    BEFORE DELETE ON incident_artifact_access_events
    BEGIN SELECT RAISE(ABORT, 'artifact access events are append-only'); END
    """,
    """
    CREATE TABLE diagnostic_tasks (
        task_id TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK(status IN ('pending','investigating','reported','failed')),
        owner TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(incident_id) REFERENCES operational_incidents(incident_id)
    )
    """,
    """
    CREATE TABLE incident_diagnostic_evidence (
        evidence_id TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL,
        diagnostic_task_id TEXT NOT NULL,
        evidence_kind TEXT NOT NULL,
        producer TEXT NOT NULL,
        evidence_hash TEXT NOT NULL,
        source_refs_json TEXT NOT NULL,
        reproduction_command TEXT NOT NULL,
        before_status TEXT NOT NULL CHECK(before_status IN ('failed','not_run')),
        after_status TEXT NOT NULL CHECK(after_status IN ('passed','failed','not_run')),
        created_at TEXT NOT NULL,
        FOREIGN KEY(incident_id) REFERENCES operational_incidents(incident_id),
        FOREIGN KEY(diagnostic_task_id) REFERENCES diagnostic_tasks(task_id)
    )
    """,
    """
    CREATE TRIGGER incident_diagnostic_evidence_no_update
    BEFORE UPDATE ON incident_diagnostic_evidence
    BEGIN SELECT RAISE(ABORT, 'diagnostic evidence is append-only'); END
    """,
    """
    CREATE TRIGGER incident_diagnostic_evidence_no_delete
    BEFORE DELETE ON incident_diagnostic_evidence
    BEGIN SELECT RAISE(ABORT, 'diagnostic evidence is append-only'); END
    """,
    """
    CREATE TABLE root_cause_reports (
        report_id TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL,
        diagnostic_task_id TEXT NOT NULL,
        report_revision INTEGER NOT NULL CHECK(report_revision > 0),
        root_cause_status TEXT NOT NULL CHECK(root_cause_status IN ('confirmed','investigating')),
        root_cause_code TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL,
        reproduction_command TEXT NOT NULL,
        repair TEXT NOT NULL,
        verification TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(incident_id, report_revision),
        FOREIGN KEY(incident_id) REFERENCES operational_incidents(incident_id),
        FOREIGN KEY(diagnostic_task_id) REFERENCES diagnostic_tasks(task_id)
    )
    """,
    """
    CREATE TRIGGER root_cause_reports_no_update
    BEFORE UPDATE ON root_cause_reports
    BEGIN SELECT RAISE(ABORT, 'root cause reports are append-only'); END
    """,
    """
    CREATE TRIGGER root_cause_reports_no_delete
    BEFORE DELETE ON root_cause_reports
    BEGIN SELECT RAISE(ABORT, 'root cause reports are append-only'); END
    """,
    """
    CREATE TABLE incident_notification_commands (
        command_id TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL UNIQUE,
        report_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','processing','delivered')),
        payload_json TEXT NOT NULL,
        lease_expires_at TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(incident_id) REFERENCES operational_incidents(incident_id),
        FOREIGN KEY(report_id) REFERENCES root_cause_reports(report_id)
    )
    """,
    """
    CREATE TABLE incident_notification_attempts (
        attempt_id TEXT PRIMARY KEY,
        command_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('failed','committed')),
        error_type TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(command_id) REFERENCES incident_notification_commands(command_id)
    )
    """,
    """
    CREATE TRIGGER incident_notification_attempts_no_update
    BEFORE UPDATE ON incident_notification_attempts
    BEGIN SELECT RAISE(ABORT, 'notification attempts are append-only'); END
    """,
    """
    CREATE TRIGGER incident_notification_attempts_no_delete
    BEFORE DELETE ON incident_notification_attempts
    BEGIN SELECT RAISE(ABORT, 'notification attempts are append-only'); END
    """,
    """
    CREATE TABLE incident_notification_receipts (
        receipt_id TEXT PRIMARY KEY,
        command_id TEXT NOT NULL UNIQUE,
        incident_id TEXT NOT NULL,
        report_id TEXT NOT NULL,
        external_ref TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        delivered_at TEXT NOT NULL,
        FOREIGN KEY(command_id) REFERENCES incident_notification_commands(command_id),
        FOREIGN KEY(incident_id) REFERENCES operational_incidents(incident_id),
        FOREIGN KEY(report_id) REFERENCES root_cause_reports(report_id)
    )
    """,
    """
    CREATE TABLE incident_replay_commands (
        command_id TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL,
        occurrence_id TEXT NOT NULL UNIQUE,
        artifact_hash TEXT NOT NULL,
        input_binding_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','committed','failed')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(incident_id) REFERENCES operational_incidents(incident_id),
        FOREIGN KEY(occurrence_id) REFERENCES incident_occurrences(occurrence_id)
    )
    """,
    """
    CREATE TABLE incident_replay_receipts (
        receipt_id TEXT PRIMARY KEY,
        command_id TEXT NOT NULL UNIQUE,
        incident_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('committed','failed')),
        output_hash TEXT NOT NULL,
        input_binding_hash TEXT NOT NULL,
        executor TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(command_id) REFERENCES incident_replay_commands(command_id),
        FOREIGN KEY(incident_id) REFERENCES operational_incidents(incident_id)
    )
    """,
    """
    CREATE TRIGGER incident_replay_receipts_no_update
    BEFORE UPDATE ON incident_replay_receipts
    BEGIN SELECT RAISE(ABORT, 'replay receipts are append-only'); END
    """,
    """
    CREATE TRIGGER incident_replay_receipts_no_delete
    BEFORE DELETE ON incident_replay_receipts
    BEGIN SELECT RAISE(ABORT, 'replay receipts are append-only'); END
    """,
    """
    CREATE TABLE incident_resolutions (
        resolution_id TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL UNIQUE,
        replay_receipt_id TEXT NOT NULL,
        repair_ref TEXT NOT NULL,
        verification_ref TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(incident_id) REFERENCES operational_incidents(incident_id),
        FOREIGN KEY(replay_receipt_id) REFERENCES incident_replay_receipts(receipt_id)
    )
    """,
    """
    CREATE TRIGGER incident_resolutions_no_update
    BEFORE UPDATE ON incident_resolutions
    BEGIN SELECT RAISE(ABORT, 'incident resolutions are append-only'); END
    """,
    """
    CREATE TRIGGER incident_resolutions_no_delete
    BEFORE DELETE ON incident_resolutions
    BEGIN SELECT RAISE(ABORT, 'incident resolutions are append-only'); END
    """,
    """
    CREATE TABLE incident_retrospectives (
        retrospective_id TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL,
        resolution_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('proposed','accepted','declined')),
        reusable_lesson TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(incident_id) REFERENCES operational_incidents(incident_id),
        FOREIGN KEY(resolution_id) REFERENCES incident_resolutions(resolution_id),
        UNIQUE(incident_id, reusable_lesson)
    )
    """,
    """
    CREATE TRIGGER incident_retrospectives_no_update
    BEFORE UPDATE ON incident_retrospectives
    BEGIN SELECT RAISE(ABORT, 'incident retrospective proposals are append-only'); END
    """,
    """
    CREATE TRIGGER incident_retrospectives_no_delete
    BEFORE DELETE ON incident_retrospectives
    BEGIN SELECT RAISE(ABORT, 'incident retrospective proposals are append-only'); END
    """,
    """
    CREATE TABLE legacy_incident_migrations (
        migration_id TEXT PRIMARY KEY,
        source_type TEXT NOT NULL CHECK(source_type IN ('artifact','recap','reminder')),
        source_id TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        incident_id TEXT NOT NULL,
        occurrence_id TEXT NOT NULL DEFAULT '',
        target_ref TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK(status IN ('migrated','superseded','archived')),
        migrated_at TEXT NOT NULL,
        UNIQUE(source_type, source_id, content_hash),
        FOREIGN KEY(incident_id) REFERENCES operational_incidents(incident_id)
    )
    """,
    """
    CREATE TRIGGER legacy_incident_migrations_no_update
    BEFORE UPDATE ON legacy_incident_migrations
    BEGIN SELECT RAISE(ABORT, 'legacy incident migrations are append-only'); END
    """,
    """
    CREATE TRIGGER legacy_incident_migrations_no_delete
    BEFORE DELETE ON legacy_incident_migrations
    BEGIN SELECT RAISE(ABORT, 'legacy incident migrations are append-only'); END
    """,
    """
    CREATE TABLE legacy_incident_migration_events (
        event_id TEXT PRIMARY KEY,
        source_type TEXT NOT NULL CHECK(source_type IN ('reminder')),
        source_id TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        incident_id TEXT NOT NULL,
        target_ref TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN ('processing','archived')),
        created_at TEXT NOT NULL,
        UNIQUE(source_type, source_id, content_hash, event_type),
        FOREIGN KEY(incident_id) REFERENCES operational_incidents(incident_id)
    )
    """,
    """
    CREATE TRIGGER legacy_incident_migration_events_no_update
    BEFORE UPDATE ON legacy_incident_migration_events
    BEGIN SELECT RAISE(ABORT, 'legacy incident migration events are append-only'); END
    """,
    """
    CREATE TRIGGER legacy_incident_migration_events_no_delete
    BEFORE DELETE ON legacy_incident_migration_events
    BEGIN SELECT RAISE(ABORT, 'legacy incident migration events are append-only'); END
    """,
    """
    CREATE TABLE legacy_incident_quarantine (
        quarantine_id TEXT PRIMARY KEY,
        source_type TEXT NOT NULL CHECK(source_type IN ('recap','reminder')),
        source_id TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        reason TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(source_type, source_id, content_hash)
    )
    """,
    """
    CREATE TRIGGER legacy_incident_quarantine_no_update
    BEFORE UPDATE ON legacy_incident_quarantine
    BEGIN SELECT RAISE(ABORT, 'legacy incident quarantine is append-only'); END
    """,
    """
    CREATE TRIGGER legacy_incident_quarantine_no_delete
    BEFORE DELETE ON legacy_incident_quarantine
    BEGIN SELECT RAISE(ABORT, 'legacy incident quarantine is append-only'); END
    """,
)

SCHEMA_HASH = _sha256([statement.strip() for statement in _DDL])


def _normalized_ddl(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).lower()


def _canonical_schema_objects() -> dict[tuple[str, str], str]:
    objects: dict[tuple[str, str], str] = {}
    pattern = re.compile(
        r"^create\s+(?:(unique)\s+)?(table|index|trigger)\s+([a-z0-9_]+)",
        re.IGNORECASE,
    )
    for statement in _DDL:
        match = pattern.match(statement.strip())
        if match is None:
            raise RuntimeError("canonical operational incident DDL is not classifiable")
        object_type = str(match.group(2)).lower()
        object_name = str(match.group(3))
        objects[(object_type, object_name)] = _normalized_ddl(statement)
    return objects


_CANONICAL_SCHEMA_OBJECTS = _canonical_schema_objects()


def validate_operational_incident_schema(conn: sqlite3.Connection) -> None:
    """Fail closed unless registry and every canonical SQLite object match."""

    registry = conn.execute("""
        SELECT schema_version, schema_hash
        FROM operational_incident_schema_registry
        WHERE component='operational_incident'
        """).fetchone()
    if registry is None or tuple(registry) != (SCHEMA_VERSION, SCHEMA_HASH):
        raise RuntimeError("operational incident schema registry mismatch")
    rows = conn.execute("""
        SELECT type, name, sql
        FROM sqlite_master
        WHERE type IN ('table','index','trigger') AND sql IS NOT NULL
        """).fetchall()
    actual = {
        (str(row[0]), str(row[1])): _normalized_ddl(str(row[2]))
        for row in rows
        if (str(row[0]), str(row[1])) in _CANONICAL_SCHEMA_OBJECTS
    }
    if actual != _CANONICAL_SCHEMA_OBJECTS:
        missing = sorted(set(_CANONICAL_SCHEMA_OBJECTS) - set(actual))
        mismatched = sorted(
            key
            for key in set(actual) & set(_CANONICAL_SCHEMA_OBJECTS)
            if actual[key] != _CANONICAL_SCHEMA_OBJECTS[key]
        )
        raise RuntimeError(
            "operational incident physical schema mismatch: "
            f"missing={missing}, mismatched={mismatched}"
        )


@dataclass(frozen=True)
class DistillationFailureEvidence:
    """System-owned facts that define one distillation failure occurrence."""

    session_id: str
    source_family: str
    producer: str
    severity: str
    failure_class: str
    error_codes: tuple[str, ...]
    validation_errors: tuple[str, ...]
    execution_spec_hash: str
    prompt_hash: str
    provider: str
    model: str
    route: str
    schema_hash: str
    parser_hash: str
    validator_hash: str
    visible_input_sha256: str
    response_hash: str
    source_event_refs: tuple[str, ...]
    artifact_path: Path
    artifact_hash: str
    artifact_acl: str
    retention_class: str
    raw_response_available: bool
    raw_response_length: int
    missing_evidence: tuple[str, ...] = ()

    def fingerprint_payload(self) -> dict[str, Any]:
        """Return only stable root-cause dimensions, never variable text/path."""

        return {
            "failure_class": self.failure_class,
            "error_codes": sorted(set(self.error_codes)),
            "source_family": self.source_family,
            "producer": self.producer,
            "execution_spec_hash": self.execution_spec_hash,
            "prompt_hash": self.prompt_hash,
            "provider": self.provider,
            "model": self.model,
            "route": self.route,
            "schema_hash": self.schema_hash,
            "parser_hash": self.parser_hash,
            "validator_hash": self.validator_hash,
        }


@dataclass(frozen=True)
class IncidentRecordResult:
    """Identifiers committed for one new immutable failure occurrence."""

    incident_id: str
    occurrence_id: str
    diagnostic_task_id: str
    fingerprint: str
    incident_created: bool


def initialize_operational_incident_schema(db_path: str | Path) -> None:
    """Explicitly initialize the canonical schema.

    Runtime constructors validate only; production initialization belongs to
    the reconciliation command.
    """

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path), timeout=10) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        existing = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if existing:
            try:
                validate_operational_incident_schema(conn)
                return
            except (RuntimeError, sqlite3.Error):
                pass
            raise RuntimeError(
                "operational incident schema migration required; "
                "run scripts/reconcile_operational_incidents.py"
            )
        for statement in _DDL:
            conn.execute(statement)
        conn.execute(
            """
            INSERT INTO operational_incident_schema_registry (
                component, schema_version, schema_hash, applied_at
            ) VALUES ('operational_incident', ?, ?, ?)
            """,
            (SCHEMA_VERSION, SCHEMA_HASH, _now()),
        )


class OperationalIncidentStore(OperationalIncidentAuxiliaryMixin):
    """Append occurrences and expose incident state through one deep interface."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._validate_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _validate_schema(self) -> None:
        if not self.db_path.is_file():
            raise RuntimeError(
                "operational incident schema is uninitialized; "
                "run scripts/reconcile_operational_incidents.py"
            )
        with self._connect() as conn:
            try:
                validate_operational_incident_schema(conn)
            except (RuntimeError, sqlite3.Error) as exc:
                raise RuntimeError(
                    "operational incident schema migration required; "
                    "run scripts/reconcile_operational_incidents.py"
                ) from exc

    @staticmethod
    def _validate_occurrence_artifact(evidence: DistillationFailureEvidence) -> None:
        artifact_path = evidence.artifact_path.resolve(strict=True)
        if not artifact_path.is_file():
            raise ValueError("incident occurrence artifact is not a regular file")
        actual_hash = "sha256:" + sha256_file(artifact_path)
        if actual_hash != evidence.artifact_hash:
            raise RuntimeError("incident occurrence artifact hash does not match durable evidence")

    @staticmethod
    def _validate_evidence_contract(evidence: DistillationFailureEvidence) -> None:
        required_text = (
            evidence.session_id,
            evidence.source_family,
            evidence.producer,
            evidence.severity,
            evidence.failure_class,
            evidence.provider,
            evidence.model,
            evidence.route,
        )
        if any(not str(value).strip() for value in required_text):
            raise ValueError("incident occurrence identity is incomplete")
        hashes = (
            evidence.execution_spec_hash,
            evidence.prompt_hash,
            evidence.schema_hash,
            evidence.parser_hash,
            evidence.validator_hash,
            evidence.visible_input_sha256,
            evidence.response_hash,
            evidence.artifact_hash,
        )
        bound_hashes = hashes[:-1]
        if any(
            not (
                re.fullmatch(r"sha256:[0-9a-f]{64}", str(value))
                or re.fullmatch(r"missing:[a-z0-9_:-]+", str(value))
            )
            for value in bound_hashes
        ) or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(evidence.artifact_hash)):
            raise ValueError("incident occurrence requires canonical sha256 bindings")
        if not evidence.error_codes or any(
            not re.fullmatch(r"[a-z0-9][a-z0-9_:-]*", str(code)) for code in evidence.error_codes
        ):
            raise ValueError("incident occurrence error codes are not stable")
        if any(not str(value).strip() for value in evidence.source_event_refs):
            raise ValueError("incident occurrence source evidence references are invalid")
        missing = set(evidence.missing_evidence)
        allowed_missing = {
            "execution_spec_hash",
            "prompt_hash",
            "provider",
            "model",
            "route",
            "schema_hash",
            "parser_hash",
            "validator_hash",
            "visible_input_sha256",
            "response_hash",
            "source_event_refs",
        }
        if not missing.issubset(allowed_missing):
            raise ValueError("incident occurrence missing-evidence labels are invalid")
        observed_missing = {
            name
            for name, value in (
                ("execution_spec_hash", evidence.execution_spec_hash),
                ("prompt_hash", evidence.prompt_hash),
                ("provider", evidence.provider),
                ("model", evidence.model),
                ("route", evidence.route),
                ("schema_hash", evidence.schema_hash),
                ("parser_hash", evidence.parser_hash),
                ("validator_hash", evidence.validator_hash),
                ("visible_input_sha256", evidence.visible_input_sha256),
                ("response_hash", evidence.response_hash),
            )
            if str(value).startswith("missing:")
        }
        if not evidence.source_event_refs:
            observed_missing.add("source_event_refs")
        if missing != observed_missing:
            raise ValueError("incident occurrence missing-evidence declaration is inconsistent")
        if (
            evidence.artifact_acl != "distillation_failure_diagnostic_restricted_v1"
            or evidence.retention_class != "unresolved_incident_hold_v1"
        ):
            raise ValueError("incident occurrence artifact governance is invalid")
        if evidence.raw_response_length < 0 or (
            evidence.raw_response_available != (evidence.raw_response_length > 0)
        ):
            raise ValueError("incident occurrence raw-response evidence is inconsistent")

    def record_distillation_failure(
        self,
        evidence: DistillationFailureEvidence,
    ) -> IncidentRecordResult:
        """Append one verified occurrence and ensure one active diagnostic task."""

        self._validate_evidence_contract(evidence)
        self._validate_occurrence_artifact(evidence)
        fingerprint = _sha256(evidence.fingerprint_payload())
        now = _now()
        occurrence_id = f"occ-{uuid.uuid4().hex}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            incident = conn.execute(
                """
                SELECT incident_id FROM operational_incidents
                WHERE fingerprint=? AND status != 'resolved'
                """,
                (fingerprint,),
            ).fetchone()
            incident_created = incident is None
            if incident is None:
                generation_row = conn.execute(
                    """
                    SELECT COALESCE(MAX(generation), 0)
                    FROM operational_incidents WHERE fingerprint=?
                    """,
                    (fingerprint,),
                ).fetchone()
                generation = int(generation_row[0]) + 1
                incident_id = f"incident-{fingerprint.removeprefix('sha256:')[:20]}-{generation}"
                conn.execute(
                    """
                    INSERT INTO operational_incidents (
                        incident_id, fingerprint, generation, status, severity,
                        failure_class, source_family, producer,
                        execution_spec_hash, provider, model, route,
                        schema_hash, parser_hash, validator_hash,
                        occurrence_count, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, 'investigating', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        incident_id,
                        fingerprint,
                        generation,
                        evidence.severity,
                        evidence.failure_class,
                        evidence.source_family,
                        evidence.producer,
                        evidence.execution_spec_hash,
                        evidence.provider,
                        evidence.model,
                        evidence.route,
                        evidence.schema_hash,
                        evidence.parser_hash,
                        evidence.validator_hash,
                        now,
                        now,
                    ),
                )
            else:
                incident_id = str(incident["incident_id"])
            conn.execute(
                """
                INSERT INTO incident_occurrences (
                    occurrence_id, incident_id, session_id, error_codes_json,
                    validation_errors_json, prompt_hash, visible_input_sha256,
                    response_hash, source_event_refs_json, missing_evidence_json,
                    artifact_path, artifact_hash, artifact_acl, retention_class,
                    raw_response_available, raw_response_length, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    occurrence_id,
                    incident_id,
                    evidence.session_id,
                    _canonical_json(sorted(set(evidence.error_codes))),
                    _canonical_json(list(evidence.validation_errors)),
                    evidence.prompt_hash,
                    evidence.visible_input_sha256,
                    evidence.response_hash,
                    _canonical_json(list(evidence.source_event_refs)),
                    _canonical_json(sorted(set(evidence.missing_evidence))),
                    str(evidence.artifact_path),
                    evidence.artifact_hash,
                    evidence.artifact_acl,
                    evidence.retention_class,
                    int(evidence.raw_response_available),
                    int(evidence.raw_response_length),
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE operational_incidents
                SET occurrence_count=occurrence_count+1, last_seen_at=?,
                    severity=CASE
                        WHEN severity='critical' OR ?!='critical' THEN severity
                        ELSE 'critical'
                    END
                WHERE incident_id=?
                """,
                (now, evidence.severity, incident_id),
            )
            conn.execute(
                """
                INSERT INTO incident_ingest_receipts (
                    receipt_id, occurrence_id, incident_id, artifact_path,
                    artifact_hash, status, committed_at
                ) VALUES (?, ?, ?, ?, ?, 'committed', ?)
                """,
                (
                    f"incident-ingest-{occurrence_id}",
                    occurrence_id,
                    incident_id,
                    str(evidence.artifact_path),
                    evidence.artifact_hash,
                    now,
                ),
            )
            diagnostic_task_id = f"diagnostic-{incident_id}"
            conn.execute(
                """
                INSERT OR IGNORE INTO diagnostic_tasks (
                    task_id, incident_id, status, owner, created_at, updated_at
                ) VALUES (?, ?, 'pending', 'operational-diagnostics', ?, ?)
                """,
                (diagnostic_task_id, incident_id, now, now),
            )
        return IncidentRecordResult(
            incident_id=incident_id,
            occurrence_id=occurrence_id,
            diagnostic_task_id=diagnostic_task_id,
            fingerprint=fingerprint,
            incident_created=incident_created,
        )

    def _list_rows(
        self,
        table: str,
        incident_id: str,
        *,
        order_by: str,
    ) -> list[dict[str, Any]]:
        allowed: dict[str, str] = {
            "incident_occurrences": "created_at, occurrence_id",
            "diagnostic_tasks": "created_at, task_id",
            "incident_diagnostic_evidence": "created_at, evidence_id",
            "root_cause_reports": "created_at, report_id",
            "incident_notification_commands": "created_at, command_id",
            "incident_notification_receipts": "delivered_at, receipt_id",
            "incident_replay_commands": "created_at, command_id",
            "incident_replay_receipts": "created_at, receipt_id",
            "incident_resolutions": "created_at, resolution_id",
            "incident_retrospectives": "created_at, retrospective_id",
        }
        if table not in allowed or order_by != allowed[table]:
            raise ValueError("unsupported operational incident query")
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE incident_id=? ORDER BY {order_by}",  # nosec B608
                (incident_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_occurrences(self, incident_id: str) -> list[dict[str, Any]]:
        """List immutable occurrence rows for one incident."""

        return self._list_rows(
            "incident_occurrences",
            incident_id,
            order_by="created_at, occurrence_id",
        )

    def get_occurrence(self, occurrence_id: str) -> dict[str, Any]:
        """Return one immutable occurrence by its exact identifier."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM incident_occurrences WHERE occurrence_id=?",
                (occurrence_id,),
            ).fetchone()
        if row is None:
            raise ValueError("unknown operational incident occurrence")
        return dict(row)

    def list_diagnostic_tasks(self, incident_id: str) -> list[dict[str, Any]]:
        """List the diagnostic task owned by one incident."""

        return self._list_rows(
            "diagnostic_tasks",
            incident_id,
            order_by="created_at, task_id",
        )

    def list_retrospectives(self, incident_id: str) -> list[dict[str, Any]]:
        """List optional post-resolution retrospective proposals."""

        return self._list_rows(
            "incident_retrospectives",
            incident_id,
            order_by="created_at, retrospective_id",
        )

    @staticmethod
    def _diagnosis_for_codes(codes: set[str]) -> tuple[str, str, str]:
        if "transport_empty" in codes:
            return (
                "investigating",
                "symptom_provider_empty_response",
                "Inspect provider transport/request evidence and retry through the formal pipeline.",
            )
        if "provider_failure" in codes:
            return (
                "investigating",
                "symptom_provider_failure",
                "Repair provider routing or credentials, then retry through the formal pipeline.",
            )
        if "non_json_response" in codes:
            return (
                "investigating",
                "symptom_provider_non_json_response",
                "Correct the provider output contract, then retry through the formal pipeline.",
            )
        if "correction_exhausted" in codes:
            return (
                "investigating",
                "symptom_correction_exhausted",
                "Repair the correction contract or validator mismatch before a formal replay.",
            )
        if "schema_validation_failed" in codes:
            return (
                "investigating",
                "symptom_schema_contract_mismatch",
                "Repair the prompt/schema/validator contract before a formal replay.",
            )
        return (
            "investigating",
            "root_cause_unresolved",
            "Collect additional transport, parser, schema, and execution-spec evidence.",
        )

    def diagnose_next(self) -> dict[str, Any] | None:
        """Diagnose one pending incident before any user notification exists."""

        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("""
                SELECT * FROM diagnostic_tasks
                WHERE status='pending'
                ORDER BY created_at, task_id LIMIT 1
                """).fetchone()
            if task is None:
                return None
            incident_id = str(task["incident_id"])
            occurrences = conn.execute(
                """
                SELECT * FROM incident_occurrences
                WHERE incident_id=?
                ORDER BY created_at, occurrence_id
                """,
                (incident_id,),
            ).fetchall()
            if not occurrences:
                raise RuntimeError("diagnostic task has no incident occurrence evidence")
            incident = conn.execute(
                "SELECT * FROM operational_incidents WHERE incident_id=?",
                (incident_id,),
            ).fetchone()
            if incident is None:
                raise RuntimeError("diagnostic task incident is missing")
            codes: set[str] = set()
            evidence_refs: list[str] = [
                f"execution-spec:{incident['execution_spec_hash']}",
                f"provider:{incident['provider']}",
                f"model:{incident['model']}",
                f"route:{incident['route']}",
                f"schema:{incident['schema_hash']}",
                f"parser:{incident['parser_hash']}",
                f"validator:{incident['validator_hash']}",
            ]
            for occurrence in occurrences:
                codes.update(json.loads(str(occurrence["error_codes_json"])))
                evidence_refs.append(f"occurrence:{occurrence['occurrence_id']}")
                evidence_refs.append(f"artifact:{occurrence['artifact_hash']}")
                evidence_refs.append(f"prompt:{occurrence['prompt_hash']}")
                evidence_refs.append(f"visible-input:{occurrence['visible_input_sha256']}")
                evidence_refs.append(f"response:{occurrence['response_hash']}")
                evidence_refs.extend(
                    f"source-event:{value}"
                    for value in json.loads(str(occurrence["source_event_refs_json"]))
                )
            evidence_refs = list(dict.fromkeys(evidence_refs))
            root_status, root_code, repair = self._diagnosis_for_codes(codes)
            occurrence_set_hash = _sha256([str(row["occurrence_id"]) for row in occurrences])
            report_id = (
                "root-cause-"
                + _sha256(
                    {
                        "incident_id": incident_id,
                        "occurrence_set_hash": occurrence_set_hash,
                        "root_cause_code": root_code,
                    }
                ).removeprefix("sha256:")[:24]
            )
            first_occurrence_id = str(occurrences[0]["occurrence_id"])
            reproduction_command = (
                "python3 scripts/replay_distillation_failure.py "
                f"--occurrence-id {first_occurrence_id} --dry-run --json"
            )
            verification = (
                "Run the same replay command after repair and require a terminal "
                "replay receipt before resolving the incident."
            )
            conn.execute(
                """
                INSERT INTO root_cause_reports (
                    report_id, incident_id, diagnostic_task_id,
                    report_revision,
                    root_cause_status, root_cause_code, evidence_refs_json,
                    reproduction_command, repair, verification, created_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    incident_id,
                    str(task["task_id"]),
                    root_status,
                    root_code,
                    _canonical_json(evidence_refs),
                    reproduction_command,
                    repair,
                    verification,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE diagnostic_tasks
                SET status='reported', updated_at=?
                WHERE task_id=?
                """,
                (now, str(task["task_id"])),
            )
            if root_status == "confirmed":
                conn.execute(
                    "UPDATE operational_incidents SET status='diagnosed' WHERE incident_id=?",
                    (incident_id,),
                )
            payload = {
                "schema_version": "mnemos.operational_incident_notification.v1",
                "incident_id": incident_id,
                "report_id": report_id,
                "root_cause_status": root_status,
                "root_cause_code": root_code,
                "message": (
                    "Operational incident diagnosis is available. "
                    "Notification does not create or finalize a retrospective."
                ),
            }
            command_id = f"notify-{incident_id}"
            conn.execute(
                """
                INSERT INTO incident_notification_commands (
                    command_id, incident_id, report_id, status,
                    payload_json, lease_expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, '', ?, ?)
                """,
                (
                    command_id,
                    incident_id,
                    report_id,
                    _canonical_json(payload),
                    now,
                    now,
                ),
            )
        return {
            "report_id": report_id,
            "incident_id": incident_id,
            "diagnostic_task_id": str(task["task_id"]),
            "root_cause_status": root_status,
            "root_cause_code": root_code,
            "evidence_refs": evidence_refs,
            "reproduction_command": reproduction_command,
            "repair": repair,
            "verification": verification,
        }

    def append_root_cause_report(
        self,
        incident_id: str,
        *,
        root_cause_status: str,
        root_cause_code: str,
        evidence_refs: Iterable[str],
        reproduction_command: str,
        repair: str,
        verification: str,
    ) -> dict[str, Any]:
        """Append a diagnostic revision without replacing prior evidence."""

        if root_cause_status not in {"confirmed", "investigating"}:
            raise ValueError("invalid root cause status")
        submitted_refs = list(
            dict.fromkeys(str(ref).strip() for ref in evidence_refs if str(ref).strip())
        )
        if (
            not root_cause_code.strip()
            or not submitted_refs
            or not reproduction_command.strip()
            or not repair.strip()
            or not verification.strip()
        ):
            raise ValueError("root cause report evidence is incomplete")
        if root_cause_status == "confirmed" and (
            root_cause_code.startswith("symptom_")
            or root_cause_code == "root_cause_unresolved"
        ):
            raise ValueError("confirmed root cause requires independent diagnostic proof")
        refs = submitted_refs
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute(
                "SELECT task_id FROM diagnostic_tasks WHERE incident_id=?",
                (incident_id,),
            ).fetchone()
            if task is None:
                raise ValueError("unknown incident diagnostic task")
            if root_cause_status == "confirmed":
                proof_rows = conn.execute(
                    """
                    SELECT evidence_id, evidence_hash, reproduction_command
                    FROM incident_diagnostic_evidence
                    WHERE incident_id=? AND diagnostic_task_id=?
                      AND producer='formal_diagnostic_reproducer.v1'
                      AND before_status='failed' AND after_status='passed'
                    """,
                    (incident_id, str(task["task_id"])),
                ).fetchall()
                selected_proofs = [
                    row
                    for row in proof_rows
                    if (
                        f"diagnostic-evidence:{row['evidence_id']}:{row['evidence_hash']}"
                        in submitted_refs
                    )
                ]
                if not selected_proofs:
                    raise ValueError(
                        "confirmed root cause requires durable failed-to-passed diagnostic proof"
                    )
                allowed_root_codes = {
                    root_cause_code_for_reproducer(
                        str(row["reproduction_command"]).removeprefix(
                            "registered-diagnostic:"
                        )
                    )
                    for row in selected_proofs
                }
                if root_cause_code not in allowed_root_codes:
                    raise ValueError(
                        "confirmed root cause code is not bound to the diagnostic proof"
                    )
            previous = conn.execute(
                """
                SELECT evidence_refs_json FROM root_cause_reports
                WHERE incident_id=?
                ORDER BY report_revision DESC LIMIT 1
                """,
                (incident_id,),
            ).fetchone()
            if previous is not None:
                refs = list(
                    dict.fromkeys(
                        [
                            *json.loads(str(previous["evidence_refs_json"])),
                            *refs,
                        ]
                    )
                )
            revision = int(
                conn.execute(
                    """
                    SELECT COALESCE(MAX(report_revision), 0) + 1
                    FROM root_cause_reports WHERE incident_id=?
                    """,
                    (incident_id,),
                ).fetchone()[0]
            )
            report_id = (
                "root-cause-"
                + _sha256(
                    {
                        "incident_id": incident_id,
                        "revision": revision,
                        "root_cause_status": root_cause_status,
                        "root_cause_code": root_cause_code,
                        "evidence_refs": refs,
                    }
                ).removeprefix("sha256:")[:24]
            )
            conn.execute(
                """
                INSERT INTO root_cause_reports (
                    report_id, incident_id, diagnostic_task_id, report_revision,
                    root_cause_status, root_cause_code, evidence_refs_json,
                    reproduction_command, repair, verification, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    incident_id,
                    str(task["task_id"]),
                    revision,
                    root_cause_status,
                    root_cause_code,
                    _canonical_json(refs),
                    reproduction_command,
                    repair,
                    verification,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE diagnostic_tasks SET status='reported', updated_at=?
                WHERE task_id=?
                """,
                (now, str(task["task_id"])),
            )
            if root_cause_status == "confirmed":
                conn.execute(
                    """
                    UPDATE operational_incidents SET status='diagnosed'
                    WHERE incident_id=? AND status='investigating'
                    """,
                    (incident_id,),
                )
        return {
            "report_id": report_id,
            "incident_id": incident_id,
            "diagnostic_task_id": str(task["task_id"]),
            "report_revision": revision,
            "root_cause_status": root_cause_status,
            "root_cause_code": root_cause_code,
            "evidence_refs": refs,
            "reproduction_command": reproduction_command,
            "repair": repair,
            "verification": verification,
        }

    def create_replay_command(
        self,
        incident_id: str,
        *,
        occurrence_id: str,
    ) -> dict[str, Any]:
        """Create the only allowed repair path for a failed distillation artifact."""

        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            report = conn.execute(
                """
                SELECT report_id FROM root_cause_reports
                WHERE incident_id=? AND root_cause_status='confirmed'
                ORDER BY report_revision DESC LIMIT 1
                """,
                (incident_id,),
            ).fetchone()
            if report is None:
                raise RuntimeError("formal replay requires a confirmed root cause report")
            occurrence = conn.execute(
                """
                SELECT session_id, prompt_hash, visible_input_sha256,
                       response_hash, source_event_refs_json, artifact_hash
                FROM incident_occurrences
                WHERE incident_id=? AND occurrence_id=?
                """,
                (incident_id, occurrence_id),
            ).fetchone()
            if occurrence is None:
                raise ValueError("replay occurrence does not belong to incident")
            input_binding_hash = canonical_replay_input_binding_hash(
                session_id=str(occurrence["session_id"]),
                prompt_hash=str(occurrence["prompt_hash"]),
                visible_input_sha256=str(occurrence["visible_input_sha256"]),
                response_hash=str(occurrence["response_hash"]),
                source_event_refs=json.loads(str(occurrence["source_event_refs_json"])),
                artifact_hash=str(occurrence["artifact_hash"]),
            )
            command_id = f"replay-{occurrence_id}"
            conn.execute(
                """
                INSERT OR IGNORE INTO incident_replay_commands (
                    command_id, incident_id, occurrence_id, artifact_hash,
                    input_binding_hash,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    command_id,
                    incident_id,
                    occurrence_id,
                    str(occurrence["artifact_hash"]),
                    input_binding_hash,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM incident_replay_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
        return dict(row)

    def record_replay_receipt(
        self,
        command_id: str,
        *,
        status: str,
        output_hash: str,
        input_binding_hash: str,
        executor: str,
    ) -> dict[str, Any]:
        """Append one terminal receipt for a previously created replay command."""

        if status not in {"committed", "failed"}:
            raise ValueError("invalid replay receipt status")
        if not output_hash.startswith("sha256:") or len(output_hash) != 71:
            raise ValueError("replay output hash must be a canonical sha256")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", input_binding_hash):
            raise ValueError("replay input binding must be a canonical sha256")
        if executor != "formal_distillation_replay.v1":
            raise ValueError("replay receipt executor is not canonical")
        now = _now()
        receipt_id = f"replay-receipt-{command_id}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            command = conn.execute(
                "SELECT * FROM incident_replay_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if command is None:
                raise ValueError("unknown replay command")
            if input_binding_hash != str(command["input_binding_hash"]):
                raise ValueError("replay receipt input binding mismatch")
            conn.execute(
                """
                INSERT INTO incident_replay_receipts (
                    receipt_id, command_id, incident_id, status,
                    output_hash, input_binding_hash, executor, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    command_id,
                    str(command["incident_id"]),
                    status,
                    output_hash,
                    input_binding_hash,
                    executor,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE incident_replay_commands
                SET status=?, updated_at=? WHERE command_id=?
                """,
                (status, now, command_id),
            )
        return {
            "receipt_id": receipt_id,
            "command_id": command_id,
            "incident_id": str(command["incident_id"]),
            "status": status,
            "output_hash": output_hash,
            "input_binding_hash": input_binding_hash,
            "executor": executor,
            "created_at": now,
        }

    def resolve_incident(
        self,
        incident_id: str,
        *,
        repair_ref: str,
        verification_ref: str,
    ) -> dict[str, Any]:
        """Resolve a diagnosed incident only after a committed formal replay."""

        if not repair_ref.strip() or not verification_ref.strip():
            raise ValueError("incident resolution evidence is incomplete")
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            receipt = conn.execute(
                """
                SELECT receipt.receipt_id
                FROM incident_replay_receipts AS receipt
                JOIN incident_replay_commands AS command
                  ON command.command_id=receipt.command_id
                 AND command.input_binding_hash=receipt.input_binding_hash
                WHERE receipt.incident_id=?
                  AND receipt.status='committed'
                  AND receipt.executor='formal_distillation_replay.v1'
                ORDER BY receipt.created_at DESC LIMIT 1
                """,
                (incident_id,),
            ).fetchone()
            if receipt is None:
                raise RuntimeError("incident resolution requires a committed replay receipt")
            report = conn.execute(
                """
                SELECT report_id FROM root_cause_reports
                WHERE incident_id=? AND root_cause_status='confirmed'
                ORDER BY report_revision DESC LIMIT 1
                """,
                (incident_id,),
            ).fetchone()
            if report is None:
                raise RuntimeError("incident resolution requires a confirmed diagnosis")
            resolution_id = f"resolution-{incident_id}"
            conn.execute(
                """
                INSERT INTO incident_resolutions (
                    resolution_id, incident_id, replay_receipt_id,
                    repair_ref, verification_ref, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    resolution_id,
                    incident_id,
                    str(receipt["receipt_id"]),
                    repair_ref,
                    verification_ref,
                    now,
                ),
            )
            updated = conn.execute(
                """
                UPDATE operational_incidents
                SET status='resolved', resolved_at=?
                WHERE incident_id=? AND status='diagnosed'
                """,
                (now, incident_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("only a diagnosed incident can be resolved")
        return {
            "incident_id": incident_id,
            "resolution_id": resolution_id,
            "status": "resolved",
            "resolved_at": now,
        }


__all__ = [
    "DialogReminderIncidentNotificationAdapter",
    "DistillationFailureEvidence",
    "IncidentRecordResult",
    "OperationalIncidentStore",
    "SCHEMA_HASH",
    "SCHEMA_VERSION",
    "canonical_replay_input_binding_hash",
    "initialize_operational_incident_schema",
    "validate_operational_incident_schema",
]
