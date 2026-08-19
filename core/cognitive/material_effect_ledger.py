"""Target-local effect journals for crash-safe material-action closure."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Iterable

from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionCoordinator,
    MaterialActionObservation,
    MaterialActionPermit,
    MaterialActionReceipt,
    validate_material_receipt_observation,
)
from core.cognitive.material_effect_schema import (
    ROW_SCHEMA_VERSION,
    TABLE_NAME,
    validate_material_effect_schema,
)
from core.cognitive.state_store import CognitiveStateStore


TARGET_EFFECT_LEDGER_SCHEMA = ROW_SCHEMA_VERSION


def record_target_effect(
    conn: sqlite3.Connection,
    permit: MaterialActionPermit,
    *,
    status: str,
    before_hash: str,
    after_hash: str,
    evidence_refs: Iterable[str],
    observed_at: str,
    reason_code: str = "",
    retry_exhausted: bool = False,
    outcome: str = "",
) -> None:
    """Journal an effect in the same transaction as its canonical target row."""

    validate_material_effect_schema(conn)
    refs = tuple(dict.fromkeys(str(value) for value in evidence_refs if str(value)))
    values = (
        permit.command_id,
        permit.effect_id,
        permit.decision_revision_id,
        permit.action_id,
        permit.owner,
        permit.executor_id,
        permit.action_type,
        permit.target_ref,
        permit.input_hash,
        status,
        before_hash,
        after_hash,
        json.dumps(refs, ensure_ascii=False),
        reason_code,
        int(retry_exhausted),
        outcome,
        observed_at,
        TARGET_EFFECT_LEDGER_SCHEMA,
    )
    try:
        conn.execute(
            """
            INSERT INTO material_target_effects (
                command_id, effect_id, decision_revision_id, action_id,
                owner, executor_id, action_type, target_ref, input_hash,
                status, before_hash, after_hash, evidence_refs_json,
                reason_code, retry_exhausted, outcome, observed_at,
                schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
    except sqlite3.IntegrityError:
        existing = conn.execute(
            "SELECT * FROM material_target_effects WHERE command_id=?",
            (permit.command_id,),
        ).fetchone()
        if existing is None:
            raise
        columns = tuple(
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(material_target_effects)"
            ).fetchall()
        )
        if tuple(dict(zip(columns, tuple(existing))).values()) != values:
            raise RuntimeError(
                "material target effect journal conflicts with its command"
            ) from None


class SqliteTargetEffectOracle:
    """Base for sink-owned, fixed-family read-only SQLite effect oracles."""

    owner = ""
    executor_id = ""
    action_type = ""

    def __init__(self, db_path: Path):
        if not self.owner or not self.executor_id or not self.action_type:
            raise TypeError("target effect oracle must declare a fixed sink family")
        self.db_path = Path(db_path)

    def observe(
        self,
        permit: MaterialActionPermit,
    ) -> MaterialActionObservation | None:
        """Return the exact durable target effect for a matching permit."""

        if not self.db_path.is_file():
            return None
        with sqlite3.connect(
            f"file:{self.db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            conn.row_factory = sqlite3.Row
            table = conn.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name=?""",
                (TABLE_NAME,),
            ).fetchone()
            if table is None:
                return None
            validate_material_effect_schema(conn)
            row = conn.execute(
                "SELECT * FROM material_target_effects WHERE command_id=?",
                (permit.command_id,),
            ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        expected = {
            "command_id": permit.command_id,
            "effect_id": permit.effect_id,
            "decision_revision_id": permit.decision_revision_id,
            "action_id": permit.action_id,
            "owner": permit.owner,
            "executor_id": permit.executor_id,
            "action_type": permit.action_type,
            "target_ref": permit.target_ref,
            "input_hash": permit.input_hash,
            "schema_version": TARGET_EFFECT_LEDGER_SCHEMA,
        }
        if any(str(payload.get(key) or "") != value for key, value in expected.items()):
            raise RuntimeError(
                "target effect journal row does not match its material command"
            )
        try:
            refs = json.loads(str(payload.get("evidence_refs_json") or "[]"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("target effect journal evidence is invalid") from exc
        if not isinstance(refs, list) or not refs:
            raise RuntimeError("target effect journal evidence is missing")
        return MaterialActionObservation(
            status=str(payload["status"]),
            before_hash=str(payload["before_hash"]),
            after_hash=str(payload["after_hash"]),
            evidence_refs=tuple(str(value) for value in refs),
            reason_code=str(payload.get("reason_code") or ""),
            retry_exhausted=bool(payload.get("retry_exhausted")),
            outcome=str(payload.get("outcome") or ""),
            observed_at=str(payload.get("observed_at") or ""),
        )


def recover_recorded_target_effect(
    authorization: MaterialActionAuthorization,
    oracle: SqliteTargetEffectOracle,
) -> bool:
    """Close a journaled command and report whether execution must be skipped."""

    receipt = authorization.recover(oracle)
    if receipt is None:
        return False
    observation = oracle.observe(authorization.permit)
    if observation is None:
        raise RuntimeError(
            "terminal material command lacks its exact target effect journal"
        )
    _validate_receipt_observation(receipt, observation)
    return True


def _validate_receipt_observation(
    receipt: MaterialActionReceipt,
    observation: MaterialActionObservation,
) -> None:
    """Require reciprocal target-journal fields to equal the terminal receipt."""

    if receipt.created_at != observation.observed_at:
        raise RuntimeError(
            "target effect journal does not match its terminal receipt"
        )
    validate_material_receipt_observation(receipt, observation)


def recover_pending_target_effects(
    *,
    state_db_path: Path,
    oracle: SqliteTargetEffectOracle,
    target_ref: str = "",
) -> tuple[str, ...]:
    """Close every journaled command in one fixed target family, read-only."""

    if not oracle.db_path.is_file() or not Path(state_db_path).is_file():
        return ()
    with sqlite3.connect(
        f"file:{oracle.db_path.resolve(strict=True)}?mode=ro",
        uri=True,
    ) as conn:
        table = conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name=?""",
            (TABLE_NAME,),
        ).fetchone()
        if table is None:
            return ()
        validate_material_effect_schema(conn)
        query = (
            "SELECT command_id FROM material_target_effects "
            "WHERE owner=? AND executor_id=? AND action_type=?"
        )
        params: tuple[str, ...] = (
            oracle.owner,
            oracle.executor_id,
            oracle.action_type,
        )
        if target_ref:
            query += " AND target_ref=?"
            params = (*params, str(target_ref))
        query += " ORDER BY observed_at, command_id"
        command_ids = tuple(
            str(row[0]) for row in conn.execute(query, params).fetchall()
        )
    coordinator = MaterialActionCoordinator(CognitiveStateStore(state_db_path))
    with sqlite3.connect(
        f"file:{Path(state_db_path).resolve(strict=True)}?mode=ro",
        uri=True,
    ) as conn:
        terminal_commands = {
            str(row[0])
            for row in conn.execute(
                """SELECT command_id FROM cognitive_state_effect_receipts
                   WHERE command_id IN (
                       SELECT command_id FROM cognitive_state_outbox
                   )"""
            ).fetchall()
        }
    recovered: list[str] = []
    for command_id in command_ids:
        receipt = coordinator.recover(
            command_id,
            executor_id=oracle.executor_id,
            oracle=oracle,
        )
        if receipt is not None and command_id not in terminal_commands:
            recovered.append(command_id)
    return tuple(recovered)


def recorded_target_effect_command_ids(
    oracle: SqliteTargetEffectOracle,
    *,
    target_ref: str = "",
    input_hash: str = "",
) -> tuple[str, ...]:
    """List exact durable command identities without mutating either store."""

    if not oracle.db_path.is_file():
        return ()
    with sqlite3.connect(
        f"file:{oracle.db_path.resolve(strict=True)}?mode=ro",
        uri=True,
    ) as conn:
        table = conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name=?""",
            (TABLE_NAME,),
        ).fetchone()
        if table is None:
            return ()
        validate_material_effect_schema(conn)
        query = (
            "SELECT command_id FROM material_target_effects "
            "WHERE owner=? AND executor_id=? AND action_type=?"
        )
        params: tuple[str, ...] = (
            oracle.owner,
            oracle.executor_id,
            oracle.action_type,
        )
        if target_ref:
            query += " AND target_ref=?"
            params = (*params, str(target_ref))
        if input_hash:
            query += " AND input_hash=?"
            params = (*params, str(input_hash))
        query += " ORDER BY observed_at, command_id"
        return tuple(
            str(row[0]) for row in conn.execute(query, params).fetchall()
        )
