"""User cognitive profile v2 storage and assembly helpers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import (
    authorize_cognitive_access,
    derive_strictest_cognitive_access,
    make_cognitive_access_envelope,
    validate_cognitive_access_envelope,
)
from core.persona.profile_assertion_schema import (
    PROFILE_ASSERTION_PROJECTION_SQL,
    PROFILE_ASSERTION_SCHEMA_SQL,
    REGISTRY_SQL,
    inspect_profile_assertion_schema,
)
from core.persona.profile_access_schema import ensure_cognitive_profile_access_schema
from core.persona.profile_payload import build_profile_v2_payload, clamp_confidence
from core.persona.profile_usage_repository import (
    ProfileUsageLog,
    ProfileUsageRepositoryMixin,
    _PROFILE_READ_PURPOSES,
    _access_json,
    _restricted_profile_access,
    parse_json_list,
)

CORE = 30

# Backward import name retained for the explicit reconciliation command.  The
# DDL itself has exactly one owner in ``profile_assertion_schema``.
PROFILE_ASSERTION_REVISION_SCHEMA_SQL = PROFILE_ASSERTION_SCHEMA_SQL


PROFILE_SCHEMA_SQL = (
    """
-- 用户认知画像 v2：原始画像信号
CREATE TABLE IF NOT EXISTS profile_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_event_id TEXT NOT NULL,
    source_identity TEXT NOT NULL DEFAULT '',
    source_authority_id TEXT NOT NULL DEFAULT '',
    source_authority TEXT NOT NULL DEFAULT '' CHECK (source_authority IN ('', 'explicit_user')),
    source_revision_sha256 TEXT NOT NULL DEFAULT '',
    source_span_start INTEGER NOT NULL DEFAULT -1,
    source_span_end INTEGER NOT NULL DEFAULT -1,
    source_content_sha256 TEXT NOT NULL DEFAULT '',
    signal_type TEXT NOT NULL,
    dimension TEXT NOT NULL,
    value TEXT NOT NULL,
    evidence TEXT,
    confidence REAL DEFAULT 0.5,
    privacy_level TEXT DEFAULT 'local',
    observed_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT DEFAULT 'active',
    access_control TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_profile_signals_dimension ON profile_signals(dimension);
CREATE INDEX IF NOT EXISTS idx_profile_signals_status ON profile_signals(status);
CREATE INDEX IF NOT EXISTS idx_profile_signals_source ON profile_signals(source_event_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_signals_source_identity_unique
ON profile_signals(source_identity)
WHERE source_identity != '';

"""
    + PROFILE_ASSERTION_PROJECTION_SQL
    + PROFILE_ASSERTION_SCHEMA_SQL
    + """

-- 用户认知画像 v2：消费效果日志
CREATE TABLE IF NOT EXISTS profile_read_authorizations (
    token_id TEXT PRIMARY KEY,
    consumer TEXT NOT NULL,
    read_purpose TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    principal_agent TEXT NOT NULL,
    scope_snapshot TEXT NOT NULL,
    authorized_assertion_revisions TEXT NOT NULL,
    assertion_access_hashes TEXT NOT NULL,
    access_control TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('issued', 'consumed')),
    consumed_command_id TEXT NOT NULL DEFAULT '',
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_profile_read_authorizations_status
ON profile_read_authorizations(status, expires_at);

CREATE TABLE IF NOT EXISTS profile_usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    consumer TEXT NOT NULL,
    profile_fields_used TEXT NOT NULL,
    profile_revision_ids TEXT NOT NULL DEFAULT '[]',
    matched_assertion_revisions TEXT NOT NULL DEFAULT '{}',
    scope_snapshot TEXT NOT NULL DEFAULT '',
    read_purpose TEXT NOT NULL DEFAULT '',
    read_authorization_token TEXT NOT NULL DEFAULT '',
    action_changed INTEGER DEFAULT 0,
    outcome TEXT,
    user_feedback TEXT,
    request_id TEXT NOT NULL DEFAULT '',
    decision_id TEXT NOT NULL DEFAULT '',
    baseline_hash TEXT NOT NULL DEFAULT '',
    persona_enabled_hash TEXT NOT NULL DEFAULT '',
    expected_delta TEXT NOT NULL DEFAULT '{}',
    actual_target_delta TEXT NOT NULL DEFAULT '{}',
    target_receipt TEXT NOT NULL DEFAULT '{}',
    target_receipt_hash TEXT NOT NULL DEFAULT '',
    terminal_status TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL DEFAULT '',
    access_control TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_profile_usage_consumer ON profile_usage_log(consumer);
CREATE INDEX IF NOT EXISTS idx_profile_usage_created_at ON profile_usage_log(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_usage_idempotency
ON profile_usage_log(idempotency_key)
WHERE idempotency_key != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_usage_target_receipt
ON profile_usage_log(target_receipt_hash)
WHERE target_receipt_hash != '';

CREATE TABLE IF NOT EXISTS profile_usage_outbox (
    command_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    intent_json TEXT NOT NULL,
    target_receipt_hash TEXT NOT NULL UNIQUE,
    access_control TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'committed')),
    usage_id INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (usage_id) REFERENCES profile_usage_log(id)
);

CREATE INDEX IF NOT EXISTS idx_profile_usage_outbox_status
ON profile_usage_outbox(status, created_at);
"""
)

PROFILE_RUNTIME_SCHEMA_COMPONENT = "persona.profile_access_usage"
PROFILE_RUNTIME_SCHEMA_VERSION = "mnemos.profile_access_usage.v1"
_PROFILE_RUNTIME_TABLES = (
    "profile_signals",
    "profile_read_authorizations",
    "profile_usage_log",
    "profile_usage_outbox",
)
_PROFILE_RUNTIME_INDEXES = (
    "idx_profile_signals_dimension",
    "idx_profile_signals_status",
    "idx_profile_signals_source",
    "idx_profile_signals_source_identity_unique",
    "idx_profile_read_authorizations_status",
    "idx_profile_usage_consumer",
    "idx_profile_usage_created_at",
    "idx_profile_usage_idempotency",
    "idx_profile_usage_target_receipt",
    "idx_profile_usage_outbox_status",
)


def _normalize_schema_sql(value: Any) -> str:
    return " ".join(str(value or "").split()).lower()


def _profile_runtime_signature(conn: sqlite3.Connection) -> Dict[str, Any]:
    tables: Dict[str, Any] = {}
    for table in _PROFILE_RUNTIME_TABLES:
        tables[table] = {
            str(row[1]): [str(row[2]).upper(), int(row[3]), row[4], int(row[5])]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()  # nosec B608
        }
    placeholders = ",".join("?" for _ in _PROFILE_RUNTIME_INDEXES)
    index_rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        f"WHERE type='index' AND name IN ({placeholders})",  # nosec B608
        _PROFILE_RUNTIME_INDEXES,
    ).fetchall()
    indexes = {str(name): _normalize_schema_sql(sql) for name, sql in index_rows}
    foreign_keys = {
        table: sorted(
            [
                [str(row[2]), str(row[3]), str(row[4])]
                for row in conn.execute(
                    f"PRAGMA foreign_key_list({table})"
                ).fetchall()  # nosec B608
            ]
        )
        for table in _PROFILE_RUNTIME_TABLES
    }
    return {
        "tables": tables,
        "indexes": indexes,
        "foreign_keys": foreign_keys,
    }


def _canonical_profile_runtime_signature() -> Dict[str, Any]:
    with sqlite3.connect(":memory:") as conn:
        conn.executescript(PROFILE_SCHEMA_SQL)
        return _profile_runtime_signature(conn)


CANONICAL_PROFILE_RUNTIME_SIGNATURE = _canonical_profile_runtime_signature()
CANONICAL_PROFILE_RUNTIME_HASH = (
    "sha256:"
    + hashlib.sha256(
        json.dumps(
            {
                "component": PROFILE_RUNTIME_SCHEMA_COMPONENT,
                "schema_version": PROFILE_RUNTIME_SCHEMA_VERSION,
                "signature": CANONICAL_PROFILE_RUNTIME_SIGNATURE,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
)


def register_cognitive_profile_runtime_schema(conn: sqlite3.Connection) -> None:
    """Register an already canonical profile access/usage schema."""

    if _profile_runtime_signature(conn) != CANONICAL_PROFILE_RUNTIME_SIGNATURE:
        raise RuntimeError("profile runtime schema requires explicit reconciliation")
    conn.execute(REGISTRY_SQL)
    conn.execute(
        """
        INSERT INTO mnemos_schema_registry(component, schema_version, ddl_hash, applied_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(component) DO UPDATE SET
            schema_version=excluded.schema_version,
            ddl_hash=excluded.ddl_hash,
            applied_at=excluded.applied_at
        WHERE schema_version IS NOT excluded.schema_version
           OR ddl_hash IS NOT excluded.ddl_hash
        """,
        (
            PROFILE_RUNTIME_SCHEMA_COMPONENT,
            PROFILE_RUNTIME_SCHEMA_VERSION,
            CANONICAL_PROFILE_RUNTIME_HASH,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def validate_cognitive_profile_runtime_schema(conn: sqlite3.Connection) -> None:
    """Fail closed without DDL when any Profile v2 runtime object drifts."""

    state = inspect_cognitive_profile_runtime_schema(conn)
    if state["errors"]:
        raise RuntimeError(
            "profile runtime schema requires explicit reconciliation: " + ", ".join(state["errors"])
        )


def inspect_cognitive_profile_runtime_schema(
    conn: sqlite3.Connection,
) -> Dict[str, Any]:
    """Return canonical Profile v2 schema state without mutating the database."""

    assertion_state = inspect_profile_assertion_schema(conn)
    errors = list(assertion_state.errors)
    if _profile_runtime_signature(conn) != CANONICAL_PROFILE_RUNTIME_SIGNATURE:
        errors.append("profile_access_usage_schema_hash_mismatch")
    registry_version = ""
    registry_hash = ""
    try:
        row = conn.execute(
            "SELECT schema_version, ddl_hash FROM mnemos_schema_registry WHERE component=?",
            (PROFILE_RUNTIME_SCHEMA_COMPONENT,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is not None:
        registry_version, registry_hash = map(str, row)
    if (registry_version, registry_hash) != (
        PROFILE_RUNTIME_SCHEMA_VERSION,
        CANONICAL_PROFILE_RUNTIME_HASH,
    ):
        errors.append("profile_access_usage_schema_registry_mismatch")
    return {
        "schema_version": PROFILE_RUNTIME_SCHEMA_VERSION,
        "component": PROFILE_RUNTIME_SCHEMA_COMPONENT,
        "canonical_schema_hash": CANONICAL_PROFILE_RUNTIME_HASH,
        "registry_version": registry_version,
        "registry_hash": registry_hash,
        "assertion_schema": assertion_state.as_dict(),
        "errors": errors,
        "ok": not errors,
    }


@dataclass
class ProfileSignal:
    """User cognitive profile v2 source signal."""

    source_event_id: str
    signal_type: str
    dimension: str
    value: str
    evidence: str = ""
    confidence: float = 0.5
    privacy_level: str = "local"
    observed_at: str = ""
    expires_at: str = ""
    status: str = "active"
    access_control: Dict[str, Any] = field(default_factory=dict)
    source_authority_id: str = ""
    source_authority: str = ""
    source_revision_sha256: str = ""
    source_span_start: int = -1
    source_span_end: int = -1
    source_content_sha256: str = ""
    source_identity: str = ""


@dataclass
class ProfileAssertion:
    """Aggregated user cognitive profile v2 claim."""

    assertion_id: str
    dimension: str
    claim: str
    supporting_signals: List[str]
    contradicting_signals: List[str] = field(default_factory=list)
    confidence: float = 0.0
    privacy_level: str = "local"
    last_verified_at: str = ""
    revision_policy: str = "revise_on_contradiction"
    status: str = "active"
    expected_revision_id: str = ""


def make_assertion_id(dimension: str, identity_key: str) -> str:
    """Make a stable assertion identity from its subject, never its claim text."""

    raw = f"{dimension}:{identity_key}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:12]
    safe_dim = "".join(ch if ch.isalnum() else "_" for ch in dimension.lower())[:40]
    return f"pa_{safe_dim}_{digest}"


def _profile_signal_access(signal: ProfileSignal) -> Dict[str, Any]:
    if not signal.access_control:
        return _restricted_profile_access(signal.source_event_id)
    return validate_cognitive_access_envelope(signal.access_control)


def _profile_signal_row_id(reference: str) -> int | None:
    prefix = "profile_signals:"
    if not str(reference).startswith(prefix):
        return None
    try:
        value = int(str(reference)[len(prefix) :])
    except ValueError:
        return None
    return value if value > 0 else None


def _profile_signal_identity(signal: ProfileSignal) -> str:
    """Return the immutable source identity used for replay idempotency.

    The governed producer supplies all authority fields.  Direct historical
    fixtures remain replay-safe by collapsing to their source event only, but
    cannot masquerade as an authority-bound production signal.
    """

    if signal.source_identity:
        return str(signal.source_identity)
    authority_fields = {
        "source_authority_id": str(signal.source_authority_id or ""),
        "source_authority": str(signal.source_authority or ""),
        "source_revision_sha256": str(signal.source_revision_sha256 or ""),
        "source_content_sha256": str(signal.source_content_sha256 or ""),
    }
    spans_present = (
        signal.source_span_start >= 0 and signal.source_span_end > signal.source_span_start
    )
    if any(authority_fields.values()) or spans_present:
        if not (
            str(signal.source_event_id or "")
            and all(authority_fields.values())
            and authority_fields["source_authority"] == "explicit_user"
            and spans_present
        ):
            raise ValueError("authority-bound profile signal identity is incomplete")
        payload = {
            "schema_version": "mnemos.profile_signal_identity.v1",
            "source_event_id": str(signal.source_event_id),
            **authority_fields,
            "source_span_start": int(signal.source_span_start),
            "source_span_end": int(signal.source_span_end),
        }
    else:
        if not str(signal.source_event_id or ""):
            raise ValueError("profile signal source_event_id is required")
        payload = {
            "schema_version": "mnemos.profile_signal_legacy_identity.v1",
            "source_event_id": str(signal.source_event_id),
        }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return "profile-signal:" + digest


class CognitiveProfileRepository(ProfileUsageRepositoryMixin):
    """SQLite repository for user cognitive profile v2."""

    def __init__(self, pool: Any, *, ownership_config: Any | None = None):
        self._pool = pool
        # Keep the ownership lookup explicit instead of inferring a ledger
        # from the SQLite file path.  Test and custom runtimes frequently put
        # profile data under ``database_dir`` while their ownership ledger is
        # rooted at ``mnemos_dir``; guessing would turn a freeze into a
        # caller-dependent bypass.
        self._ownership_config = ownership_config

    def _assert_write_not_frozen(
        self,
        access_control: Mapping[str, Any],
        *,
        source_event_ids: tuple[str, ...] = (),
    ) -> None:
        """Reject profile writes covered by a durable ownership freeze."""

        if self._ownership_config is None:
            # The repository is a cognitive write owner.  It must not make a
            # privacy decision from an inferred local path when its runtime
            # did not provide the configured ownership ledger root.
            raise PermissionError(
                "cognitive profile write blocked because ownership configuration is unavailable"
            )
        from core.privacy.ownership_freeze import cognitive_write_is_frozen

        scope = access_control["scope"]
        if cognitive_write_is_frozen(
            self._ownership_config,
            session_id=str(scope.get("session_id") or ""),
            project=str(scope.get("project") or ""),
            agent=str(access_control["owner"].get("agent") or ""),
            source_event_ids=tuple(str(value) for value in source_event_ids if str(value).strip()),
        ):
            raise PermissionError(
                "cognitive profile write is blocked by a matching frozen data ownership scope"
            )

    @staticmethod
    def _derive_assertion_access(
        conn: sqlite3.Connection,
        *,
        assertion_id: str,
        supporting_signals: List[str],
    ) -> Dict[str, Any]:
        """Derive one assertion ACL from every supporting signal.

        A broken reference, historical blank ACL, or incompatible source scope is
        intentionally represented as restricted-unknown; a profile assertion
        must never broaden an inferred preference merely because it has been
        aggregated.
        """

        source_accesses: List[Dict[str, Any]] = []
        for reference in supporting_signals or []:
            signal_id = _profile_signal_row_id(str(reference))
            if signal_id is None:
                return _restricted_profile_access(assertion_id)
            row = conn.execute(
                "SELECT access_control FROM profile_signals WHERE id=?",
                (signal_id,),
            ).fetchone()
            if row is None:
                return _restricted_profile_access(assertion_id)
            try:
                source_accesses.append(
                    validate_cognitive_access_envelope(json.loads(str(row[0] or "")))
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                return _restricted_profile_access(assertion_id)
        if not source_accesses:
            return _restricted_profile_access(assertion_id)
        first = source_accesses[0]
        try:
            permitted_purposes = set(_PROFILE_READ_PURPOSES)
            for source_access in source_accesses:
                permitted_purposes.intersection_update(source_access["purposes"])
            if not permitted_purposes:
                return _restricted_profile_access(assertion_id)
            return derive_strictest_cognitive_access(
                source_accesses,
                owner_principal_id=str(first["owner"]["principal_id"]),
                owner_agent=str(first["owner"]["agent"]),
                scope_type=str(first["scope"]["scope_type"]),
                scope_id=str(first["scope"]["scope_id"]),
                purposes=tuple(sorted(permitted_purposes)),
                retention_policy="persona_retention",
            )
        except (KeyError, TypeError, ValueError):
            return _restricted_profile_access(assertion_id)

    def record_signal(
        self,
        signal: ProfileSignal,
        *,
        _conn: sqlite3.Connection | None = None,
        _commit: bool = True,
    ) -> int:
        observed_at = signal.observed_at or datetime.now().isoformat()
        access_control = _profile_signal_access(signal)
        self._assert_write_not_frozen(
            access_control,
            source_event_ids=(str(signal.source_event_id or ""),),
        )
        source_identity = _profile_signal_identity(signal)
        conn = _conn or self._pool.get_conn()
        existing = conn.execute(
            "SELECT id FROM profile_signals WHERE source_identity=?",
            (source_identity,),
        ).fetchone()
        if existing is not None:
            if _commit:
                conn.commit()
            return int(existing[0])
        cursor = conn.execute(
            """
            INSERT INTO profile_signals (
                source_event_id, source_identity, source_authority_id,
                source_authority, source_revision_sha256, source_span_start, source_span_end,
                source_content_sha256, signal_type, dimension, value, evidence,
                confidence, privacy_level, observed_at, expires_at, status,
                access_control
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.source_event_id,
                source_identity,
                signal.source_authority_id or "",
                signal.source_authority or "",
                signal.source_revision_sha256 or "",
                int(signal.source_span_start),
                int(signal.source_span_end),
                signal.source_content_sha256 or "",
                signal.signal_type,
                signal.dimension,
                signal.value,
                signal.evidence,
                clamp_confidence(signal.confidence),
                signal.privacy_level or "local",
                observed_at,
                signal.expires_at or None,
                signal.status or "active",
                _access_json(access_control),
            ),
        )
        if _commit:
            conn.commit()
        return cursor.lastrowid or 0

    def upsert_assertion(
        self,
        assertion: ProfileAssertion,
        *,
        _conn: sqlite3.Connection | None = None,
        _commit: bool = True,
    ) -> str:
        conn = _conn or self._pool.get_conn()
        owns_transaction = _conn is None and _commit
        try:
            if owns_transaction:
                # Serialize the compare-and-swap against the canonical head.
                # Reading before this lock would permit two corrections to
                # branch from the same prior revision.
                conn.execute("BEGIN IMMEDIATE")
            assertion_id = self._upsert_assertion_locked(assertion, conn=conn)
            if _commit:
                conn.commit()
            return assertion_id
        except BaseException:
            if _commit and conn.in_transaction:
                conn.rollback()
            raise

    def _upsert_assertion_locked(
        self,
        assertion: ProfileAssertion,
        *,
        conn: sqlite3.Connection,
    ) -> str:
        """Append/update while the caller owns the write transaction."""

        assertion_id = str(assertion.assertion_id or "").strip()
        if not assertion_id:
            raise ValueError("profile assertion requires a stable assertion_id")
        last_verified_at = assertion.last_verified_at or datetime.now().isoformat()
        supporting = json.dumps(assertion.supporting_signals or [], ensure_ascii=False)
        contradicting = json.dumps(assertion.contradicting_signals or [], ensure_ascii=False)
        access_control = self._derive_assertion_access(
            conn,
            assertion_id=assertion_id,
            supporting_signals=assertion.supporting_signals,
        )
        self._assert_write_not_frozen(access_control)
        content = {
            "dimension": assertion.dimension,
            "claim": assertion.claim,
            "supporting_signals": json.loads(supporting),
            "contradicting_signals": json.loads(contradicting),
            "confidence": clamp_confidence(assertion.confidence),
            "privacy_level": assertion.privacy_level or "local",
            "revision_policy": assertion.revision_policy or "revise_on_contradiction",
            "status": assertion.status or "active",
            "access_control": access_control,
        }
        content_hash = hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        self._anchor_legacy_assertion_if_needed(conn, assertion_id)
        head = conn.execute(
            "SELECT revision_id FROM profile_assertion_heads WHERE assertion_id=?",
            (assertion_id,),
        ).fetchone()
        head_revision_id = str(head[0]) if head is not None else ""
        existing = conn.execute(
            "SELECT revision_id FROM profile_assertion_revisions "
            "WHERE assertion_id=? AND content_hash=?",
            (assertion_id, content_hash),
        ).fetchone()
        if existing is not None:
            if head_revision_id != str(existing[0]):
                raise ValueError("assertion content is not the current head")
            return assertion_id
        expected_revision_id = str(assertion.expected_revision_id or "")
        if head_revision_id:
            if not expected_revision_id:
                raise ValueError("assertion correction requires expected_revision_id")
            if expected_revision_id != head_revision_id:
                raise ValueError("stale expected_revision_id for assertion correction")
        elif expected_revision_id:
            raise ValueError("new assertion must not specify expected_revision_id")
        prior = conn.execute(
            "SELECT revision_id, revision_number FROM profile_assertion_revisions "
            "WHERE assertion_id=? ORDER BY revision_number DESC LIMIT 1",
            (assertion_id,),
        ).fetchone()
        prior_revision_id = str(prior[0]) if prior else ""
        revision_number = int(prior[1] or 0) + 1 if prior else 1
        revision_id = f"par_{assertion_id}_{revision_number}_{content_hash[:12]}"
        conn.execute(
            """
            INSERT INTO profile_assertion_revisions (
                revision_id, assertion_id, revision_number, content_hash,
                supersedes_revision_id, dimension, claim, supporting_signals,
                contradicting_signals, confidence, privacy_level,
                last_verified_at, revision_policy, status, access_control
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                assertion_id,
                revision_number,
                content_hash,
                prior_revision_id or None,
                assertion.dimension,
                assertion.claim,
                supporting,
                contradicting,
                clamp_confidence(assertion.confidence),
                assertion.privacy_level or "local",
                last_verified_at,
                assertion.revision_policy or "revise_on_contradiction",
                assertion.status or "active",
                _access_json(access_control),
            ),
        )
        conn.execute(
            """
            INSERT INTO profile_assertion_heads (assertion_id, revision_id, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(assertion_id) DO UPDATE SET
                revision_id=excluded.revision_id,
                updated_at=CURRENT_TIMESTAMP
            """,
            (assertion_id, revision_id),
        )
        projection_values = (
            assertion.dimension,
            assertion.claim,
            supporting,
            contradicting,
            clamp_confidence(assertion.confidence),
            assertion.privacy_level or "local",
            last_verified_at,
            assertion.revision_policy or "revise_on_contradiction",
            assertion.status or "active",
            _access_json(access_control),
        )
        # ``profile_assertions`` is a replaceable current projection, never
        # the evidence ledger.  Its mutation occurs only after the immutable
        # revision was appended above in the same transaction.
        current_exists = conn.execute(
            "SELECT 1 FROM profile_assertions WHERE assertion_id=?",
            (assertion_id,),
        ).fetchone()
        if current_exists is None:
            conn.execute(
                """
                INSERT INTO profile_assertions (
                    assertion_id, current_revision_id, dimension, claim, supporting_signals,
                    contradicting_signals, confidence, privacy_level,
                    last_verified_at, revision_policy, status, access_control, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (assertion_id, revision_id, *projection_values),
            )
        else:
            conn.execute(
                """
                UPDATE profile_assertions
                SET current_revision_id=?, dimension=?, claim=?, supporting_signals=?,
                    contradicting_signals=?, confidence=?, privacy_level=?,
                    last_verified_at=?, revision_policy=?, status=?,
                    access_control=?, updated_at=CURRENT_TIMESTAMP
                WHERE assertion_id=?
                """,
                (revision_id, *projection_values, assertion_id),
            )
        return assertion_id

    def record_authorized_profile_evidence(
        self,
        *,
        source_authority_catalog: Any,
        source_authority_id: str,
        raw_db_path: str,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        signal_type: str,
        dimension: str,
        quote: str,
        confidence: float,
        assertion_id: str = "",
        expected_revision_id: str = "",
    ) -> Dict[str, Any]:
        """Persist one user-owned profile signal and its exact assertion.

        This is the sole production write primitive for Profile v2.  It only
        accepts a system-generated exact Raw authority entry for a user span;
        assistant, tool, quoted, and external material never reaches the
        signal table.  The source identity is persisted with the signal, and
        replaying the same immutable span converges to the original row.
        """

        from core.evidence.source_authority import (
            SourceAuthority,
            verify_source_authority_raw_span,
        )

        if principal is None:
            raise PermissionError("profile producer requires a server-resolved principal")
        source_authority_catalog.require_admissible()
        entry = source_authority_catalog.get(source_authority_id)
        if entry is None:
            raise PermissionError("profile producer source authority is unknown")
        if (
            entry.authority is not SourceAuthority.EXPLICIT_USER
            or entry.role != "user"
            or not entry.allows_cognitive_update
            or entry.span_status != "exact"
            or not entry.source_revision_sha256
            or entry.artifact_ref_id
        ):
            raise PermissionError("profile producer requires an exact explicit user authority")
        source_quote = str(quote or "").strip()
        if not source_quote or not entry.matches_quote(source_quote):
            raise ValueError("profile producer quote must be an exact selected Raw span")
        if not verify_source_authority_raw_span(entry, Path(raw_db_path)):
            raise PermissionError("profile producer Raw authority verification failed")

        session_id = str(getattr(narrowing, "session_id", "") or "")
        project = str(getattr(narrowing, "project", "") or "")
        if session_id:
            scope_type, scope_id = "session", session_id
        elif project:
            scope_type, scope_id = "project", project
        else:
            raise PermissionError("profile producer requires a resolved session or project scope")
        access_control = make_cognitive_access_envelope(
            owner_principal_id=str(principal.principal_id),
            owner_agent=str(principal.agent),
            scope_type=scope_type,
            scope_id=scope_id,
            session_id=session_id,
            project=project,
            purposes=_PROFILE_READ_PURPOSES,
            consent_provenance_refs=(
                "raw-revision:" + str(entry.source_event_id),
                str(entry.source_authority_id),
            ),
            sensitivity="sensitive",
            retention_policy="persona_retention",
            source_acl_lineage=(
                "raw-revision:" + str(entry.source_event_id),
                "raw-revision-hash:" + str(entry.source_revision_sha256),
                "raw-span-hash:" + str(entry.content_sha256),
                "source-authority:" + str(entry.source_authority_id),
            ),
            visibility="private",
        )
        signal = ProfileSignal(
            source_event_id=str(entry.source_event_id),
            source_authority_id=str(entry.source_authority_id),
            source_authority=entry.authority.value,
            source_revision_sha256=str(entry.source_revision_sha256),
            source_span_start=int(entry.span_start),
            source_span_end=int(entry.span_end),
            source_content_sha256=str(entry.content_sha256),
            signal_type=str(signal_type or "").strip(),
            dimension=str(dimension or "").strip(),
            value=source_quote,
            evidence=source_quote,
            confidence=clamp_confidence(confidence),
            privacy_level="local",
            access_control=access_control,
        )
        if not signal.signal_type or not signal.dimension:
            raise ValueError("profile producer signal_type and dimension are required")

        conn = self._pool.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            signal_id = self.record_signal(signal, _conn=conn, _commit=False)
            assertion = ProfileAssertion(
                assertion_id=str(assertion_id or "").strip()
                or make_assertion_id(signal.dimension, str(entry.source_event_id)),
                dimension=signal.dimension,
                claim=source_quote,
                supporting_signals=[f"profile_signals:{signal_id}"],
                confidence=signal.confidence,
                privacy_level="local",
                revision_policy="revise_on_explicit_user_correction",
                status="active",
                expected_revision_id=str(expected_revision_id or "").strip(),
            )
            assertion_id = self.upsert_assertion(assertion, _conn=conn, _commit=False)
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        return {
            "signal_id": signal_id,
            "assertion_id": assertion_id,
            "source_identity": _profile_signal_identity(signal),
        }

    @staticmethod
    def _anchor_legacy_assertion_if_needed(
        conn: sqlite3.Connection,
        assertion_id: str,
    ) -> None:
        """Preserve a pre-revision current row before its first correction."""

        has_history = conn.execute(
            "SELECT 1 FROM profile_assertion_revisions WHERE assertion_id=? LIMIT 1",
            (assertion_id,),
        ).fetchone()
        if has_history is not None:
            return
        row = conn.execute(
            "SELECT dimension, claim, supporting_signals, contradicting_signals, "
            "confidence, privacy_level, last_verified_at, revision_policy, status, "
            "access_control FROM profile_assertions WHERE assertion_id=?",
            (assertion_id,),
        ).fetchone()
        if row is None:
            return
        values = tuple(row)
        legacy_content = {
            "dimension": values[0],
            "claim": values[1],
            "supporting_signals": parse_json_list(values[2]),
            "contradicting_signals": parse_json_list(values[3]),
            "confidence": clamp_confidence(values[4]),
            "privacy_level": values[5],
            "revision_policy": values[7],
            "status": values[8],
            "access_control": values[9],
        }
        content_hash = hashlib.sha256(
            json.dumps(
                legacy_content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        conn.execute(
            """
            INSERT INTO profile_assertion_revisions (
                revision_id, assertion_id, revision_number, content_hash,
                supersedes_revision_id, dimension, claim, supporting_signals,
                contradicting_signals, confidence, privacy_level,
                last_verified_at, revision_policy, status, access_control
            ) VALUES (?, ?, 1, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"par_{assertion_id}_1_{content_hash[:12]}",
                assertion_id,
                content_hash,
                *values,
            ),
        )
        conn.execute(
            """
            INSERT INTO profile_assertion_heads (assertion_id, revision_id, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(assertion_id) DO NOTHING
            """,
            (assertion_id, f"par_{assertion_id}_1_{content_hash[:12]}"),
        )

    def get_assertion_revisions(self, assertion_id: str) -> List[Dict[str, Any]]:
        """Return immutable revision history in increasing revision order."""

        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row  # noqa
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM profile_assertion_revisions "
                "WHERE assertion_id=? ORDER BY revision_number ASC",
                (assertion_id,),
            ).fetchall()
        ]

    def rebuild_profile_assertion_projection(self, assertion_id: str) -> Dict[str, Any]:
        """Rebuild one replaceable current projection from its immutable head."""

        conn = self._pool.get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """
                SELECT revision.revision_id, revision.dimension, revision.claim,
                       revision.supporting_signals, revision.contradicting_signals,
                       revision.confidence, revision.privacy_level,
                       revision.last_verified_at, revision.revision_policy,
                       revision.status, revision.access_control
                FROM profile_assertion_heads AS head
                JOIN profile_assertion_revisions AS revision
                  ON revision.revision_id=head.revision_id
                WHERE head.assertion_id=? AND revision.assertion_id=head.assertion_id
                """,
                (assertion_id,),
            ).fetchone()
            if row is None:
                raise ValueError("assertion has no immutable head")
            expected = tuple(row)
            current = conn.execute(
                """
                SELECT current_revision_id, dimension, claim, supporting_signals,
                       contradicting_signals, confidence, privacy_level,
                       last_verified_at, revision_policy, status, access_control
                FROM profile_assertions
                WHERE assertion_id=?
                """,
                (assertion_id,),
            ).fetchone()
            repaired = current is None or tuple(current) != expected
            if current is None:
                conn.execute(
                    """
                    INSERT INTO profile_assertions (
                        assertion_id, current_revision_id, dimension, claim, supporting_signals,
                        contradicting_signals, confidence, privacy_level, last_verified_at,
                        revision_policy, status, access_control, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (assertion_id, *expected),
                )
            elif repaired:
                conn.execute(
                    """
                    UPDATE profile_assertions
                    SET current_revision_id=?, dimension=?, claim=?, supporting_signals=?,
                        contradicting_signals=?, confidence=?, privacy_level=?,
                        last_verified_at=?, revision_policy=?, status=?, access_control=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE assertion_id=?
                    """,
                    (*expected, assertion_id),
                )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        return {
            "assertion_id": assertion_id,
            "revision_id": str(expected[0]),
            "repaired": repaired,
        }

    def get_assertions(
        self,
        status: str = "active",
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        purpose: str = "",
    ) -> List[Dict[str, Any]]:
        """Compatibility read entrypoint guarded by the canonical ACL seam."""

        assertions, _access = self.get_authorized_assertions(
            principal=principal,
            narrowing=narrowing,
            purpose=purpose,
            status=status,
        )
        return assertions

    @staticmethod
    def _assertion_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        data["supporting_signals"] = parse_json_list(data.get("supporting_signals"))
        data["contradicting_signals"] = parse_json_list(data.get("contradicting_signals"))
        data["evidence_refs"] = list(data["supporting_signals"])
        raw_access = data.get("access_control")
        try:
            data["access_control"] = json.loads(str(raw_access or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            data["access_control"] = {}
        return data

    def get_authorized_assertions(
        self,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        purpose: str,
        status: str = "active",
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Authorize assertion headers before loading their claims/evidence."""

        if principal is None:
            return [], {
                "candidate_count": 0,
                "authorized_count": 0,
                "denied_by_reason": {"principal_required": 1},
            }
        if not str(purpose or "").strip():
            return [], {
                "candidate_count": 0,
                "authorized_count": 0,
                "denied_by_reason": {"purpose_required": 1},
            }
        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row  # noqa
        candidates = conn.execute(
            """
            SELECT assertion_id, access_control
            FROM profile_assertions
            WHERE status = ?
            ORDER BY confidence DESC, updated_at DESC
            """,
            (status,),
        ).fetchall()
        authorized_ids: List[str] = []
        denied_by_reason: Dict[str, int] = {}
        for candidate in candidates:
            try:
                access_control = validate_cognitive_access_envelope(
                    json.loads(str(candidate["access_control"] or ""))
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                reason = "acl_unknown"
            else:
                reason = authorize_cognitive_access(
                    access_control,
                    principal=principal,
                    narrowing=narrowing,
                    purpose=purpose,
                ).reason
            if reason == "authorized":
                authorized_ids.append(str(candidate["assertion_id"]))
            else:
                denied_by_reason[reason] = denied_by_reason.get(reason, 0) + 1
        if not authorized_ids:
            return [], {
                "candidate_count": len(candidates),
                "authorized_count": 0,
                "denied_by_reason": denied_by_reason,
            }
        placeholders = ",".join("?" for _ in authorized_ids)
        rows_by_id = {
            str(row["assertion_id"]): row
            for row in conn.execute(
                "SELECT * FROM profile_assertions "
                f"WHERE assertion_id IN ({placeholders})",  # nosec B608
                tuple(authorized_ids),
            ).fetchall()
        }
        authorized_assertions: List[Dict[str, Any]] = []
        for assertion_id in authorized_ids:
            row = rows_by_id.get(assertion_id)
            if row is None:
                continue
            assertion = self._assertion_from_row(row)
            current, reason = self._assertion_is_current(conn, assertion)
            if current:
                authorized_assertions.append(assertion)
            else:
                denied_by_reason[reason] = denied_by_reason.get(reason, 0) + 1
        return (
            authorized_assertions,
            {
                "candidate_count": len(candidates),
                "authorized_count": len(authorized_assertions),
                "denied_by_reason": denied_by_reason,
            },
        )

    @staticmethod
    def _assertion_is_current(
        conn: sqlite3.Connection,
        assertion: Mapping[str, Any],
    ) -> tuple[bool, str]:
        """Accept only unconflicted assertions with live canonical evidence.

        The assertion table deliberately stores immutable evidence references;
        signal expiry and revocation remain on the evidence rows.  Consumers
        must re-check that state at read time rather than treating an old
        assertion header as indefinitely effective.
        """

        assertion_id = str(assertion.get("assertion_id") or "")
        current_revision_id = str(assertion.get("current_revision_id") or "")
        if not assertion_id or not current_revision_id:
            return False, "assertion_projection_head_mismatch"
        ledger_row = conn.execute(
            """
            SELECT revision.revision_id, revision.dimension, revision.claim,
                   revision.supporting_signals, revision.contradicting_signals,
                   revision.confidence, revision.privacy_level,
                   revision.last_verified_at, revision.revision_policy,
                   revision.status, revision.access_control
            FROM profile_assertion_heads AS head
            JOIN profile_assertion_revisions AS revision
              ON revision.revision_id=head.revision_id
             AND revision.assertion_id=head.assertion_id
            WHERE head.assertion_id=? AND head.revision_id=?
            """,
            (assertion_id, current_revision_id),
        ).fetchone()
        if ledger_row is None:
            return False, "assertion_projection_head_mismatch"
        projection_values = (
            current_revision_id,
            str(assertion.get("dimension") or ""),
            str(assertion.get("claim") or ""),
            json.dumps(
                assertion.get("supporting_signals") or [],
                ensure_ascii=False,
            ),
            json.dumps(
                assertion.get("contradicting_signals") or [],
                ensure_ascii=False,
            ),
            float(assertion.get("confidence") or 0.0),
            str(assertion.get("privacy_level") or ""),
            str(assertion.get("last_verified_at") or ""),
            str(assertion.get("revision_policy") or ""),
            str(assertion.get("status") or ""),
            _access_json(assertion.get("access_control") or {}),
        )
        ledger_values = tuple(ledger_row)
        if any(
            str(projected) != str(ledger)
            for projected, ledger in zip(projection_values, ledger_values)
        ):
            return False, "assertion_projection_head_mismatch"
        if assertion.get("contradicting_signals"):
            return False, "assertion_conflicted"
        supporting = assertion.get("supporting_signals") or []
        if not supporting:
            return False, "assertion_evidence_missing"
        signal_ids: List[int] = []
        for ref in supporting:
            prefix, separator, raw_id = str(ref).partition(":")
            if prefix != "profile_signals" or not separator or not raw_id.isdigit():
                return False, "assertion_evidence_unverifiable"
            signal_ids.append(int(raw_id))
        placeholders = ",".join("?" for _ in signal_ids)
        rows = conn.execute(
            "SELECT id, status, expires_at FROM profile_signals "
            f"WHERE id IN ({placeholders})",  # nosec B608: generated placeholders
            tuple(signal_ids),
        ).fetchall()
        if len(rows) != len(set(signal_ids)):
            return False, "assertion_evidence_missing"
        now = datetime.now().isoformat()
        for _signal_id, status, expires_at in rows:
            if str(status or "") != "active":
                return False, "assertion_evidence_inactive"
            if expires_at and str(expires_at) < now:
                return False, "assertion_evidence_expired"
        return True, "authorized"

    def count_active_signals(self) -> int:
        conn = self._pool.get_conn()
        cursor = conn.execute(
            """
            SELECT COUNT(*) FROM profile_signals
            WHERE status = 'active'
              AND (expires_at IS NULL OR expires_at = '' OR expires_at >= ?)
            """,
            (datetime.now().isoformat(),),
        )
        return int(cursor.fetchone()[0] or 0)

    @staticmethod
    def _empty_usage_metrics(
        days: int,
        *,
        access_filter: Dict[str, int] | None = None,
    ) -> Dict[str, Any]:
        return {
            "schema_version": "mnemos.profile_usage.v1",
            "days": days,
            "total_usages": 0,
            "action_changed_count": 0,
            "by_consumer": {},
            "feedback": {},
            "access_filter": dict(access_filter or {}),
        }

    def get_authorized_usage_metrics(
        self,
        days: int = CORE,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        purpose: str,
    ) -> Dict[str, Any]:
        """Aggregate usage only after ACL-only header authorization."""

        normalized_days = max(0, int(days))
        if principal is None:
            return self._empty_usage_metrics(
                normalized_days,
                access_filter={"principal_required": 1},
            )
        if not str(purpose or "").strip():
            return self._empty_usage_metrics(
                normalized_days,
                access_filter={"purpose_required": 1},
            )
        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row  # noqa
        candidates = conn.execute(
            """
            SELECT id, access_control
            FROM profile_usage_log
            WHERE created_at >= datetime('now', ?)
            """,
            (f"-{normalized_days} days",),
        ).fetchall()
        authorized_ids: List[int] = []
        denied_by_reason: Dict[str, int] = {}
        for candidate in candidates:
            try:
                access_control = validate_cognitive_access_envelope(
                    json.loads(str(candidate["access_control"] or ""))
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                reason = "acl_unknown"
            else:
                reason = authorize_cognitive_access(
                    access_control,
                    principal=principal,
                    narrowing=narrowing,
                    purpose=purpose,
                ).reason
            if reason == "authorized":
                authorized_ids.append(int(candidate["id"]))
            else:
                denied_by_reason[reason] = denied_by_reason.get(reason, 0) + 1
        if not authorized_ids:
            return self._empty_usage_metrics(
                normalized_days,
                access_filter=denied_by_reason,
            )
        placeholders = ",".join("?" for _ in authorized_ids)
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT consumer, action_changed, outcome, user_feedback "
                f"FROM profile_usage_log WHERE id IN ({placeholders})",  # nosec B608
                tuple(authorized_ids),
            ).fetchall()
        ]
        by_consumer: Dict[str, int] = {}
        changed_count = 0
        feedback: Dict[str, int] = {}
        for row in rows:
            consumer = str(row.get("consumer") or "unknown")
            by_consumer[consumer] = by_consumer.get(consumer, 0) + 1
            if row.get("action_changed"):
                changed_count += 1
            fb = str(row.get("user_feedback") or "")
            if fb:
                feedback[fb] = feedback.get(fb, 0) + 1
        return {
            "schema_version": "mnemos.profile_usage.v1",
            "days": normalized_days,
            "total_usages": len(rows),
            "action_changed_count": changed_count,
            "by_consumer": by_consumer,
            "feedback": feedback,
            "access_filter": denied_by_reason,
        }

    def build_authorized_profile_v2(
        self,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        purpose: str,
        consumer: str = "",
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        assertions, access = self.get_authorized_assertions(
            principal=principal,
            narrowing=narrowing,
            purpose=purpose,
        )
        access = dict(access)
        if consumer:
            if principal is None or not assertions:
                raise ValueError("profile read authorization token requires authorized assertions")
            access["read_authorization_token"] = self._issue_profile_read_authorization(
                assertions=assertions,
                principal=principal,
                narrowing=narrowing,
                purpose=purpose,
                consumer=consumer,
            )
        return (
            build_profile_v2_payload(
                assertions,
                active_signal_count=len(assertions),
            ),
            access,
        )
