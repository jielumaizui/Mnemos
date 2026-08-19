"""Low-level append-only journal used by domain-owned feedback proposal stores.

The canonical feedback owner never constructs this journal directly.  Each
domain owner supplies its own database, tables, owner identity, and gate
contract through a small factory in that domain module.  The resulting target
proof binds an independently readable proposal/action row to a reciprocal
domain receipt; a receipt row alone is insufficient.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from core.cognitive.feedback_migration_barrier import assert_feedback_writes_enabled
from core.cognitive.feedback_contract import FEEDBACK_TARGET_JOURNAL_CONTRACTS
from core.cognitive.feedback_models import (
    CognitiveEntityReference,
    FeedbackProposalGateFactory,
    FeedbackTargetEffect,
)
from core.cognitive.state_contract import sha256_json
from core.db_utils import render_sql


DOMAIN_PROPOSAL_SCHEMA_VERSION = "mnemos.domain_feedback_proposal.v1"
DOMAIN_ACTION_SCHEMA_VERSION = "mnemos.domain_feedback_proposal_action.v1"
DOMAIN_RECEIPT_SCHEMA_VERSION = "mnemos.domain_feedback_proposal_receipt.v1"
DOMAIN_EXTERNAL_PROPOSAL_SCHEMA_VERSION = (
    "mnemos.domain_feedback_external_proposal.v1"
)
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*\Z")


class DomainFeedbackProposalStore:
    """Persist and verify proposal-only state on behalf of one domain owner."""

    def __init__(
        self,
        *,
        database_dir: Path,
        db_file: str,
        target_id: str,
        owner_id: str,
        proposal_table: str,
        action_table: str,
        receipt_table: str,
        gate_contract_id: str,
        proposal_gate_factory: FeedbackProposalGateFactory | None = None,
    ) -> None:
        identifiers = (proposal_table, action_table, receipt_table)
        if any(not _IDENTIFIER.fullmatch(value) for value in identifiers):
            raise ValueError("domain feedback proposal table identifier is invalid")
        candidate_contract = {
            "db_file": str(db_file),
            "owner_id": str(owner_id),
            "proposal_table": str(proposal_table),
            "action_table": str(action_table),
            "receipt_table": str(receipt_table),
            "gate_contract_id": str(gate_contract_id),
        }
        if FEEDBACK_TARGET_JOURNAL_CONTRACTS.get(str(target_id)) != candidate_contract:
            raise ValueError("domain feedback proposal journal is not registered")
        self.database_dir = Path(database_dir).expanduser()
        self.db_path = self.database_dir / str(db_file)
        self.target_id = str(target_id)
        self.owner_id = str(owner_id)
        self.proposal_table = proposal_table
        self.action_table = action_table
        self.receipt_table = receipt_table
        self.gate_contract_id = str(gate_contract_id)
        self.proposal_gate_factory = proposal_gate_factory
        self._schema_inode: tuple[int, int] | None = None

    def apply(self, command: Mapping[str, Any]) -> FeedbackTargetEffect:
        """Append one pending-review proposal and its reciprocal domain receipt."""

        assert_feedback_writes_enabled(self.database_dir)
        payload = self._validate_apply_command(command)
        existing = self.inspect_command_effect(payload)
        if existing is not None:
            return existing
        if self.proposal_gate_factory is None:
            raise PermissionError(
                "feedback proposal requires an injected trusted material gate"
            )
        gate = self.proposal_gate_factory(
            database_dir=self.database_dir,
            target_id=self.target_id,
            owner_id=self.owner_id,
            gate_contract_id=self.gate_contract_id,
            proposal=self._proposal_payload(payload),
        )
        proposal = dict(gate.proposal)
        proposal_id = self._identity("proposal", proposal)
        if gate.proposal_id != proposal_id:
            raise PermissionError("feedback proposal gate identity mismatch")
        terminal = gate.terminal_proof()
        self._ensure_schema()
        if terminal is not None:
            recovered = self.inspect_command_effect(payload)
            if (
                recovered is None
                or recovered.target_effect_id != gate.material_effect_id
                or recovered.decision_trace_refs != gate.decision_trace_refs
                or recovered.action_refs != gate.action_refs
                or terminal.effect_id != recovered.target_effect_id
                or terminal.before_hash != recovered.before_hash
                or terminal.after_hash != recovered.after_hash
            ):
                raise RuntimeError(
                    "terminal feedback proposal authorization lacks its exact domain proof"
                )
            return recovered
        gate.validate()
        before_hash = sha256_json(
            {
                "target_id": self.target_id,
                "owner_id": self.owner_id,
                "proposal_id": proposal_id,
                "state": "absent",
            }
        )
        after_hash = sha256_json(proposal)
        with self._connect() as conn:
            self._insert_proposal(conn, proposal_id, proposal)
            effect = self._insert_receipt(
                conn,
                command_key=str(payload["command_key"]),
                state_kind="proposal",
                state_id=proposal_id,
                disposition="proposal_committed",
                before_hash=before_hash,
                after_hash=after_hash,
                state_payload_hash=after_hash,
                target_effect_id=gate.material_effect_id,
                material_command_id=gate.material_command_id,
                decision_trace_refs=gate.decision_trace_refs,
                action_refs=gate.action_refs,
            )
        gate.record_committed(
            before_hash=effect.before_hash,
            after_hash=effect.after_hash,
            target_receipt_ref=effect.target_receipt_ref,
            created_at=self._now(),
        )
        if not self.verify(effect):
            raise RuntimeError("domain feedback proposal proof is not recoverable")
        return effect

    def neutralize(self, command: Mapping[str, Any]) -> FeedbackTargetEffect:
        """Append an exact suppress/revoke/compensate action for a prior proposal."""

        assert_feedback_writes_enabled(self.database_dir)
        payload = self._validate_neutralization_command(command)
        self._ensure_schema()
        prior_ref = str(payload["prior_target_receipt_ref"])
        prior_receipt_id = self._receipt_id_from_ref(prior_ref)
        with self._connect() as conn:
            prior = conn.execute(
                render_sql(
                    "SELECT * FROM {table} WHERE receipt_id=?",
                    identifiers={"table": self.receipt_table},
                ),
                (prior_receipt_id,),
            ).fetchone()
            if (
                prior is None
                or str(prior["target_id"]) != self.target_id
                or str(prior["after_hash"]) != str(payload["prior_after_hash"])
                or str(prior["disposition"])
                not in {"proposal_committed", "committed_effect", "compensated"}
            ):
                raise ValueError("domain feedback neutralization source mismatch")
            action = self._action_payload(payload, prior)
            action_id = self._identity("action", action)
            after_hash = sha256_json(action)
            self._insert_action(conn, action_id, action)
            disposition = {
                "suppress": "suppressed",
                "revoke": "revoked",
                "compensate": "compensated",
            }[str(payload["neutralization_kind"])]
            effect = self._insert_receipt(
                conn,
                command_key=str(payload["command_key"]),
                state_kind="action",
                state_id=action_id,
                disposition=disposition,
                before_hash=str(payload["prior_after_hash"]),
                after_hash=after_hash,
                state_payload_hash=after_hash,
            )
        if not self.verify(effect):
            raise RuntimeError("domain feedback neutralization proof is not recoverable")
        return effect

    def apply_external_proposal(
        self,
        *,
        command_key: str,
        source_event_ref: str,
        source_content_hash: str,
        evidence_refs: tuple[str, ...],
        metadata: Mapping[str, Any],
    ) -> FeedbackTargetEffect:
        """Append a domain-owned review proposal from a non-COG command owner."""

        assert_feedback_writes_enabled(self.database_dir)
        normalized_command = str(command_key or "").strip()
        normalized_event = str(source_event_ref or "").strip()
        normalized_hash = str(source_content_hash or "").strip()
        normalized_refs = tuple(str(ref).strip() for ref in evidence_refs)
        if (
            not normalized_command
            or not normalized_event
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", normalized_hash)
            or not normalized_refs
            or any(not ref for ref in normalized_refs)
        ):
            raise ValueError("external domain feedback proposal input is incomplete")
        proposal = {
            "schema_version": DOMAIN_EXTERNAL_PROPOSAL_SCHEMA_VERSION,
            "target_id": self.target_id,
            "owner_id": self.owner_id,
            "gate_contract_id": self.gate_contract_id,
            "command_key": normalized_command,
            "source_event_ref": normalized_event,
            "source_content_hash": normalized_hash,
            "evidence_refs": list(normalized_refs),
            "metadata": dict(metadata),
            "status": "pending_review",
            "direct_domain_update": False,
            "training_admitted": False,
        }
        self._ensure_schema()
        proposal_id = self._identity("proposal", proposal)
        before_hash = sha256_json(
            {
                "target_id": self.target_id,
                "owner_id": self.owner_id,
                "proposal_id": proposal_id,
                "state": "absent",
            }
        )
        after_hash = sha256_json(proposal)
        with self._connect() as conn:
            self._insert_proposal(conn, proposal_id, proposal)
            effect = self._insert_receipt(
                conn,
                command_key=normalized_command,
                state_kind="proposal",
                state_id=proposal_id,
                disposition="proposal_committed",
                before_hash=before_hash,
                after_hash=after_hash,
                state_payload_hash=after_hash,
            )
        if not self.verify(effect):
            raise RuntimeError("external domain feedback proposal proof is not recoverable")
        return effect

    def verify(self, effect: FeedbackTargetEffect) -> bool:
        """Independently re-read domain state and prove the reciprocal receipt."""

        if not self.db_path.is_file() or effect.target_id != self.target_id:
            return False
        try:
            receipt_id = self._receipt_id_from_ref(effect.target_receipt_ref)
            with self._connect(read_only=True) as conn:
                receipt = conn.execute(
                    render_sql(
                        "SELECT * FROM {table} WHERE receipt_id=?",
                        identifiers={"table": self.receipt_table},
                    ),
                    (receipt_id,),
                ).fetchone()
                if receipt is None:
                    return False
                state_kind = str(receipt["state_kind"])
                table = (
                    self.proposal_table if state_kind == "proposal" else self.action_table
                )
                if state_kind not in {"proposal", "action"}:
                    return False
                state = conn.execute(
                    render_sql(
                        "SELECT payload_json, payload_hash FROM {table} "
                        "WHERE {id_column}=?",
                        identifiers={
                            "table": table,
                            "id_column": (
                                "proposal_id"
                                if state_kind == "proposal"
                                else "action_id"
                            ),
                        },
                    ),
                    (str(receipt["state_id"]),),
                ).fetchone()
        except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
            return False
        if state is None:
            return False
        try:
            state_payload = json.loads(str(state["payload_json"]))
            decision_refs = self._entity_refs_from_json(
                str(receipt["decision_trace_refs_json"])
            )
            action_refs = self._entity_refs_from_json(
                str(receipt["action_refs_json"])
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        state_hash = sha256_json(state_payload)
        return bool(
            state_hash == str(state["payload_hash"])
            and state_hash == str(receipt["state_payload_hash"])
            and str(receipt["schema_version"]) == DOMAIN_RECEIPT_SCHEMA_VERSION
            and str(receipt["target_id"]) == self.target_id
            and str(receipt["owner_id"]) == self.owner_id
            and str(receipt["target_effect_id"]) == effect.target_effect_id
            and str(receipt["disposition"]) == effect.disposition
            and str(receipt["before_hash"]) == effect.before_hash
            and str(receipt["after_hash"]) == effect.after_hash
            and state_hash == effect.after_hash
            and decision_refs == effect.decision_trace_refs
            and action_refs == effect.action_refs
        )

    def verify_command_effect(
        self,
        command: Mapping[str, Any],
        effect: FeedbackTargetEffect,
    ) -> bool:
        """Prove that one domain receipt was created for this exact command."""

        payload = dict(command)
        if not self.db_path.is_file() or effect.target_id != self.target_id:
            return False
        try:
            receipt_id = self._receipt_id_from_ref(effect.target_receipt_ref)
            with self._connect(read_only=True) as conn:
                receipt = conn.execute(
                    render_sql(
                        "SELECT * FROM {table} WHERE receipt_id=?",
                        identifiers={"table": self.receipt_table},
                    ),
                    (receipt_id,),
                ).fetchone()
                if receipt is None:
                    return False
                state_kind = str(receipt["state_kind"])
                if state_kind not in {"proposal", "action"}:
                    return False
                state_table = (
                    self.proposal_table
                    if state_kind == "proposal"
                    else self.action_table
                )
                id_column = (
                    "proposal_id" if state_kind == "proposal" else "action_id"
                )
                state = conn.execute(
                    render_sql(
                        "SELECT payload_json, payload_hash FROM {table} "
                        "WHERE {id_column}=?",
                        identifiers={"table": state_table, "id_column": id_column},
                    ),
                    (str(receipt["state_id"]),),
                ).fetchone()
                if state is None:
                    return False
                state_payload = json.loads(str(state["payload_json"]))
                state_hash = sha256_json(state_payload)
                decision_refs = self._entity_refs_from_json(
                    str(receipt["decision_trace_refs_json"])
                )
                action_refs = self._entity_refs_from_json(
                    str(receipt["action_refs_json"])
                )
                if payload.get("schema_version") == (
                    "mnemos.feedback_target_command.v1"
                ):
                    expected_payload = self._proposal_payload(
                        self._validate_apply_command(payload)
                    )
                    expected_kind = "proposal"
                    stored_payload = dict(state_payload)
                    stored_payload.pop("trusted_gate", None)
                    exact_material_proof = self._verify_material_proposal_proof(
                        receipt=receipt,
                        proposal=state_payload,
                    )
                elif payload.get("schema_version") == (
                    "mnemos.feedback_neutralization_command.v1"
                ):
                    validated = self._validate_neutralization_command(payload)
                    prior_receipt_id = self._receipt_id_from_ref(
                        str(validated["prior_target_receipt_ref"])
                    )
                    prior = conn.execute(
                        render_sql(
                            "SELECT * FROM {table} WHERE receipt_id=?",
                            identifiers={"table": self.receipt_table},
                        ),
                        (prior_receipt_id,),
                    ).fetchone()
                    if prior is None:
                        return False
                    expected_payload = self._action_payload(validated, prior)
                    stored_payload = state_payload
                    expected_kind = "action"
                    exact_material_proof = True
                else:
                    return False
        except (OSError, sqlite3.Error, ValueError, KeyError, json.JSONDecodeError):
            return False
        return bool(
            state_kind == expected_kind
            and state_hash == str(state["payload_hash"])
            and state_hash == str(receipt["state_payload_hash"])
            and str(receipt["schema_version"])
            == DOMAIN_RECEIPT_SCHEMA_VERSION
            and str(receipt["command_key"])
            == str(payload.get("command_key") or "")
            and str(receipt["target_id"]) == self.target_id
            and str(receipt["owner_id"]) == self.owner_id
            and str(receipt["target_effect_id"])
            == effect.target_effect_id
            and str(receipt["disposition"]) == effect.disposition
            and str(receipt["before_hash"]) == effect.before_hash
            and str(receipt["after_hash"]) == effect.after_hash
            and state_hash == effect.after_hash
            and stored_payload == expected_payload
            and decision_refs == effect.decision_trace_refs
            and action_refs == effect.action_refs
            and exact_material_proof
        )

    def _verify_material_proposal_proof(
        self,
        *,
        receipt: sqlite3.Row,
        proposal: Mapping[str, Any],
    ) -> bool:
        """Verify trusted-gate and DecisionTrace rows without importing the owner."""

        trusted = proposal.get("trusted_gate")
        if not isinstance(trusted, Mapping) or set(trusted) != {
            "decision",
            "risk_level",
            "reasons",
            "missing_info",
            "candidate_payload_hash",
        }:
            return False
        base_proposal = dict(proposal)
        base_proposal.pop("trusted_gate", None)
        if (
            trusted.get("decision")
            not in {"allow_pending_user_decision", "needs_manual_review"}
            or not str(trusted.get("risk_level") or "")
            or not isinstance(trusted.get("reasons"), list)
            or not isinstance(trusted.get("missing_info"), list)
            or trusted.get("candidate_payload_hash")
            != sha256_json(base_proposal).removeprefix("sha256:")
        ):
            return False
        try:
            decision_refs = self._entity_refs_from_json(
                str(receipt["decision_trace_refs_json"])
            )
            action_refs = self._entity_refs_from_json(
                str(receipt["action_refs_json"])
            )
            material_command_id = str(receipt["material_command_id"])
            if (
                not material_command_id
                or len(decision_refs) != 1
                or len(action_refs) != 1
            ):
                return False
            state_db = self.database_dir / "producer_consumer_ledger.db"
            if not state_db.is_file():
                return False
            with sqlite3.connect(
                f"file:{state_db.resolve(strict=True)}?mode=ro",
                uri=True,
            ) as conn:
                conn.row_factory = sqlite3.Row
                command = conn.execute(
                    "SELECT command_type, payload_json FROM cognitive_state_outbox "
                    "WHERE command_id=?",
                    (material_command_id,),
                ).fetchone()
                decision = conn.execute(
                    "SELECT object_id, object_type, payload_hash, payload_json "
                    "FROM cognitive_state_revisions WHERE revision_id=?",
                    (decision_refs[0].revision_id,),
                ).fetchone()
                terminal = conn.execute(
                    "SELECT status, target_effect_id, before_hash, after_hash "
                    "FROM cognitive_state_effect_receipts WHERE command_id=?",
                    (material_command_id,),
                ).fetchone()
            if command is None or decision is None or terminal is None:
                return False
            command_payload = json.loads(str(command["payload_json"]))
            decision_payload = json.loads(str(decision["payload_json"]))
            action_specs = [
                dict(item)
                for item in decision_payload.get("action_specs") or ()
                if isinstance(item, Mapping)
                and item.get("action_id") == action_refs[0].id
            ]
        except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
            return False
        return bool(
            str(command["command_type"]) == "execute_material_action"
            and command_payload.get("decision_revision_id")
            == decision_refs[0].revision_id
            and command_payload.get("action_id") == action_refs[0].id
            and command_payload.get("effect_id") == str(receipt["target_effect_id"])
            and str(decision["object_type"]) == "decision_trace"
            and str(decision["object_id"]) == decision_refs[0].id
            and str(decision["payload_hash"]) == decision_refs[0].content_hash
            and action_refs[0].revision_id == decision_refs[0].revision_id
            and len(action_specs) == 1
            and sha256_json(action_specs[0]) == action_refs[0].content_hash
            and str(terminal["status"]) == "committed"
            and str(terminal["target_effect_id"])
            == str(receipt["target_effect_id"])
            and str(terminal["before_hash"]) == str(receipt["before_hash"])
            and str(terminal["after_hash"]) == str(receipt["after_hash"])
        )

    def recover_command_effect(
        self,
        command: Mapping[str, Any],
    ) -> FeedbackTargetEffect | None:
        """Best-effort recovery used after an adapter call was interrupted."""

        try:
            return self.inspect_command_effect(command)
        except (OSError, sqlite3.Error, RuntimeError, ValueError):
            return None

    def inspect_command_effect(
        self,
        command: Mapping[str, Any],
    ) -> FeedbackTargetEffect | None:
        """Inspect a command receipt, distinguishing absence from oracle failure.

        Absence is deliberately not treated as proof that the target domain is
        unchanged after an adapter ran.  Structural-failure closure uses this
        stricter API only to ensure no exact receipt already contradicts the
        claimed no-effect state.
        """

        payload = dict(command)
        command_key = str(payload.get("command_key") or "")
        if not command_key:
            raise ValueError("feedback command key is unavailable for inspection")
        if not self.db_path.is_file():
            return None
        with self._connect(read_only=True) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (self.receipt_table,),
            ).fetchone()
            if table is None:
                return None
            receipt = conn.execute(
                render_sql(
                    "SELECT * FROM {table} WHERE command_key=?",
                    identifiers={"table": self.receipt_table},
                ),
                (command_key,),
            ).fetchone()
        if receipt is None:
            return None
        effect = FeedbackTargetEffect(
            target_id=str(receipt["target_id"]),
            target_effect_id=str(receipt["target_effect_id"]),
            disposition=str(receipt["disposition"]),
            before_hash=str(receipt["before_hash"]),
            after_hash=str(receipt["after_hash"]),
            target_receipt_ref=(
                f"domain-feedback-receipt:{self.target_id}:"
                + str(receipt["receipt_id"])
            ),
            decision_trace_refs=self._entity_refs_from_json(
                str(receipt["decision_trace_refs_json"])
            ),
            action_refs=self._entity_refs_from_json(
                str(receipt["action_refs_json"])
            ),
        )
        if not self.verify_command_effect(payload, effect):
            raise RuntimeError(
                "domain feedback command receipt is not independently valid"
            )
        return effect

    def _proposal_payload(self, command: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": DOMAIN_PROPOSAL_SCHEMA_VERSION,
            "target_id": self.target_id,
            "owner_id": self.owner_id,
            "gate_contract_id": self.gate_contract_id,
            "command_key": str(command["command_key"]),
            "attribution_revision_id": str(command["attribution_revision_id"]),
            "attribution_payload_hash": str(command["attribution_payload_hash"]),
            "input_set_hash": str(command["input_set_hash"]),
            "status": "pending_review",
            "direct_domain_update": False,
            "training_admitted": False,
        }

    def _action_payload(
        self,
        command: Mapping[str, Any],
        prior: sqlite3.Row,
    ) -> dict[str, Any]:
        return {
            "schema_version": DOMAIN_ACTION_SCHEMA_VERSION,
            "target_id": self.target_id,
            "owner_id": self.owner_id,
            "gate_contract_id": self.gate_contract_id,
            "command_key": str(command["command_key"]),
            "attribution_revision_id": str(command["attribution_revision_id"]),
            "attribution_payload_hash": str(command["attribution_payload_hash"]),
            "prior_domain_receipt_id": str(prior["receipt_id"]),
            "prior_state_id": str(prior["state_id"]),
            "prior_after_hash": str(command["prior_after_hash"]),
            "neutralization_kind": str(command["neutralization_kind"]),
            "status": "committed",
            "direct_domain_update": False,
            "training_admitted": False,
        }

    def _insert_proposal(
        self,
        conn: sqlite3.Connection,
        proposal_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        self._insert_state(
            conn,
            table=self.proposal_table,
            id_column="proposal_id",
            state_id=proposal_id,
            payload=payload,
        )

    def _insert_action(
        self,
        conn: sqlite3.Connection,
        action_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        self._insert_state(
            conn,
            table=self.action_table,
            id_column="action_id",
            state_id=action_id,
            payload=payload,
        )

    def _insert_state(
        self,
        conn: sqlite3.Connection,
        *,
        table: str,
        id_column: str,
        state_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_hash = sha256_json(payload)
        conn.execute(
            render_sql(
                "INSERT OR IGNORE INTO {table} "
                "({id_column}, command_key, payload_hash, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                identifiers={"table": table, "id_column": id_column},
            ),
            (
                state_id,
                str(payload["command_key"]),
                payload_hash,
                payload_json,
                self._now(),
            ),
        )
        stored = conn.execute(
            render_sql(
                "SELECT command_key, payload_hash, payload_json FROM {table} "
                "WHERE {id_column}=?",
                identifiers={"table": table, "id_column": id_column},
            ),
            (state_id,),
        ).fetchone()
        if stored is None or tuple(str(value) for value in stored) != (
            str(payload["command_key"]),
            payload_hash,
            payload_json,
        ):
            raise RuntimeError("immutable domain feedback proposal state conflict")

    def _insert_receipt(
        self,
        conn: sqlite3.Connection,
        *,
        command_key: str,
        state_kind: str,
        state_id: str,
        disposition: str,
        before_hash: str,
        after_hash: str,
        state_payload_hash: str,
        target_effect_id: str = "",
        material_command_id: str = "",
        decision_trace_refs: tuple[CognitiveEntityReference, ...] = (),
        action_refs: tuple[CognitiveEntityReference, ...] = (),
    ) -> FeedbackTargetEffect:
        identity = {
            "target_id": self.target_id,
            "owner_id": self.owner_id,
            "command_key": command_key,
            "state_kind": state_kind,
            "state_id": state_id,
            "after_hash": after_hash,
            "material_command_id": material_command_id,
        }
        suffix = sha256_json(identity).split(":", 1)[1][:32]
        receipt_id = "domain-feedback-receipt-" + suffix
        exact_target_effect_id = (
            str(target_effect_id).strip() or "domain-feedback-effect-" + suffix
        )
        decision_refs_json = self._entity_refs_json(decision_trace_refs)
        action_refs_json = self._entity_refs_json(action_refs)
        row = (
            receipt_id,
            DOMAIN_RECEIPT_SCHEMA_VERSION,
            command_key,
            self.target_id,
            self.owner_id,
            state_kind,
            state_id,
            exact_target_effect_id,
            disposition,
            before_hash,
            after_hash,
            state_payload_hash,
            str(material_command_id),
            decision_refs_json,
            action_refs_json,
            self._now(),
        )
        conn.execute(
            render_sql(
                "INSERT OR IGNORE INTO {table} ("
                "receipt_id, schema_version, command_key, target_id, owner_id, "
                "state_kind, state_id, target_effect_id, disposition, before_hash, "
                "after_hash, state_payload_hash, material_command_id, "
                "decision_trace_refs_json, action_refs_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                identifiers={"table": self.receipt_table},
            ),
            row,
        )
        stored = conn.execute(
            render_sql(
                "SELECT * FROM {table} WHERE command_key=?",
                identifiers={"table": self.receipt_table},
            ),
            (command_key,),
        ).fetchone()
        if stored is None or tuple(str(stored[key]) for key in (
            "receipt_id",
            "schema_version",
            "command_key",
            "target_id",
            "owner_id",
            "state_kind",
            "state_id",
            "target_effect_id",
            "disposition",
            "before_hash",
            "after_hash",
            "state_payload_hash",
            "material_command_id",
            "decision_trace_refs_json",
            "action_refs_json",
        )) != tuple(str(value) for value in row[:-1]):
            raise RuntimeError("immutable domain feedback proposal receipt conflict")
        return FeedbackTargetEffect(
            target_id=self.target_id,
            target_effect_id=exact_target_effect_id,
            disposition=disposition,
            before_hash=before_hash,
            after_hash=after_hash,
            target_receipt_ref=(
                f"domain-feedback-receipt:{self.target_id}:{receipt_id}"
            ),
            decision_trace_refs=decision_trace_refs,
            action_refs=action_refs,
        )

    @staticmethod
    def _entity_refs_json(
        references: tuple[CognitiveEntityReference, ...],
    ) -> str:
        return json.dumps(
            [reference.to_dict() for reference in references],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _entity_refs_from_json(value: str) -> tuple[CognitiveEntityReference, ...]:
        payload = json.loads(str(value or "[]"))
        if not isinstance(payload, list):
            raise ValueError("domain feedback cognitive refs are invalid")
        references = []
        for item in payload:
            if not isinstance(item, Mapping) or set(item) != {
                "id",
                "revision_id",
                "content_hash",
            }:
                raise ValueError("domain feedback cognitive refs are invalid")
            reference = CognitiveEntityReference(
                id=str(item["id"]),
                revision_id=str(item["revision_id"]),
                content_hash=str(item["content_hash"]),
            )
            if (
                not reference.id
                or not reference.revision_id
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", reference.content_hash)
            ):
                raise ValueError("domain feedback cognitive refs are incomplete")
            references.append(reference)
        return tuple(references)

    def _validate_apply_command(self, command: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(command)
        if (
            payload.get("schema_version") != "mnemos.feedback_target_command.v1"
            or payload.get("target_id") != self.target_id
            or payload.get("eligible") is not True
            or payload.get("effect_kind") != "proposal"
            or payload.get("exclusion_reason")
            or not str(payload.get("command_key") or "")
        ):
            raise ValueError("domain feedback proposal command mismatch")
        return payload

    def _validate_neutralization_command(
        self,
        command: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = dict(command)
        if (
            payload.get("schema_version")
            != "mnemos.feedback_neutralization_command.v1"
            or payload.get("target_id") != self.target_id
            or payload.get("neutralization_kind")
            not in {"suppress", "revoke", "compensate"}
            or not str(payload.get("prior_target_receipt_ref") or "")
            or not str(payload.get("prior_after_hash") or "").startswith("sha256:")
        ):
            raise ValueError("domain feedback neutralization command mismatch")
        return payload

    def _receipt_id_from_ref(self, value: str) -> str:
        prefix = f"domain-feedback-receipt:{self.target_id}:"
        if not str(value).startswith(prefix):
            raise ValueError("domain feedback receipt reference mismatch")
        receipt_id = str(value)[len(prefix) :]
        if not receipt_id:
            raise ValueError("domain feedback receipt reference is empty")
        return receipt_id

    def _identity(self, kind: str, payload: Mapping[str, Any]) -> str:
        suffix = sha256_json(payload).split(":", 1)[1][:32]
        return f"{self.target_id}-{kind}-{suffix}"

    def _ensure_schema(self) -> None:
        if self.db_path.is_file():
            current = self.db_path.stat()
            if self._schema_inode == (current.st_dev, current.st_ino):
                return
        self.database_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            for table, id_column in (
                (self.proposal_table, "proposal_id"),
                (self.action_table, "action_id"),
            ):
                conn.execute(
                    render_sql(
                        "CREATE TABLE IF NOT EXISTS {table} ("
                        "{id_column} TEXT PRIMARY KEY, "
                        "command_key TEXT NOT NULL UNIQUE, "
                        "payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL, "
                        "created_at TEXT NOT NULL)",
                        identifiers={"table": table, "id_column": id_column},
                    )
                )
                conn.execute(
                    render_sql(
                        "CREATE TRIGGER IF NOT EXISTS {trigger} BEFORE UPDATE "
                        "ON {table} BEGIN SELECT RAISE(ABORT, "
                        "'domain feedback state is immutable'); END",
                        identifiers={
                            "trigger": table + "_no_update",
                            "table": table,
                        },
                    )
                )
                conn.execute(
                    render_sql(
                        "CREATE TRIGGER IF NOT EXISTS {trigger} BEFORE DELETE "
                        "ON {table} BEGIN SELECT RAISE(ABORT, "
                        "'domain feedback state is immutable'); END",
                        identifiers={
                            "trigger": table + "_no_delete",
                            "table": table,
                        },
                    )
                )
            conn.execute(
                render_sql(
                    "CREATE TABLE IF NOT EXISTS {table} ("
                    "receipt_id TEXT PRIMARY KEY, schema_version TEXT NOT NULL, "
                    "command_key TEXT NOT NULL UNIQUE, target_id TEXT NOT NULL, "
                    "owner_id TEXT NOT NULL, state_kind TEXT NOT NULL, "
                    "state_id TEXT NOT NULL, target_effect_id TEXT NOT NULL UNIQUE, "
                    "disposition TEXT NOT NULL, before_hash TEXT NOT NULL, "
                    "after_hash TEXT NOT NULL, state_payload_hash TEXT NOT NULL, "
                    "material_command_id TEXT NOT NULL, "
                    "decision_trace_refs_json TEXT NOT NULL, "
                    "action_refs_json TEXT NOT NULL, "
                    "created_at TEXT NOT NULL)",
                    identifiers={"table": self.receipt_table},
                )
            )
            conn.execute(
                render_sql(
                    "CREATE TRIGGER IF NOT EXISTS {trigger} "
                    "BEFORE UPDATE ON {table} BEGIN SELECT "
                    "RAISE(ABORT, 'domain feedback receipt is immutable'); END",
                    identifiers={
                        "trigger": self.receipt_table + "_no_update",
                        "table": self.receipt_table,
                    },
                )
            )
            conn.execute(
                render_sql(
                    "CREATE TRIGGER IF NOT EXISTS {trigger} "
                    "BEFORE DELETE ON {table} BEGIN SELECT "
                    "RAISE(ABORT, 'domain feedback receipt is immutable'); END",
                    identifiers={
                        "trigger": self.receipt_table + "_no_delete",
                        "table": self.receipt_table,
                    },
                )
            )
        current = self.db_path.stat()
        self._schema_inode = (current.st_dev, current.st_ino)

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            conn = sqlite3.connect(
                f"file:{self.db_path.resolve(strict=True)}?mode=ro",
                uri=True,
            )
        else:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
