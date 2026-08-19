"""Canonical receipt adapters for capture, sync, distillation, and KG pipelines."""

from __future__ import annotations

from typing import Any, Mapping

from core.pipeline_receipts import (
    DistillationWriteReceipt,
    distillation_failed_terminal_sha256,
    distillation_write_receipt_sha256,
)

from core.ops.cognitive_data_contract import (
    CognitiveDataEvent,
    now_utc,
    stable_dedupe_key,
    stable_event_id,
)
from core.ops.producer_consumer_ledger import ProducerConsumerLedger
from core.ops.runtime_flow_lookup import (
    cognitive_event_allows_consumer,
    cognitive_event_current_consumption,
    find_produced_event,
    find_runtime_terminal_receipts,
)
from core.ops.runtime_flow_telemetry import (
    record_cognitive_data_consumed,
    record_cognitive_data_event,
    record_runtime_consumed,
    record_runtime_dead_letter,
    record_runtime_produced,
    record_runtime_skipped,
    record_runtime_stage,
    runtime_item_id,
)


def record_synced_turn(
    config: Any,
    *,
    source_name: str,
    session_id: str,
    turn: Any,
    content_hash: str,
    persona_committed: bool,
) -> str:
    """Emit the synced-turn event after sync_log and optional persona commit."""
    subject = f"{source_name}:{session_id}:turn:{turn.turn_number}"
    event_id = stable_event_id(
        "sync_engine", source_name, session_id, str(turn.turn_number), content_hash
    )
    raw_event_id = str((turn.metadata or {}).get("raw_event_id") or "")
    event = CognitiveDataEvent(
        event_id=event_id,
        source_id=raw_event_id or subject,
        asset_id=raw_event_id or subject,
        source_kind="sync_engine",
        source_uri=f"sync://{source_name}/{session_id}/turn/{turn.turn_number}",
        content_hash=content_hash,
        canonical_subject=subject,
        data_type="synced_turn",
        producer="sync_engine",
        intended_consumers=("amphora", "distill", "persona"),
        privacy_level="local",
        confidence=1.0,
        evidence_refs=(raw_event_id or f"sync-log:{subject}",),
        dedupe_key=stable_dedupe_key("sync_engine", subject, content_hash),
        created_at=now_utc(),
        retention_policy="raw_retention",
    )
    durable_event_id = record_cognitive_data_event(
        event,
        config_or_path=config,
    )
    if durable_event_id != event_id:
        raise RuntimeError("cognitive_sync_event_not_durable")
    if persona_committed:
        record_cognitive_data_consumed(
            event_id,
            consumer_id="persona",
            outcome="persona_signal_committed",
            config_or_path=config,
        )
    if turn.metadata is None:
        turn.metadata = {}
    turn.metadata["cognitive_sync_event_id"] = event_id
    return event_id


def record_sync_handoff(config: Any, session_id: str, meta: dict[str, Any], receipt: Any) -> None:
    """Record Amphora acknowledgement and the runtime sync-to-distill handoff."""
    ledger = _read_only_ledger(config)
    for event_id in meta.get("cognitive_sync_event_ids", []):
        event_id = str(event_id or "")
        if (
            not event_id
            or ledger is None
            or not cognitive_event_allows_consumer(
                ledger.db_path,
                event_id,
                "amphora",
            )
        ):
            continue
        record_cognitive_data_consumed(
            event_id,
            consumer_id="amphora",
            outcome="distill_task_enqueued",
            metadata={"task_id": receipt.task_id},
            config_or_path=config,
        )
    generation_id = _distill_generation_id(receipt.task_id, receipt.input_revision)
    record_runtime_produced(
        "raw_quality_to_distill_gate",
        source="core/sync_framework/sync_engine.py",
        item_id=runtime_item_id("distill-session", session_id),
        intended_consumers=["core/hephaestus/distillation_engine.py"],
        metadata={
            "task_id": receipt.task_id,
            "input_revision": receipt.input_revision,
            "transition": "amphora_enqueued",
        },
        generation_id=generation_id,
        idempotency_key=f"raw_quality_to_distill_gate:{generation_id}:produced",
        config_or_path=config,
    )


def record_capture_worker_handoff(config: Any, session_id: str, receipt: Any) -> None:
    """Record the worker-owned Amphora handoff on the canonical runtime flow."""
    generation_id = _distill_generation_id(receipt.task_id, receipt.input_revision)
    record_runtime_produced(
        "raw_quality_to_distill_gate",
        source="core/sync_framework/capture_worker.py",
        item_id=runtime_item_id("distill-session", session_id),
        intended_consumers=["core/hephaestus/distillation_engine.py"],
        metadata={
            "task_id": receipt.task_id,
            "input_revision": receipt.input_revision,
            "transition": "capture_worker_amphora_enqueued",
        },
        generation_id=generation_id,
        idempotency_key=f"raw_quality_to_distill_gate:{generation_id}:produced",
        config_or_path=config,
    )


def record_distillation_prejudgment(
    config: Any,
    *,
    session_id: str,
    meta: dict[str, Any],
    verdict: str,
) -> None:
    """Record the value prejudgment as a nonterminal stage event.

    A prejudgment verdict (MAYBE/YES/CERTAINLY_NO) is consumer progress, not a
    terminal outcome: the exact task generation stays pending until a typed
    committed, typed intentional-skip, or failed terminal/dead-letter receipt
    closes it.  The stage event deliberately writes no runtime terminal
    receipt and no cognitive consumption head, so a later real failure cannot
    be shadowed by receipt dedupe.
    """
    task_id = str(meta.get("_amphora_task_id") or meta.get("task_id") or "")
    input_revision = str(meta.get("input_revision") or "")
    item_id = runtime_item_id("distill-session", session_id)
    production = _find_distill_production(
        config,
        item_id=item_id,
        task_id=task_id,
        input_revision=input_revision,
    )
    if production:
        record_runtime_stage(
            "raw_quality_to_distill_gate",
            source="core/hephaestus/distillation_engine.py",
            item_id=item_id,
            production_event_id=production["event_id"],
            generation_id=production["generation_id"],
            metadata={"transition": "value_prejudgment_completed", "verdict": verdict},
            idempotency_key=(
                "raw_quality_to_distill_gate:"
                f"{production['generation_id']}:value_prejudgment_completed"
            ),
            config_or_path=config,
        )


def _distillation_task_identity(
    task: Mapping[str, Any],
) -> tuple[str, str, str, Mapping[str, Any]]:
    """Return the immutable task generation identity and its typed metadata."""
    session_id = str(task.get("session_id") or "")
    task_id = str(task.get("task_id") or "")
    input_revision = str(task.get("input_revision") or "")
    meta = task.get("meta")
    if not isinstance(meta, Mapping):
        meta = {}
    if not task_id:
        task_id = str(meta.get("_amphora_task_id") or meta.get("task_id") or "")
    if not input_revision:
        input_revision = str(meta.get("input_revision") or "")
    return session_id, task_id, input_revision, meta


def _cognitive_event_ids(meta: Mapping[str, Any]) -> tuple[str, ...]:
    values = meta.get("cognitive_sync_event_ids")
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(
        dict.fromkeys(
            event_id
            for value in values
            if (event_id := str(value or "").strip())
        )
    )


def _failed_terminal_payload_sha256(
    task: Mapping[str, Any],
    *,
    reason: str,
) -> str:
    session_id, task_id, input_revision, meta = _distillation_task_identity(
        task
    )
    return distillation_failed_terminal_sha256(
        task_id=task_id,
        session_id=session_id,
        input_revision=input_revision,
        reason=str(reason),
        retry_count=int(task.get("retry_count") or 0),
        max_retries=int(task.get("max_retries") or 0),
        cognitive_event_ids=_cognitive_event_ids(meta),
    )


def _active_runtime_terminal_receipts(
    receipts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return terminal receipts that have not been explicitly superseded."""
    receipt_by_id = {
        str(receipt.get("receipt_id") or ""): receipt
        for receipt in receipts
        if str(receipt.get("receipt_id") or "")
    }
    superseded_ids: set[str] = set()
    for successor in receipts:
        metadata = successor.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        raw_ids = metadata.get("supersedes_receipt_ids")
        if (
            successor.get("status") not in {"consumed", "dead_letter", "skipped"}
            or not isinstance(raw_ids, list)
            or not str(metadata.get("supersession_reason") or "").strip()
        ):
            continue
        for raw_id in raw_ids:
            receipt_id = str(raw_id or "")
            predecessor = receipt_by_id.get(receipt_id)
            if (
                predecessor is not None
                and predecessor.get("production_event_id")
                == successor.get("production_event_id")
                and predecessor.get("item_id") == successor.get("item_id")
                and predecessor.get("generation_id") == successor.get("generation_id")
            ):
                superseded_ids.add(receipt_id)
    return [
        receipt
        for receipt in receipts
        if receipt.get("status") in {"consumed", "dead_letter", "skipped"}
        and str(receipt.get("receipt_id") or "") not in superseded_ids
    ]


def _matches_success_terminal_runtime_receipt(
    terminal: Mapping[str, Any],
    *,
    item_id: str,
    generation_id: str,
    receipt: DistillationWriteReceipt,
) -> bool:
    return (
        terminal.get("status") == "consumed"
        and terminal.get("item_id") == item_id
        and terminal.get("generation_id") == generation_id
        and terminal.get("metadata", {}).get("transition")
        == "distillation_terminal_receipt_verified"
        and terminal.get("metadata", {}).get("receipt_status")
        == receipt.status
        and terminal.get("metadata", {}).get("receipt_sha256")
        == distillation_write_receipt_sha256(receipt)
    )


def _classify_success_terminal_runtime_state(
    terminals: list[dict[str, Any]],
    *,
    item_id: str,
    generation_id: str,
    receipt: DistillationWriteReceipt,
) -> dict[str, Any]:
    exact = [
        terminal
        for terminal in terminals
        if _matches_success_terminal_runtime_receipt(
            terminal,
            item_id=item_id,
            generation_id=generation_id,
            receipt=receipt,
        )
    ]
    if len(terminals) == 1 and len(exact) == 1:
        return {
            "state": "exact",
            "runtime_receipt_id": str(exact[0]["receipt_id"]),
            "supersedes_receipt_ids": [],
            "supersession_reason": "",
        }
    if not terminals:
        return {
            "state": "missing",
            "runtime_receipt_id": "",
            "supersedes_receipt_ids": [],
            "supersession_reason": "",
        }
    if len(terminals) != 1:
        return {
            "state": "conflict",
            "runtime_receipt_id": "",
            "supersedes_receipt_ids": [],
            "supersession_reason": "",
        }
    legacy = terminals[0]
    metadata = legacy.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    if (
        legacy.get("status") != "consumed"
        or legacy.get("consumer_id") != "core/hephaestus/distillation_engine.py"
        or legacy.get("item_id") != item_id
        or legacy.get("generation_id") != generation_id
        or metadata.get("supersedes_receipt_ids")
        or str(metadata.get("supersession_reason") or "").strip()
    ):
        return {
            "state": "conflict",
            "runtime_receipt_id": "",
            "supersedes_receipt_ids": [],
            "supersession_reason": "",
        }
    transition = str(metadata.get("transition") or "")
    if transition == "value_prejudgment_completed":
        reason = (
            "legacy_prejudgment_false_terminal_replaced_by_verified_terminal_receipt"
        )
    elif (
        transition == "distillation_terminal_receipt_verified"
        and metadata.get("receipt_status") == receipt.status
        and not str(metadata.get("receipt_sha256") or "").strip()
    ):
        reason = (
            "legacy_terminal_missing_payload_binding_replaced_by_verified_terminal_receipt"
        )
    else:
        return {
            "state": "conflict",
            "runtime_receipt_id": "",
            "supersedes_receipt_ids": [],
            "supersession_reason": "",
        }
    return {
        "state": "legacy_reconcilable",
        "runtime_receipt_id": "",
        "supersedes_receipt_ids": [str(legacy["receipt_id"])],
        "supersession_reason": reason,
    }


def inspect_distillation_terminal_runtime_state(
    config: Any,
    *,
    task: Mapping[str, Any],
    receipt: DistillationWriteReceipt,
) -> dict[str, Any]:
    """Inspect the active success terminal without writing any receipt."""
    session_id, task_id, input_revision, _meta = _distillation_task_identity(task)
    if not session_id or not task_id or not input_revision:
        return {"state": "identity_missing"}
    item_id = runtime_item_id("distill-session", session_id)
    production = _find_distill_production(
        config,
        item_id=item_id,
        task_id=task_id,
        input_revision=input_revision,
    )
    if not production:
        return {"state": "production_missing"}
    ledger = _read_only_ledger(config)
    if ledger is None:
        return {"state": "runtime_ledger_missing"}
    terminals = _active_runtime_terminal_receipts(
        find_runtime_terminal_receipts(
            ledger.db_path,
            "raw_quality_to_distill_gate",
            production_event_id=production["event_id"],
        )
    )
    state = _classify_success_terminal_runtime_state(
        terminals,
        item_id=item_id,
        generation_id=production["generation_id"],
        receipt=receipt,
    )
    return {
        **state,
        "production_event_id": production["event_id"],
        "generation_id": production["generation_id"],
        "item_id": item_id,
    }


def inspect_distillation_terminal_cognitive_state(
    config: Any,
    *,
    task: Mapping[str, Any],
    receipt: DistillationWriteReceipt,
) -> dict[str, Any]:
    """Inspect whether every cognitive head can reach this exact terminal."""
    _session_id, task_id, _input_revision, meta = _distillation_task_identity(
        task
    )
    event_ids = _cognitive_event_ids(meta)
    if not event_ids:
        return {
            "state": "not_applicable",
            "supersedes_consumption_ids": [],
            "supersession_reasons": [],
            "conflicting_event_ids": [],
        }
    ledger = _read_only_ledger(config)
    if ledger is None:
        return {
            "state": "runtime_ledger_missing",
            "supersedes_consumption_ids": [],
            "supersession_reasons": [],
            "conflicting_event_ids": list(event_ids),
        }
    supersedes: list[str] = []
    reasons: list[str] = []
    conflicts: list[str] = []
    append_new = False
    expectations = (
        (
            "amphora",
            {"distill_task_enqueued", "distill_task_handoff_verified"},
            {"distill_task_enqueued"},
            "legacy_unbound_amphora_handoff",
        ),
        (
            "distill",
            {f"distill_task_{receipt.status}"},
            {"value_prejudgment_completed"},
            "legacy_prejudgment_false_terminal",
        ),
    )
    for event_id in event_ids:
        for consumer_id, exact_outcomes, legacy_outcomes, reason in expectations:
            if not cognitive_event_allows_consumer(
                ledger.db_path,
                event_id,
                consumer_id,
            ):
                conflicts.append(event_id)
                continue
            current = cognitive_event_current_consumption(
                ledger.db_path,
                event_id,
                consumer_id,
            )
            if current is None:
                append_new = True
                continue
            status = str(current.get("status") or "")
            outcome = str(current.get("outcome") or "")
            metadata = current.get("metadata")
            if not isinstance(metadata, Mapping):
                metadata = {}
            if (
                status in {"consumed", "committed"}
                and outcome in exact_outcomes
                and metadata.get("task_id") == task_id
            ):
                continue
            if (
                status in {"consumed", "committed"}
                and outcome in legacy_outcomes
                and not str(metadata.get("task_id") or "")
            ):
                supersedes.append(str(current.get("consumption_id") or ""))
                reasons.append(reason)
                continue
            reopened = (
                status == "revoked"
                and metadata.get("reopen_required") is True
                and bool(current.get("supersedes_consumption_id"))
                and current.get("supersedes_consumption_id")
                == current.get("correction_of_consumption_id")
            )
            if reopened:
                append_new = True
                continue
            conflicts.append(event_id)
    if conflicts:
        state = "conflict"
    elif supersedes:
        state = "legacy_reconcilable"
    elif append_new:
        state = "missing"
    else:
        state = "exact"
    return {
        "state": state,
        "supersedes_consumption_ids": list(dict.fromkeys(supersedes)),
        "supersession_reasons": sorted(set(reasons)),
        "conflicting_event_ids": list(dict.fromkeys(conflicts)),
    }


def _record_verified_cognitive_consumptions(
    config: Any,
    *,
    event_ids: tuple[str, ...],
    consumer_id: str,
    outcome: str,
    task_id: str,
    status: str = "consumed",
    accepted_outcomes: tuple[str, ...] = (),
    allow_legacy_unbound_current: bool = False,
) -> dict[str, int]:
    """Write exact receipts and defer every explicit event that is not provable."""
    ledger = _read_only_ledger(config)
    if ledger is None:
        return {
            "eligible_events": 0,
            "existing_receipts": 0,
            "recorded_receipts": 0,
            "deferred_receipts": len(event_ids),
        }

    eligible_events = 0
    existing_receipts = 0
    recorded_receipts = 0
    for event_id in event_ids:
        if not cognitive_event_allows_consumer(ledger.db_path, event_id, consumer_id):
            continue
        eligible_events += 1
        current = cognitive_event_current_consumption(
            ledger.db_path,
            event_id,
            consumer_id,
        )
        supersedes_consumption_id = ""
        if current is not None:
            legacy_unbound_current = (
                allow_legacy_unbound_current
                and current.get("status") in {status, "committed"}
                and current.get("outcome") in accepted_outcomes
                and not str(current.get("metadata", {}).get("task_id") or "")
            )
            exact_current = (
                current.get("status")
                in ({status, "committed"} if status == "consumed" else {status})
                and current.get("outcome") in {outcome, *accepted_outcomes}
                and current.get("metadata", {}).get("task_id") == task_id
            )
            if exact_current:
                existing_receipts += 1
                continue
            if legacy_unbound_current:
                supersedes_consumption_id = str(
                    current.get("consumption_id") or ""
                )
            else:
                reopened = (
                    current.get("status") == "revoked"
                    and current.get("metadata", {}).get("reopen_required") is True
                    and bool(current.get("supersedes_consumption_id"))
                    and current.get("supersedes_consumption_id")
                    == current.get("correction_of_consumption_id")
                )
                if not reopened:
                    continue
                supersedes_consumption_id = str(
                    current.get("consumption_id") or ""
                )
        receipt_id = record_cognitive_data_consumed(
            event_id,
            consumer_id=consumer_id,
            outcome=outcome,
            status=status,
            metadata={"task_id": task_id},
            supersedes_consumption_id=supersedes_consumption_id,
            config_or_path=config,
        )
        if receipt_id is not None:
            recorded_receipts += 1
    return {
        "eligible_events": eligible_events,
        "existing_receipts": existing_receipts,
        "recorded_receipts": recorded_receipts,
        "deferred_receipts": len(event_ids) - existing_receipts - recorded_receipts,
    }


def _verify_distillation_cognitive_receipts(
    ledger: ProducerConsumerLedger,
    *,
    event_ids: tuple[str, ...],
    task_id: str,
    terminal_status: str,
) -> dict[str, int]:
    """Verify the Amphora handoff and exact distill terminal for every event."""
    if terminal_status == "failed":
        distill_status = "failed_terminal"
        distill_outcome = "distill_task_failed_terminal"
    elif terminal_status in {"committed", "intentional_skip"}:
        distill_status = "consumed"
        distill_outcome = f"distill_task_{terminal_status}"
    else:
        raise ValueError("invalid_distillation_terminal_status")
    expectations = (
        (
            "amphora",
            "committed",
            {
                "distill_task_enqueued",
                "distill_task_handoff_verified",
            },
        ),
        (
            "distill",
            "committed" if distill_status == "consumed" else distill_status,
            {distill_outcome},
        ),
    )
    referenced = len(event_ids) * len(expectations)
    eligible = 0
    verified = 0
    missing_or_ineligible = 0
    for event_id in event_ids:
        for consumer_id, expected_status, expected_outcomes in expectations:
            if not cognitive_event_allows_consumer(
                ledger.db_path,
                event_id,
                consumer_id,
            ):
                missing_or_ineligible += 1
                continue
            eligible += 1
            current = cognitive_event_current_consumption(
                ledger.db_path,
                event_id,
                consumer_id,
            )
            if (
                current is not None
                and current.get("status") == expected_status
                and current.get("outcome") in expected_outcomes
                and current.get("metadata", {}).get("task_id") == task_id
            ):
                verified += 1
    return {
        "referenced": referenced,
        "eligible": eligible,
        "verified": verified,
        "missing_or_ineligible": missing_or_ineligible,
        "deferred": referenced - verified,
    }


def _verify_failed_terminal_cognitive_receipts(
    ledger: ProducerConsumerLedger,
    *,
    event_ids: tuple[str, ...],
    task_id: str,
) -> dict[str, int]:
    return _verify_distillation_cognitive_receipts(
        ledger,
        event_ids=event_ids,
        task_id=task_id,
        terminal_status="failed",
    )


def record_distillation_handoff(
    config: Any,
    *,
    task: Mapping[str, Any],
    allow_legacy_unbound_current: bool = False,
) -> dict[str, Any]:
    """Restore the Amphora receipt from a durable task-generation record."""
    session_id, task_id, input_revision, meta = _distillation_task_identity(task)
    if not session_id or not task_id or not input_revision:
        return {
            "verified": False,
            "reason": "task_identity_missing",
            "cognitive_receipts": 0,
            "cognitive_deferred": 0,
        }
    receipts = _record_verified_cognitive_consumptions(
        config,
        event_ids=_cognitive_event_ids(meta),
        consumer_id="amphora",
        outcome="distill_task_handoff_verified",
        task_id=task_id,
        accepted_outcomes=("distill_task_enqueued",),
        allow_legacy_unbound_current=allow_legacy_unbound_current,
    )
    verified = receipts["deferred_receipts"] == 0
    return {
        "verified": verified,
        "reason": "recorded" if verified else "cognitive_handoff_deferred",
        "cognitive_receipts": receipts["recorded_receipts"],
        "cognitive_deferred": receipts["deferred_receipts"],
    }


def record_distillation_cognitive_terminal(
    config: Any,
    *,
    task: Mapping[str, Any],
    receipt: DistillationWriteReceipt,
    allow_legacy_prejudgment_supersession: bool = False,
) -> dict[str, Any]:
    """Restore the distill consumer receipt from a typed terminal result."""
    if not receipt.terminal or receipt.status not in {"committed", "intentional_skip"}:
        return {
            "verified": False,
            "reason": "nonterminal_receipt",
            "cognitive_receipts": 0,
            "cognitive_deferred": 0,
        }
    if not str(receipt.terminal_reason or "").strip():
        return {
            "verified": False,
            "reason": "terminal_reason_missing",
            "cognitive_receipts": 0,
            "cognitive_deferred": 0,
        }

    session_id, task_id, input_revision, meta = _distillation_task_identity(task)
    if not session_id or not task_id or not input_revision:
        return {
            "verified": False,
            "reason": "task_identity_missing",
            "cognitive_receipts": 0,
            "cognitive_deferred": 0,
        }
    receipts = _record_verified_cognitive_consumptions(
        config,
        event_ids=_cognitive_event_ids(meta),
        consumer_id="distill",
        outcome=f"distill_task_{receipt.status}",
        task_id=task_id,
        accepted_outcomes=(
            ("value_prejudgment_completed",)
            if allow_legacy_prejudgment_supersession
            else ()
        ),
        allow_legacy_unbound_current=allow_legacy_prejudgment_supersession,
    )
    verified = receipts["deferred_receipts"] == 0
    return {
        "verified": verified,
        "reason": "recorded" if verified else "cognitive_terminal_deferred",
        "cognitive_receipts": receipts["recorded_receipts"],
        "cognitive_deferred": receipts["deferred_receipts"],
    }


def record_distillation_failed_terminal(
    config: Any,
    *,
    task: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Close the exact task generation as a failed terminal (dead letter).

    Only a permanent, retry-exhausted failure may call this adapter; retryable
    failures keep the generation pending so the next attempt can still reach
    a typed committed or intentional-skip terminal.  The runtime side records
    a ``dead_letter`` receipt for the exact generation and the cognitive side
    records a ``failed_terminal`` consumption head, so a post-failure audit
    can distinguish proven failure from never-attempted pending work.
    """
    failure_reason = str(reason or "").strip()
    if not failure_reason:
        return {"matched": False, "reason": "failure_reason_missing"}

    session_id, task_id, input_revision, meta = _distillation_task_identity(task)
    if not session_id or not task_id or not input_revision:
        return {"matched": False, "reason": "task_identity_missing"}
    payload_sha256 = _failed_terminal_payload_sha256(
        task,
        reason=failure_reason,
    )

    item_id = runtime_item_id("distill-session", session_id)
    production = _find_distill_production(
        config,
        item_id=item_id,
        task_id=task_id,
        input_revision=input_revision,
    )
    if not production:
        return {"matched": False, "reason": "production_missing"}

    ledger = _read_only_ledger(config)
    if ledger is None:
        return {"matched": False, "reason": "runtime_ledger_missing"}
    existing_terminal = _active_runtime_terminal_receipts(
        find_runtime_terminal_receipts(
            ledger.db_path,
            "raw_quality_to_distill_gate",
            production_event_id=production["event_id"],
        )
    )
    if any(receipt["status"] != "dead_letter" for receipt in existing_terminal):
        return {"matched": False, "reason": "terminal_receipt_conflict"}
    exact_existing = [
        receipt
        for receipt in existing_terminal
        if receipt.get("status") == "dead_letter"
        and receipt.get("item_id") == item_id
        and receipt.get("generation_id") == production["generation_id"]
        and receipt.get("metadata", {}).get("transition")
        == "distillation_failed_terminal"
        and receipt.get("metadata", {}).get("failure_reason")
        == failure_reason
        and receipt.get("metadata", {}).get("payload_sha256")
        == payload_sha256
    ]
    if existing_terminal and len(exact_existing) != 1:
        return {"matched": False, "reason": "terminal_receipt_conflict"}
    handoff = record_distillation_handoff(config, task=task)
    cognitive_receipts = _record_verified_cognitive_consumptions(
        config,
        event_ids=_cognitive_event_ids(meta),
        consumer_id="distill",
        outcome="distill_task_failed_terminal",
        task_id=task_id,
        status="failed_terminal",
    )
    cognitive_verification = _verify_failed_terminal_cognitive_receipts(
        ledger,
        event_ids=_cognitive_event_ids(meta),
        task_id=task_id,
    )
    if cognitive_verification["deferred"]:
        return {
            "matched": False,
            "reason": "cognitive_failed_terminal_deferred",
            "cognitive_receipts": (
                handoff["cognitive_receipts"]
                + cognitive_receipts["recorded_receipts"]
            ),
            "cognitive_deferred": cognitive_verification["deferred"],
        }
    runtime_receipt_id = (
        str(exact_existing[0]["receipt_id"])
        if exact_existing
        else record_runtime_dead_letter(
            "raw_quality_to_distill_gate",
            source="core/hephaestus/distillation_engine.py",
            item_id=item_id,
            production_event_id=production["event_id"],
            generation_id=production["generation_id"],
            metadata={
                "transition": "distillation_failed_terminal",
                "failure_reason": failure_reason,
                "payload_sha256": payload_sha256,
            },
            idempotency_key=(
                "raw_quality_to_distill_gate:"
                f"{production['generation_id']}:distillation_failed_terminal:"
                f"{payload_sha256}"
            ),
            config_or_path=config,
        )
    )
    if runtime_receipt_id is None:
        return {
            "matched": False,
            "reason": (
                "terminal_receipt_conflict"
                if existing_terminal
                else "runtime_receipt_deferred"
            ),
            "cognitive_receipts": (
                handoff["cognitive_receipts"]
                + cognitive_receipts["recorded_receipts"]
            ),
            "cognitive_deferred": 0,
        }
    return {
        "matched": True,
        "reason": "recorded",
        "runtime_receipt_id": runtime_receipt_id,
        "production_event_id": production["event_id"],
        "generation_id": production["generation_id"],
        "cognitive_receipts": (
            handoff["cognitive_receipts"]
            + cognitive_receipts["recorded_receipts"]
        ),
        "cognitive_deferred": 0,
    }


def verify_distillation_failed_terminal(
    config: Any,
    *,
    task: Mapping[str, Any],
    expected_reason: str,
) -> dict[str, Any]:
    """Verify one exact dead-letter receipt from the durable runtime ledger."""
    session_id, task_id, input_revision, meta = _distillation_task_identity(task)
    if not session_id or not task_id or not input_revision:
        return {"verified": False, "reason": "task_identity_missing"}
    item_id = runtime_item_id("distill-session", session_id)
    payload_sha256 = _failed_terminal_payload_sha256(
        task,
        reason=str(expected_reason),
    )
    production = _find_distill_production(
        config,
        item_id=item_id,
        task_id=task_id,
        input_revision=input_revision,
    )
    if not production:
        return {"verified": False, "reason": "production_missing"}
    ledger = _read_only_ledger(config)
    if ledger is None:
        return {"verified": False, "reason": "runtime_ledger_missing"}
    terminals = _active_runtime_terminal_receipts(
        find_runtime_terminal_receipts(
            ledger.db_path,
            "raw_quality_to_distill_gate",
            production_event_id=production["event_id"],
        )
    )
    matches = [
        receipt
        for receipt in terminals
        if receipt.get("status") == "dead_letter"
        and receipt.get("item_id") == item_id
        and receipt.get("generation_id") == production["generation_id"]
        and receipt.get("metadata", {}).get("transition")
        == "distillation_failed_terminal"
        and receipt.get("metadata", {}).get("failure_reason")
        == str(expected_reason)
        and receipt.get("metadata", {}).get("payload_sha256")
        == payload_sha256
    ]
    if len(matches) != 1:
        return {
            "verified": False,
            "reason": (
                "terminal_receipt_conflict"
                if terminals
                else "failed_terminal_receipt_missing"
            ),
        }
    cognitive = _verify_failed_terminal_cognitive_receipts(
        ledger,
        event_ids=_cognitive_event_ids(meta),
        task_id=task_id,
    )
    if cognitive["deferred"]:
        return {
            "verified": False,
            "reason": "cognitive_failed_terminal_missing",
            "cognitive_deferred": cognitive["deferred"],
        }
    return {
        "verified": True,
        "reason": "verified",
        "runtime_receipt_id": str(matches[0]["receipt_id"]),
        "production_event_id": str(production["event_id"]),
        "generation_id": str(production["generation_id"]),
        "cognitive_deferred": 0,
    }


def record_distillation_terminal(
    config: Any,
    *,
    task: Mapping[str, Any],
    receipt: DistillationWriteReceipt,
    allow_legacy_terminal_supersession: bool = False,
) -> dict[str, Any]:
    """Attach terminal task evidence to the exact Raw-to-distill generation.

    A committed page or typed intentional skip proves that Amphora accepted
    this task and the distillation consumer reached a terminal result.  This
    is the only success-side closure of the generation: the prejudgment stage
    event never closes it, and retryable/failed/proposal-pending work stays
    pending until this receipt or a failed-terminal dead letter arrives.
    """
    if not receipt.terminal or receipt.status not in {"committed", "intentional_skip"}:
        return {"matched": False, "reason": "nonterminal_receipt", "cognitive_receipts": 0}
    if not str(receipt.terminal_reason or "").strip():
        return {
            "matched": False,
            "reason": "terminal_reason_missing",
            "cognitive_receipts": 0,
        }
    receipt_sha256 = distillation_write_receipt_sha256(receipt)

    session_id, task_id, input_revision, meta = _distillation_task_identity(task)
    if not session_id or not task_id or not input_revision:
        return {"matched": False, "reason": "task_identity_missing", "cognitive_receipts": 0}

    item_id = runtime_item_id("distill-session", session_id)
    production = _find_distill_production(
        config,
        item_id=item_id,
        task_id=task_id,
        input_revision=input_revision,
    )
    if not production:
        return {
            "matched": False,
            "reason": "production_missing",
            "cognitive_receipts": 0,
            "cognitive_deferred": 0,
            "cognitive_handoff_verified": False,
            "cognitive_terminal_verified": False,
        }

    ledger = _read_only_ledger(config)
    if ledger is None:
        return {"matched": False, "reason": "runtime_ledger_missing", "cognitive_receipts": 0}
    existing_terminal = _active_runtime_terminal_receipts(
        find_runtime_terminal_receipts(
            ledger.db_path,
            "raw_quality_to_distill_gate",
            production_event_id=production["event_id"],
        )
    )
    runtime_state = _classify_success_terminal_runtime_state(
        existing_terminal,
        item_id=item_id,
        generation_id=production["generation_id"],
        receipt=receipt,
    )
    if runtime_state["state"] == "conflict" or (
        runtime_state["state"] == "legacy_reconcilable"
        and not allow_legacy_terminal_supersession
    ):
        return {
            "matched": False,
            "reason": "terminal_receipt_conflict",
            "cognitive_receipts": 0,
            "cognitive_deferred": 0,
            "cognitive_handoff_verified": False,
            "cognitive_terminal_verified": False,
        }
    handoff = record_distillation_handoff(
        config,
        task=task,
        allow_legacy_unbound_current=allow_legacy_terminal_supersession,
    )
    cognitive_terminal = record_distillation_cognitive_terminal(
        config,
        task=task,
        receipt=receipt,
        allow_legacy_prejudgment_supersession=(
            allow_legacy_terminal_supersession
        ),
    )
    cognitive_verification = _verify_distillation_cognitive_receipts(
        ledger,
        event_ids=_cognitive_event_ids(meta),
        task_id=task_id,
        terminal_status=receipt.status,
    )
    cognitive_receipts = (
        handoff["cognitive_receipts"]
        + cognitive_terminal["cognitive_receipts"]
    )
    if cognitive_verification["deferred"]:
        return {
            "matched": False,
            "reason": "cognitive_terminal_deferred",
            "cognitive_receipts": cognitive_receipts,
            "cognitive_deferred": cognitive_verification["deferred"],
            "cognitive_handoff_verified": handoff["verified"],
            "cognitive_terminal_verified": cognitive_terminal["verified"],
        }

    runtime_receipt_id = (
        str(runtime_state["runtime_receipt_id"])
        if runtime_state["state"] == "exact"
        else record_runtime_consumed(
            "raw_quality_to_distill_gate",
            source="core/hephaestus/distillation_engine.py",
            item_id=item_id,
            production_event_id=production["event_id"],
            generation_id=production["generation_id"],
            metadata={
                "transition": "distillation_terminal_receipt_verified",
                "receipt_status": receipt.status,
                "receipt_sha256": receipt_sha256,
                **(
                    {
                        "supersedes_receipt_ids": runtime_state[
                            "supersedes_receipt_ids"
                        ],
                        "supersession_reason": runtime_state[
                            "supersession_reason"
                        ],
                    }
                    if runtime_state["state"] == "legacy_reconcilable"
                    else {}
                ),
            },
            idempotency_key=(
                "raw_quality_to_distill_gate:"
                f"{production['generation_id']}:distillation_{receipt.status}:"
                f"{receipt_sha256}"
            ),
            config_or_path=config,
        )
    )
    if runtime_receipt_id is None:
        return {
            "matched": False,
            "reason": (
                "terminal_receipt_conflict"
                if existing_terminal
                else "runtime_receipt_deferred"
            ),
            "cognitive_receipts": 0,
            "cognitive_deferred": 0,
            "cognitive_handoff_verified": False,
            "cognitive_terminal_verified": False,
        }
    return {
        "matched": True,
        "reason": "recorded",
        "runtime_receipt_id": runtime_receipt_id,
        "production_event_id": production["event_id"],
        "generation_id": production["generation_id"],
        "cognitive_receipts": cognitive_receipts,
        "cognitive_deferred": 0,
        "cognitive_handoff_verified": handoff["verified"],
        "cognitive_terminal_verified": cognitive_terminal["verified"],
    }


def verify_distillation_terminal(
    config: Any,
    *,
    task: Mapping[str, Any],
    receipt: DistillationWriteReceipt,
) -> dict[str, Any]:
    """Verify one exact successful/intentional-skip terminal and its denominator."""
    if not receipt.terminal or receipt.status not in {"committed", "intentional_skip"}:
        return {"verified": False, "reason": "nonterminal_receipt"}
    session_id, task_id, input_revision, meta = _distillation_task_identity(task)
    if not session_id or not task_id or not input_revision:
        return {"verified": False, "reason": "task_identity_missing"}
    item_id = runtime_item_id("distill-session", session_id)
    production = _find_distill_production(
        config,
        item_id=item_id,
        task_id=task_id,
        input_revision=input_revision,
    )
    if not production:
        return {"verified": False, "reason": "production_missing"}
    ledger = _read_only_ledger(config)
    if ledger is None:
        return {"verified": False, "reason": "runtime_ledger_missing"}
    terminals = _active_runtime_terminal_receipts(
        find_runtime_terminal_receipts(
            ledger.db_path,
            "raw_quality_to_distill_gate",
            production_event_id=production["event_id"],
        )
    )
    matches = [
        terminal
        for terminal in terminals
        if _matches_success_terminal_runtime_receipt(
            terminal,
            item_id=item_id,
            generation_id=production["generation_id"],
            receipt=receipt,
        )
    ]
    if len(matches) != 1:
        return {
            "verified": False,
            "reason": (
                "terminal_receipt_conflict"
                if terminals
                else "terminal_receipt_missing"
            ),
        }
    cognitive = _verify_distillation_cognitive_receipts(
        ledger,
        event_ids=_cognitive_event_ids(meta),
        task_id=task_id,
        terminal_status=receipt.status,
    )
    if cognitive["deferred"]:
        return {
            "verified": False,
            "reason": "cognitive_terminal_missing",
            "cognitive_deferred": cognitive["deferred"],
        }
    return {
        "verified": True,
        "reason": "verified",
        "runtime_receipt_id": str(matches[0]["receipt_id"]),
        "production_event_id": str(production["event_id"]),
        "generation_id": str(production["generation_id"]),
        "cognitive_deferred": 0,
    }


def record_distillation_generation_superseded(
    config: Any,
    *,
    legacy_task: Mapping[str, Any],
    replacement_task_id: str,
) -> dict[str, Any]:
    """Close only the obsolete runtime generation during a proof migration.

    The underlying cognitive events are deliberately *not* marked consumed by
    ``distill``: the replacement generation still has to reach a real
    prejudgment/terminal result.  This is narrower than an intentional-skip
    write receipt and prevents a source-span compatibility migration from
    masquerading as knowledge consumption.
    """

    session_id, task_id, input_revision, _meta = _distillation_task_identity(legacy_task)
    replacement = str(replacement_task_id or "").strip()
    if not session_id or not task_id or not input_revision or not replacement:
        return {"matched": False, "reason": "task_identity_missing"}
    item_id = runtime_item_id("distill-session", session_id)
    production = _find_distill_production(
        config,
        item_id=item_id,
        task_id=task_id,
        input_revision=input_revision,
    )
    if not production:
        return {"matched": False, "reason": "production_missing"}
    ledger = _read_only_ledger(config)
    if ledger is None:
        return {"matched": False, "reason": "runtime_ledger_missing"}
    existing_receipts = find_runtime_terminal_receipts(
        ledger.db_path,
        "raw_quality_to_distill_gate",
        production_event_id=production["event_id"],
    )
    superseded_receipt_ids = [
        receipt["receipt_id"]
        for receipt in existing_receipts
        if not (
            receipt["consumer_id"] == "core/hephaestus/distillation_engine.py"
            and receipt["status"] == "skipped"
            and receipt["metadata"].get("transition")
            == "verified_source_span_generation_superseded"
            and receipt["metadata"].get("supersession_reason")
            == "source_span_generation_replaced_with_exact_raw"
        )
    ]
    receipt_id = record_runtime_skipped(
        "raw_quality_to_distill_gate",
        source="scripts/reconcile_amphora_source_spans.py",
        consumer_id="core/hephaestus/distillation_engine.py",
        item_id=item_id,
        production_event_id=production["event_id"],
        generation_id=production["generation_id"],
        metadata={
            "transition": "verified_source_span_generation_superseded",
            "replacement_task_id": replacement,
            "recorded_by": "scripts/reconcile_amphora_source_spans.py",
            "supersession_reason": "source_span_generation_replaced_with_exact_raw",
            "supersedes_receipt_ids": superseded_receipt_ids,
        },
        idempotency_key=(
            "raw_quality_to_distill_gate:"
            f"{production['event_id']}:source_span_generation_superseded:v3"
        ),
        config_or_path=config,
    )
    return {
        "matched": receipt_id is not None,
        "reason": "recorded" if receipt_id is not None else "runtime_receipt_deferred",
    }


def _distill_generation_id(task_id: Any, input_revision: Any) -> str:
    return f"distill-task:{str(task_id or '')}:{str(input_revision or '')}"


def _read_only_ledger(config: Any) -> ProducerConsumerLedger | None:
    try:
        return ProducerConsumerLedger(config, initialize=False, read_only=True)
    except FileNotFoundError:
        return None


def _find_distill_production(
    config: Any,
    *,
    item_id: str,
    task_id: str,
    input_revision: str,
) -> dict[str, str] | None:
    ledger = _read_only_ledger(config)
    if ledger is None:
        return None
    return find_produced_event(
        ledger.db_path,
        "raw_quality_to_distill_gate",
        item_id=item_id,
        metadata_match={"task_id": task_id, "input_revision": input_revision},
    )


def record_distillation_write_receipt(config: Any, result: Any, receipt: Any) -> None:
    for fragment in result.fragments:
        action_ref = str(fragment.frontmatter.get("quality_gate_action_ledger_ref") or "")
        if action_ref:
            record_runtime_consumed(
                "distill_quality_to_write_admission",
                source="core/hephaestus/distillation_engine.py",
                item_id=action_ref,
                metadata={
                    "transition": "write_admission_terminal",
                    "receipt_status": receipt.status,
                },
                config_or_path=config,
            )


def record_quality_gate_decisions(
    config: Any, fragments: list[Any], rejected_indices: set[int]
) -> None:
    """Attach runtime receipts to durable quality-gate decisions."""
    for index, fragment in enumerate(fragments):
        action_ref = str(fragment.frontmatter.get("quality_gate_action_ledger_ref") or "")
        if not action_ref:
            continue
        record_runtime_produced(
            "distill_quality_to_write_admission",
            source="core/hephaestus/distillation_quality.py",
            item_id=action_ref,
            intended_consumers=["core/hephaestus/distillation_engine.py"],
            metadata={"transition": "quality_gate_decided"},
            config_or_path=config,
        )
        if index in rejected_indices:
            record_runtime_consumed(
                "distill_quality_to_write_admission",
                source="core/hephaestus/distillation_engine.py",
                item_id=action_ref,
                metadata={"transition": "write_admission_rejected"},
                config_or_path=config,
            )


def persist_distillation_with_receipt(
    engine: Any, result: Any, config: Any
) -> DistillationWriteReceipt:
    """Persist a distillation and attach runtime terminal receipts."""
    from core.hephaestus.distillation_write_receipt import persist_with_receipt

    receipt = persist_with_receipt(engine, result, config)
    record_distillation_write_receipt(engine._runtime_receipt_config, result, receipt)
    return receipt


def record_kg_relation_commit(
    db_path: Any,
    relation: Any,
    relation_id: int,
    reverse_relation_id: int | None,
    *,
    material_action: Any,
    mutation_metadata: Mapping[str, Any],
) -> None:
    from core.trust.formal_cognitive_mutation import FormalCognitiveMutationJournal

    permit = material_action.permit
    FormalCognitiveMutationJournal.for_database(db_path).record(
        asset_kind="kg_relation",
        action="upsert_relation",
        target_ref=permit.target_ref,
        actor=relation.source_method or "system",
        decision=permit.decision_revision_id,
        reason="knowledge_graph.add_relation",
        evidence_refs=[
            f"material-command:{permit.command_id}",
            f"decision-revision:{permit.decision_revision_id}",
            f"material-effect:{permit.effect_id}",
            f"kg-relation:{relation_id}",
            (
                f"kg-reverse-relation:{reverse_relation_id}"
                if reverse_relation_id is not None
                else "kg-reverse-relation:none"
            ),
            *[
                f"{evidence.evidence_type}:{evidence.content}"
                for evidence in (relation.evidence or [])
                if evidence.content
            ],
        ],
        metadata=dict(mutation_metadata),
        material_action=material_action,
    )
    record_runtime_produced(
        "kg_confidence_to_relation_display",
        source="core/kia/knowledge_graph.py",
        item_id=runtime_item_id(
            "kg-relation",
            relation.source,
            relation.target,
            relation.relation_type.value,
        ),
        intended_consumers=["core/kia/kg_exporter.py"],
        metadata={
            "transition": "relation_upsert_committed",
            "confidence": relation.confidence,
        },
        config_or_path=db_path.parent,
    )
