"""Auxiliary diagnostic and notification transitions for incident storage."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.ops.operational_incident_diagnostics import (
    diagnostic_issue_codes,
    execute_registered_diagnostic_reproducer,
)
from core.utils import read_bytes_value, read_text_value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class OperationalIncidentAuxiliaryMixin:
    """Keep secondary incident transitions out of the canonical store module."""

    def execute_diagnostic_reproducer(
        self,
        incident_id: str,
        *,
        occurrence_id: str,
        evidence_kind: str,
        source_refs: Iterable[str],
        reproducer_id: str,
        before_input: Mapping[str, Any],
        after_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Execute one registered production reproducer on before/after inputs."""

        refs = list(dict.fromkeys(str(ref).strip() for ref in source_refs if str(ref).strip()))
        if not evidence_kind.strip() or not refs or not reproducer_id.strip():
            raise ValueError("diagnostic evidence source or reproducer is missing")
        with self._connect() as conn:
            occurrence = conn.execute(
                """
                SELECT occurrence.error_codes_json, occurrence.validation_errors_json,
                       occurrence.artifact_path, occurrence.artifact_hash,
                       incident.failure_class
                FROM incident_occurrences AS occurrence
                JOIN operational_incidents AS incident
                  ON incident.incident_id=occurrence.incident_id
                WHERE occurrence.occurrence_id=? AND occurrence.incident_id=?
                """,
                (occurrence_id, incident_id),
            ).fetchone()
        if occurrence is None:
            raise ValueError("diagnostic reproducer occurrence is not bound to the incident")
        error_codes = set(json.loads(str(occurrence["error_codes_json"])))
        compatible_codes = {
            "distillation_fragment_contract.v1": {
                "schema_validation_failed",
                "correction_exhausted",
            }
        }
        if not error_codes.intersection(compatible_codes.get(reproducer_id, set())):
            raise ValueError("diagnostic reproducer is not relevant to the occurrence error codes")
        artifact_path = Path(str(occurrence["artifact_path"])).resolve(strict=True)
        artifact_hash = "sha256:" + hashlib.sha256(
            read_bytes_value(artifact_path)
        ).hexdigest()
        if artifact_hash != str(occurrence["artifact_hash"]):
            raise RuntimeError("diagnostic occurrence artifact binding changed")
        artifact_payload = json.loads(read_text_value(artifact_path))
        artifact_fragments = artifact_payload.get("fragments", [])
        if not isinstance(artifact_fragments, list) or not any(
            isinstance(fragment, dict)
            and _canonical_json(fragment) == _canonical_json(before_input)
            for fragment in artifact_fragments
        ):
            raise ValueError("diagnostic before fixture is not derived from the bound artifact")
        refs = list(
            dict.fromkeys(
                (
                    f"occurrence:{occurrence_id}",
                    f"artifact:{artifact_path}:{artifact_hash}",
                    *refs,
                )
            )
        )
        before = execute_registered_diagnostic_reproducer(reproducer_id, before_input)
        after = execute_registered_diagnostic_reproducer(reproducer_id, after_input)
        observed_issue_codes = diagnostic_issue_codes(
            list(json.loads(str(occurrence["validation_errors_json"])))
        )
        if not set(before.get("issue_codes", ())).intersection(observed_issue_codes):
            raise ValueError(
                "diagnostic before fixture does not match occurrence validation issues"
            )
        if before["status"] != "failed" or after["status"] != "passed":
            raise ValueError("diagnostic reproducer must fail before and pass after")
        reproduction_command = f"registered-diagnostic:{reproducer_id}"
        evidence_hash = _sha256(
            {
                "evidence_kind": evidence_kind,
                "source_refs": refs,
                "reproduction_command": reproduction_command,
                "reproducer_id": reproducer_id,
                "occurrence_id": occurrence_id,
                "occurrence_error_codes": sorted(error_codes),
                "occurrence_failure_class": str(occurrence["failure_class"]),
                "before_input_hash": _sha256(before_input),
                "after_input_hash": _sha256(after_input),
                "before": before,
                "after": after,
                "executor": "formal_diagnostic_reproducer.v1",
            }
        )
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute(
                "SELECT task_id FROM diagnostic_tasks WHERE incident_id=?",
                (incident_id,),
            ).fetchone()
            if task is None:
                raise ValueError("unknown incident diagnostic task")
            evidence_id = (
                "diagnostic-evidence-"
                + _sha256(
                    {
                        "incident_id": incident_id,
                        "evidence_kind": evidence_kind,
                        "evidence_hash": evidence_hash,
                        "source_refs": refs,
                        "reproduction_command": reproduction_command,
                        "reproducer_id": reproducer_id,
                        "before_status": before["status"],
                        "after_status": after["status"],
                    }
                ).removeprefix("sha256:")[:24]
            )
            conn.execute(
                """
                INSERT INTO incident_diagnostic_evidence (
                    evidence_id, incident_id, diagnostic_task_id, evidence_kind,
                    producer, evidence_hash, source_refs_json, reproduction_command,
                    before_status, after_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    incident_id,
                    str(task["task_id"]),
                    evidence_kind,
                    "formal_diagnostic_reproducer.v1",
                    evidence_hash,
                    _canonical_json(refs),
                    reproduction_command,
                    before["status"],
                    after["status"],
                    now,
                ),
            )
        return {
            "evidence_id": evidence_id,
            "evidence_hash": evidence_hash,
            "before_status": before["status"],
            "after_status": after["status"],
            "reproducer_id": reproducer_id,
            "executor": "formal_diagnostic_reproducer.v1",
        }

    def record_artifact_access(
        self,
        occurrence_id: str,
        *,
        principal: str,
        purpose: str,
    ) -> dict[str, Any]:
        """Append an ACL audit event for protected artifact access."""

        if not principal.strip() or not purpose.strip():
            raise ValueError("artifact access principal and purpose are required")
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            occurrence = conn.execute(
                """
                SELECT occurrence_id, incident_id, artifact_hash
                FROM incident_occurrences WHERE occurrence_id=?
                """,
                (occurrence_id,),
            ).fetchone()
            if occurrence is None:
                raise ValueError("unknown incident occurrence")
            access_id = f"artifact-access-{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO incident_artifact_access_events (
                    access_id, occurrence_id, incident_id, principal,
                    purpose, artifact_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    access_id,
                    occurrence_id,
                    str(occurrence["incident_id"]),
                    principal,
                    purpose,
                    str(occurrence["artifact_hash"]),
                    now,
                ),
            )
        return {
            "access_id": access_id,
            "occurrence_id": occurrence_id,
            "incident_id": str(occurrence["incident_id"]),
            "artifact_hash": str(occurrence["artifact_hash"]),
            "principal": principal,
            "purpose": purpose,
            "created_at": now,
        }

    def list_notification_commands(self, incident_id: str) -> list[dict[str, Any]]:
        """List durable notification commands with decoded payloads."""

        rows = self._list_rows(
            "incident_notification_commands",
            incident_id,
            order_by="created_at, command_id",
        )
        for row in rows:
            row["payload"] = json.loads(str(row.pop("payload_json")))
        return rows

    def dispatch_next_notification(self, adapter: Any) -> dict[str, Any] | None:
        """Deliver one durable notification command with a stable idempotency key."""

        now = _now()
        lease_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            command = conn.execute(
                """
                SELECT * FROM incident_notification_commands
                WHERE status='pending'
                   OR (status='processing' AND lease_expires_at <= ?)
                ORDER BY created_at, command_id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if command is None:
                return None
            command_id = str(command["command_id"])
            conn.execute(
                """
                UPDATE incident_notification_commands
                SET status='processing', lease_expires_at=?, updated_at=?
                WHERE command_id=?
                """,
                (lease_expires_at, now, command_id),
            )
            payload = json.loads(str(command["payload_json"]))
        attempt_id = f"notification-attempt-{uuid.uuid4().hex}"
        try:
            external_ref = str(adapter.deliver(payload, idempotency_key=command_id)).strip()
            if not external_ref:
                raise RuntimeError("notification adapter returned an empty receipt")
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            failed_at = _now()
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO incident_notification_attempts (
                        attempt_id, command_id, status, error_type, created_at
                    ) VALUES (?, ?, 'failed', ?, ?)
                    """,
                    (attempt_id, command_id, type(exc).__name__, failed_at),
                )
                conn.execute(
                    """
                    UPDATE incident_notification_commands
                    SET status='pending', lease_expires_at='', updated_at=?
                    WHERE command_id=?
                    """,
                    (failed_at, command_id),
                )
            return {
                "command_id": command_id,
                "status": "retry",
                "error_type": type(exc).__name__,
            }
        delivered_at = _now()
        payload_hash = _sha256(payload)
        receipt_id = f"notification-receipt-{command_id}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO incident_notification_attempts (
                    attempt_id, command_id, status, error_type, created_at
                ) VALUES (?, ?, 'committed', '', ?)
                """,
                (attempt_id, command_id, delivered_at),
            )
            conn.execute(
                """
                INSERT INTO incident_notification_receipts (
                    receipt_id, command_id, incident_id, report_id,
                    external_ref, payload_hash, delivered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    command_id,
                    str(command["incident_id"]),
                    str(command["report_id"]),
                    external_ref,
                    payload_hash,
                    delivered_at,
                ),
            )
            conn.execute(
                """
                UPDATE incident_notification_commands
                SET status='delivered', lease_expires_at='', updated_at=?
                WHERE command_id=?
                """,
                (delivered_at, command_id),
            )
        return {
            "command_id": command_id,
            "receipt_id": receipt_id,
            "external_ref": external_ref,
            "status": "delivered",
        }

    def list_notification_receipts(self, incident_id: str) -> list[dict[str, Any]]:
        """List committed notification delivery receipts."""

        return self._list_rows(
            "incident_notification_receipts",
            incident_id,
            order_by="delivered_at, receipt_id",
        )

    def propose_retrospective(
        self,
        incident_id: str,
        *,
        reusable_lesson: str,
    ) -> dict[str, Any]:
        """Propose a reusable lesson only after incident resolution."""

        lesson = reusable_lesson.strip()
        if not lesson:
            raise ValueError("retrospective proposal requires a reusable lesson")
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            resolution = conn.execute(
                "SELECT resolution_id FROM incident_resolutions WHERE incident_id=?",
                (incident_id,),
            ).fetchone()
            if resolution is None:
                raise RuntimeError("retrospective can be proposed only after incident resolution")
            retrospective_id = (
                "incident-retrospective-"
                + _sha256({"incident_id": incident_id, "reusable_lesson": lesson}).removeprefix(
                    "sha256:"
                )[:24]
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO incident_retrospectives (
                    retrospective_id, incident_id, resolution_id,
                    status, reusable_lesson, created_at
                ) VALUES (?, ?, ?, 'proposed', ?, ?)
                """,
                (
                    retrospective_id,
                    incident_id,
                    str(resolution["resolution_id"]),
                    lesson,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM incident_retrospectives WHERE retrospective_id=?",
                (retrospective_id,),
            ).fetchone()
        return dict(row)

    def protected_artifact_paths(self) -> set[Path]:
        """Return evidence paths owned by incidents that are not resolved."""

        with self._connect() as conn:
            rows = conn.execute("""
                SELECT occurrence.artifact_path
                FROM incident_occurrences AS occurrence
                JOIN operational_incidents AS incident
                  ON incident.incident_id=occurrence.incident_id
                WHERE incident.status != 'resolved'
                """).fetchall()
        return {Path(str(row["artifact_path"])).resolve(strict=False) for row in rows}

    def registered_artifact_paths(self) -> set[Path]:
        """Return every artifact path durably linked to an occurrence."""

        with self._connect() as conn:
            rows = conn.execute("SELECT artifact_path FROM incident_occurrences").fetchall()
        return {Path(str(row["artifact_path"])).resolve(strict=False) for row in rows}


__all__ = ["OperationalIncidentAuxiliaryMixin"]
