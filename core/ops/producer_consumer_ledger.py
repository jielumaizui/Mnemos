"""Runtime producer/consumer closure ledger for adaptive data flows."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core.cognitive.state_schema import (
    RUNTIME_LEDGER_SCHEMA_VERSION,
    initialize_cognitive_state_schema,
    validate_cognitive_state_schema,
)
from core.ops.cognitive_data_contract import CognitiveDataEvent
from core.ops.durable_io import inspect_path_kind
from core.ops.readiness_query_budget import connect_readonly_sqlite
from core.ops.durable_io import read_native_bytes
from core.ops.cognitive_event_ledger import (
    cognitive_data_snapshot_in_connection,
    insert_data_consumption_in_connection,
    insert_data_event_in_connection,
    insert_reconciliation_in_connection,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = ROOT / "docs" / "acceptance" / "adaptive_data_flows.json"
SCHEMA_VERSION = RUNTIME_LEDGER_SCHEMA_VERSION
DEFAULT_LEDGER_NAME = "producer_consumer_ledger.db"
DEFAULT_MAX_LAG_SECONDS = 24 * 60 * 60
TERMINAL_RECEIPT_STATUSES = frozenset({"consumed", "dead_letter", "skipped"})
STAGE_RECEIPT_STATUSES = frozenset({"in_progress"})
ALLOWED_RECEIPT_STATUSES = TERMINAL_RECEIPT_STATUSES | STAGE_RECEIPT_STATUSES
_PROCESS_GENERATION_ID = f"process-{os.getpid()}-{uuid.uuid4().hex}"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _config_database_dir(config_or_path: Any) -> Path:
    if isinstance(config_or_path, Path):
        return config_or_path
    database_dir = (
        config_or_path.get("database_dir")
        if isinstance(config_or_path, Mapping)
        else getattr(config_or_path, "database_dir", None)
    )
    if database_dir is None:
        raise ValueError("config.database_dir is required for producer/consumer ledger")
    return Path(database_dir)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _matrix_flows(matrix_path: Path | None) -> list[dict[str, Any]]:
    if matrix_path is None:
        return []
    matrix = json.loads(read_native_bytes(Path(matrix_path)).decode("utf-8"))
    flows = matrix.get("flows", [])
    if not isinstance(flows, list):
        return []
    runtime_audit = matrix.get("runtime_audit", {})
    flow_contracts = (
        runtime_audit.get("flow_contracts", {}) if isinstance(runtime_audit, Mapping) else {}
    )
    normalized: list[dict[str, Any]] = []
    for flow in flows:
        if not isinstance(flow, dict):
            continue
        flow_copy = dict(flow)
        flow_id = str(flow_copy.get("id") or "")
        contract = flow_contracts.get(flow_id, {}) if isinstance(flow_contracts, Mapping) else {}
        local_runtime = flow_copy.get("runtime_audit", {})
        flow_copy["runtime_audit"] = {
            **(dict(contract) if isinstance(contract, Mapping) else {}),
            **(dict(local_runtime) if isinstance(local_runtime, Mapping) else {}),
        }
        normalized.append(flow_copy)
    return normalized


def _runtime_options(flow: Mapping[str, Any]) -> dict[str, Any]:
    runtime = flow.get("runtime_audit", {})
    if not isinstance(runtime, Mapping):
        runtime = {}
    flow_id = str(flow.get("id") or "")
    return {
        "topic": str(runtime.get("topic") or flow_id),
        "pending_budget": int(runtime.get("pending_budget", 0) or 0),
        "dead_letter_budget": int(runtime.get("dead_letter_budget", 0) or 0),
        "max_lag_seconds": int(
            runtime.get("max_lag_seconds", DEFAULT_MAX_LAG_SECONDS) or DEFAULT_MAX_LAG_SECONDS
        ),
        "receipt_grace_seconds": int(runtime.get("receipt_grace_seconds", 0) or 0),
        "required": bool(runtime.get("required", True)),
        "min_observations": int(runtime.get("min_observations", 1) or 0),
        "observation_mode": str(runtime.get("observation_mode") or "continuous"),
        "not_applicable_reason": str(runtime.get("not_applicable_reason") or ""),
        "freshness_required": bool(runtime.get("freshness_required", True)),
    }


class ProducerConsumerLedger:
    """SQLite-backed ledger for pairing runtime production and consumption."""

    def __init__(
        self,
        config_or_path: Any,
        *,
        initialize: bool = False,
        read_only: bool = False,
    ):
        self.database_dir = _config_database_dir(config_or_path)
        self.db_path = self.database_dir / DEFAULT_LEDGER_NAME
        self.read_only = bool(read_only)
        if initialize:
            if self.read_only:
                raise ValueError("read-only ledger cannot initialize schema")
            initialize_cognitive_state_schema(self.db_path)
        else:
            database_kind = inspect_path_kind(self.db_path)
            if database_kind == "missing":
                raise FileNotFoundError(self.db_path)
            if database_kind != "file":
                raise ValueError(
                    "producer consumer ledger path is not a regular file"
                )
            with self._connect(validate=False) as conn:
                validate_cognitive_state_schema(conn)

    def _connect(self, *, validate: bool = False) -> sqlite3.Connection:
        if self.read_only:
            conn = connect_readonly_sqlite(self.db_path, timeout_seconds=5)
        else:
            conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.execute("PRAGMA foreign_keys = ON")
        if validate:
            validate_cognitive_state_schema(conn)
        return conn

    def register_flow(
        self,
        *,
        flow_id: str,
        data_type: str,
        producer_refs: list[str],
        consumer_refs: list[str],
        topic: str | None = None,
        pending_budget: int = 0,
        dead_letter_budget: int = 0,
        max_lag_seconds: int = DEFAULT_MAX_LAG_SECONDS,
        receipt_grace_seconds: int = 0,
        required: bool = True,
        min_observations: int = 1,
        observation_mode: str = "continuous",
        not_applicable_reason: str = "",
        freshness_required: bool = True,
    ) -> None:
        """Register one runtime flow and its reconciliation budgets."""
        if not flow_id:
            raise ValueError("flow_id is required")
        with self._connect() as conn:
            self._register_flow_in_connection(
                conn,
                flow_id=flow_id,
                data_type=data_type,
                producer_refs=producer_refs,
                consumer_refs=consumer_refs,
                topic=topic,
                pending_budget=pending_budget,
                dead_letter_budget=dead_letter_budget,
                max_lag_seconds=max_lag_seconds,
                receipt_grace_seconds=receipt_grace_seconds,
                required=required,
                min_observations=min_observations,
                observation_mode=observation_mode,
                not_applicable_reason=not_applicable_reason,
                freshness_required=freshness_required,
            )
            conn.commit()

    def _register_flow_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        flow_id: str,
        data_type: str,
        producer_refs: list[str],
        consumer_refs: list[str],
        topic: str | None = None,
        pending_budget: int = 0,
        dead_letter_budget: int = 0,
        max_lag_seconds: int = DEFAULT_MAX_LAG_SECONDS,
        receipt_grace_seconds: int = 0,
        required: bool = True,
        min_observations: int = 1,
        observation_mode: str = "continuous",
        not_applicable_reason: str = "",
        freshness_required: bool = True,
    ) -> None:
        if observation_mode not in {"continuous", "on_event", "not_applicable"}:
            raise ValueError(f"unsupported observation mode: {observation_mode}")
        if observation_mode == "not_applicable" and not not_applicable_reason.strip():
            raise ValueError("not_applicable flow requires a reason")
        if int(receipt_grace_seconds) < 0:
            raise ValueError("receipt_grace_seconds must be non-negative")
        now = _now_utc()
        conn.execute(
            """
            INSERT INTO runtime_flow_registry (
                flow_id, data_type, topic, producer_refs, consumer_refs,
                pending_budget, dead_letter_budget, max_lag_seconds,
                registered_at, updated_at, required, min_observations,
                observation_mode, not_applicable_reason, freshness_required,
                receipt_grace_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(flow_id) DO UPDATE SET
                data_type = excluded.data_type,
                topic = excluded.topic,
                producer_refs = excluded.producer_refs,
                consumer_refs = excluded.consumer_refs,
                pending_budget = excluded.pending_budget,
                dead_letter_budget = excluded.dead_letter_budget,
                max_lag_seconds = excluded.max_lag_seconds,
                updated_at = excluded.updated_at,
                required = excluded.required,
                min_observations = excluded.min_observations,
                observation_mode = excluded.observation_mode,
                not_applicable_reason = excluded.not_applicable_reason,
                freshness_required = excluded.freshness_required,
                receipt_grace_seconds = excluded.receipt_grace_seconds
            """,
            (
                flow_id,
                data_type,
                topic or flow_id,
                _json_dumps(list(producer_refs)),
                _json_dumps(list(consumer_refs)),
                int(pending_budget),
                int(dead_letter_budget),
                int(max_lag_seconds),
                now,
                now,
                1 if required else 0,
                max(0, int(min_observations)),
                observation_mode,
                not_applicable_reason,
                1 if freshness_required else 0,
                max(0, int(receipt_grace_seconds)),
            ),
        )

    def register_adaptive_flows(self, matrix_path: Path = DEFAULT_MATRIX) -> int:
        """Register adaptive-data-flow matrix rows as runtime topics."""
        count = 0
        for flow in _matrix_flows(matrix_path):
            flow_id = str(flow.get("id") or "")
            if not flow_id:
                continue
            options = _runtime_options(flow)
            producer = flow.get("producer", {})
            consumer = flow.get("consumer", {})
            producer_refs = producer.get("code", []) if isinstance(producer, Mapping) else []
            consumer_refs = consumer.get("code", []) if isinstance(consumer, Mapping) else []
            self.register_flow(
                flow_id=flow_id,
                data_type=str(flow.get("data_type") or ""),
                producer_refs=[str(item) for item in producer_refs],
                consumer_refs=[str(item) for item in consumer_refs],
                topic=options["topic"],
                pending_budget=options["pending_budget"],
                dead_letter_budget=options["dead_letter_budget"],
                max_lag_seconds=options["max_lag_seconds"],
                receipt_grace_seconds=options["receipt_grace_seconds"],
                required=bool(options["required"]),
                min_observations=int(options["min_observations"]),
                observation_mode=str(options["observation_mode"]),
                not_applicable_reason=str(options["not_applicable_reason"]),
                freshness_required=bool(options["freshness_required"]),
            )
            count += 1
        return count

    def migrate_v1_terminal_events(self) -> int:
        """Move v1 consumed/dead-letter rows into the append-only receipt table."""
        migrated = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("""
                SELECT event_id, flow_id, direction, source, item_id,
                       created_at, metadata, generation_id
                FROM runtime_flow_events
                WHERE direction IN ('consumed', 'dead_letter')
                ORDER BY created_at, event_id
                """).fetchall()
            for (
                legacy_event_id,
                flow_id,
                direction,
                source,
                item_id,
                created_at,
                metadata,
                generation_id,
            ) in rows:
                produced = conn.execute(
                    """
                    SELECT event_id
                    FROM runtime_flow_events
                    WHERE flow_id = ? AND direction = 'produced' AND item_id = ?
                    ORDER BY created_at DESC, event_id DESC
                    LIMIT 1
                    """,
                    (flow_id, item_id),
                ).fetchone()
                conn.execute(
                    """
                    INSERT OR IGNORE INTO runtime_flow_receipts (
                        receipt_id, production_event_id, flow_id, consumer_id,
                        status, item_id, generation_id, idempotency_key,
                        created_at, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        str(produced[0]) if produced else "",
                        str(flow_id),
                        str(source),
                        str(direction),
                        str(item_id),
                        str(generation_id or "legacy-unknown"),
                        f"legacy-runtime-event:{legacy_event_id}",
                        str(created_at),
                        str(metadata),
                    ),
                )
                migrated += int(conn.execute("SELECT changes()").fetchone()[0])
            if rows:
                conn.execute(
                    "DELETE FROM runtime_flow_events WHERE direction IN ('consumed', 'dead_letter')"
                )
            conn.commit()
        return migrated

    def record_produced(
        self,
        flow_id: str,
        *,
        source: str,
        item_id: str = "",
        topic: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        created_at: str | None = None,
        intended_consumers: list[str] | tuple[str, ...] | None = None,
        generation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Record that a producer emitted one runtime item."""
        return self._record_event(
            flow_id,
            direction="produced",
            source=source,
            item_id=item_id,
            topic=topic,
            metadata=metadata,
            created_at=created_at,
            intended_consumers=intended_consumers,
            generation_id=generation_id,
            idempotency_key=idempotency_key,
        )

    def record_consumed(
        self,
        flow_id: str,
        *,
        source: str,
        item_id: str = "",
        topic: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        created_at: str | None = None,
        production_event_id: str | None = None,
        generation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Record that a consumer consumed one runtime item."""
        return self._record_terminal_receipt(
            flow_id,
            status="consumed",
            consumer_id=source,
            item_id=item_id,
            metadata=metadata,
            created_at=created_at,
            production_event_id=production_event_id,
            generation_id=generation_id,
            idempotency_key=idempotency_key,
        )

    def record_stage(
        self,
        flow_id: str,
        *,
        source: str,
        item_id: str = "",
        topic: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        created_at: str | None = None,
        production_event_id: str | None = None,
        generation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Record a nonterminal stage event for one runtime item.

        A stage receipt proves that a consumer made progress (for example a
        value prejudgment) without closing the (production, consumer) pair.
        Only terminal receipts in ``TERMINAL_RECEIPT_STATUSES`` close a
        generation.
        """
        return self._record_stage_receipt(
            flow_id,
            status="in_progress",
            consumer_id=source,
            item_id=item_id,
            metadata=metadata,
            created_at=created_at,
            production_event_id=production_event_id,
            generation_id=generation_id,
            idempotency_key=idempotency_key,
        )

    def record_dead_letter(
        self,
        flow_id: str,
        *,
        source: str,
        item_id: str = "",
        topic: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        created_at: str | None = None,
        production_event_id: str | None = None,
        generation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Record that one runtime item entered a terminal dead-letter path."""
        return self._record_terminal_receipt(
            flow_id,
            status="dead_letter",
            consumer_id=source,
            item_id=item_id,
            metadata=metadata,
            created_at=created_at,
            production_event_id=production_event_id,
            generation_id=generation_id,
            idempotency_key=idempotency_key,
        )

    def record_skipped(
        self,
        flow_id: str,
        *,
        source: str,
        consumer_id: str | None = None,
        item_id: str = "",
        topic: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        created_at: str | None = None,
        production_event_id: str | None = None,
        generation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Record a reviewed terminal no-effect outcome for one runtime item."""

        return self._record_terminal_receipt(
            flow_id,
            status="skipped",
            consumer_id=consumer_id or source,
            item_id=item_id,
            metadata=metadata,
            created_at=created_at,
            production_event_id=production_event_id,
            generation_id=generation_id,
            idempotency_key=idempotency_key,
        )

    def record_data_event(
        self,
        event: CognitiveDataEvent,
        *,
        lifecycle_status: str = "produced",
    ) -> str:
        """Record one immutable transport envelope.

        Semantic object types are reserved for CognitiveStateUnitOfWork so
        a caller cannot create metadata-only cognition.
        """

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            event_id, _ = insert_data_event_in_connection(
                conn,
                event,
                lifecycle_status=lifecycle_status,
                allow_semantic=False,
            )
            conn.commit()
        return event_id

    def pending_productions(self, flow_id: str, consumer_id: str) -> list[dict[str, str]]:
        """Return produced events still missing one intended consumer terminal receipt."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, item_id, intended_consumers
                FROM runtime_flow_events
                WHERE flow_id = ? AND direction = 'produced'
                ORDER BY created_at, event_id
                """,
                (flow_id,),
            ).fetchall()
            terminal = {
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT production_event_id
                    FROM runtime_flow_receipts
                    WHERE flow_id = ? AND consumer_id = ?
                      AND status IN ('consumed', 'dead_letter', 'skipped')
                    """,
                    (flow_id, consumer_id),
                ).fetchall()
            }
        pending = []
        for event_id, item_id, intended_json in rows:
            intended = set(json.loads(str(intended_json) or "[]"))
            if consumer_id in intended and str(event_id) not in terminal:
                pending.append({"event_id": str(event_id), "item_id": str(item_id)})
        return pending

    def record_data_consumed(
        self,
        event_id: str,
        *,
        consumer_id: str,
        action_changed: bool = False,
        outcome: str = "",
        status: str = "consumed",
        metadata: Mapping[str, Any] | None = None,
        created_at: str | None = None,
        idempotency_key: str | None = None,
        target_effect_id: str = "",
        before_hash: str = "",
        after_hash: str = "",
        effect_evidence_refs: tuple[str, ...] | list[str] = (),
        supersedes_consumption_id: str = "",
        correction_of_consumption_id: str = "",
    ) -> str:
        """Append one terminal consumer-pair receipt.

        The aggregate event status is rebuilt from all required pair heads;
        this method never mutates the immutable event envelope.
        """

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            consumption_id, _ = insert_data_consumption_in_connection(
                conn,
                event_id,
                consumer_id=consumer_id,
                action_changed=action_changed,
                outcome=outcome,
                status=status,
                metadata=metadata,
                created_at=created_at,
                idempotency_key=idempotency_key,
                target_effect_id=target_effect_id,
                before_hash=before_hash,
                after_hash=after_hash,
                effect_evidence_refs=effect_evidence_refs,
                supersedes_consumption_id=supersedes_consumption_id,
                correction_of_consumption_id=correction_of_consumption_id,
            )
            conn.commit()
        return consumption_id

    def record_data_reconciliation(
        self,
        *,
        event_id: str,
        related_event_id: str,
        relation_type: str,
        dedupe_key: str,
        reason: str,
        source_revision_refs: tuple[str, ...] | list[str],
        proof_hash: str,
        proof_status: str = "verified",
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        """Record an explicit relation backed by source revision proof."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reconciliation_id, _ = insert_reconciliation_in_connection(
                conn,
                event_id=event_id,
                related_event_id=related_event_id,
                relation_type=relation_type,
                dedupe_key=dedupe_key,
                reason=reason,
                source_revision_refs=source_revision_refs,
                proof_hash=proof_hash,
                proof_status=proof_status,
                metadata=metadata,
            )
            conn.commit()
        return reconciliation_id

    def _record_event(
        self,
        flow_id: str,
        *,
        direction: str,
        source: str,
        item_id: str,
        topic: str | None,
        metadata: Mapping[str, Any] | None,
        created_at: str | None,
        intended_consumers: list[str] | tuple[str, ...] | None = None,
        generation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        if direction != "produced":
            raise ValueError(f"unsupported runtime flow direction: {direction}")
        event_id = uuid.uuid4().hex
        event_topic = topic or flow_id
        resolved_generation_id = generation_id or _PROCESS_GENERATION_ID
        resolved_idempotency_key = idempotency_key or (
            f"{flow_id}:{resolved_generation_id}:{item_id}:produced"
            if item_id
            else uuid.uuid4().hex
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_event = conn.execute(
                "SELECT event_id FROM runtime_flow_events WHERE idempotency_key = ?",
                (resolved_idempotency_key,),
            ).fetchone()
            if existing_event is not None:
                return str(existing_event[0])
            registered = conn.execute(
                "SELECT consumer_refs FROM runtime_flow_registry WHERE flow_id = ?",
                (flow_id,),
            ).fetchone()
            if registered is None:
                self._register_flow_in_connection(
                    conn,
                    flow_id=flow_id,
                    data_type="runtime observed",
                    producer_refs=[],
                    consumer_refs=[],
                    topic=event_topic,
                )
                resolved_consumers = list(intended_consumers or [])
            elif intended_consumers is None:
                resolved_consumers = [str(value) for value in json.loads(str(registered[0]))]
            else:
                resolved_consumers = [str(value) for value in intended_consumers]
            conn.execute(
                """
                INSERT INTO runtime_flow_events (
                    event_id, flow_id, direction, topic, source,
                    item_id, created_at, metadata, generation_id, intended_consumers
                    , idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    flow_id,
                    direction,
                    event_topic,
                    source,
                    item_id,
                    created_at or _now_utc(),
                    _json_dumps(dict(metadata or {})),
                    resolved_generation_id,
                    _json_dumps(sorted(set(resolved_consumers))),
                    resolved_idempotency_key,
                ),
            )
            conn.commit()
        return event_id

    def _record_stage_receipt(
        self,
        flow_id: str,
        *,
        status: str,
        consumer_id: str,
        item_id: str,
        metadata: Mapping[str, Any] | None,
        created_at: str | None,
        production_event_id: str | None,
        generation_id: str | None,
        idempotency_key: str | None,
    ) -> str:
        if status not in STAGE_RECEIPT_STATUSES:
            raise ValueError(f"unsupported stage receipt status: {status}")
        return self._record_receipt(
            flow_id,
            status=status,
            consumer_id=consumer_id,
            item_id=item_id,
            metadata=metadata,
            created_at=created_at,
            production_event_id=production_event_id,
            generation_id=generation_id,
            idempotency_key=idempotency_key,
        )

    def _record_terminal_receipt(
        self,
        flow_id: str,
        *,
        status: str,
        consumer_id: str,
        item_id: str,
        metadata: Mapping[str, Any] | None,
        created_at: str | None,
        production_event_id: str | None,
        generation_id: str | None,
        idempotency_key: str | None,
    ) -> str:
        if status not in TERMINAL_RECEIPT_STATUSES:
            raise ValueError(f"unsupported terminal receipt status: {status}")
        return self._record_receipt(
            flow_id,
            status=status,
            consumer_id=consumer_id,
            item_id=item_id,
            metadata=metadata,
            created_at=created_at,
            production_event_id=production_event_id,
            generation_id=generation_id,
            idempotency_key=idempotency_key,
        )

    def _record_receipt(
        self,
        flow_id: str,
        *,
        status: str,
        consumer_id: str,
        item_id: str,
        metadata: Mapping[str, Any] | None,
        created_at: str | None,
        production_event_id: str | None,
        generation_id: str | None,
        idempotency_key: str | None,
    ) -> str:
        if status not in ALLOWED_RECEIPT_STATUSES:
            raise ValueError(f"unsupported receipt status: {status}")
        if not consumer_id:
            raise ValueError("consumer_id is required")
        resolved_generation_id = generation_id or _PROCESS_GENERATION_ID
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            registered = conn.execute(
                "SELECT 1 FROM runtime_flow_registry WHERE flow_id = ?",
                (flow_id,),
            ).fetchone()
            if registered is None:
                self._register_flow_in_connection(
                    conn,
                    flow_id=flow_id,
                    data_type="runtime observed",
                    producer_refs=[],
                    consumer_refs=[consumer_id],
                    topic=flow_id,
                )
            resolved_event_id = production_event_id
            if not resolved_event_id and item_id:
                if generation_id:
                    row = conn.execute(
                        """
                        SELECT event_id, generation_id
                        FROM runtime_flow_events
                        WHERE flow_id = ? AND direction = 'produced'
                          AND item_id = ? AND generation_id = ?
                        ORDER BY created_at DESC, event_id DESC
                        LIMIT 1
                        """,
                        (flow_id, item_id, generation_id),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT event_id, generation_id
                        FROM runtime_flow_events
                        WHERE flow_id = ? AND direction = 'produced' AND item_id = ?
                        ORDER BY created_at DESC, event_id DESC
                        LIMIT 1
                        """,
                        (flow_id, item_id),
                    ).fetchone()
                if row is not None:
                    resolved_event_id = str(row[0])
                    resolved_generation_id = str(row[1])
            resolved_event_id = resolved_event_id or ""
            transition = ""
            if status in STAGE_RECEIPT_STATUSES and isinstance(metadata, Mapping):
                transition = str(metadata.get("transition") or "")
            dedupe_key = idempotency_key or (
                f"{flow_id}:{resolved_event_id}:{consumer_id}:{status}:{item_id}"
                + (f":{transition}" if transition else "")
            )
            canonical_metadata = _json_dumps(dict(metadata or {}))
            existing = conn.execute(
                """
                SELECT receipt_id, production_event_id, flow_id, consumer_id,
                       status, item_id, generation_id, metadata
                FROM runtime_flow_receipts
                WHERE idempotency_key = ?
                """,
                (dedupe_key,),
            ).fetchone()
            if existing is not None:
                existing_identity = (
                    str(existing[1]),
                    str(existing[2]),
                    str(existing[3]),
                    str(existing[4]),
                    str(existing[5]),
                    str(existing[6]),
                    _json_dumps(json.loads(str(existing[7] or "{}"))),
                )
                requested_identity = (
                    resolved_event_id,
                    flow_id,
                    consumer_id,
                    status,
                    item_id,
                    resolved_generation_id,
                    canonical_metadata,
                )
                if existing_identity != requested_identity:
                    raise ValueError("idempotency_key_conflict")
                return str(existing[0])
            if status in TERMINAL_RECEIPT_STATUSES and resolved_event_id:
                terminal_rows = conn.execute(
                    """
                    SELECT receipt_id, status, item_id, generation_id, metadata
                    FROM runtime_flow_receipts
                    WHERE production_event_id=? AND consumer_id=?
                      AND status IN ('consumed', 'dead_letter', 'skipped')
                    ORDER BY created_at, receipt_id
                    """,
                    (resolved_event_id, consumer_id),
                ).fetchall()
                superseded_ids: set[str] = set()
                for row in terminal_rows:
                    try:
                        row_metadata = json.loads(str(row[4] or "{}"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        row_metadata = {}
                    if not isinstance(row_metadata, Mapping):
                        continue
                    raw_ids = row_metadata.get("supersedes_receipt_ids", [])
                    if (
                        isinstance(raw_ids, list)
                        and str(row_metadata.get("supersession_reason") or "").strip()
                    ):
                        superseded_ids.update(str(value) for value in raw_ids)
                active_terminal_rows = [
                    row for row in terminal_rows if str(row[0]) not in superseded_ids
                ]
                if active_terminal_rows:
                    raw_superseded = (
                        metadata.get("supersedes_receipt_ids", [])
                        if isinstance(metadata, Mapping)
                        else []
                    )
                    explicit_supersession = (
                        isinstance(raw_superseded, list)
                        and {str(value) for value in raw_superseded}
                        >= {str(row[0]) for row in active_terminal_rows}
                        and bool(
                            str(
                                (metadata or {}).get("supersession_reason")
                                if isinstance(metadata, Mapping)
                                else ""
                            ).strip()
                        )
                    )
                    if explicit_supersession:
                        pass
                    elif (
                        len(active_terminal_rows) == 1
                        and str(active_terminal_rows[0][1]) == status
                        and str(active_terminal_rows[0][2]) == item_id
                        and str(active_terminal_rows[0][3]) == resolved_generation_id
                    ):
                        return str(active_terminal_rows[0][0])
                    else:
                        raise ValueError("terminal_receipt_conflict")
            receipt_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO runtime_flow_receipts (
                    receipt_id, production_event_id, flow_id, consumer_id,
                    status, item_id, generation_id, idempotency_key,
                    created_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    resolved_event_id,
                    flow_id,
                    consumer_id,
                    status,
                    item_id,
                    resolved_generation_id,
                    dedupe_key,
                    created_at or _now_utc(),
                    canonical_metadata,
                ),
            )
            conn.commit()
        return receipt_id

    def cognitive_data_snapshot(self) -> dict[str, Any]:
        """Return dynamically aggregated immutable event/receipt evidence."""

        with self._connect() as conn:
            return cognitive_data_snapshot_in_connection(conn)

    def snapshot(self) -> dict[str, Any]:
        """Return a machine-readable closure snapshot for all registered flows."""
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT flow_id, data_type, topic, producer_refs, consumer_refs,
                       pending_budget, dead_letter_budget, max_lag_seconds,
                       required, min_observations, observation_mode,
                       not_applicable_reason, freshness_required,
                       receipt_grace_seconds
                FROM runtime_flow_registry
                ORDER BY flow_id
                """).fetchall()
            production_rows = conn.execute("""
                SELECT event_id, flow_id, item_id, created_at,
                       generation_id, intended_consumers
                FROM runtime_flow_events
                WHERE direction = 'produced'
                ORDER BY created_at, event_id
                """).fetchall()
            receipt_rows = conn.execute("""
                SELECT receipt_id, production_event_id, flow_id, consumer_id,
                       status, item_id, generation_id, created_at, metadata
                FROM runtime_flow_receipts
                ORDER BY created_at, receipt_id
                """).fetchall()
        productions_by_flow: dict[str, list[dict[str, Any]]] = {}
        for event_id, flow_id, item_id, created_at, generation_id, intended_json in production_rows:
            productions_by_flow.setdefault(str(flow_id), []).append(
                {
                    "event_id": str(event_id),
                    "item_id": str(item_id),
                    "created_at": str(created_at),
                    "generation_id": str(generation_id),
                    "intended_consumers": {str(value) for value in json.loads(str(intended_json))},
                }
            )
        receipts_by_flow: dict[str, list[dict[str, Any]]] = {}
        for (
            receipt_id,
            production_event_id,
            flow_id,
            consumer_id,
            receipt_status,
            item_id,
            generation_id,
            created_at,
            metadata_json,
        ) in receipt_rows:
            try:
                metadata = json.loads(str(metadata_json or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            receipts_by_flow.setdefault(str(flow_id), []).append(
                {
                    "receipt_id": str(receipt_id),
                    "production_event_id": str(production_event_id),
                    "consumer_id": str(consumer_id),
                    "status": str(receipt_status),
                    "item_id": str(item_id),
                    "generation_id": str(generation_id),
                    "created_at": str(created_at),
                    "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
                }
            )

        flows: dict[str, dict[str, Any]] = {}
        counts = {
            "registered_flows": len(rows),
            "observed_flows": 0,
            "unobserved_flows": 0,
            "partial_flows": 0,
            "in_flight_flows": 0,
            "stale_flows": 0,
            "producer_only": 0,
            "consumer_only": 0,
            "orphan_items": 0,
            "no_source_items": 0,
            "item_mismatch_flows": 0,
            "pending_over_budget": 0,
            "in_flight_receipts": 0,
            "overdue_pending": 0,
            "dead_letter_over_budget": 0,
            "lag_over_budget": 0,
            "terminal_conflicts": 0,
        }
        for row in rows:
            (
                flow_id,
                data_type,
                topic,
                producer_refs_json,
                consumer_refs_json,
                pending_budget,
                dead_letter_budget,
                max_lag_seconds,
                required,
                min_observations,
                observation_mode,
                not_applicable_reason,
                freshness_required,
                receipt_grace_seconds,
            ) = row
            flow_id = str(flow_id)
            productions = productions_by_flow.get(flow_id, [])
            receipts = receipts_by_flow.get(flow_id, [])
            produced_count = len(productions)
            production_by_id = {event["event_id"]: event for event in productions}
            receipt_by_id = {receipt["receipt_id"]: receipt for receipt in receipts}
            superseded_receipt_ids: set[str] = set()
            for successor in receipts:
                raw_superseded = successor["metadata"].get("supersedes_receipt_ids", [])
                if not isinstance(raw_superseded, list):
                    continue
                production = production_by_id.get(successor["production_event_id"])
                if (
                    successor["status"] not in TERMINAL_RECEIPT_STATUSES
                    or production is None
                    or successor["consumer_id"] not in production["intended_consumers"]
                    or not str(successor["metadata"].get("supersession_reason") or "")
                ):
                    continue
                for receipt_id in raw_superseded:
                    predecessor = receipt_by_id.get(str(receipt_id))
                    if predecessor is None or predecessor["receipt_id"] == successor["receipt_id"]:
                        continue
                    if (
                        predecessor["production_event_id"] == successor["production_event_id"]
                        and predecessor["item_id"] == successor["item_id"]
                        and predecessor["generation_id"] == successor["generation_id"]
                    ):
                        superseded_receipt_ids.add(predecessor["receipt_id"])
            active_receipts = [
                receipt
                for receipt in receipts
                if receipt["receipt_id"] not in superseded_receipt_ids
            ]
            consumed_receipts = [
                receipt for receipt in active_receipts if receipt["status"] == "consumed"
            ]
            dead_letter_receipts = [
                receipt for receipt in active_receipts if receipt["status"] == "dead_letter"
            ]
            consumed_count = len(consumed_receipts)
            dead_letter_count = len(dead_letter_receipts)
            intended_pairs = {
                (event["event_id"], consumer)
                for event in productions
                for consumer in event["intended_consumers"]
            }
            terminal_pairs = {
                (receipt["production_event_id"], receipt["consumer_id"])
                for receipt in active_receipts
                if receipt["status"] in TERMINAL_RECEIPT_STATUSES
            }
            terminal_receipts_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for receipt in active_receipts:
                if receipt["status"] not in TERMINAL_RECEIPT_STATUSES:
                    continue
                terminal_receipts_by_pair.setdefault(
                    (receipt["production_event_id"], receipt["consumer_id"]),
                    [],
                ).append(receipt)
            terminal_conflict_pairs = {
                pair: values
                for pair, values in terminal_receipts_by_pair.items()
                if len(values) > 1
            }
            terminal_conflict_count = len(terminal_conflict_pairs)
            closed_pairs = intended_pairs & terminal_pairs
            pending_pairs = intended_pairs - terminal_pairs
            extra_pairs = terminal_pairs - intended_pairs
            pending_ages: dict[tuple[str, str], int] = {}
            for pair in pending_pairs:
                production = production_by_id.get(pair[0])
                if production is None:
                    continue
                produced_at = _parse_time(production["created_at"])
                pending_ages[pair] = (
                    max(0, int((now - produced_at).total_seconds()))
                    if produced_at is not None
                    else max(int(receipt_grace_seconds), int(max_lag_seconds)) + 1
                )
            overdue_pairs = {
                pair
                for pair, age_seconds in pending_ages.items()
                if int(receipt_grace_seconds) <= 0
                or age_seconds > int(receipt_grace_seconds)
            }
            in_flight_pairs = pending_pairs - overdue_pairs
            missing_consumers = sorted({consumer for _, consumer in overdue_pairs})
            extra_consumers = sorted({consumer for _, consumer in extra_pairs})
            intended_count = len(intended_pairs)
            terminal_consumer_count = len(closed_pairs)
            pending_count = len(pending_pairs)
            overdue_pending_count = len(overdue_pairs)
            in_flight_count = len(in_flight_pairs)
            consumer_only = bool(active_receipts) and produced_count == 0
            producer_only = overdue_pending_count > 0 and terminal_consumer_count == 0
            orphan_item_ids = sorted(
                {
                    production_by_id[event_id]["item_id"]
                    for event_id, _consumer in overdue_pairs
                    if event_id in production_by_id
                }
            )
            no_source_item_ids = sorted(
                {
                    receipt["item_id"]
                    for receipt in active_receipts
                    if receipt["production_event_id"] not in production_by_id
                }
            )
            orphan_item_count = overdue_pending_count
            no_source_item_count = sum(
                1
                for receipt in active_receipts
                if receipt["production_event_id"] not in production_by_id
            )
            item_mismatch = orphan_item_count > 0 or no_source_item_count > 0
            last_produced_at = productions[-1]["created_at"] if productions else None
            last_consumed_at = consumed_receipts[-1]["created_at"] if consumed_receipts else None
            last_dead_letter_at = (
                dead_letter_receipts[-1]["created_at"] if dead_letter_receipts else None
            )
            last_produced_dt = _parse_time(last_produced_at)
            lag_seconds = 0
            if pending_ages:
                lag_seconds = max(pending_ages.values())
            pending_over = overdue_pending_count > int(pending_budget)
            dead_letter_over = dead_letter_count > int(dead_letter_budget)
            lag_over = lag_seconds > int(max_lag_seconds)
            freshness_age_seconds = (
                max(0, int((now - last_produced_dt).total_seconds()))
                if last_produced_dt is not None
                else None
            )
            freshness_ok = bool(not freshness_required) or (
                freshness_age_seconds is not None and freshness_age_seconds <= int(max_lag_seconds)
            )
            if observation_mode == "not_applicable":
                observation_state = "not_applicable"
            elif bool(required) and produced_count < int(min_observations):
                observation_state = "unobserved"
            elif observation_mode == "on_event" and produced_count == 0:
                observation_state = "inactive"
            elif produced_count < int(min_observations):
                observation_state = "unobserved"
            elif not freshness_ok:
                observation_state = "stale"
            elif (
                overdue_pairs
                or extra_pairs
                or dead_letter_over
                or lag_over
                or terminal_conflict_count
            ):
                observation_state = "partial"
            elif in_flight_pairs:
                observation_state = "in_flight"
            else:
                observation_state = "observed"
            counts["producer_only"] += int(producer_only)
            counts["consumer_only"] += int(consumer_only)
            counts["orphan_items"] += orphan_item_count
            counts["no_source_items"] += no_source_item_count
            counts["item_mismatch_flows"] += int(item_mismatch)
            counts["pending_over_budget"] += int(pending_over)
            counts["in_flight_receipts"] += in_flight_count
            counts["overdue_pending"] += overdue_pending_count
            counts["dead_letter_over_budget"] += int(dead_letter_over)
            counts["lag_over_budget"] += int(lag_over)
            counts["terminal_conflicts"] += terminal_conflict_count
            if observation_state == "observed":
                counts["observed_flows"] += 1
            elif observation_state == "unobserved":
                counts["unobserved_flows"] += 1
            elif observation_state == "stale":
                counts["stale_flows"] += 1
            elif observation_state == "partial":
                counts["partial_flows"] += 1
            elif observation_state == "in_flight":
                counts["in_flight_flows"] += 1
            status = (
                "ok"
                if observation_state in {
                    "observed",
                    "in_flight",
                    "inactive",
                    "not_applicable",
                }
                else "degraded"
            )
            flows[flow_id] = {
                "status": status,
                "observation_state": observation_state,
                "data_type": data_type,
                "topic": topic,
                "producer_refs": json.loads(producer_refs_json),
                "consumer_refs": json.loads(consumer_refs_json),
                "required": bool(required),
                "min_observations": int(min_observations),
                "observation_mode": str(observation_mode),
                "not_applicable_reason": str(not_applicable_reason),
                "freshness_required": bool(freshness_required),
                "generation_id": productions[-1]["generation_id"] if productions else None,
                "produced_count": produced_count,
                "consumed_count": consumed_count,
                "dead_letter_count": dead_letter_count,
                "intended_count": intended_count,
                "terminal_consumer_count": terminal_consumer_count,
                "terminal_conflict_count": terminal_conflict_count,
                "terminal_conflict_pairs": [
                    {
                        "production_event_id": event_id,
                        "consumer_id": consumer_id,
                        "receipt_ids": [receipt["receipt_id"] for receipt in receipts],
                        "statuses": [receipt["status"] for receipt in receipts],
                    }
                    for (event_id, consumer_id), receipts in sorted(
                        terminal_conflict_pairs.items()
                    )
                ][:20],
                "missing_consumers": missing_consumers,
                "extra_consumers": extra_consumers,
                "orphan_item_count": orphan_item_count,
                "no_source_item_count": no_source_item_count,
                "orphan_item_ids": orphan_item_ids[:20],
                "no_source_item_ids": no_source_item_ids[:20],
                "pending_count": pending_count,
                "overdue_pending_count": overdue_pending_count,
                "in_flight_count": in_flight_count,
                "pending_budget": int(pending_budget),
                "receipt_grace_seconds": int(receipt_grace_seconds),
                "dead_letter_budget": int(dead_letter_budget),
                "max_lag_seconds": int(max_lag_seconds),
                "lag_seconds": lag_seconds,
                "freshness_ok": freshness_ok,
                "freshness_age_seconds": freshness_age_seconds,
                "last_produced_at": last_produced_at,
                "last_consumed_at": last_consumed_at,
                "last_dead_letter_at": last_dead_letter_at,
            }
        degraded = [flow_id for flow_id, flow in flows.items() if flow["status"] != "ok"]
        cognitive_data = self.cognitive_data_snapshot()
        status = (
            "degraded" if not rows or degraded or cognitive_data.get("status") != "ok" else "ok"
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "observation_state": "observed" if status == "ok" else "degraded",
            "db_path": str(self.db_path),
            "counts": counts,
            "degraded_flows": degraded,
            "flows": flows,
            "cognitive_data": cognitive_data,
        }
