"""Append-only journal for formal non-Markdown cognitive mutations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    require_material_action_projection,
)
from core.cognitive.state_contract import sha256_json as cognitive_sha256_json
from core.trust.models import sha256_json


def formal_cognitive_mutation_input_hash(
    *,
    asset_kind: str,
    action: str,
    target_ref: str,
    actor: str,
    reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> str:
    """Hash the exact non-Markdown mutation input before runtime receipts exist."""

    return cognitive_sha256_json(
        {
            "schema_version": "mnemos.formal_cognitive_mutation_input.v1",
            "asset_kind": str(asset_kind),
            "action": str(action),
            "target_ref": str(target_ref),
            "actor": str(actor),
            "reason": str(reason or ""),
            "metadata": dict(metadata or {}),
        }
    )


class FormalCognitiveMutationJournal:
    """Record auditable mutations for KG, cognitive graph, persona, and recaps."""

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS formal_cognitive_mutations (
            event_id TEXT PRIMARY KEY,
            asset_kind TEXT NOT NULL,
            action TEXT NOT NULL,
            target_ref TEXT NOT NULL,
            actor TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT DEFAULT '',
            evidence_refs TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_formal_mutation_asset
            ON formal_cognitive_mutations(asset_kind);
        CREATE INDEX IF NOT EXISTS idx_formal_mutation_target
            ON formal_cognitive_mutations(target_ref);
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @classmethod
    def for_database(cls, database_path: str | Path) -> "FormalCognitiveMutationJournal":
        path = Path(database_path).expanduser()
        if path.name == "trusted_push.db":
            return cls(path)
        return cls(path.parent / "trusted_push.db")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(self.SCHEMA)

    def record(
        self,
        *,
        asset_kind: str,
        action: str,
        target_ref: str,
        actor: str,
        decision: str,
        reason: str = "",
        evidence_refs: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
        material_action: MaterialActionAuthorization | None = None,
    ) -> dict[str, Any]:
        refs = [str(ref) for ref in evidence_refs if str(ref)]
        metadata_input = dict(metadata or {})
        if not isinstance(material_action, MaterialActionAuthorization):
            raise PermissionError(
                "canonical material-action authorization is required"
            )
        permit = material_action.permit
        input_hash = formal_cognitive_mutation_input_hash(
            asset_kind=asset_kind,
            action=action,
            target_ref=target_ref,
            actor=actor,
            reason=reason,
            metadata=metadata_input,
        )
        require_material_action_projection(
            material_action,
            owner=permit.owner,
            executor_id=permit.executor_id,
            action_type=action,
            target_ref=target_ref,
            input_hash=input_hash,
            expected_state_db=self.db_path.parent / "producer_consumer_ledger.db",
        )
        if decision != permit.decision_revision_id:
            raise PermissionError(
                "formal mutation decision does not match its material permit"
            )
        required_refs = {
            f"material-command:{permit.command_id}",
            f"decision-revision:{permit.decision_revision_id}",
            f"material-effect:{permit.effect_id}",
        }
        if not required_refs.issubset(refs):
            raise PermissionError(
                "formal mutation lacks reciprocal material evidence"
            )
        metadata_payload = {
            **metadata_input,
            "material_action": {
                "command_id": permit.command_id,
                "decision_revision_id": permit.decision_revision_id,
                "action_id": permit.action_id,
                "effect_id": permit.effect_id,
                "action_type": permit.action_type,
                "owner": permit.owner,
                "executor_id": permit.executor_id,
                "target_ref": permit.target_ref,
                "input_hash": input_hash,
            },
        }
        payload = {
            "asset_kind": asset_kind,
            "action": action,
            "target_ref": target_ref,
            "actor": actor,
            "decision": decision,
            "reason": reason,
            "evidence_refs": refs,
            "metadata": metadata_payload,
        }
        event = {
            "event_id": "fcm_"
            + sha256_json(
                {
                    "command_id": permit.command_id,
                    "asset_kind": asset_kind,
                    "action": action,
                    "target_ref": target_ref,
                }
            )[:32],
            **payload,
            "content_hash": sha256_json(payload),
            "created_at": permit.issued_at,
        }
        values = (
            event["event_id"],
            asset_kind,
            action,
            target_ref,
            actor,
            decision,
            reason,
            json.dumps(refs, ensure_ascii=False),
            json.dumps(metadata_payload, ensure_ascii=False, sort_keys=True),
            event["content_hash"],
            event["created_at"],
        )
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO formal_cognitive_mutations (
                        event_id, asset_kind, action, target_ref, actor, decision,
                        reason, evidence_refs, metadata_json, content_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            except sqlite3.IntegrityError:
                existing = conn.execute(
                    "SELECT * FROM formal_cognitive_mutations WHERE event_id=?",
                    (event["event_id"],),
                ).fetchone()
                if existing is None or tuple(existing) != values:
                    raise ValueError(
                        "immutable formal cognitive mutation conflict"
                    ) from None
                return self._row_to_dict(existing)
        return event

    def list_events(
        self,
        *,
        asset_kind: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM formal_cognitive_mutations"
        params: tuple[Any, ...]
        if asset_kind:
            query += " WHERE asset_kind = ?"
            params = (asset_kind, int(limit))
        else:
            params = (int(limit),)
        query += " ORDER BY created_at ASC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["evidence_refs"] = json.loads(data.get("evidence_refs") or "[]")
        data["metadata"] = json.loads(data.get("metadata_json") or "{}")
        data.pop("metadata_json", None)
        return data
