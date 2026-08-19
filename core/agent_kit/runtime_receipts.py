"""Durable, content-free receipts for verified Agent Kit runtime probes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core.agent_kit.authorization import AgentAuthorizationStore
from core.agent_kit.protocol import TARGET_AGENT_NAMES, normalize_agent_name
from core.agent_kit.runtime_probe_contract import (
    EXPECTED_RUNTIME_PROBE_COMPLETENESS,
    RUNTIME_PROBE_ASSISTANT_CONTENT,
    RUNTIME_PROBE_CALL_ID,
    RUNTIME_PROBE_SCHEMA_VERSION,
    RUNTIME_PROBE_USER_CONTENT,
    runtime_probe_canary_hash,
    runtime_probe_contract,
)
from core.agent_kit.source_support_manifest import get_agent_source_support_manifest
from core.config import get_config
from core.ops.durable_io import DurableIOError, inspect_path_kind
from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH
from core.ops.readiness_query_budget import connect_readonly_sqlite
from core.utils import secure_directory, secure_file

SOURCE_CAPTURE_RECEIPT_SCHEMA_VERSION = "mnemos.agent_source_capture_receipt.v1"
_SOURCE_CAPTURE_EVIDENCE_SCHEMA_VERSION = "mnemos.agent_source_capture_evidence.v4"
_SOURCE_CAPTURE_EVIDENCE_KEYS = {
    "schema_version",
    "source_name",
    "support_manifest_hash",
    "native_source_snapshot_hash",
    "capture_completeness",
    "ok",
    "errors",
}
_SOURCE_CAPTURE_COMPLETENESS_KEYS = {
    "schema_version",
    "discovery_covered",
    "content_parsed",
    "raw_committed",
    "discovered_sessions",
    "native_turns",
    "parsed_turns",
    "raw_committed_turns",
    "typed_empty_sessions",
    "evidence_excluded_sessions",
    "session_disposition_hash",
    "raw_revision_ids_hash",
    "cursor_roster_hash",
    "capture_generation_id",
    "capture_generation_eligible",
    "capture_expected_turn_count",
    "capture_receipt_count",
    "capture_exact_receipt_count",
    "capture_pending_turn_count",
    "capture_orphan_receipt_count",
    "capture_denominator_session_set_hash",
    "capture_expected_turn_fingerprint_set_hash",
    "capture_receipt_binding_set_hash",
    "runtime_canary_verified",
    "runtime_canary_hash",
    "runtime_receipt_id_hash",
    "runtime_canary_raw_revision_ids_hash",
}


class AgentRuntimeReceiptStateError(RuntimeError):
    """An existing receipt store is unavailable, never semantically missing."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _authorization_state(db_path: Path, agent: str) -> str:
    try:
        record = AgentAuthorizationStore(db_path, initialize=False).get_record(agent)
    except (OSError, sqlite3.Error, ValueError):
        return "unavailable"
    return record.state if record is not None else "detected"


def _validate_sample(sample: Mapping[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    if set(sample) != {
        "schema_version",
        "user_content",
        "assistant_content",
        "tool_calls",
        "tool_results",
        "completeness",
    }:
        return False, "runtime probe fields do not match the synthetic-safe contract", {}
    if sample.get("schema_version") != RUNTIME_PROBE_SCHEMA_VERSION:
        return False, "runtime probe schema_version is unsupported", {}
    if sample.get("user_content") != RUNTIME_PROBE_USER_CONTENT:
        return False, "runtime probe user_content is not the safe sentinel", {}
    if sample.get("assistant_content") != RUNTIME_PROBE_ASSISTANT_CONTENT:
        return False, "runtime probe assistant_content is not the safe sentinel", {}
    expected_calls = [{"id": RUNTIME_PROBE_CALL_ID, "name": "health_check", "arguments": {}}]
    expected_results = [{"tool_call_id": RUNTIME_PROBE_CALL_ID, "status": "ok"}]
    if sample.get("tool_calls") != expected_calls:
        return False, "runtime probe tool_calls are malformed", {}
    if sample.get("tool_results") != expected_results:
        return False, "runtime probe tool_results are malformed", {}
    completeness = sample.get("completeness")
    if completeness != EXPECTED_RUNTIME_PROBE_COMPLETENESS:
        return False, "runtime probe completeness is malformed", {}
    return True, "", dict(EXPECTED_RUNTIME_PROBE_COMPLETENESS)


def _is_sha256(value: object) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _validate_source_capture_evidence(
    evidence: Mapping[str, Any],
    *,
    agent: str,
    support_manifest_hash: str,
    runtime_receipt: Mapping[str, Any],
) -> tuple[bool, str, dict[str, Any], str]:
    """Reject caller-shaped source reports before they become a full-power proof."""
    if set(evidence) != _SOURCE_CAPTURE_EVIDENCE_KEYS:
        return False, "source capture evidence fields are malformed", {}, ""
    if evidence.get("schema_version") != _SOURCE_CAPTURE_EVIDENCE_SCHEMA_VERSION:
        return False, "source capture evidence schema is unsupported", {}, ""
    if normalize_agent_name(str(evidence.get("source_name") or "")) != agent:
        return False, "source capture evidence agent mismatch", {}, ""
    if evidence.get("support_manifest_hash") != support_manifest_hash:
        return False, "source capture evidence manifest hash mismatch", {}, ""
    snapshot_hash = str(evidence.get("native_source_snapshot_hash") or "")
    if not _is_sha256(snapshot_hash):
        return False, "source capture evidence snapshot hash is malformed", {}, ""
    completeness = evidence.get("capture_completeness")
    if (
        not isinstance(completeness, Mapping)
        or set(completeness) != _SOURCE_CAPTURE_COMPLETENESS_KEYS
    ):
        return False, "source capture completeness fields are malformed", {}, ""
    if completeness.get("schema_version") != _SOURCE_CAPTURE_EVIDENCE_SCHEMA_VERSION:
        return False, "source capture completeness schema is unsupported", {}, ""
    for key in ("discovery_covered", "content_parsed", "raw_committed"):
        if completeness.get(key) is not True:
            return False, f"source capture completeness {key} is not verified", {}, ""
    integer_keys = (
        "discovered_sessions",
        "native_turns",
        "parsed_turns",
        "raw_committed_turns",
        "typed_empty_sessions",
        "evidence_excluded_sessions",
        "capture_expected_turn_count",
        "capture_receipt_count",
        "capture_exact_receipt_count",
        "capture_pending_turn_count",
        "capture_orphan_receipt_count",
    )
    for key in integer_keys:
        value = completeness.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False, f"source capture completeness {key} is malformed", {}, ""
    native_turns = int(completeness["native_turns"])
    if native_turns <= 0:
        return False, "source capture completeness has no native turn denominator", {}, ""
    if (
        completeness.get("parsed_turns") != native_turns
        or completeness.get("raw_committed_turns") != native_turns
    ):
        return False, "source capture denominator does not reconcile to Raw", {}, ""
    if int(completeness["typed_empty_sessions"]) + int(
        completeness["evidence_excluded_sessions"]
    ) > int(completeness["discovered_sessions"]):
        return False, "source capture session dispositions do not conserve", {}, ""
    if not _is_sha256(completeness.get("session_disposition_hash")):
        return False, "source capture session disposition hash is malformed", {}, ""
    if not _is_sha256(completeness.get("raw_revision_ids_hash")):
        return False, "source capture Raw revision hash is malformed", {}, ""
    if not str(completeness.get("cursor_roster_hash") or ""):
        return False, "source capture cursor roster hash is missing", {}, ""
    if not str(completeness.get("capture_generation_id") or ""):
        return False, "source capture generation identity is missing", {}, ""
    if completeness.get("capture_generation_eligible") is not True:
        return False, "source capture generation is not binding eligible", {}, ""
    if (
        completeness.get("capture_expected_turn_count") != native_turns
        or completeness.get("capture_receipt_count") != native_turns
        or completeness.get("capture_exact_receipt_count") != native_turns
        or completeness.get("capture_pending_turn_count") != 0
        or completeness.get("capture_orphan_receipt_count") != 0
    ):
        return False, "source capture exact receipt set does not reconcile", {}, ""
    for key in (
        "capture_denominator_session_set_hash",
        "capture_expected_turn_fingerprint_set_hash",
        "capture_receipt_binding_set_hash",
    ):
        if not _is_sha256(completeness.get(key)):
            return False, f"source capture completeness {key} is malformed", {}, ""
    if completeness.get("runtime_canary_verified") is not True:
        return False, "source capture runtime canary is not independently verified", {}, ""
    canary_hash = str(completeness.get("runtime_canary_hash") or "")
    if not _is_sha256(canary_hash):
        return False, "source capture runtime canary hash is malformed", {}, ""
    if canary_hash != str(runtime_receipt.get("runtime_canary_hash") or ""):
        return False, "source capture runtime canary hash does not match runtime receipt", {}, ""
    expected_receipt_id_hash = hashlib.sha256(
        str(runtime_receipt.get("receipt_id") or "").encode("utf-8")
    ).hexdigest()
    if completeness.get("runtime_receipt_id_hash") != expected_receipt_id_hash:
        return False, "source capture runtime receipt identity does not match", {}, ""
    if not _is_sha256(completeness.get("runtime_canary_raw_revision_ids_hash")):
        return False, "source capture runtime canary Raw binding is malformed", {}, ""
    if evidence.get("ok") is not True or evidence.get("errors") != []:
        return False, "source capture evidence is not a clean verification", {}, ""
    return True, "", dict(completeness), snapshot_hash


class AgentRuntimeReceiptStore:
    """Own the latest per-agent runtime capability verdict and its receipt."""

    def __init__(self, db_path: Path | None = None, *, initialize: bool = True):
        self.db_path = Path(db_path or get_config().database_dir / "agent_authorization.db")
        if initialize:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            secure_directory(self.db_path.parent)
            self._init_db()
            self._secure_storage_files()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _connect_read_only(self) -> sqlite3.Connection:
        conn = connect_readonly_sqlite(self.db_path, timeout_seconds=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _store_is_missing(self, unavailable_code: str) -> bool:
        try:
            return inspect_path_kind(self.db_path) == "missing"
        except DurableIOError as exc:
            raise AgentRuntimeReceiptStateError(unavailable_code) from exc

    def _secure_storage_files(self) -> None:
        for path in (self.db_path, Path(f"{self.db_path}-wal"), Path(f"{self.db_path}-shm")):
            secure_file(path)

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_health_roundtrips (
                    agent TEXT PRIMARY KEY,
                    health_check_ids_hash TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    schema_version INTEGER NOT NULL DEFAULT 1
                )
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_runtime_receipts (
                    agent TEXT PRIMARY KEY,
                    receipt_id TEXT NOT NULL,
                    runtime_state TEXT NOT NULL,
                    authorization_state TEXT NOT NULL,
                    runtime_receipt_at TEXT NOT NULL,
                    health_check_ids_hash TEXT NOT NULL,
                    sample_completeness_json TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    schema_version INTEGER NOT NULL DEFAULT 1
                )
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_source_capture_receipts (
                    agent TEXT PRIMARY KEY,
                    receipt_id TEXT NOT NULL,
                    source_capture_state TEXT NOT NULL,
                    authorization_state TEXT NOT NULL,
                    source_capture_receipt_at TEXT NOT NULL,
                    health_check_ids_hash TEXT NOT NULL,
                    support_manifest_hash TEXT NOT NULL,
                    native_source_snapshot_hash TEXT NOT NULL,
                    capture_completeness_json TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    schema_version INTEGER NOT NULL DEFAULT 1
                )
                """)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_health_check(
        self,
        agent: str,
        health_check_ids_hash: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Record that an authenticated host reached the canonical health tool."""
        normalized_agent = normalize_agent_name(agent)
        if normalized_agent not in TARGET_AGENT_NAMES:
            raise ValueError(f"unsupported agent: {agent}")
        observed_at = _iso(now or _utc_now())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_health_roundtrips (
                    agent, health_check_ids_hash, observed_at, schema_version
                ) VALUES (?, ?, ?, 1)
                """,
                (normalized_agent, str(health_check_ids_hash), observed_at),
            )
        self._secure_storage_files()
        return {
            "agent": normalized_agent,
            "health_check_ids_hash": str(health_check_ids_hash),
            "observed_at": observed_at,
        }

    def get_health_check(self, agent: str) -> dict[str, Any]:
        normalized_agent = normalize_agent_name(agent)
        if self._store_is_missing("agent_health_receipt_store_unavailable"):
            return {}
        try:
            with self._connect_read_only() as conn:
                row = conn.execute(
                    "SELECT * FROM agent_health_roundtrips WHERE agent = ?",
                    (normalized_agent,),
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise AgentRuntimeReceiptStateError("agent_health_receipt_store_unavailable") from exc
        if row is None:
            return {}
        return {
            "agent": str(row["agent"]),
            "health_check_ids_hash": str(row["health_check_ids_hash"]),
            "observed_at": str(row["observed_at"]),
        }

    def _health_roundtrip_failure(
        self,
        agent: str,
        submitted_hash: str,
        *,
        now: datetime,
        max_age_seconds: int,
    ) -> tuple[str, str] | None:
        observation = self.get_health_check(agent)
        if not observation:
            return "health_roundtrip_missing", "authenticated health roundtrip missing"
        if observation["health_check_ids_hash"] != submitted_hash:
            return (
                "health_check_set_mismatch",
                "health roundtrip hash does not match the submitted probe",
            )
        try:
            observed_at = datetime.fromisoformat(observation["observed_at"])
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            comparable_now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
            age = (comparable_now - observed_at).total_seconds()
        except (TypeError, ValueError):
            return "health_roundtrip_malformed", "health roundtrip timestamp is malformed"
        if age < 0 or age > max(1, int(max_age_seconds)):
            return "health_roundtrip_stale", "authenticated health roundtrip is stale"
        return None

    def _write_attempt(
        self,
        agent: str,
        *,
        runtime_state: str,
        authorization_state: str,
        runtime_receipt_at: str,
        health_check_ids_hash: str,
        sample_completeness: Mapping[str, Any],
        support_manifest_hash: str,
        runtime_canary_hash: str,
        error: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_runtime_receipts (
                    agent, receipt_id, runtime_state, authorization_state,
                    runtime_receipt_at, health_check_ids_hash,
                    sample_completeness_json, error, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    agent,
                    str(uuid.uuid4()),
                    runtime_state,
                    authorization_state,
                    runtime_receipt_at,
                    health_check_ids_hash,
                    json.dumps(
                        {
                            "schema_version": "mnemos.agent_runtime_receipt_payload.v3",
                            "sample_completeness": dict(sample_completeness),
                            "support_manifest_hash": support_manifest_hash,
                            "runtime_canary_hash": runtime_canary_hash,
                        },
                        sort_keys=True,
                    ),
                    error,
                ),
            )
        self._secure_storage_files()

    def record_probe(
        self,
        agent: str,
        *,
        health_check_ids_hash: str,
        sample: Mapping[str, Any],
        now: datetime | None = None,
        health_max_age_seconds: int = 300,
    ) -> dict[str, Any]:
        normalized_agent = normalize_agent_name(agent)
        if normalized_agent not in TARGET_AGENT_NAMES:
            raise ValueError(f"unsupported agent: {agent}")
        support_manifest = get_agent_source_support_manifest()
        support_manifest.require_host_agent(normalized_agent)
        current_time = now or _utc_now()
        timestamp = _iso(current_time)
        authorization_state = _authorization_state(self.db_path, normalized_agent)
        runtime_state = "verified"
        error = ""
        sample_completeness: dict[str, Any] = {}
        canary_hash = ""
        if not AgentAuthorizationStore.content_access_authorized(authorization_state):
            runtime_state = "authorization_denied"
            error = "content access is not user-authorized"
        else:
            health_failure = self._health_roundtrip_failure(
                normalized_agent,
                str(health_check_ids_hash),
                now=current_time,
                max_age_seconds=health_max_age_seconds,
            )
            if health_failure is not None:
                runtime_state, error = health_failure
        if runtime_state == "verified" and (
            health_check_ids_hash != CANONICAL_HEALTH_CHECK_IDS_HASH
        ):
            runtime_state = "health_check_set_mismatch"
            error = "health check id set does not match the current runtime"
        if runtime_state == "verified":
            if not isinstance(sample, Mapping):
                valid, error, sample_completeness = (
                    False,
                    "runtime probe sample must be an object",
                    {},
                )
            else:
                valid, error, sample_completeness = _validate_sample(sample)
            if not valid:
                runtime_state = "malformed_sample"
            else:
                canary_hash = runtime_probe_canary_hash(
                    health_check_ids_hash=str(health_check_ids_hash),
                    sample=sample,
                )
        self._write_attempt(
            normalized_agent,
            runtime_state=runtime_state,
            authorization_state=authorization_state,
            runtime_receipt_at=timestamp,
            health_check_ids_hash=str(health_check_ids_hash),
            sample_completeness=sample_completeness,
            support_manifest_hash=support_manifest.manifest_hash,
            runtime_canary_hash=canary_hash,
            error=error,
        )
        return self.evaluate(normalized_agent, now=now)

    def _write_source_capture_attempt(
        self,
        agent: str,
        *,
        source_capture_state: str,
        authorization_state: str,
        source_capture_receipt_at: str,
        health_check_ids_hash: str,
        support_manifest_hash: str,
        native_source_snapshot_hash: str,
        capture_completeness: Mapping[str, Any],
        error: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_source_capture_receipts (
                    agent, receipt_id, source_capture_state, authorization_state,
                    source_capture_receipt_at, health_check_ids_hash,
                    support_manifest_hash, native_source_snapshot_hash,
                    capture_completeness_json, error, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    agent,
                    str(uuid.uuid4()),
                    source_capture_state,
                    authorization_state,
                    source_capture_receipt_at,
                    health_check_ids_hash,
                    support_manifest_hash,
                    native_source_snapshot_hash,
                    json.dumps(dict(capture_completeness), sort_keys=True),
                    error,
                ),
            )
        self._secure_storage_files()

    def record_source_capture(
        self,
        agent: str,
        *,
        coverage: Mapping[str, Any],
        cursor_db_path: Path,
        raw_db_path: Path,
        now: datetime | None = None,
        max_age_seconds: int = 86400,
    ) -> dict[str, Any]:
        """Recompute and persist source-to-Raw proof after a safe host probe.

        The persistence owner invokes the read-only auditor itself.  Callers
        provide the frozen coverage generation and canonical database paths,
        never caller-shaped verification booleans or hashes.
        """
        normalized_agent = normalize_agent_name(agent)
        if normalized_agent not in TARGET_AGENT_NAMES:
            raise ValueError(f"unsupported agent: {agent}")
        support_manifest = get_agent_source_support_manifest()
        support_manifest.require_host_agent(normalized_agent)
        current_time = now or _utc_now()
        authorization_state = _authorization_state(self.db_path, normalized_agent)
        runtime = self.evaluate(
            normalized_agent,
            max_age_seconds=max_age_seconds,
            now=current_time,
        )
        source_capture_state = "verified"
        error = ""
        completeness: dict[str, Any] = {}
        snapshot_hash = ""
        if runtime.get("runtime_state") != "verified":
            source_capture_state = "runtime_probe_unverified"
            error = str(runtime.get("error") or "runtime capability receipt is not verified")
        else:
            from core.agent_kit.source_capture_verification import verify_source_capture

            evidence = verify_source_capture(
                source_name=normalized_agent,
                coverage=coverage,
                cursor_db_path=Path(cursor_db_path),
                raw_db_path=Path(raw_db_path),
                runtime_receipt=runtime,
            )
            valid, error, completeness, snapshot_hash = _validate_source_capture_evidence(
                evidence,
                agent=normalized_agent,
                support_manifest_hash=support_manifest.manifest_hash,
                runtime_receipt=runtime,
            )
            if not valid:
                source_capture_state = "source_capture_invalid"
        self._write_source_capture_attempt(
            normalized_agent,
            source_capture_state=source_capture_state,
            authorization_state=authorization_state,
            source_capture_receipt_at=_iso(current_time),
            health_check_ids_hash=str(runtime.get("health_check_ids_hash") or ""),
            support_manifest_hash=support_manifest.manifest_hash,
            native_source_snapshot_hash=snapshot_hash,
            capture_completeness=completeness,
            error=error,
        )
        return self.evaluate_source_capture(
            normalized_agent,
            max_age_seconds=max_age_seconds,
            now=current_time,
        )

    def get_source_capture_receipt(self, agent: str) -> dict[str, Any]:
        """Return the content-free source capture receipt without provisioning state."""
        normalized_agent = normalize_agent_name(agent)
        if self._store_is_missing("agent_source_capture_receipt_store_unavailable"):
            return {}
        try:
            with self._connect_read_only() as conn:
                row = conn.execute(
                    "SELECT * FROM agent_source_capture_receipts WHERE agent = ?",
                    (normalized_agent,),
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise AgentRuntimeReceiptStateError(
                "agent_source_capture_receipt_store_unavailable"
            ) from exc
        if row is None:
            return {}
        try:
            completeness = json.loads(str(row["capture_completeness_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentRuntimeReceiptStateError("agent_source_capture_receipt_malformed") from exc
        if not isinstance(completeness, dict):
            raise AgentRuntimeReceiptStateError("agent_source_capture_receipt_malformed")
        return {
            "receipt_id": str(row["receipt_id"]),
            "agent": str(row["agent"]),
            "source_capture_state": str(row["source_capture_state"]),
            "authorization_state": str(row["authorization_state"]),
            "source_capture_receipt_at": str(row["source_capture_receipt_at"]),
            "health_check_ids_hash": str(row["health_check_ids_hash"]),
            "support_manifest_hash": str(row["support_manifest_hash"]),
            "native_source_snapshot_hash": str(row["native_source_snapshot_hash"]),
            "capture_completeness": completeness,
            "error": str(row["error"] or ""),
        }

    def get_receipt(self, agent: str) -> dict[str, Any]:
        normalized_agent = normalize_agent_name(agent)
        if self._store_is_missing("agent_runtime_receipt_store_unavailable"):
            return {}
        try:
            with self._connect_read_only() as conn:
                row = conn.execute(
                    "SELECT * FROM agent_runtime_receipts WHERE agent = ?",
                    (normalized_agent,),
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise AgentRuntimeReceiptStateError("agent_runtime_receipt_store_unavailable") from exc
        if row is None:
            return {}
        try:
            stored_payload = json.loads(str(row["sample_completeness_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentRuntimeReceiptStateError("agent_runtime_receipt_malformed") from exc
        if not isinstance(stored_payload, dict):
            raise AgentRuntimeReceiptStateError("agent_runtime_receipt_malformed")
        if "sample_completeness" in stored_payload:
            payload_schema_version = str(stored_payload.get("schema_version") or "")
            completeness = stored_payload.get("sample_completeness")
            support_manifest_hash = str(stored_payload.get("support_manifest_hash") or "")
            canary_hash = str(stored_payload.get("runtime_canary_hash") or "")
        else:
            # Prior-schema receipts cannot certify a current support manifest.
            payload_schema_version = ""
            completeness = stored_payload
            support_manifest_hash = ""
            canary_hash = ""
        return {
            "receipt_id": str(row["receipt_id"]),
            "agent": str(row["agent"]),
            "runtime_state": str(row["runtime_state"]),
            "authorization_state": str(row["authorization_state"]),
            "runtime_receipt_at": str(row["runtime_receipt_at"]),
            "health_check_ids_hash": str(row["health_check_ids_hash"]),
            "payload_schema_version": payload_schema_version,
            "sample_completeness": completeness if isinstance(completeness, dict) else {},
            "support_manifest_hash": support_manifest_hash,
            "runtime_canary_hash": canary_hash,
            "error": str(row["error"] or ""),
        }

    def evaluate(
        self,
        agent: str,
        *,
        max_age_seconds: int = 86400,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_agent = normalize_agent_name(agent)
        receipt = self.get_receipt(normalized_agent)
        if not receipt:
            return {
                "success": False,
                "agent": normalized_agent,
                "runtime_state": "missing",
                "authorization_state": _authorization_state(self.db_path, normalized_agent),
                "runtime_receipt_at": "",
                "health_check_ids_hash": "",
                "payload_schema_version": "",
                "sample_completeness": {},
                "support_manifest_hash": "",
                "runtime_canary_hash": "",
                "error": "runtime capability receipt missing",
            }
        authorization_state = _authorization_state(self.db_path, normalized_agent)
        receipt["authorization_state"] = authorization_state
        try:
            support_manifest = get_agent_source_support_manifest()
            support_manifest.require_host_agent(normalized_agent)
            expected_manifest_hash = support_manifest.manifest_hash
        except ValueError:
            expected_manifest_hash = ""
        if not AgentAuthorizationStore.content_access_authorized(authorization_state):
            receipt["runtime_state"] = "authorization_denied"
            receipt["error"] = "content access is not user-authorized"
        elif receipt.get("payload_schema_version") != ("mnemos.agent_runtime_receipt_payload.v3"):
            receipt["runtime_state"] = "runtime_receipt_payload_unsupported"
            receipt["error"] = "runtime receipt payload schema is not current"
        elif not receipt.get("support_manifest_hash"):
            receipt["runtime_state"] = "support_manifest_hash_missing"
            receipt["error"] = "runtime receipt is missing support_manifest_hash"
        elif receipt["support_manifest_hash"] != expected_manifest_hash:
            receipt["runtime_state"] = "support_manifest_hash_mismatch"
            receipt["error"] = (
                "runtime receipt support manifest does not match the current contract"
            )
        elif receipt["health_check_ids_hash"] != CANONICAL_HEALTH_CHECK_IDS_HASH:
            receipt["runtime_state"] = "health_check_set_mismatch"
            receipt["error"] = "health check id set does not match the current runtime"
        elif receipt["runtime_state"] == "verified" and not receipt.get("runtime_canary_hash"):
            receipt["runtime_state"] = "runtime_canary_hash_missing"
            receipt["error"] = "runtime receipt is missing its content-bound canary hash"
        elif receipt["runtime_state"] == "verified" and not _is_sha256(
            receipt.get("runtime_canary_hash")
        ):
            receipt["runtime_state"] = "runtime_canary_hash_malformed"
            receipt["error"] = "runtime receipt canary hash is malformed"
        elif receipt["runtime_state"] == "verified" and receipt.get(
            "runtime_canary_hash"
        ) != runtime_probe_canary_hash(
            health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
            sample=runtime_probe_contract()["sample"],
        ):
            receipt["runtime_state"] = "runtime_canary_hash_mismatch"
            receipt["error"] = "runtime receipt canary hash does not match the fixed probe"
        elif receipt["runtime_state"] == "verified":
            try:
                timestamp = datetime.fromisoformat(receipt["runtime_receipt_at"])
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                age = ((now or _utc_now()) - timestamp).total_seconds()
                if age < 0 or age > max(1, int(max_age_seconds)):
                    receipt["runtime_state"] = "stale"
                    receipt["error"] = "runtime capability receipt is stale"
            except (TypeError, ValueError):
                receipt["runtime_state"] = "malformed_receipt"
                receipt["error"] = "runtime capability receipt timestamp is malformed"
        receipt["success"] = receipt["runtime_state"] == "verified"
        return receipt

    def evaluate_source_capture(
        self,
        agent: str,
        *,
        max_age_seconds: int = 86400,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Evaluate the second half of full power: verified native→Raw capture."""
        normalized_agent = normalize_agent_name(agent)
        receipt = self.get_source_capture_receipt(normalized_agent)
        if not receipt:
            return {
                "success": False,
                "agent": normalized_agent,
                "source_capture_state": "missing",
                "source_capture_receipt_at": "",
                "health_check_ids_hash": "",
                "support_manifest_hash": "",
                "native_source_snapshot_hash": "",
                "capture_completeness": {},
                "error": "source capture receipt missing",
            }
        runtime = self.evaluate(normalized_agent, max_age_seconds=max_age_seconds, now=now)
        try:
            support_manifest = get_agent_source_support_manifest()
            support_manifest.require_host_agent(normalized_agent)
            expected_manifest_hash = support_manifest.manifest_hash
        except ValueError:
            expected_manifest_hash = ""
        authorization_state = _authorization_state(self.db_path, normalized_agent)
        receipt["authorization_state"] = authorization_state
        if not AgentAuthorizationStore.content_access_authorized(authorization_state):
            receipt["source_capture_state"] = "authorization_denied"
            receipt["error"] = "content access is not user-authorized"
        elif runtime.get("runtime_state") != "verified":
            receipt["source_capture_state"] = "runtime_probe_unverified"
            receipt["error"] = str(
                runtime.get("error") or "runtime capability receipt is not verified"
            )
        elif receipt.get("health_check_ids_hash") != CANONICAL_HEALTH_CHECK_IDS_HASH:
            receipt["source_capture_state"] = "health_check_set_mismatch"
            receipt["error"] = "source capture receipt health hash does not match current runtime"
        elif receipt.get("support_manifest_hash") != expected_manifest_hash:
            receipt["source_capture_state"] = "support_manifest_hash_mismatch"
            receipt["error"] = "source capture receipt manifest does not match current contract"
        elif receipt.get("source_capture_state") != "verified":
            # Preserve the typed rejection written by the attestation path;
            # do not replace it with a secondary missing-field symptom.
            pass
        elif not _is_sha256(receipt.get("native_source_snapshot_hash")):
            receipt["source_capture_state"] = "native_source_snapshot_hash_malformed"
            receipt["error"] = "source capture receipt snapshot hash is malformed"
        else:
            evidence: dict[str, Any] = {
                "schema_version": _SOURCE_CAPTURE_EVIDENCE_SCHEMA_VERSION,
                "source_name": normalized_agent,
                "support_manifest_hash": receipt.get("support_manifest_hash"),
                "native_source_snapshot_hash": receipt.get("native_source_snapshot_hash"),
                "capture_completeness": receipt.get("capture_completeness"),
                "ok": True,
                "errors": [],
            }
            valid, error, _completeness, _snapshot_hash = _validate_source_capture_evidence(
                evidence,
                agent=normalized_agent,
                support_manifest_hash=expected_manifest_hash,
                runtime_receipt=runtime,
            )
            if not valid:
                receipt["source_capture_state"] = "source_capture_invalid"
                receipt["error"] = error
            else:
                try:
                    timestamp = datetime.fromisoformat(str(receipt["source_capture_receipt_at"]))
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    age = ((now or _utc_now()) - timestamp).total_seconds()
                    if age < 0 or age > max(1, int(max_age_seconds)):
                        receipt["source_capture_state"] = "stale"
                        receipt["error"] = "source capture receipt is stale"
                except (TypeError, ValueError):
                    receipt["source_capture_state"] = "malformed_receipt"
                    receipt["error"] = "source capture receipt timestamp is malformed"
        receipt["success"] = receipt["source_capture_state"] == "verified"
        return receipt
