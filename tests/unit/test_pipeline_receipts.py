from core.pipeline_receipts import (
    DistillationWriteReceipt,
    canonical_distillation_failed_terminal_payload,
    canonical_distillation_write_receipt_payload,
    distillation_failed_terminal_sha256,
    distillation_write_receipt_sha256,
)


def test_success_terminal_hash_binds_all_receipt_fields():
    baseline = DistillationWriteReceipt(
        status="intentional_skip",
        terminal_reason="no durable knowledge",
    )
    count_drift = DistillationWriteReceipt(
        status="intentional_skip",
        terminal_reason="no durable knowledge",
        expected_count=1,
        failed_count=1,
    )

    assert canonical_distillation_write_receipt_payload(baseline)[
        "schema_version"
    ] == "mnemos.distillation_write_receipt.v1"
    assert distillation_write_receipt_sha256(baseline) != (
        distillation_write_receipt_sha256(count_drift)
    )


def test_failed_terminal_hash_is_order_stable_and_binds_denominator():
    common = {
        "task_id": "task-1",
        "session_id": "session-1",
        "input_revision": "revision-1",
        "reason": "retry_exhausted:failure",
        "retry_count": 3,
        "max_retries": 3,
    }

    tuple_hash = distillation_failed_terminal_sha256(
        **common,
        cognitive_event_ids=("event-1", "event-2"),
    )
    list_hash = distillation_failed_terminal_sha256(
        **common,
        cognitive_event_ids=["event-1", "event-2"],
    )
    shrunk_hash = distillation_failed_terminal_sha256(
        **common,
        cognitive_event_ids=["event-1"],
    )

    assert tuple_hash == list_hash
    assert tuple_hash != shrunk_hash


def test_failed_terminal_payload_binds_identity_reason_and_retry_budget():
    payload = canonical_distillation_failed_terminal_payload(
        task_id="task-1",
        session_id="session-1",
        input_revision="revision-1",
        reason="retry_exhausted:failure",
        retry_count=3,
        max_retries=3,
        cognitive_event_ids=["event-1"],
    )

    assert payload == {
        "schema_version": "mnemos.distillation_failed_terminal.v1",
        "task_id": "task-1",
        "session_id": "session-1",
        "input_revision": "revision-1",
        "reason": "retry_exhausted:failure",
        "retry_count": 3,
        "max_retries": 3,
        "cognitive_event_ids": ["event-1"],
    }
    changed = dict(payload)
    changed["max_retries"] = 4
    assert changed != payload
