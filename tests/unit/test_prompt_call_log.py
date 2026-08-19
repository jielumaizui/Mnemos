import ast
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from core.telemetry.prompt_call_log import (
    ModelCallBudgetExceeded,
    ModelCallLedger,
    ModelCallLedgerInvariantError,
    ModelCallSubjectFrozen,
    MeteredProviderUsage,
    PromptCallLog,
    metered_provider_usage,
)


def test_production_model_call_error_codes_are_explicitly_safe_categories():
    """Every boundary category must remain diagnosable without storing error text."""
    repository_root = Path(__file__).resolve().parents[2]
    source_paths = [
        *sorted((repository_root / "core").rglob("*.py")),
        *sorted((repository_root / "scripts").rglob("*.py")),
    ]
    observed: dict[str, list[str]] = {}
    for path in source_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"release", "preserve_incurred"}
            ):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "error_code"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    observed.setdefault(keyword.value.value, []).append(
                        f"{path.relative_to(repository_root)}:{node.lineno}"
                    )

    expected_verify_smoke_codes = {
        "verify_llm_smoke_usage_missing",
        "verify_llm_smoke_exception",
        "verify_llm_smoke_pre_dispatch_exception",
        "verify_embedding_smoke_usage_missing",
        "verify_embedding_smoke_exception",
        "verify_embedding_smoke_pre_dispatch_exception",
        "verify_rerank_smoke_usage_missing",
        "verify_rerank_smoke_exception",
        "verify_rerank_smoke_pre_dispatch_exception",
    }
    assert expected_verify_smoke_codes <= set(observed)

    from core.telemetry.prompt_call_log import _SAFE_ERROR_CODES

    missing = {
        code: locations
        for code, locations in observed.items()
        if code not in _SAFE_ERROR_CODES
    }
    assert not missing, (
        "production ModelCallReservation error categories would be silently "
        f"redacted: {missing}"
    )


class LedgerConfig:
    def __init__(self, root, *, daily_cost_cap: float = 10.0):
        self.data_dir = root
        self.database_dir = root
        self._data = {
            "model_call_ledger": {"daily_cost_cap": daily_cost_cap},
            "llm": {
                "provider_prices": {
                    "test": {"model": {"input": 0.1, "output": 0.2}}
                }
            },
        }

    def get(self, key, default=None):
        value = self._data
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


def _ledger(tmp_path, *, daily_cost_cap: float = 10.0):
    config = LedgerConfig(tmp_path, daily_cost_cap=daily_cost_cap)
    return ModelCallLedger.for_config(config), config


def _usage(input_tokens: int, output_tokens: int = 0, *, request_id: str = "request-1"):
    usage = metered_provider_usage(
        {"prompt_tokens": input_tokens, "completion_tokens": output_tokens},
        request_id=request_id,
        output_required=True,
    )
    assert usage is not None
    return usage


def test_model_call_ledger_reserves_then_settles_actual_cost_without_prompt_preview(tmp_path):
    ledger, config = _ledger(tmp_path)
    marker = "DUMMY_CREDENTIAL_" + "PAYLOAD"
    run_id = ledger.start_run("run-1", cost_budget=1.0, subject_scope=("session", "test-1"))
    provider_payload = f"visible prompt {marker}"

    reservation = ledger.reserve(
        run_id=run_id,
        operation="distill_extract",
        provider="test",
        model="model",
        input_text=provider_payload,
        input_tokens=len(provider_payload.encode("utf-8")),
        output_tokens=10,
    )
    reservation.mark_dispatched()
    reservation.settle(usage=_usage(6, 4), latency_ms=42)

    summary = ledger.run_summary(run_id)
    assert summary["reserved_cost"] == pytest.approx(
        (len(provider_payload.encode("utf-8")) / 1000) * 0.1 + (10 / 1000) * 0.2
    )
    assert summary["actual_cost"] == pytest.approx(0.0014)
    assert summary["refund_cost"] == pytest.approx(summary["reserved_cost"] - 0.0014)
    assert summary["effective_cost"] == pytest.approx(0.0014)
    assert summary["states"] == {"settled": 1}

    with sqlite3.connect(str(ledger.db_path)) as conn:
        row = conn.execute("SELECT * FROM model_call_entries").fetchone()
        columns = {item[1] for item in conn.execute("PRAGMA table_info(model_call_entries)")}
    assert marker not in " ".join(str(value) for value in row)
    assert not columns & {"prompt", "prompt_summary", "prompt_preview", "response", "response_preview"}
    inspection = ModelCallLedger.inspect(config)
    assert inspection["settled_cost_without_provider_usage"] == 0
    assert inspection["sensitive_prompt_preview"] == 0
    assert "path" not in inspection
    assert str(tmp_path) not in json.dumps(inspection)


@pytest.mark.parametrize("metadata_field", ("operation", "provider", "model", "cache_status"))
def test_runtime_model_call_metadata_rejects_arbitrary_text_without_persisting_it(
    tmp_path, metadata_field
):
    """Runtime accounting metadata is a bounded vocabulary, never a text side channel."""
    ledger, config = _ledger(tmp_path)
    marker = f"RAW_RUNTIME_{metadata_field.upper()}_" + ("x" * 4_096)
    run_id = ledger.start_run(
        f"metadata-{metadata_field}", subject_scope=("session", "metadata-boundary")
    )
    values = {
        "operation": "embedding",
        "provider": "test",
        "model": "model",
        "cache_status": "miss",
    }
    values[metadata_field] = marker
    if metadata_field == "provider":
        config._data["llm"]["provider_prices"][marker] = {
            "model": {"input": 0.1, "output": 0.2}
        }
    elif metadata_field == "model":
        config._data["llm"]["provider_prices"]["test"][marker] = {
            "input": 0.1,
            "output": 0.2,
        }

    with pytest.raises(ModelCallLedgerInvariantError):
        ledger.reserve(
            run_id=run_id,
            operation=values["operation"],
            provider=values["provider"],
            model=values["model"],
            input_text="x",
            input_tokens=1,
            cache_status=values["cache_status"],
        )

    for candidate in (
        ledger.db_path,
        Path(str(ledger.db_path) + "-wal"),
        Path(str(ledger.db_path) + "-shm"),
    ):
        if candidate.exists():
            assert marker.encode("utf-8") not in candidate.read_bytes()


def test_provider_identifiers_are_domain_separated_nonreversible_references(tmp_path):
    """External request/usage IDs must not become durable raw provider metadata."""
    ledger, _ = _ledger(tmp_path)
    marker = "EXTERNAL_PROVIDER_IDENTIFIER_" + ("y" * 4_096)
    run_id = ledger.start_run("external-ids", subject_scope=("session", "external-ids"))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.mark_dispatched()
    usage = metered_provider_usage(
        {
            "prompt_tokens": 1,
            "completion_tokens": 0,
            "usage_id": marker,
        },
        request_id=marker,
        output_required=True,
    )
    assert usage is not None
    reservation.settle(usage=usage)

    with sqlite3.connect(str(ledger.db_path)) as conn:
        provider_usage_id, request_id, meter_receipt = conn.execute(
            "SELECT provider_usage_id, request_id, metered_usage_receipt "
            "FROM model_call_entries"
        ).fetchone()
    assert provider_usage_id != marker
    assert request_id != marker
    assert provider_usage_id != request_id
    assert marker not in " ".join((provider_usage_id, request_id, meter_receipt))
    for candidate in (
        ledger.db_path,
        Path(str(ledger.db_path) + "-wal"),
        Path(str(ledger.db_path) + "-shm"),
    ):
        if candidate.exists():
            assert marker.encode("utf-8") not in candidate.read_bytes()


def test_model_call_ledger_preserves_dispatched_cost_when_provider_usage_is_absent(tmp_path):
    ledger, config = _ledger(tmp_path)
    run_id = ledger.start_run("run-no-usage", subject_scope=("session", "no-usage"))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=10,
    )
    reservation.mark_dispatched()
    reservation.preserve_incurred(error_code="provider_usage_missing")

    inspection = ModelCallLedger.inspect(config)
    assert inspection["unverified_provider_usage"] == 1
    assert inspection["settled_cost_without_provider_usage"] == 0
    assert inspection["status"] == "degraded"
    assert ledger.run_summary("run-no-usage")["states"] == {"incurred_unknown": 1}


def test_model_call_ledger_refuses_settlement_before_dispatch(tmp_path):
    ledger, _ = _ledger(tmp_path)
    run_id = ledger.start_run("run-dispatch-required", subject_scope=("source", "unit-test"))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )

    with pytest.raises(Exception, match="dispatched reservation"):
        reservation.settle(usage=_usage(1), latency_ms=1)

    assert ledger.run_summary("run-dispatch-required")["states"] == {"reserved": 1}


def test_model_call_ledger_releases_only_undispatched_reservations(tmp_path):
    ledger, _ = _ledger(tmp_path)
    run_id = ledger.start_run("run-release", subject_scope=("source", "unit-test"))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="rerank",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=10,
    )
    reservation.release(error_code="client_setup_failed")

    summary = ledger.run_summary("run-release")
    assert summary["states"] == {"released": 1}
    assert summary["effective_cost"] == 0.0
    with pytest.raises(Exception):
        reservation.mark_dispatched()


def test_model_call_ledger_enforces_daily_cap_before_dispatch(tmp_path):
    ledger, _ = _ledger(tmp_path, daily_cost_cap=0.0001)
    run_id = ledger.start_run("run-cap", subject_scope=("source", "unit-test"))

    with pytest.raises(ModelCallBudgetExceeded):
        ledger.reserve(
            run_id=run_id,
            operation="intent_router",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=10,
        )

    assert ledger.recent() == []


def test_model_call_ledger_marks_provider_actual_cost_overrun_as_blocking(tmp_path):
    ledger, config = _ledger(tmp_path, daily_cost_cap=0.1)
    run_id = ledger.start_run(
        "run-provider-overage", cost_budget=0.1, subject_scope=("session", "overage")
    )
    reservation = ledger.reserve(
        run_id=run_id,
        operation="distill_extract",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.mark_dispatched()
    reservation.settle(usage=_usage(1000, 500, request_id="overage"), latency_ms=1)

    summary = ledger.run_summary(run_id)
    assert summary["actual_cost"] == pytest.approx(0.2)
    assert summary["effective_cost"] == pytest.approx(0.2)
    assert summary["states"] == {"incurred_overrun": 1}
    assert ModelCallLedger.inspect(config)["daily_effective_cost"] == pytest.approx(0.2)
    assert ModelCallLedger.inspect(config)["reservation_cost_overrun"] == 1
    with pytest.raises(ModelCallLedgerInvariantError, match="unresolved provider cost overrun"):
        ledger.reserve(
            run_id=run_id,
            operation="distill_correct",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=1,
        )


def test_model_call_ledger_deletes_hashed_subject_scope_without_persisting_subject_value(tmp_path):
    ledger, _ = _ledger(tmp_path)
    subject_value = "session-with-private-identifier"
    run_id = ledger.start_run(
        "opaque-run-id",
        subject_scope=("session", subject_value),
    )
    reservation = ledger.reserve(
        run_id=run_id,
        operation="distill_extract",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.mark_dispatched()
    reservation.settle(usage=_usage(1, 0, request_id="subject-delete"))

    with sqlite3.connect(str(ledger.db_path)) as conn:
        row = conn.execute(
            "SELECT scope_kind, subject_hash FROM model_call_run_subjects WHERE run_id=?",
            (run_id,),
        ).fetchone()
        serialized = " ".join(
            str(value) for result in conn.execute("SELECT * FROM model_call_run_subjects") for value in result
        )
    assert row is not None
    assert row[0] == "session"
    assert row[1] == hashlib.sha256(f"session:{subject_value}".encode("utf-8")).hexdigest()
    assert subject_value not in serialized

    ledger.freeze_subject_scope("session", subject_value)
    deletion = ledger.delete_subject_scope("session", subject_value)

    assert deletion == {
        "status": "applied",
        "matched_run_count": 1,
        "deleted_entry_count": 1,
        "deleted_run_count": 1,
    }
    assert ledger.run_summary(run_id) == {"run_id": run_id, "exists": False}
    assert subject_value.encode("utf-8") not in ledger.db_path.read_bytes()


def test_model_call_delete_and_retention_switch_to_private_scrub_only_for_apply(tmp_path):
    """Dry runs stay read-only; physical deletion cannot leave a live WAL behind."""

    def _configure_wal_without_secure_delete() -> None:
        conn = sqlite3.connect(str(ledger.db_path))
        try:
            assert str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() == "wal"
            conn.execute("PRAGMA secure_delete=OFF")
            conn.commit()
        finally:
            conn.close()

    def _journal_and_entry_count() -> tuple[str, int]:
        conn = sqlite3.connect(str(ledger.db_path))
        try:
            return (
                str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
                int(conn.execute("SELECT COUNT(*) FROM model_call_entries").fetchone()[0]),
            )
        finally:
            conn.close()

    ledger, _ = _ledger(tmp_path)
    subject = "private-scrub-delete"
    run_id = ledger.start_run("private-scrub-delete", subject_scope=("session", subject))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.mark_dispatched()
    reservation.settle(usage=_usage(1, 0, request_id="private-scrub-delete"))
    ledger.freeze_subject_scope("session", subject)

    _configure_wal_without_secure_delete()

    dry_run = ledger.delete_subject_scope("session", subject, dry_run=True)
    assert dry_run["status"] == "dry_run"
    assert _journal_and_entry_count() == ("wal", 1)

    assert ledger.delete_subject_scope("session", subject)["status"] == "applied"
    assert _journal_and_entry_count() == ("delete", 0)

    retention_run = ledger.start_run(
        "private-scrub-retention", subject_scope=("session", "private-scrub-retention")
    )
    retained = ledger.reserve(
        run_id=retention_run,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    retained.mark_dispatched()
    retained.settle(usage=_usage(1, 0, request_id="private-scrub-retention"))
    conn = sqlite3.connect(str(ledger.db_path))
    try:
        conn.execute(
            "UPDATE model_call_entries SET created_at='2000-01-01T00:00:00+00:00' "
            "WHERE run_id=?",
            (retention_run,),
        )
        conn.commit()
        assert str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() == "wal"
        conn.execute("PRAGMA secure_delete=OFF")
        conn.commit()
    finally:
        conn.close()

    assert ledger.cleanup_older_than(30, dry_run=True) == 1
    assert _journal_and_entry_count() == ("wal", 1)

    assert ledger.cleanup_older_than(30) == 1
    assert _journal_and_entry_count() == ("delete", 0)


def test_model_call_health_read_closes_sqlite_handle_before_private_scrub(tmp_path):
    """A read-only health probe cannot retain the WAL reader needed for erasure."""
    ledger, config = _ledger(tmp_path)
    subject = "health-before-erase"
    run_id = ledger.start_run("health-before-erase", subject_scope=("session", subject))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.mark_dispatched()
    reservation.settle(usage=_usage(1, 0, request_id="health-before-erase"))
    ledger.freeze_subject_scope("session", subject)

    conn = sqlite3.connect(str(ledger.db_path))
    try:
        assert str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() == "wal"
    finally:
        conn.close()

    assert ModelCallLedger.inspect(config)["status"] == "ok"
    assert ledger.delete_subject_scope("session", subject)["status"] == "applied"
    conn = sqlite3.connect(str(ledger.db_path))
    try:
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "delete"
    finally:
        conn.close()


def test_model_call_ledger_rejects_unattributed_new_runs_and_reservations(tmp_path):
    ledger, _ = _ledger(tmp_path)

    with pytest.raises(ModelCallLedgerInvariantError, match="explicit subject"):
        ledger.start_run("missing-subject")
    with pytest.raises(ModelCallLedgerInvariantError, match="pre-created attributed"):
        ledger.reserve(
            run_id=None,
            operation="embedding",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=1,
        )


def test_freeze_is_a_durable_dispatch_barrier_and_releases_undispatched_entry(tmp_path):
    ledger, _ = _ledger(tmp_path)
    subject = "frozen-session"
    run_id = ledger.start_run("freeze-dispatch", subject_scope=("session", subject))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="distill_extract",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )

    ledger.freeze_subject_scope("session", subject)
    with pytest.raises(ModelCallSubjectFrozen):
        reservation.mark_dispatched()
    # Normal boundary cleanup must not mask the durable freeze with a second
    # release transition after the ledger has already released the entry.
    reservation.release()
    assert ledger.run_summary(run_id)["states"] == {"released": 1}
    with pytest.raises(ModelCallSubjectFrozen):
        ledger.start_run("freeze-after", subject_scope=("session", subject))


def test_reservation_cannot_be_marked_dispatched_twice(tmp_path):
    ledger, _ = _ledger(tmp_path)
    run_id = ledger.start_run("single-dispatch", subject_scope=("session", "single"))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.mark_dispatched()
    with pytest.raises(ModelCallLedgerInvariantError):
        reservation.mark_dispatched()
    # A second process can hold another wrapper for the same durable entry;
    # the SQL transition itself must reject that race as well.
    with pytest.raises(ModelCallLedgerInvariantError):
        ledger._mark_dispatched(reservation.entry_id)
    reservation.preserve_incurred(error_code="test_after_single_dispatch")


def test_entry_level_subject_delete_preserves_other_entries_in_a_shared_run(tmp_path):
    ledger, _ = _ledger(tmp_path)
    first_path = "/assets/private-a.md"
    second_path = "/assets/private-b.md"
    run_id = ledger.start_run("batch-root", subject_scope=("source", "embedding-indexer"))
    first = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
        subject_scopes=[("path", first_path), ("path", second_path)],
    )
    first.mark_dispatched()
    first.settle(usage=_usage(1, 0, request_id="batch-one"))
    second = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
        subject_scopes=[("path", second_path)],
    )
    second.mark_dispatched()
    second.settle(usage=_usage(1, 0, request_id="batch-two"))

    ledger.freeze_subject_scope("path", first_path)
    deletion = ledger.delete_subject_scope("path", first_path)

    assert deletion["status"] == "applied"
    assert deletion["deleted_entry_count"] == 1
    assert deletion["deleted_run_count"] == 0
    assert ledger.run_summary(run_id)["entry_count"] == 1
    with sqlite3.connect(str(ledger.db_path)) as conn:
        remaining = int(conn.execute("SELECT COUNT(*) FROM model_call_entries").fetchone()[0])
    assert remaining == 1


def test_delete_blocks_dispatched_inflight_entry_until_usage_is_preserved(tmp_path):
    ledger, _ = _ledger(tmp_path)
    subject = "inflight-session"
    run_id = ledger.start_run("inflight", subject_scope=("session", subject))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="distill_extract",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.mark_dispatched()
    ledger.freeze_subject_scope("session", subject)

    blocked = ledger.delete_subject_scope("session", subject)
    assert blocked["status"] == "blocked"
    assert blocked["error"] == "inflight_model_call_entries"

    reservation.preserve_incurred(error_code="response_unavailable_after_freeze")
    applied = ledger.delete_subject_scope("session", subject)
    assert applied["status"] == "applied"


def test_deleted_or_retained_cost_cannot_reset_today_budget(tmp_path):
    ledger, _ = _ledger(tmp_path, daily_cost_cap=0.0015)
    subject = "budget-delete-session"
    run_id = ledger.start_run("budget-delete", subject_scope=("session", subject))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=10,
    )
    reservation.mark_dispatched()
    reservation.settle(usage=_usage(10, 0, request_id="budget-delete"))
    ledger.freeze_subject_scope("session", subject)
    assert ledger.delete_subject_scope("session", subject)["status"] == "applied"
    assert ModelCallLedger.inspect(ledger._config)["daily_tombstoned_spend"] == pytest.approx(0.001)

    next_run = ledger.start_run("budget-next", subject_scope=("session", "new-session"))
    with pytest.raises(ModelCallBudgetExceeded):
        ledger.reserve(
            run_id=next_run,
            operation="embedding",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=10,
        )


def test_deleted_run_tombstone_is_hashed_and_permanently_retires_the_run_id(tmp_path):
    ledger, _ = _ledger(tmp_path)
    private_run_id = "person@example.invalid"
    subject = "run-tombstone-subject"
    run_id = ledger.start_run(
        private_run_id,
        cost_budget=1.0,
        subject_scope=("session", subject),
    )
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.mark_dispatched()
    reservation.settle(usage=_usage(1, 0, request_id="run-tombstone"))
    ledger.freeze_subject_scope("session", subject)
    assert ledger.delete_subject_scope("session", subject)["status"] == "applied"

    with sqlite3.connect(str(ledger.db_path)) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(model_call_run_spend_tombstones)")
        }
        tombstones = list(conn.execute("SELECT * FROM model_call_run_spend_tombstones"))
        serialized = " ".join(str(value) for row in tombstones for value in row)
    assert columns == {"run_id_digest", "effective_cost", "deleted_entry_count", "updated_at"}
    assert private_run_id not in serialized
    assert len(tombstones) == 1
    with pytest.raises(ModelCallLedgerInvariantError, match="permanently retired"):
        ledger.start_run(private_run_id, subject_scope=("session", "another-subject"))


def test_negative_or_nonfinite_prices_cannot_create_budget_credit(tmp_path):
    ledger, config = _ledger(tmp_path, daily_cost_cap=0.0)
    config._data["llm"]["provider_prices"]["test"]["model"]["input"] = -100
    run_id = ledger.start_run("negative-price", cost_budget=0.0, subject_scope=("session", "price"))
    with pytest.raises(ModelCallLedgerInvariantError, match="configured input price"):
        ledger.reserve(
            run_id=run_id,
            operation="embedding",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=10,
        )


def test_unknown_or_unapproved_zero_price_cannot_bypass_cost_caps(tmp_path):
    ledger, config = _ledger(tmp_path, daily_cost_cap=0.001)
    run_id = ledger.start_run("unpriced", cost_budget=0.001, subject_scope=("session", "price"))
    with pytest.raises(ModelCallLedgerInvariantError, match="price is missing"):
        ledger.reserve(
            run_id=run_id,
            operation="embedding",
            provider="unpriced-provider",
            model="unpriced-model",
            input_text="x",
            input_tokens=10_000_000,
            output_tokens=10_000_000,
        )

    config._data["llm"]["provider_prices"]["test"]["model"] = {"input": 0.0, "output": 0.0}
    with pytest.raises(ModelCallLedgerInvariantError, match="zero model-call price"):
        ledger.reserve(
            run_id=run_id,
            operation="embedding",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=10,
        )

    # A partially-zero table is just as dangerous: embeddings can consume the
    # zero input rate and completions can consume a zero output rate.
    config._data["llm"]["provider_prices"]["test"]["model"] = {
        "input": 0.0,
        "output": 0.2,
    }
    with pytest.raises(ModelCallLedgerInvariantError, match="zero model-call price"):
        ledger.reserve(
            run_id=run_id,
            operation="embedding",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=10,
        )
    config._data["llm"]["provider_prices"]["test"]["model"] = {
        "input": 0.1,
        "output": 0.0,
    }
    with pytest.raises(ModelCallLedgerInvariantError, match="zero model-call price"):
        ledger.reserve(
            run_id=run_id,
            operation="distill_extract",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=10,
            output_tokens=10,
        )

    config._data["llm"]["provider_prices"]["test"]["model"] = {
        "input": 0.0,
        "output": 0.0,
    }
    config._data["model_call_ledger"]["allow_explicit_zero_price"] = True
    config._data["llm"]["provider_prices"]["test"] = {
        "default": {"input": 0.0, "output": 0.0}
    }
    with pytest.raises(ModelCallLedgerInvariantError, match="zero model-call price"):
        ledger.reserve(
            run_id=run_id,
            operation="embedding",
            provider="test",
            model="unreviewed-new-model",
            input_text="x",
            input_tokens=10,
        )
    config._data["llm"]["provider_prices"]["test"] = {
        "model": {"input": 0.0, "output": 0.0}
    }
    zero_price = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=10,
    )
    assert zero_price.reserved_cost == 0.0
    zero_price.release()

    config._data["llm"]["provider_prices"]["test"]["model"]["input"] = float("nan")
    with pytest.raises(ModelCallLedgerInvariantError, match="configured input price"):
        ledger.reserve(
            run_id=run_id,
            operation="embedding",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=10,
        )


def test_tiny_positive_costs_accumulate_without_per_entry_rounding_bypass(tmp_path):
    ledger, config = _ledger(tmp_path, daily_cost_cap=1e-12)
    config._data["llm"]["provider_prices"]["test"]["model"] = {
        "input": 1e-10,
        "output": 0.2,
    }

    for index in range(10):
        run_id = ledger.start_run(
            f"tiny-cost-{index}",
            subject_scope=("session", f"tiny-cost-{index}"),
        )
        reservation = ledger.reserve(
            run_id=run_id,
            operation="embedding",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=1,
        )
        reservation.mark_dispatched()
        reservation.settle(usage=_usage(1, 0, request_id=f"tiny-{index}"))

    inspection = ModelCallLedger.inspect(config)
    assert inspection["daily_effective_cost"] == pytest.approx(1e-12)
    next_run = ledger.start_run("tiny-cost-over", subject_scope=("session", "tiny-over"))
    with pytest.raises(ModelCallBudgetExceeded):
        ledger.reserve(
            run_id=next_run,
            operation="embedding",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=1,
        )


def test_budget_rejects_every_representable_positive_monetary_excess(tmp_path):
    """A relative float tolerance must not become a sub-cent budget bypass."""
    ledger, config = _ledger(tmp_path, daily_cost_cap=1e-12)
    config._data["llm"]["provider_prices"]["test"]["model"] = {
        # One input token costs a representable amount only 5e-13 relatively
        # above the cap. Arithmetic-equality tolerance is okay for persisted
        # invariants, but a budget decision must still reject it.
        "input": 1.0000000000005e-9,
        "output": 0.2,
    }
    run_id = ledger.start_run("tiny-excess", subject_scope=("session", "tiny-excess"))
    with pytest.raises(ModelCallBudgetExceeded):
        ledger.reserve(
            run_id=run_id,
            operation="embedding",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=1,
        )


def test_retention_also_tombstones_current_day_cost_before_removal(tmp_path):
    ledger, _ = _ledger(tmp_path, daily_cost_cap=0.0015)
    run_id = ledger.start_run("retention", subject_scope=("session", "retention-session"))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=10,
    )
    reservation.mark_dispatched()
    reservation.settle(usage=_usage(10, 0, request_id="retention"))

    assert ledger.cleanup_older_than(0) == 1
    next_run = ledger.start_run("retention-next", subject_scope=("session", "new-session"))
    with pytest.raises(ModelCallBudgetExceeded):
        ledger.reserve(
            run_id=next_run,
            operation="embedding",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=10,
        )


def test_partial_subject_delete_cannot_reset_the_same_run_budget(tmp_path):
    ledger, _ = _ledger(tmp_path, daily_cost_cap=1.0)
    first_path = "/assets/run-budget-a.md"
    second_path = "/assets/run-budget-b.md"
    run_id = ledger.start_run(
        "partial-delete-budget",
        cost_budget=0.0015,
        subject_scope=("source", "embedding-indexer"),
    )
    first = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=10,
        subject_scopes=[("path", first_path)],
    )
    first.mark_dispatched()
    first.settle(usage=_usage(10, 0, request_id="partial-delete-first"))
    second = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
        subject_scopes=[("path", second_path)],
    )
    second.mark_dispatched()
    second.settle(usage=_usage(1, 0, request_id="partial-delete-second"))

    ledger.freeze_subject_scope("path", first_path)
    assert ledger.delete_subject_scope("path", first_path)["status"] == "applied"
    summary = ledger.run_summary(run_id)
    assert summary["tombstoned_effective_cost"] == pytest.approx(0.001)
    assert summary["effective_cost"] == pytest.approx(0.0011)

    with pytest.raises(ModelCallBudgetExceeded):
        ledger.reserve(
            run_id=run_id,
            operation="embedding",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=5,
            subject_scopes=[("path", second_path)],
        )


def test_retention_blocks_an_aged_dispatched_call_until_it_can_settle(tmp_path):
    ledger, _ = _ledger(tmp_path)
    run_id = ledger.start_run("retention-inflight", subject_scope=("session", "retention-inflight"))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="distill_extract",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.mark_dispatched()
    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.execute(
            "UPDATE model_call_entries SET created_at='2000-01-01T00:00:00+00:00' "
            "WHERE entry_id=?",
            (reservation.entry_id,),
        )

    with pytest.raises(ModelCallLedgerInvariantError, match="dispatched model-call reservations"):
        ledger.cleanup_older_than(30)
    reservation.settle(usage=_usage(1, 0, request_id="retention-inflight"))
    assert ledger.run_summary(run_id)["states"] == {"settled": 1}


def test_retention_cannot_delete_an_unrelated_empty_open_run(tmp_path):
    ledger, _ = _ledger(tmp_path)
    old_run = ledger.start_run("retention-old", subject_scope=("session", "retention-old"))
    old = ledger.reserve(
        run_id=old_run,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    old.mark_dispatched()
    old.settle(usage=_usage(1, 0, request_id="retention-old"))
    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.execute(
            "UPDATE model_call_entries SET created_at='2000-01-01T00:00:00+00:00' "
            "WHERE run_id=?",
            (old_run,),
        )

    open_run = ledger.start_run("retention-open", subject_scope=("session", "retention-open"))
    assert ledger.cleanup_older_than(30) == 1
    open_reservation = ledger.reserve(
        run_id=open_run,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    open_reservation.release()


def test_metered_usage_must_come_from_the_provider_meter_factory(tmp_path):
    with pytest.raises(ModelCallLedgerInvariantError, match="provider-meter receipt factory"):
        MeteredProviderUsage(
            input_tokens=1,
            output_tokens=0,
            metered_usage_receipt="forged",
            provider_usage_id="forged",
        )
    with pytest.raises(ModelCallLedgerInvariantError, match="cannot be negative"):
        metered_provider_usage(
            {"prompt_tokens": -1, "completion_tokens": 0},
            request_id="negative-meter",
            output_required=True,
        )
    issued = _usage(1, 0, request_id="issued")
    forged = object.__new__(MeteredProviderUsage)
    assert forged.is_factory_issued is False
    # No token/capability slot is carried by a public receipt object, so a
    # caller cannot clone a valid usage receipt by copying its attributes.
    with pytest.raises(AttributeError):
        object.__setattr__(forged, "_issuer_capability", object())

    ledger, _ = _ledger(tmp_path)
    run_id = ledger.start_run("forged-meter", subject_scope=("session", "meter"))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.mark_dispatched()
    with pytest.raises(ModelCallLedgerInvariantError, match="metered provider usage receipt"):
        reservation.settle(usage=forged)
    reservation.settle(usage=issued)


def test_metered_usage_rejects_fractional_contradictory_or_unidentified_provider_facts(tmp_path):
    del tmp_path
    assert metered_provider_usage(
        {"prompt_tokens": 0.9, "completion_tokens": 0.9},
        request_id="provider-request",
        output_required=True,
    ) is None
    assert metered_provider_usage(
        {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 100},
        request_id="provider-request",
        output_required=True,
    ) is None
    assert metered_provider_usage(
        {"prompt_tokens": 1, "total_tokens": 100},
        request_id="provider-request",
        output_required=False,
    ) is None
    assert metered_provider_usage(
        {"prompt_tokens": 1, "completion_tokens": 1},
        output_required=True,
    ) is None
    usage = metered_provider_usage(
        {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "usage_id": "usage-1"},
        output_required=True,
    )
    assert usage is not None
    assert usage.provider_usage_id == "usage-1"


def test_runtime_rejects_noncanonical_subject_hashes_and_reversible_columns(tmp_path):
    ledger, _ = _ledger(tmp_path)
    run_id = ledger.start_run("schema-subject", subject_scope=("session", "schema-subject"))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.execute(
            "UPDATE model_call_entry_subjects SET subject_hash='not-a-canonical-digest' "
            "WHERE entry_id=?",
            (reservation.entry_id,),
        )

    with pytest.raises(ModelCallLedgerInvariantError, match="invalid_subject_binding"):
        ModelCallLedger(ledger.db_path)

    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.execute(
            "UPDATE model_call_entry_subjects SET subject_hash=? WHERE entry_id=?",
            (hashlib.sha256(b"session:schema-subject").hexdigest(), reservation.entry_id),
        )
        conn.execute("ALTER TABLE model_call_entries ADD COLUMN prompt_preview TEXT")

    with pytest.raises(ModelCallLedgerInvariantError, match="forbidden_column"):
        # ``initialize=False`` may avoid creating an absent path, but it
        # cannot bypass validation for an existing malformed ledger.
        ModelCallLedger(ledger.db_path, initialize=False)


def test_runtime_rejects_raw_digest_or_state_that_would_erase_dispatched_spend(tmp_path):
    ledger, _ = _ledger(tmp_path)
    run_id = ledger.start_run("runtime-semantic", subject_scope=("session", "semantic"))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.mark_dispatched()
    reservation.settle(usage=_usage(1, 0, request_id="semantic"))
    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.execute(
            "UPDATE model_call_entries SET input_digest='raw prompt secret' WHERE entry_id=?",
            (reservation.entry_id,),
        )

    with pytest.raises(ModelCallLedgerInvariantError, match="invalid_digest:model_call_entries.input_digest"):
        ModelCallLedger(ledger.db_path)

    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.execute(
            "UPDATE model_call_entries SET input_digest=?, lifecycle_state='released', "
            "actual_cost=NULL, actual_input_tokens=NULL, actual_output_tokens=NULL, "
            "actual_total_tokens=NULL, refund_cost=reserved_cost WHERE entry_id=?",
            (hashlib.sha256(b"request-metadata").hexdigest(), reservation.entry_id),
        )
        conn.execute(
            "UPDATE model_call_entries SET request_dispatched=1 WHERE entry_id=?",
            (reservation.entry_id,),
        )
    with pytest.raises(ModelCallLedgerInvariantError, match="invalid_monetary_state:model_call_entries"):
        ModelCallLedger(ledger.db_path)


def test_runtime_rejects_settlement_refund_that_breaks_exact_conservation(tmp_path):
    ledger, _ = _ledger(tmp_path)
    run_id = ledger.start_run("refund-conservation", subject_scope=("session", "refund-check"))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=10,
    )
    reservation.mark_dispatched()
    reservation.settle(usage=_usage(1, 0, request_id="refund-check"))

    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.execute(
            "UPDATE model_call_entries SET refund_cost=0 WHERE entry_id=?",
            (reservation.entry_id,),
        )

    with pytest.raises(ModelCallLedgerInvariantError, match="invalid_monetary_state:model_call_entries"):
        ModelCallLedger(ledger.db_path)


def test_runtime_rejects_refund_on_a_provider_cost_overrun(tmp_path):
    ledger, _ = _ledger(tmp_path)
    run_id = ledger.start_run("overrun-refund", subject_scope=("session", "overrun-refund"))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.mark_dispatched()
    reservation.settle(usage=_usage(1_000, 500, request_id="overrun-refund"))

    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.execute(
            "UPDATE model_call_entries SET refund_cost=0.0001 WHERE entry_id=?",
            (reservation.entry_id,),
        )

    with pytest.raises(ModelCallLedgerInvariantError, match="invalid_monetary_state:model_call_entries"):
        ModelCallLedger(ledger.db_path)


def _set_reservation_dispatch_time(ledger, entry_id: str, dispatched_at: str) -> None:
    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.execute(
            "UPDATE model_call_entries SET dispatched_at=? WHERE entry_id=?",
            (dispatched_at, entry_id),
        )


def test_stale_dispatched_reservation_recovers_conservatively_before_new_reservation(
    tmp_path,
):
    ledger, config = _ledger(tmp_path)
    run_id = ledger.start_run("stale-dispatch", subject_scope=("session", "stale-dispatch"))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=3,
        output_tokens=2,
        subject_scopes=(("source", "stale-page"),),
    )
    reservation.mark_dispatched()
    _set_reservation_dispatch_time(
        ledger,
        reservation.entry_id,
        "2000-01-01T00:00:00+00:00",
    )

    with sqlite3.connect(str(ledger.db_path)) as conn:
        original_subjects = conn.execute(
            "SELECT scope_kind, subject_hash FROM model_call_entry_subjects "
            "WHERE entry_id=? ORDER BY scope_kind, subject_hash",
            (reservation.entry_id,),
        ).fetchall()

    inspection = ModelCallLedger.inspect(config)
    assert inspection["status"] == "degraded"
    assert inspection["stale_inflight_model_call_entry_count"] == 1

    next_run = ledger.start_run("after-stale", subject_scope=("session", "after-stale"))
    next_reservation = ledger.reserve(
        run_id=next_run,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )

    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        recovered = conn.execute(
            "SELECT * FROM model_call_entries WHERE entry_id=?",
            (reservation.entry_id,),
        ).fetchone()
        recovered_subjects = conn.execute(
            "SELECT scope_kind, subject_hash FROM model_call_entry_subjects "
            "WHERE entry_id=? ORDER BY scope_kind, subject_hash",
            (reservation.entry_id,),
        ).fetchall()

    assert recovered is not None
    assert recovered["lifecycle_state"] == "incurred_unknown"
    assert recovered["request_dispatched"] == 1
    assert recovered["dispatched_at"] == "2000-01-01T00:00:00+00:00"
    assert recovered["actual_input_tokens"] is None
    assert recovered["actual_output_tokens"] is None
    assert recovered["actual_total_tokens"] == 5
    assert recovered["actual_cost"] == pytest.approx(reservation.reserved_cost)
    assert recovered["refund_cost"] == pytest.approx(0.0)
    assert recovered["error_code"] == "stale_dispatched_reservation_recovered"
    assert recovered["settled_at"] is not None
    assert [tuple(row) for row in recovered_subjects] == [
        tuple(row) for row in original_subjects
    ]
    assert ModelCallLedger.inspect(config)["stale_inflight_model_call_entry_count"] == 0
    next_reservation.release()


def test_fresh_dispatched_reservation_is_not_recovered(tmp_path):
    ledger, _ = _ledger(tmp_path)
    run_id = ledger.start_run("fresh-dispatch", subject_scope=("session", "fresh-dispatch"))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.mark_dispatched()

    next_run = ledger.start_run("after-fresh", subject_scope=("session", "after-fresh"))
    next_reservation = ledger.reserve(
        run_id=next_run,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )

    with sqlite3.connect(str(ledger.db_path)) as conn:
        row = conn.execute(
            "SELECT lifecycle_state, settled_at, error_code FROM model_call_entries "
            "WHERE entry_id=?",
            (reservation.entry_id,),
        ).fetchone()
    assert row == ("reserved", None, "")
    next_reservation.release()


def test_all_stale_dispatched_reservations_recover_in_one_reservation(tmp_path):
    ledger, config = _ledger(tmp_path)
    stale_entry_ids = []
    for index in range(2):
        run_id = ledger.start_run(
            f"stale-{index}",
            subject_scope=("session", f"stale-{index}"),
        )
        reservation = ledger.reserve(
            run_id=run_id,
            operation="embedding",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=1,
        )
        reservation.mark_dispatched()
        stale_entry_ids.append(reservation.entry_id)
    for entry_id in stale_entry_ids:
        _set_reservation_dispatch_time(
            ledger,
            entry_id,
            "2000-01-01T00:00:00+00:00",
        )

    next_run = ledger.start_run("after-all-stale", subject_scope=("session", "after-all-stale"))
    next_reservation = ledger.reserve(
        run_id=next_run,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )

    with sqlite3.connect(str(ledger.db_path)) as conn:
        recovered = conn.execute(
            "SELECT entry_id, lifecycle_state, error_code FROM model_call_entries "
            f"WHERE entry_id IN ({','.join('?' for _ in stale_entry_ids)}) "
            "ORDER BY entry_id",
            stale_entry_ids,
        ).fetchall()
    assert recovered == [
        (entry_id, "incurred_unknown", "stale_dispatched_reservation_recovered")
        for entry_id in sorted(stale_entry_ids)
    ]
    assert ModelCallLedger.inspect(config)["stale_inflight_model_call_entry_count"] == 0
    next_reservation.release()


def test_stale_recovery_rolls_back_when_new_reservation_exceeds_daily_cap(tmp_path):
    ledger, config = _ledger(tmp_path, daily_cost_cap=0.00015)
    run_id = ledger.start_run("stale-budget", subject_scope=("session", "stale-budget"))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.mark_dispatched()
    _set_reservation_dispatch_time(
        ledger,
        reservation.entry_id,
        "2000-01-01T00:00:00+00:00",
    )

    next_run = ledger.start_run("stale-budget-next", subject_scope=("session", "stale-budget-next"))
    with pytest.raises(ModelCallBudgetExceeded, match="daily budget would be exceeded"):
        ledger.reserve(
            run_id=next_run,
            operation="embedding",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=1,
        )

    with sqlite3.connect(str(ledger.db_path)) as conn:
        row = conn.execute(
            "SELECT lifecycle_state, actual_cost, actual_total_tokens, settled_at, error_code "
            "FROM model_call_entries WHERE entry_id=?",
            (reservation.entry_id,),
        ).fetchone()
    assert row == ("reserved", None, None, None, "")
    assert ModelCallLedger.inspect(config)["stale_inflight_model_call_entry_count"] == 1


def test_stale_recovery_rolls_back_when_same_run_budget_is_exhausted(tmp_path):
    ledger, config = _ledger(tmp_path)
    run_id = ledger.start_run(
        "stale-run-budget",
        cost_budget=0.00015,
        subject_scope=("session", "stale-run-budget"),
    )
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.mark_dispatched()
    _set_reservation_dispatch_time(
        ledger,
        reservation.entry_id,
        "2000-01-01T00:00:00+00:00",
    )

    with pytest.raises(ModelCallBudgetExceeded, match="run budget would be exceeded"):
        ledger.reserve(
            run_id=run_id,
            operation="embedding",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=1,
        )

    with sqlite3.connect(str(ledger.db_path)) as conn:
        row = conn.execute(
            "SELECT lifecycle_state, actual_cost, actual_total_tokens, settled_at, error_code "
            "FROM model_call_entries WHERE entry_id=?",
            (reservation.entry_id,),
        ).fetchone()
    assert row == ("reserved", None, None, None, "")
    assert ModelCallLedger.inspect(config)["stale_inflight_model_call_entry_count"] == 1


def test_inspect_degrades_instead_of_raising_for_malformed_tombstone_cost(tmp_path):
    ledger, config = _ledger(tmp_path)
    subject = "tombstone-health"
    run_id = ledger.start_run("tombstone-health", subject_scope=("session", subject))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    reservation.mark_dispatched()
    reservation.settle(usage=_usage(1, 0, request_id="tombstone-health"))
    ledger.freeze_subject_scope("session", subject)
    assert ledger.delete_subject_scope("session", subject)["status"] == "applied"
    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.execute("UPDATE model_call_daily_spend_tombstones SET effective_cost='not-a-number'")

    inspection = ModelCallLedger.inspect(config)
    assert inspection["status"] == "degraded"
    assert inspection["error"] == "model_call_ledger_data_invalid"


def test_zero_or_missing_daily_cap_never_becomes_an_opt_out(tmp_path):
    ledger, config = _ledger(tmp_path, daily_cost_cap=0.0)
    run_id = ledger.start_run("zero-cap", subject_scope=("session", "zero-cap"))
    with pytest.raises(ModelCallBudgetExceeded):
        ledger.reserve(
            run_id=run_id,
            operation="embedding",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=1,
        )

    config._data["model_call_ledger"]["daily_cost_cap"] = 1.0
    charged_run = ledger.start_run("zero-cap-health", subject_scope=("session", "zero-cap-health"))
    charged = ledger.reserve(
        run_id=charged_run,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    charged.mark_dispatched()
    charged.settle(usage=_usage(1, 0, request_id="zero-cap-health"))

    config._data["model_call_ledger"]["daily_cost_cap"] = 0.0
    zero_cap_inspection = ModelCallLedger.inspect(config)
    assert zero_cap_inspection["status"] == "degraded"
    assert zero_cap_inspection["daily_cap_exceeded"] is True

    config._data["model_call_ledger"]["daily_cost_cap"] = None
    inspection = ModelCallLedger.inspect(config)
    assert inspection["status"] == "degraded"
    assert inspection["invalid_daily_cost_cap"] == 1


def test_non_default_daily_cap_is_reported_and_enforced(tmp_path):
    ledger, config = _ledger(tmp_path, daily_cost_cap=0.00015)
    assert ModelCallLedger.inspect(config)["daily_cost_cap"] == pytest.approx(0.00015)

    first_run = ledger.start_run("custom-cap-first", subject_scope=("session", "custom-cap-first"))
    first = ledger.reserve(
        run_id=first_run,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    first.mark_dispatched()
    first.settle(usage=_usage(1, 0, request_id="custom-cap-first"))

    second_run = ledger.start_run(
        "custom-cap-second",
        subject_scope=("session", "custom-cap-second"),
    )
    with pytest.raises(ModelCallBudgetExceeded, match="daily budget would be exceeded"):
        ledger.reserve(
            run_id=second_run,
            operation="embedding",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=1,
        )


def test_reservation_revalidates_live_budget_facts_inside_its_write_transaction(tmp_path):
    ledger, _ = _ledger(tmp_path)
    run_id = ledger.start_run("live-financial-check", cost_budget=0.0, subject_scope=("session", "live"))
    # Simulate another writer changing state after normal construction.  The
    # next reservation must reject it under BEGIN IMMEDIATE, not let NaN make
    # a `cost > budget` comparison evaluate false.
    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.execute("UPDATE model_call_runs SET cost_budget='nan' WHERE run_id=?", (run_id,))
    with pytest.raises(ModelCallLedgerInvariantError, match="invalid_monetary_state:model_call_runs"):
        ledger.reserve(
            run_id=run_id,
            operation="embedding",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=1,
        )


def test_retention_releases_an_undispatched_abandoned_reservation_without_tombstone_cost(tmp_path):
    ledger, _ = _ledger(tmp_path)
    run_id = ledger.start_run("abandoned-reservation", subject_scope=("session", "abandoned"))
    ledger.reserve(
        run_id=run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=10,
    )
    with sqlite3.connect(str(ledger.db_path)) as conn:
        conn.execute(
            "UPDATE model_call_entries SET created_at='2000-01-01T00:00:00+00:00' WHERE run_id=?",
            (run_id,),
        )
    assert ledger.cleanup_older_than(30) == 1
    with sqlite3.connect(str(ledger.db_path)) as conn:
        row = conn.execute(
            "SELECT effective_cost, deleted_entry_count FROM model_call_run_spend_tombstones"
        ).fetchone()
    assert row == (0.0, 1)


def test_historical_import_has_no_public_runtime_write_entry_point(tmp_path):
    ledger, _ = _ledger(tmp_path)
    assert not hasattr(ledger, "import_historical_observation")
    assert not hasattr(ModelCallLedger, "import_historical_observation")


def test_model_call_ledger_reports_legacy_split_storage_as_a_blocker(tmp_path):
    ledger, config = _ledger(tmp_path)
    del ledger
    with sqlite3.connect(str(tmp_path / "wiki_state.db")) as conn:
        conn.execute("CREATE TABLE prompt_calls (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO prompt_calls DEFAULT VALUES")

    inspection = ModelCallLedger.inspect(config)
    assert inspection["status"] == "degraded"
    assert inspection["model_call_storage_path_count"] == 2
    assert inspection["health_ledger_path_mismatch"] == 1
    assert inspection["billable_calls_without_ledger"] == 1


def test_model_call_ledger_reports_retired_stats_owner_even_without_call_rows(tmp_path):
    ledger, config = _ledger(tmp_path)
    del ledger
    with sqlite3.connect(str(tmp_path / "prompt_calls.db")) as conn:
        conn.execute("CREATE TABLE prompt_call_stats (name TEXT PRIMARY KEY, value REAL)")

    inspection = ModelCallLedger.inspect(config)

    assert inspection["status"] == "degraded"
    assert inspection["model_call_storage_path_count"] == 2
    assert inspection["health_ledger_path_mismatch"] == 1
    assert inspection["billable_calls_without_ledger"] == 0


def test_retired_prompt_call_log_hard_fails_without_creating_a_ledger(tmp_path):
    with pytest.raises(ModelCallLedgerInvariantError, match="PromptCallLog is retired"):
        PromptCallLog(tmp_path / "model_call_ledger.db")
    assert not (tmp_path / "model_call_ledger.db").exists()


def test_reservation_rejects_fractional_or_underreported_canonical_provider_input(tmp_path):
    ledger, _ = _ledger(tmp_path)
    run_id = ledger.start_run("canonical-input", subject_scope=("session", "input-bound"))

    with pytest.raises(ModelCallLedgerInvariantError, match="non-negative integer"):
        ledger.reserve(
            run_id=run_id,
            operation="embedding",
            provider="test",
            model="model",
            input_text="x",
            input_tokens=0.9,
        )

    canonical_payload = "provider-visible-input-" * 128
    with pytest.raises(ModelCallLedgerInvariantError, match="complete canonical provider payload"):
        ledger.reserve(
            run_id=run_id,
            operation="embedding",
            provider="test",
            model="model",
            input_text=canonical_payload,
            input_tokens=1,
        )
    assert ledger.recent() == []


def test_run_identifier_and_failure_category_never_persist_caller_controlled_text(tmp_path):
    ledger, _ = _ledger(tmp_path)
    raw_run_id = "caller-run-id:private-user@example.invalid"
    canonical_run_id = ledger.start_run(
        raw_run_id,
        subject_scope=("session", "opaque-run"),
    )
    assert canonical_run_id.startswith("mclrun:")
    assert canonical_run_id != raw_run_id

    reservation = ledger.reserve(
        run_id=raw_run_id,
        operation="embedding",
        provider="test",
        model="model",
        input_text="x",
        input_tokens=1,
    )
    unapproved_error = "unapproved-runtime-category"
    reservation.release(error_code=unapproved_error)

    assert ledger.run_summary(raw_run_id)["run_id"] == canonical_run_id
    with sqlite3.connect(str(ledger.db_path)) as conn:
        stored_run_id = conn.execute("SELECT run_id FROM model_call_runs").fetchone()[0]
        stored_error_code = conn.execute(
            "SELECT error_code FROM model_call_entries WHERE entry_id=?",
            (reservation.entry_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE model_call_entries SET error_code=? WHERE entry_id=?",
            (unapproved_error, reservation.entry_id),
        )
    assert stored_run_id == canonical_run_id
    assert stored_error_code == "error_redacted"
    assert raw_run_id.encode("utf-8") not in ledger.db_path.read_bytes()
    with pytest.raises(ModelCallLedgerInvariantError, match="invalid_error_code:model_call_entries"):
        ModelCallLedger(ledger.db_path)


def test_distillation_post_hoc_log_hook_does_not_recreate_wiki_state_prompt_table(tmp_path):
    from core.hephaestus.distillation_llm import HttpApiHostAgentCaller
    from core.llm_config import LLMApiChain, LLMApiConfig

    config = LedgerConfig(tmp_path)
    caller = HttpApiHostAgentCaller(
        api_chain=LLMApiChain(LLMApiConfig("test", "", "", "model", "missing")),
        config_getter=lambda: config,
    )
    caller._log_call(
        "prompt",
        "response",
        True,
        "test",
        "model",
        duration_ms=1,
        usage={"prompt_tokens": 1, "completion_tokens": 1},
    )

    assert not (tmp_path / "wiki_state.db").exists()
