"""Durable DecisionTrace-backed Persona challenge command consumer."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from core.cognitive.access_control import validate_cognitive_access_envelope
from core.cognitive.persona_challenge_contract import (
    PERSONA_CHALLENGE_COMMAND,
    PERSONA_CHALLENGE_CONSUMER,
    PERSONA_CHALLENGE_SCHEMA_VERSION,
    build_persona_challenge_command as build_persona_challenge_command,
)
from core.cognitive.state_contract import sha256_json
from core.cognitive.state_store import CognitiveStateStore

__all__ = ["build_persona_challenge_command"]

PERSONA_CHALLENGE_DELIVERY_SCHEMA_VERSION = "mnemos.persona_challenge_delivery.v1"


class ChallengeDeliveryBindingError(ValueError):
    """A computed challenge is not bound to a current canonical asset revision."""


def current_persona_revision_binding(db_path: Path | str) -> dict[str, str] | None:
    """Read the exact global Persona head without initializing or mutating it."""

    path = Path(db_path).expanduser().resolve(strict=False)
    if not path.is_file():
        return None
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30) as conn:
        conn.execute("PRAGMA query_only=ON")
        try:
            row = conn.execute(
                """
                SELECT revision.revision_id, revision.content_hash
                FROM persona_revision_heads AS head
                JOIN persona_revisions AS revision
                  ON revision.revision_id=head.revision_id
                WHERE head.scope_key='global'
                """
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise
    if row is None:
        return None
    revision_id, content_hash = str(row[0] or ""), str(row[1] or "")
    if not revision_id or not _is_sha256(content_hash):
        raise ValueError("canonical Persona head has invalid revision binding")
    return {"revision_id": revision_id, "content_hash": content_hash}


class PersonaChallengeQueueConsumer:
    """Consume one immutable Persona challenge command per daemon tick."""

    def __init__(
        self,
        config: Any,
        *,
        manager_factory: Callable[[Any], Any] | None = None,
        failpoint: Callable[[str], None] | None = None,
    ):
        self.config = config
        self.state = CognitiveStateStore(config)
        self.persona_db_path = Path(config.database_dir) / "user_signals.db"
        self.manager_factory = manager_factory or self._default_manager
        self.failpoint = failpoint

    def run_once(self) -> dict[str, Any]:
        try:
            pending = self.state.pending_commands(PERSONA_CHALLENGE_CONSUMER)
        except FileNotFoundError:
            pending = []
        if not pending:
            return {
                "status": "noop",
                "reason": "no_pending_decision_command",
                "challenges": 0,
                "consumed": 0,
            }
        command = pending[0]
        self._call_failpoint("after_command_read")
        decision = self._validate_command(command)
        current_persona = current_persona_revision_binding(self.persona_db_path)
        bound_persona = dict(command["payload"]["persona_revision"])
        if current_persona != bound_persona:
            self._record_skip(command, reason="stale_persona_revision")
            return {
                "status": "intentional_skip",
                "reason": "stale_persona_revision",
                "challenges": 0,
                "consumed": 1,
                "command_id": command["command_id"],
            }
        try:
            delivery_commands = self._evaluate_delivery_commands(
                command,
                decision,
                bound_persona,
            )
        except ChallengeDeliveryBindingError as exc:
            self._record_skip(command, reason=str(exc))
            return {
                "status": "intentional_skip",
                "reason": str(exc),
                "challenges": 0,
                "consumed": 1,
                "command_id": command["command_id"],
            }
        result_hash = sha256_json(delivery_commands)
        self._call_failpoint("after_challenge_before_receipt")
        if not delivery_commands:
            self._commit_result(
                command,
                decision=decision,
                bound_persona=bound_persona,
                delivery_commands=delivery_commands,
                presentation_receipt=None,
            )
            return {
                "status": "consumed",
                "reason": "no_admitted_canonical_revision",
                "challenges": 0,
                "consumed": 1,
                "command_id": command["command_id"],
                "challenge_result_hash": result_hash,
                "delivery_ids": [],
            }
        rendered_content_hash = _rendered_challenge_content_hash(delivery_commands)
        return {
            "status": "awaiting_presentation",
            "reason": "delivery_pending_presentation",
            "challenges": len(delivery_commands),
            "consumed": 0,
            "command_id": command["command_id"],
            "challenge_result_hash": result_hash,
            "delivery_ids": [
                delivery["delivery_id"] for delivery in delivery_commands
            ],
            "rendered_content_hash": rendered_content_hash,
        }

    def record_presentation(
        self,
        *,
        command_id: str,
        delivery_ids: Sequence[str],
        host_agent: str,
        rendered_content_hash: str,
    ) -> dict[str, Any]:
        """Close a challenge command only after an exact host presentation ack."""

        normalized_command_id = str(command_id or "").strip()
        normalized_ids = tuple(sorted(str(value or "").strip() for value in delivery_ids))
        normalized_agent = str(host_agent or "").strip().lower()
        normalized_rendered_hash = str(rendered_content_hash or "").strip()
        if (
            not normalized_command_id
            or not normalized_ids
            or any(not value for value in normalized_ids)
            or len(set(normalized_ids)) != len(normalized_ids)
            or not normalized_agent
            or not _is_sha256(normalized_rendered_hash)
        ):
            raise ValueError("challenge presentation acknowledgement is invalid")
        command = self.state.command(normalized_command_id)
        if command is None:
            raise ValueError("challenge presentation command is unavailable")
        existing = self.state.effect_receipt(normalized_command_id)
        if existing is not None:
            try:
                outcome = json.loads(str(existing["consumption_outcome"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "challenge presentation command is already terminal"
                ) from exc
            presentation = dict(outcome.get("presentation_receipt") or {})
            if (
                tuple(sorted(str(value) for value in presentation.get("delivery_ids") or ()))
                != normalized_ids
                or str(presentation.get("host_agent") or "") != normalized_agent
                or str(presentation.get("rendered_content_hash") or "")
                != normalized_rendered_hash
            ):
                raise ValueError("immutable challenge presentation receipt conflict")
            return presentation
        decision = self._validate_command(command)
        bound_persona = dict(command["payload"]["persona_revision"])
        if current_persona_revision_binding(self.persona_db_path) != bound_persona:
            raise ValueError("challenge presentation Persona revision is stale")
        delivery_commands = self._evaluate_delivery_commands(
            command,
            decision,
            bound_persona,
        )
        expected_ids = tuple(
            sorted(str(delivery["delivery_id"]) for delivery in delivery_commands)
        )
        if normalized_ids != expected_ids:
            raise ValueError("challenge presentation delivery set mismatch")
        if normalized_agent != str(command["payload"]["principal"]["agent"]).lower():
            raise PermissionError("challenge presentation principal mismatch")
        if normalized_rendered_hash != _rendered_challenge_content_hash(delivery_commands):
            raise ValueError("challenge presentation content hash mismatch")
        receipt_core = {
            "schema_version": "mnemos.persona_challenge_presentation.v1",
            "source_command_id": normalized_command_id,
            "delivery_ids": list(normalized_ids),
            "host_agent": normalized_agent,
            "rendered_content_hash": normalized_rendered_hash,
            "challenge_result_hash": sha256_json(delivery_commands),
            "presented_at": datetime.now(timezone.utc).isoformat(),
        }
        presentation = {
            **receipt_core,
            "receipt_hash": sha256_json(receipt_core),
        }
        self._commit_result(
            command,
            decision=decision,
            bound_persona=bound_persona,
            delivery_commands=delivery_commands,
            presentation_receipt=presentation,
        )
        return presentation

    def _evaluate_delivery_commands(
        self,
        command: Mapping[str, Any],
        decision: Any,
        bound_persona: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        persona = self._load_bound_persona(bound_persona)
        from core.persona.psyche import SignalStore

        signal_store = SignalStore(db_path=self.persona_db_path, config=self.config)
        try:
            manager = self.manager_factory(signal_store)
            challenges = manager.analyze_and_update(
                session_context=self._session_context(command, decision),
                user_options=self._user_options(decision),
                persona=persona,
            )
        finally:
            signal_store.close()
        return [
            self._build_delivery_command(command, decision, challenge)
            for challenge in challenges
        ]

    def _commit_result(
        self,
        command: Mapping[str, Any],
        *,
        decision: Any,
        bound_persona: Mapping[str, str],
        delivery_commands: Sequence[Mapping[str, Any]],
        presentation_receipt: Mapping[str, Any] | None,
    ) -> None:
        rows = [dict(delivery) for delivery in delivery_commands]
        result_hash = sha256_json(rows)
        disposition = (
            "presented"
            if presentation_receipt is not None
            else "no_admitted_canonical_revision"
        )
        before_hash = sha256_json(
            {"command_id": command["command_id"], "state": "pending"}
        )
        after_hash = sha256_json(
            {
                "command_id": command["command_id"],
                "state": "consumed",
                "disposition": disposition,
                "challenge_result_hash": result_hash,
                "presentation_receipt_hash": str(
                    (presentation_receipt or {}).get("receipt_hash") or ""
                ),
            }
        )
        self.state.record_effect_receipt(
            str(command["command_id"]),
            status="committed",
            target_effect_id=f"persona-challenge:{command['command_id']}",
            before_hash=before_hash,
            after_hash=after_hash,
            evidence_refs=(
                f"persona-challenge-command:{command['command_id']}",
                f"decision-revision:{decision.revision_id}",
                f"persona-revision:{bound_persona['revision_id']}",
                f"challenge-result:{result_hash}",
                *(
                    f"blindspot-revision:{delivery['asset_revision']['revision_id']}"
                    for delivery in rows
                ),
                *(
                    f"challenge-delivery:{delivery['delivery_id']}"
                    for delivery in rows
                ),
                *(
                    (
                        f"challenge-presentation:{presentation_receipt['receipt_hash']}",
                    )
                    if presentation_receipt is not None
                    else ()
                ),
            ),
            outcome=json.dumps(
                {
                    "schema_version": "mnemos.persona_decision_challenge_result.v2",
                    "disposition": disposition,
                    "challenge_count": len(rows),
                    "challenge_result_hash": result_hash,
                    "delivery_commands": rows,
                    "presentation_receipt": dict(presentation_receipt or {}),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            created_at=str(
                (presentation_receipt or {}).get("presented_at")
                or command["created_at"]
            ),
        )

    def _validate_command(self, command: Mapping[str, Any]) -> Any:
        if (
            command.get("consumer_id") != PERSONA_CHALLENGE_CONSUMER
            or command.get("command_type") != PERSONA_CHALLENGE_COMMAND
        ):
            raise ValueError("Persona challenge command owner mismatch")
        payload = command.get("payload")
        if not isinstance(payload, Mapping) or payload.get(
            "schema_version"
        ) != PERSONA_CHALLENGE_SCHEMA_VERSION:
            raise ValueError("Persona challenge command payload is invalid")
        decision = self.state.revision(str(command.get("revision_id") or ""))
        if decision is None or decision.object_type != "decision_trace":
            raise ValueError("Persona challenge command lacks DecisionTrace")
        decision_ref = payload.get("decision_trace")
        if not isinstance(decision_ref, Mapping) or (
            str(decision_ref.get("decision_id") or "") != decision.object_id
            or str(decision_ref.get("revision_id") or "") != decision.revision_id
            or str(decision_ref.get("content_hash") or "") != decision.payload_hash
        ):
            raise ValueError("Persona challenge DecisionTrace binding mismatch")
        expected = _option_bindings(decision.payload.get("candidates") or ())
        if list(payload.get("options") or ()) != expected:
            raise ValueError("Persona challenge option binding mismatch")
        access = validate_cognitive_access_envelope(
            decision.payload["access_control"],
            expected_scope_type=decision.scope_type,
            expected_scope_id=decision.scope_id,
        )
        if dict(payload.get("principal") or {}) != {
            "principal_id": str(access["owner"]["principal_id"]),
            "agent": str(access["owner"]["agent"]),
        }:
            raise ValueError("Persona challenge principal binding mismatch")
        if dict(payload.get("scope") or {}) != {
            "type": decision.scope_type,
            "id": decision.scope_id,
        }:
            raise ValueError("Persona challenge scope binding mismatch")
        return decision

    def _load_bound_persona(self, binding: Mapping[str, str]) -> Any:
        from core.persona.projection_runtime import PersonaProjectionMixin

        path = self.persona_db_path.expanduser().resolve(strict=False)
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM persona_revisions WHERE revision_id=? AND content_hash=?",
                (str(binding["revision_id"]), str(binding["content_hash"])),
            ).fetchone()
        if row is None:
            raise ValueError("bound Persona revision is unavailable")
        decoded = dict(row)
        for field in (
            "energy_profile",
            "cognitive_profile",
            "value_profile",
            "blindspot_profile",
        ):
            decoded[field] = json.loads(decoded.get(field) or "{}")
        persona, _blindspot = PersonaProjectionMixin._profile_from_db_row(decoded)
        return persona

    @staticmethod
    def _session_context(command: Mapping[str, Any], decision: Any) -> dict[str, Any]:
        access = validate_cognitive_access_envelope(decision.payload["access_control"])
        scope = access["scope"]
        return {
            "decision_id": decision.object_id,
            "decision_trace_revision_id": decision.revision_id,
            "session_id": str(scope["session_id"]),
            "project_id": str(scope["project"] or decision.scope_id),
            "principal_id": str(access["owner"]["principal_id"]),
            "decision_created_at": str(decision.created_at),
            "task": str(decision.payload["task"]),
            "goal": str(decision.payload["goal"]),
            "user_message": str(decision.payload["task"]),
        }

    def _build_delivery_command(
        self,
        command: Mapping[str, Any],
        decision: Any,
        challenge: Any,
    ) -> dict[str, Any]:
        from core.cognitive.user_model_asset_store import UserCognitiveBlindspotStore
        from core.persona.hamartia import (
            BlindSpotProfileManager,
            CanonicalBlindspotChallenge,
        )

        if not isinstance(challenge, CanonicalBlindspotChallenge):
            raise ChallengeDeliveryBindingError("challenge_result_not_canonical")
        asset_store = UserCognitiveBlindspotStore(
            Path(self.config.database_dir) / "user_cognitive_blindspots.db"
        )
        current = asset_store.current_blindspot(challenge.asset_id)
        if current is None or current.revision_id != challenge.asset_revision_id:
            raise ChallengeDeliveryBindingError("stale_canonical_blindspot_revision")
        recomputed = BlindSpotProfileManager._canonical_challenge_for_context(
            current,
            self._session_context(command, decision),
        )
        if recomputed is None:
            raise ChallengeDeliveryBindingError("revoked_or_expired_canonical_blindspot")
        if asdict(recomputed) != asdict(challenge):
            raise ChallengeDeliveryBindingError("challenge_content_or_revision_hash_changed")
        decision_ref = dict(command["payload"]["decision_trace"])
        persona_ref = dict(command["payload"]["persona_revision"])
        asset_ref = {
            "asset_id": challenge.asset_id,
            "revision_id": challenge.asset_revision_id,
            "content_hash": challenge.asset_revision_hash,
            "source_kind": challenge.source_kind,
        }
        challenge_ref = {
            "type": challenge.type,
            "content": challenge.challenge_content,
            "content_hash": challenge.challenge_content_hash,
        }
        delivery_identity = {
            "source_command_id": str(command["command_id"]),
            "source_command_hash": str(command["payload_hash"]),
            "decision_trace": decision_ref,
            "persona_revision": persona_ref,
            "asset_revision": asset_ref,
            "challenge": challenge_ref,
        }
        return {
            "schema_version": PERSONA_CHALLENGE_DELIVERY_SCHEMA_VERSION,
            "delivery_id": (
                "persona-challenge-delivery-"
                + sha256_json(delivery_identity).split(":", 1)[1][:32]
            ),
            **delivery_identity,
            "principal": dict(command["payload"]["principal"]),
            "scope": dict(command["payload"]["scope"]),
            "status": "pending_presentation",
        }

    @staticmethod
    def _user_options(decision: Any) -> list[dict[str, Any]]:
        selected = str(decision.payload["selection"]["candidate_id"])
        return [
            {
                "id": str(candidate["candidate_id"]),
                "key": str(candidate["key"]),
                "summary": str(candidate["summary"]),
                "selected": str(candidate["candidate_id"]) == selected,
            }
            for candidate in decision.payload["candidates"]
        ]

    def _record_skip(self, command: Mapping[str, Any], *, reason: str) -> None:
        payload_hash = str(command["payload_hash"])
        self.state.record_effect_receipt(
            str(command["command_id"]),
            status="intentional_skip",
            target_effect_id=f"persona-challenge:{command['command_id']}",
            before_hash=payload_hash,
            after_hash=payload_hash,
            evidence_refs=(
                f"persona-challenge-command:{command['command_id']}",
                f"decision-revision:{command['revision_id']}",
                f"persona-challenge-skip:{reason}",
            ),
            outcome=reason,
            terminal_reason_code=reason,
            created_at=str(command["created_at"]),
        )

    def _call_failpoint(self, stage: str) -> None:
        if self.failpoint is not None:
            self.failpoint(stage)

    def _default_manager(self, store: Any) -> Any:
        from core.cognitive.user_model_asset_store import UserCognitiveBlindspotStore
        from core.persona.hamartia import BlindSpotProfileManager

        return BlindSpotProfileManager(
            store=store,
            asset_store=UserCognitiveBlindspotStore(
                Path(self.config.database_dir) / "user_cognitive_blindspots.db"
            ),
        )


def _option_bindings(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    return sorted(
        (
            {
                "option_id": str(candidate["candidate_id"]),
                "option_key": str(candidate["key"]),
                "option_hash": sha256_json(dict(candidate)),
            }
            for candidate in candidates
        ),
        key=lambda item: item["option_id"],
    )


def _rendered_challenge_content_hash(
    delivery_commands: Sequence[Mapping[str, Any]],
) -> str:
    return sha256_json(
        [
            {
                "delivery_id": str(delivery["delivery_id"]),
                "content": str(delivery["challenge"]["content"]),
                "content_hash": str(delivery["challenge"]["content_hash"]),
            }
            for delivery in sorted(
                delivery_commands,
                key=lambda item: str(item["delivery_id"]),
            )
        ]
    )


def _is_sha256(value: str) -> bool:
    normalized = str(value or "")
    return (
        normalized.startswith("sha256:")
        and len(normalized) == 71
        and all(char in "0123456789abcdef" for char in normalized[7:])
    )
