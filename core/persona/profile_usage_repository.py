"""Profile usage authorization, durable effect logging, and outbox replay."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping
from uuid import uuid4

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import (
    cognitive_access_hash,
    derive_strictest_cognitive_access,
    make_cognitive_access_envelope,
    validate_cognitive_access_envelope,
)
from core.persona.profile_effect import (
    ProfileTargetEffectReceipt,
    parse_profile_target_effect_receipt,
    profile_usage_idempotency_key,
    validate_profile_target_effect_receipt,
)

_PROFILE_USAGE_CONSUMER_PURPOSES = {
    "preflight_builder": "persona_preflight_read",
    "context_search": "context_search_profile",
    "persona_behavior_prompt": "persona_behavior_prompt",
}
_PROFILE_READ_AUTHORIZATION_TTL = timedelta(minutes=5)


_PROFILE_READ_PURPOSES = (
    "persona_preflight_read",
    "context_search_profile",
    "persona_summary_read",
    "persona_behavior_prompt",
    "persona_usage_metrics",
)


@dataclass
class ProfileUsageLog:
    """User cognitive profile v2 consumption event."""

    consumer: str
    profile_fields_used: List[str]
    read_purpose: str = ""
    read_authorization_token: str = ""
    target_receipt: ProfileTargetEffectReceipt | None = None
    outcome: str = ""
    user_feedback: str = ""
    profile_revision_ids: List[str] = field(default_factory=list)
    scope_snapshot: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _PreparedProfileUsage:
    idempotency_key: str
    command_id: str
    intent_json: str
    target_receipt_hash: str
    access_control: str
    expected_row: tuple[Any, ...]


def _restricted_profile_access(source_ref: str) -> Dict[str, Any]:
    return make_cognitive_access_envelope(
        owner_principal_id="system:profile-repository",
        owner_agent="system",
        scope_type="profile",
        scope_id=str(source_ref or "unknown"),
        purposes=_PROFILE_READ_PURPOSES,
        consent_provenance_refs=(),
        sensitivity="restricted",
        retention_policy="persona_retention",
        source_acl_lineage=(f"profile-source:{source_ref or 'unknown'}",),
        visibility="restricted",
        scope_resolution="restricted_unknown",
        consent_status="restricted_unknown",
    )


def _access_json(access_control: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(access_control),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_json_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if not value:
        return []
    try:
        loaded = json.loads(str(value))
    except json.JSONDecodeError:
        return [str(value)]
    if isinstance(loaded, list):
        return [str(item) for item in loaded]
    return []


class ProfileUsageRepositoryMixin:
    """Repository behavior for one-shot profile reads and effect receipts."""

    @staticmethod
    def _derive_usage_access(
        conn: sqlite3.Connection,
        *,
        usage_id: str,
        profile_fields_used: List[str],
    ) -> Dict[str, Any]:
        """Derive usage-log ACL from every consumed assertion.

        Usage logs can reveal which preferences affected a decision, so they
        are cognitive objects too. Missing, historical, or incompatible source
        ACLs become restricted rather than creating a broadly readable metric.
        """

        source_accesses: List[Dict[str, Any]] = []
        for assertion_id in profile_fields_used or []:
            row = conn.execute(
                "SELECT access_control FROM profile_assertions WHERE assertion_id=?",
                (str(assertion_id),),
            ).fetchone()
            if row is None:
                return _restricted_profile_access(usage_id)
            try:
                source_accesses.append(
                    validate_cognitive_access_envelope(json.loads(str(row[0] or "")))
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                return _restricted_profile_access(usage_id)
        if not source_accesses:
            return _restricted_profile_access(usage_id)
        first = source_accesses[0]
        try:
            return derive_strictest_cognitive_access(
                source_accesses,
                owner_principal_id=str(first["owner"]["principal_id"]),
                owner_agent=str(first["owner"]["agent"]),
                scope_type=str(first["scope"]["scope_type"]),
                scope_id=str(first["scope"]["scope_id"]),
                purposes=("persona_usage_metrics",),
                retention_policy="persona_usage_retention",
            )
        except (KeyError, TypeError, ValueError):
            return _restricted_profile_access(usage_id)

    @staticmethod
    def _consume_profile_read_authorization(
        conn: sqlite3.Connection,
        *,
        token_id: str,
        consumer: str,
        read_purpose: str,
        revision_map: Dict[str, str],
        command_id: str,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        trusted_replay: bool,
    ) -> Dict[str, Any]:
        normalized_token = str(token_id or "").strip()
        if not normalized_token:
            raise ValueError("profile usage requires a read authorization token")
        row = conn.execute(
            """
            SELECT consumer, read_purpose, principal_id, principal_agent,
                   scope_snapshot,
                   authorized_assertion_revisions, assertion_access_hashes,
                   access_control, status, consumed_command_id, expires_at
            FROM profile_read_authorizations WHERE token_id=?
            """,
            (normalized_token,),
        ).fetchone()
        if row is None:
            raise ValueError("profile usage read authorization token is unknown")
        (
            token_consumer,
            token_purpose,
            token_principal_id,
            token_principal_agent,
            raw_scope_snapshot,
            raw_authorized_revisions,
            raw_access_hashes,
            raw_access_control,
            status,
            consumed_command_id,
            expires_at,
        ) = tuple(row)
        if str(token_consumer) != consumer or str(token_purpose) != read_purpose:
            raise ValueError("profile read authorization consumer/purpose mismatch")
        try:
            expiry = datetime.fromisoformat(str(expires_at))
        except ValueError as exc:
            raise ValueError("profile read authorization expiry is invalid") from exc
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        try:
            authorized_revisions = json.loads(str(raw_authorized_revisions))
            access_hashes = json.loads(str(raw_access_hashes))
            scope_snapshot = json.loads(str(raw_scope_snapshot))
            validate_cognitive_access_envelope(json.loads(str(raw_access_control)))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("profile read authorization token is corrupt") from exc
        if (
            not isinstance(authorized_revisions, dict)
            or not isinstance(access_hashes, dict)
            or not isinstance(scope_snapshot, dict)
            or any(
                authorized_revisions.get(assertion_id) != revision_id
                for assertion_id, revision_id in revision_map.items()
            )
        ):
            raise ValueError("profile read authorization revision mapping mismatch")
        for assertion_id in revision_map:
            current = conn.execute(
                "SELECT access_control FROM profile_assertions WHERE assertion_id=?",
                (assertion_id,),
            ).fetchone()
            if current is None:
                raise ValueError("profile read authorization assertion is missing")
            try:
                current_access = validate_cognitive_access_envelope(
                    json.loads(str(current[0] or ""))
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("profile read authorization assertion ACL drift") from exc
            if access_hashes.get(assertion_id) != cognitive_access_hash(current_access):
                raise ValueError("profile read authorization assertion ACL drift")
        normalized_status = str(status or "")
        if normalized_status == "consumed":
            if str(consumed_command_id or "") != command_id:
                raise ValueError("profile read authorization token was already consumed")
        elif normalized_status == "issued":
            if trusted_replay:
                raise ValueError("profile read authorization replay requires a consumed token")
            if datetime.now(timezone.utc) >= expiry:
                raise ValueError("profile read authorization token expired")
        else:
            raise ValueError("profile read authorization token status is invalid")
        if not trusted_replay:
            if principal is None:
                raise ValueError("profile usage requires a server-resolved principal/scope")
            expected_scope_snapshot = ProfileUsageRepositoryMixin._profile_read_scope_snapshot(
                principal=principal,
                narrowing=narrowing,
                purpose=read_purpose,
                consumer=consumer,
            )
            if (
                scope_snapshot != expected_scope_snapshot
                or str(token_principal_id) != str(principal.principal_id)
                or str(token_principal_agent) != str(principal.agent)
            ):
                raise ValueError("profile read authorization principal/scope mismatch")
        if normalized_status == "issued":
            updated = conn.execute(
                """
                UPDATE profile_read_authorizations
                SET status='consumed', consumed_command_id=?
                WHERE token_id=? AND status='issued' AND consumed_command_id=''
                """,
                (command_id, normalized_token),
            )
            if updated.rowcount != 1:
                raise ValueError("profile read authorization token consumption raced")
        return scope_snapshot

    def record_usage(
        self,
        usage: ProfileUsageLog,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        _failpoint: Callable[[str], None] | None = None,
        _trusted_replay: bool = False,
    ) -> int:
        """Persist an effect intent before atomically closing its usage receipt."""

        if principal is None and not _trusted_replay:
            raise ValueError("profile usage requires a server-resolved principal/scope")
        conn = self._pool.get_conn()
        if conn.in_transaction:
            raise RuntimeError("profile usage outbox requires its own transaction boundary")
        conn.execute("BEGIN IMMEDIATE")
        try:
            prepared = self._prepare_usage_in_connection(
                conn,
                usage,
                principal=principal,
                narrowing=narrowing,
                trusted_replay=_trusted_replay,
            )
            self._enqueue_profile_usage_outbox(conn, prepared)
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        if _failpoint is not None:
            _failpoint("after_usage_outbox_commit")

        conn.execute("BEGIN IMMEDIATE")
        try:
            usage_row_id = self._persist_prepared_usage(conn, prepared)
            updated = conn.execute(
                """
                UPDATE profile_usage_outbox
                SET status='committed', usage_id=?, attempts=attempts + 1,
                    last_error='', updated_at=CURRENT_TIMESTAMP
                WHERE command_id=? AND idempotency_key=? AND intent_json=?
                """,
                (
                    usage_row_id,
                    prepared.command_id,
                    prepared.idempotency_key,
                    prepared.intent_json,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("profile usage outbox commit binding was lost")
            if _failpoint is not None:
                _failpoint("after_usage_receipt_before_commit")
            conn.commit()
        except BaseException as exc:
            if conn.in_transaction:
                conn.rollback()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE profile_usage_outbox
                    SET attempts=attempts + 1, last_error=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE command_id=? AND status='pending'
                    """,
                    (exc.__class__.__name__, prepared.command_id),
                )
                conn.commit()
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.rollback()
            raise
        return usage_row_id

    def replay_profile_usage_outbox(self, *, limit: int = 100) -> tuple[str, ...]:
        """Replay pending immutable intents; committed commands remain idempotent."""

        conn = self._pool.get_conn()
        rows = conn.execute(
            """
            SELECT command_id, intent_json
            FROM profile_usage_outbox
            WHERE status='pending'
            ORDER BY created_at, command_id
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        replayed: list[str] = []
        for command_id, intent_json in rows:
            try:
                intent = json.loads(str(intent_json))
                usage = ProfileUsageLog(
                    consumer=str(intent["consumer"]),
                    profile_fields_used=list(intent["profile_fields_used"]),
                    read_purpose=str(intent["read_purpose"]),
                    read_authorization_token=str(intent["read_authorization_token"]),
                    target_receipt=parse_profile_target_effect_receipt(
                        dict(intent["target_receipt"])
                    ),
                    outcome=str(intent.get("outcome") or ""),
                    user_feedback=str(intent.get("user_feedback") or ""),
                    profile_revision_ids=list(intent["profile_revision_ids"]),
                    scope_snapshot=dict(intent["scope_snapshot"]),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid pending profile usage intent: {command_id}") from exc
            self.record_usage(usage, _trusted_replay=True)
            replayed.append(str(command_id))
        return tuple(replayed)

    def _prepare_usage_in_connection(
        self,
        conn: sqlite3.Connection,
        usage: ProfileUsageLog,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        trusted_replay: bool,
    ) -> _PreparedProfileUsage:
        receipt = validate_profile_target_effect_receipt(usage.target_receipt)
        consumer = str(usage.consumer or "unknown").strip()
        if receipt.owner != consumer:
            raise ValueError("profile target receipt owner must be the usage consumer")
        read_purpose = str(usage.read_purpose or "").strip()
        if _PROFILE_USAGE_CONSUMER_PURPOSES.get(consumer) != read_purpose:
            raise ValueError("profile usage consumer/purpose contract mismatch")
        raw_fields = [str(value).strip() for value in usage.profile_fields_used or []]
        if (
            not raw_fields
            or any(not value for value in raw_fields)
            or len(raw_fields) != len(set(raw_fields))
        ):
            raise ValueError("duplicate profile usage fields are not allowed")
        supplied_fields = sorted(raw_fields)
        receipt_fields = sorted(receipt.matched_assertion_revisions)
        if supplied_fields != receipt_fields:
            raise ValueError("profile usage fields must equal exact matched assertions")
        usage_id = f"profile-usage:{usage.consumer or 'unknown'}"
        access_control = self._derive_usage_access(
            conn,
            usage_id=usage_id,
            profile_fields_used=supplied_fields,
        )
        revision_map = self._current_assertion_revision_map(
            conn,
            supplied_fields,
        )
        for assertion_id in supplied_fields:
            cursor = conn.execute(
                "SELECT * FROM profile_assertions WHERE assertion_id=?",
                (assertion_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"profile usage assertion is not current: {assertion_id}")
            assertion = dict(
                zip(
                    (str(column[0]) for column in cursor.description or ()),
                    row,
                )
            )
            assertion["supporting_signals"] = parse_json_list(assertion.get("supporting_signals"))
            assertion["contradicting_signals"] = parse_json_list(
                assertion.get("contradicting_signals")
            )
            try:
                assertion["access_control"] = json.loads(str(assertion.get("access_control") or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                assertion["access_control"] = {}
            current, reason = self._assertion_is_current(
                conn,
                assertion,
            )
            if not current:
                raise ValueError(
                    f"profile usage assertion is not eligible: {assertion_id}:{reason}"
                )
        revision_ids = sorted(set(revision_map.values()))
        if not revision_ids:
            raise ValueError("profile usage requires immutable assertion revisions")
        if dict(sorted(receipt.matched_assertion_revisions.items())) != revision_map:
            raise ValueError("profile usage matched revisions must match the current projection")
        raw_supplied_revisions = [str(value).strip() for value in usage.profile_revision_ids or []]
        if len(raw_supplied_revisions) != len(set(raw_supplied_revisions)):
            raise ValueError("duplicate profile usage revisions are not allowed")
        supplied_revisions = sorted(raw_supplied_revisions)
        if supplied_revisions and supplied_revisions != revision_ids:
            raise ValueError("profile usage revision ids must match the current projection")
        idempotency_key = profile_usage_idempotency_key(
            consumer=consumer,
            read_purpose=read_purpose,
            receipt=receipt,
        )
        command_id = "profile-usage-command:" + idempotency_key.removeprefix("profile-usage:")
        read_scope_snapshot = self._consume_profile_read_authorization(
            conn,
            token_id=usage.read_authorization_token,
            consumer=consumer,
            read_purpose=read_purpose,
            revision_map=revision_map,
            command_id=command_id,
            principal=principal,
            narrowing=narrowing,
            trusted_replay=trusted_replay,
        )
        scope_snapshot = {
            "owner": dict(access_control.get("owner") or {}),
            "scope": dict(access_control.get("scope") or {}),
            "purposes": list(access_control.get("purposes") or []),
            "read_purpose": read_purpose,
            "read_authorization": {
                **read_scope_snapshot,
                "token_id": str(usage.read_authorization_token),
            },
        }
        if usage.scope_snapshot and dict(usage.scope_snapshot) != scope_snapshot:
            raise ValueError("profile usage scope snapshot must match derived access")
        self._assert_write_not_frozen(access_control)
        serialized = {
            "profile_fields_used": json.dumps(
                supplied_fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "profile_revision_ids": json.dumps(
                revision_ids, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "matched_assertion_revisions": json.dumps(
                revision_map, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "scope_snapshot": json.dumps(
                scope_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "expected_delta": json.dumps(
                dict(receipt.expected_delta),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "actual_target_delta": json.dumps(
                dict(receipt.actual_target_delta),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "target_receipt": json.dumps(
                receipt.as_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "access_control": _access_json(access_control),
        }
        expected_row = (
            consumer,
            serialized["profile_fields_used"],
            serialized["profile_revision_ids"],
            serialized["matched_assertion_revisions"],
            serialized["scope_snapshot"],
            read_purpose,
            str(usage.read_authorization_token),
            1 if receipt.action_changed else 0,
            str(usage.outcome or ""),
            str(usage.user_feedback or ""),
            receipt.request_id,
            receipt.decision_id,
            receipt.baseline_hash,
            receipt.persona_enabled_hash,
            serialized["expected_delta"],
            serialized["actual_target_delta"],
            serialized["target_receipt"],
            receipt.receipt_hash,
            receipt.terminal_status,
            idempotency_key,
            serialized["access_control"],
        )
        intent = {
            "schema_version": "mnemos.persona_profile_usage_intent.v1",
            "consumer": consumer,
            "profile_fields_used": supplied_fields,
            "profile_revision_ids": revision_ids,
            "scope_snapshot": scope_snapshot,
            "read_purpose": read_purpose,
            "read_authorization_token": str(usage.read_authorization_token),
            "target_receipt": receipt.as_dict(),
            "outcome": str(usage.outcome or ""),
            "user_feedback": str(usage.user_feedback or ""),
        }
        return _PreparedProfileUsage(
            idempotency_key=idempotency_key,
            command_id=command_id,
            intent_json=json.dumps(
                intent,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            target_receipt_hash=receipt.receipt_hash,
            access_control=serialized["access_control"],
            expected_row=expected_row,
        )

    @staticmethod
    def _enqueue_profile_usage_outbox(
        conn: sqlite3.Connection,
        prepared: _PreparedProfileUsage,
    ) -> None:
        existing = conn.execute(
            """
            SELECT command_id, idempotency_key, intent_json,
                   target_receipt_hash, access_control
            FROM profile_usage_outbox
            WHERE command_id=? OR idempotency_key=? OR target_receipt_hash=?
            """,
            (
                prepared.command_id,
                prepared.idempotency_key,
                prepared.target_receipt_hash,
            ),
        ).fetchone()
        expected = (
            prepared.command_id,
            prepared.idempotency_key,
            prepared.intent_json,
            prepared.target_receipt_hash,
            prepared.access_control,
        )
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError("immutable profile usage idempotency conflict")
            return
        conn.execute(
            """
            INSERT INTO profile_usage_outbox (
                command_id, idempotency_key, intent_json,
                target_receipt_hash, access_control, status
            ) VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            expected,
        )

    @staticmethod
    def _persist_prepared_usage(
        conn: sqlite3.Connection,
        prepared: _PreparedProfileUsage,
    ) -> int:
        existing = conn.execute(
            """
            SELECT id, consumer, profile_fields_used, profile_revision_ids,
                   matched_assertion_revisions, scope_snapshot, read_purpose,
                   read_authorization_token, action_changed, outcome,
                   user_feedback, request_id, decision_id,
                   baseline_hash, persona_enabled_hash, expected_delta,
                   actual_target_delta, target_receipt, target_receipt_hash,
                   terminal_status, idempotency_key, access_control
            FROM profile_usage_log WHERE idempotency_key=?
            """,
            (prepared.idempotency_key,),
        ).fetchone()
        if existing is not None:
            if tuple(existing[1:]) != prepared.expected_row:
                raise ValueError("immutable profile usage idempotency conflict")
            return int(existing[0])
        cursor = conn.execute(
            """
            INSERT INTO profile_usage_log (
                consumer, profile_fields_used, profile_revision_ids,
                matched_assertion_revisions, scope_snapshot, read_purpose,
                read_authorization_token, action_changed, outcome,
                user_feedback, request_id, decision_id,
                baseline_hash, persona_enabled_hash, expected_delta,
                actual_target_delta, target_receipt, target_receipt_hash,
                terminal_status, idempotency_key, access_control
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            prepared.expected_row,
        )
        return cursor.lastrowid or 0

    @staticmethod
    def _current_assertion_revision_map(
        conn: sqlite3.Connection,
        assertion_ids: List[str],
    ) -> Dict[str, str]:
        revisions: Dict[str, str] = {}
        for assertion_id in assertion_ids:
            row = conn.execute(
                """
                SELECT head.revision_id
                FROM profile_assertion_heads AS head
                JOIN profile_assertion_revisions AS revision
                  ON revision.revision_id=head.revision_id
                 AND revision.assertion_id=head.assertion_id
                JOIN profile_assertions AS current
                  ON current.assertion_id=head.assertion_id
                 AND current.current_revision_id=head.revision_id
                WHERE head.assertion_id=?
                """,
                (assertion_id,),
            ).fetchone()
            if row is None or not str(row[0] or ""):
                raise ValueError(f"profile assertion projection/head mismatch: {assertion_id}")
            revisions[assertion_id] = str(row[0])
        return dict(sorted(revisions.items()))

    @staticmethod
    def _current_assertion_revision_ids(
        conn: sqlite3.Connection,
        assertion_ids: List[str],
    ) -> List[str]:
        return sorted(
            set(
                ProfileUsageRepositoryMixin._current_assertion_revision_map(
                    conn,
                    assertion_ids,
                ).values()
            )
        )

    def _issue_profile_read_authorization(
        self,
        *,
        assertions: List[Dict[str, Any]],
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing | None,
        purpose: str,
        consumer: str,
    ) -> str:
        expected_purpose = _PROFILE_USAGE_CONSUMER_PURPOSES.get(consumer)
        if expected_purpose != purpose:
            raise ValueError("profile read consumer/purpose contract mismatch")
        revision_map = {
            str(assertion.get("assertion_id") or ""): str(
                assertion.get("current_revision_id") or ""
            )
            for assertion in assertions
        }
        if (
            not revision_map
            or len(revision_map) != len(assertions)
            or any(not key or not value for key, value in revision_map.items())
        ):
            raise ValueError("profile read authorization requires exact revisions")
        accesses = [
            validate_cognitive_access_envelope(assertion.get("access_control") or {})
            for assertion in assertions
        ]
        first = accesses[0]
        token_access = derive_strictest_cognitive_access(
            accesses,
            owner_principal_id=str(first["owner"]["principal_id"]),
            owner_agent=str(first["owner"]["agent"]),
            scope_type=str(first["scope"]["scope_type"]),
            scope_id=str(first["scope"]["scope_id"]),
            purposes=(purpose,),
            retention_policy="persona_read_authorization",
        )
        if (
            token_access["scope"]["resolution"] != "resolved"
            or token_access["visibility"] == "restricted"
            or token_access["consent"]["status"] != "granted"
            or purpose not in set(token_access["purposes"])
        ):
            raise ValueError(
                "profile read authorization requires one compatible resolved ACL context"
            )
        scope_snapshot = self._profile_read_scope_snapshot(
            principal=principal,
            narrowing=narrowing,
            purpose=purpose,
            consumer=consumer,
        )
        access_hashes = {
            assertion_id: cognitive_access_hash(access)
            for assertion_id, access in zip(revision_map, accesses)
        }
        token_id = f"profile-read:{uuid4().hex}"
        issued_at = datetime.now(timezone.utc)
        conn = self._pool.get_conn()
        conn.execute(
            """
            INSERT INTO profile_read_authorizations (
                token_id, consumer, read_purpose, principal_id, principal_agent,
                scope_snapshot, authorized_assertion_revisions,
                assertion_access_hashes, access_control, status,
                consumed_command_id, issued_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', '', ?, ?)
            """,
            (
                token_id,
                consumer,
                purpose,
                str(principal.principal_id),
                str(principal.agent),
                json.dumps(
                    scope_snapshot,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps(
                    dict(sorted(revision_map.items())),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps(
                    dict(sorted(access_hashes.items())),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                _access_json(token_access),
                issued_at.isoformat(),
                (issued_at + _PROFILE_READ_AUTHORIZATION_TTL).isoformat(),
            ),
        )
        conn.commit()
        return token_id

    @staticmethod
    def _profile_read_scope_snapshot(
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing | None,
        purpose: str,
        consumer: str,
    ) -> Dict[str, Any]:
        effective_narrowing = narrowing or AccessNarrowing()
        return {
            "principal_id": str(principal.principal_id),
            "principal_agent": str(principal.agent),
            "host_kind": str(principal.host_kind),
            "capability_id": str(principal.capability_id),
            "capabilities": sorted(str(item) for item in principal.capabilities),
            "session_id": str(effective_narrowing.session_id or ""),
            "project": str(effective_narrowing.project or "").lower(),
            "read_purpose": str(purpose),
            "consumer": str(consumer),
        }
