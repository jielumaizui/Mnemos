"""Exact local SQLite schema and persisted-fact validation for the ledger."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from core.db_utils import render_sql

from .contracts import (
    SCHEMA_VERSION,
    _FORBIDDEN_ENTRY_COLUMNS,
    _RETIRED_PROMPT_STORAGE_TABLES,
    _RUNTIME_OPTIONAL_TABLES,
    _RUNTIME_REQUIRED_COLUMN_CONTRACTS,
    _RUNTIME_REQUIRED_COLUMNS,
    _RUNTIME_REQUIRED_FOREIGN_KEYS,
    _RUNTIME_REQUIRED_INDEX_COLUMNS,
    _RUNTIME_REQUIRED_INDEX_TABLES,
    _RUNTIME_REQUIRED_PRIMARY_KEYS,
    _RUNTIME_REQUIRED_TABLES,
    _RUNTIME_REQUIRED_UNIQUE_COLUMNS,
    _SAFE_ERROR_CODES,
    _SUBJECT_SCOPE_KINDS,
    _TERMINAL_STATES,
    _UNRECOVERABLE_TOMBSTONE_DISPOSITION,
    _UNRECOVERABLE_TOMBSTONE_DISPOSITION_KEY,
    ModelCallLedgerInvariantError,
)
from .normalization import (
    _hash_text,
    _cost,
    _is_canonical_entry_id,
    _is_canonical_run_id,
    _is_canonical_spend_day,
    _is_canonical_timestamp,
    _is_digest_reference,
    _is_opaque_metadata_reference,
    _is_safe_cache_status,
    _is_safe_metered_usage_receipt,
    _is_safe_model_label,
    _is_safe_operation,
    _is_safe_price_version,
    _is_safe_provider_label,
    _is_sha256_digest,
    _money_equal,
    _money_exceeds,
    _nonnegative_finite_float,
    _nonnegative_int,
)
from .state import LedgerState


class LedgerSchemaValidation:
    """One internal module that owns exact schema and row-invariant checks."""

    def __init__(self, state: LedgerState):
        self._state = state

    @property
    def db_path(self):
        return self._state.db_path

    @property
    def _config(self):
        return self._state.config

    def _connect(self):
        return self._state.connect()

    @staticmethod
    def _all_subject_binding() -> tuple[str, str]:
        return "all", _hash_text("all:all")

    @staticmethod
    def _table_names(conn: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    @classmethod
    def _schema_gaps(
        cls,
        conn: sqlite3.Connection,
        *,
        required_tables: Iterable[str],
        required_columns: Mapping[str, Iterable[str]],
        column_contracts: Mapping[str, Mapping[str, tuple[str, bool, str | None]]],
        primary_keys: Mapping[str, tuple[str, ...]],
        unique_columns: Mapping[str, Iterable[tuple[str, ...]]],
        index_columns: Mapping[str, tuple[str, ...]],
        index_tables: Mapping[str, str],
        foreign_keys: Mapping[str, tuple[str, str, str, str]],
        optional_tables: Iterable[str] = (),
        allowed_extra_columns: Mapping[str, Iterable[str]] | None = None,
        allowed_extra_index_tables: Iterable[str] = (),
        allow_missing_indexes: bool = False,
    ) -> list[str]:
        """Return every structural incompatibility without changing the database.

        Column names alone are not a usable schema contract: delete safety also
        depends on primary keys, unique deduplication, FK cascades, defaults,
        and indexes belonging to their declared table.  This helper is shared
        by normal runtime validation and the backup-gated reconciliation path.
        """
        required_table_set = set(required_tables)
        optional_table_set = set(optional_tables)
        allowed_extra_columns = allowed_extra_columns or {}
        allowed_extra_index_table_set = set(allowed_extra_index_tables)
        tables = cls._table_names(conn)
        gaps = [f"table:{table}" for table in sorted(required_table_set - tables)]
        gaps.extend(
            f"unexpected_table:{table}"
            for table in sorted(tables - required_table_set - optional_table_set)
            if not table.startswith("sqlite_")
        )
        for object_type, object_name in conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('trigger', 'view') AND name NOT LIKE 'sqlite_%'"
        ).fetchall():
            gaps.append(f"unexpected_{object_type}:{object_name}")
        table_info: dict[str, dict[str, sqlite3.Row | tuple[Any, ...]]] = {}
        for table, expected_columns in required_columns.items():
            if table not in tables:
                continue
            # ``table_info`` omits generated/hidden columns, which would let a
            # second opaque storage channel hide behind an otherwise exact
            # table.  ``table_xinfo`` exposes those columns and keeps the
            # contract fail-closed for every on-disk schema owner.
            rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()  # nosec B608
            info = {str(row[1]): row for row in rows}
            table_info[table] = info
            gaps.extend(
                f"hidden_column:{table}.{row[1]}"
                for row in rows
                if len(row) > 6 and int(row[6] or 0) != 0
            )
            gaps.extend(
                f"column:{table}.{column}"
                for column in sorted(set(expected_columns) - set(info))
            )
            permitted_extra = set(allowed_extra_columns.get(table, ()))
            gaps.extend(
                f"unexpected_column:{table}.{column}"
                for column in sorted(set(info) - set(expected_columns) - permitted_extra)
            )
            for column, (expected_type, expected_notnull, expected_default) in (
                column_contracts.get(table, {}).items()
            ):
                row = info.get(column)
                if row is None:
                    continue
                actual_type = str(row[2] or "").upper()
                actual_notnull = bool(row[3])
                actual_default = row[4]
                if (
                    actual_type != expected_type
                    or actual_notnull != expected_notnull
                    or actual_default != expected_default
                ):
                    gaps.append(f"column_contract:{table}.{column}")
            table_sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            table_sql = str(table_sql_row[0] or "") if table_sql_row else ""
            if re.search(r"\bCHECK\s*\(", table_sql, flags=re.IGNORECASE):
                gaps.append(f"unexpected_check_constraint:{table}")
            # ``PRAGMA foreign_key_list`` does not expose deferrability.  A
            # deferred FK changes when deletion/attribution failures surface,
            # so reject it explicitly instead of treating the visible action
            # tuple as a complete contract.
            if re.search(r"\bDEFERRABLE\b", table_sql, flags=re.IGNORECASE):
                gaps.append(f"unexpected_deferred_foreign_key:{table}")

        if "model_call_entries" in table_info:
            forbidden = _FORBIDDEN_ENTRY_COLUMNS & set(table_info["model_call_entries"])
            gaps.extend(f"forbidden_column:model_call_entries.{column}" for column in sorted(forbidden))

        for table, expected_primary_key in primary_keys.items():
            primary_key_info = table_info.get(table)
            if primary_key_info is None:
                continue
            actual_primary_key = tuple(
                str(row[1])
                for row in sorted(
                    primary_key_info.values(),
                    key=lambda row: int(row[5]) or 2**31,
                )
                if int(row[5]) > 0
            )
            if actual_primary_key != expected_primary_key:
                gaps.append(f"primary_key:{table}")

        for table in table_info:
            # A set would hide an added second constraint with the same
            # columns (SQLite assigns it another autoindex).  Constraint
            # multiplicity is part of the schema/data-owner contract too.
            expected_unique_sets = Counter(
                tuple(columns) for columns in unique_columns.get(table, ())
            )
            actual_unique_sets: Counter[tuple[str, ...]] = Counter()
            partial_unique = False
            for index in conn.execute(f"PRAGMA index_list({table})").fetchall():  # nosec B608
                if not bool(index[2]):
                    continue
                # SQLite reports PRIMARY KEY's implicit autoindex as origin
                # ``pk``.  It is already checked by the exact PK contract;
                # every other unique index is an independent constraint and
                # must match the declared durable schema exactly.
                origin = str(index[3] if len(index) > 3 else "").lower()
                if origin == "pk":
                    continue
                index_name = str(index[1])
                actual_unique_sets[
                    tuple(
                        str(column[2])
                        for column in conn.execute(f"PRAGMA index_info({index_name})").fetchall()  # nosec B608
                    )
                ] += 1
                partial_unique = partial_unique or bool(index[4] if len(index) > 4 else False)
            if actual_unique_sets != expected_unique_sets or partial_unique:
                gaps.append(f"unique:{table}")

        for index_name, expected_columns in index_columns.items():
            expected_table = index_tables[index_name]
            row = conn.execute(
                "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            ).fetchone()
            if row is None:
                if not allow_missing_indexes:
                    gaps.append(f"index:{index_name}")
                continue
            actual_table = str(row[0])
            actual_columns = tuple(
                str(column[2])
                for column in conn.execute(f"PRAGMA index_info({index_name})").fetchall()  # nosec B608
            )
            index_details = next(
                (
                    detail
                    for detail in conn.execute(
                        f"PRAGMA index_list({expected_table})"  # nosec B608
                    ).fetchall()
                    if str(detail[1]) == index_name
                ),
                None,
            )
            actual_unique = bool(index_details[2]) if index_details is not None else True
            actual_partial = bool(index_details[4]) if index_details is not None and len(index_details) > 4 else True
            if (
                actual_table != expected_table
                or actual_columns != expected_columns
                or actual_unique
                or actual_partial
            ):
                gaps.append(f"index:{index_name}")

        expected_index_names = set(index_columns)
        for index_name, _table_name in conn.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_autoindex_%'"
        ).fetchall():
            if (
                str(index_name) not in expected_index_names
                and str(_table_name) not in allowed_extra_index_table_set
            ):
                gaps.append(f"unexpected_index:{index_name}")

        for table in table_info:
            expected_fk = foreign_keys.get(table)
            actual_foreign_keys = Counter(
                (
                    str(row[3]),
                    str(row[2]),
                    str(row[4]),
                    str(row[6]).upper(),
                    str(row[5]).upper(),
                    str(row[7]).upper(),
                )
                for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()  # nosec B608
            )
            expected_foreign_keys = (
                Counter(
                    [
                        (
                            expected_fk[0],
                            expected_fk[1],
                            expected_fk[2],
                            expected_fk[3],
                            "NO ACTION",
                            "NONE",
                        )
                    ]
                )
                if expected_fk is not None
                else Counter()
            )
            if actual_foreign_keys != expected_foreign_keys:
                gaps.append(f"foreign_key:{table}")
        return gaps

    @classmethod
    def _runtime_data_gaps(
        cls,
        conn: sqlite3.Connection,
    ) -> list[str]:
        """Validate the persisted attribution facts that dispatch/delete trust."""
        gaps: list[str] = []
        subject_kinds = tuple(sorted(_SUBJECT_SCOPE_KINDS))
        for table in ("model_call_run_subjects", "model_call_entry_subjects"):
            invalid = 0
            for row in conn.execute(
                f"SELECT scope_kind, subject_hash FROM {table}"  # nosec B608
            ).fetchall():
                if (
                    str(row[0] or "") not in subject_kinds
                    or not _is_sha256_digest(row[1])
                ):
                    invalid += 1
            if invalid:
                gaps.append(f"invalid_subject_binding:{table}")

        invalid_frozen = 0
        all_binding = cls._all_subject_binding()
        for row in conn.execute(
            "SELECT scope_kind, subject_hash FROM model_call_frozen_subjects"
        ).fetchall():
            scope_kind, subject_hash = str(row[0] or ""), str(row[1] or "")
            valid = (
                scope_kind in subject_kinds and _is_sha256_digest(subject_hash)
            ) or (
                scope_kind == all_binding[0]
                and subject_hash == all_binding[1]
            )
            if not valid:
                invalid_frozen += 1
        if invalid_frozen:
            gaps.append("invalid_subject_binding:model_call_frozen_subjects")

        timestamp_columns = {
            "model_call_runs": ("created_at",),
            "model_call_entries": ("created_at", "dispatched_at", "settled_at"),
            "model_call_run_subjects": ("created_at",),
            "model_call_entry_subjects": ("created_at",),
            "model_call_frozen_subjects": ("frozen_at",),
            "model_call_daily_spend_tombstones": ("updated_at",),
            "model_call_run_spend_tombstones": ("updated_at",),
        }
        for table, columns in timestamp_columns.items():
            for column in columns:
                invalid_timestamp = sum(
                    not _is_canonical_timestamp(row[0])
                    for row in conn.execute(
                        render_sql(
                            "SELECT {column} FROM {table} "
                            "WHERE {column} IS NOT NULL",
                            identifiers={"column": column, "table": table},
                        )
                    ).fetchall()
                )
                if invalid_timestamp:
                    gaps.append(f"invalid_timestamp:{table}.{column}")
        invalid_spend_day = sum(
            not _is_canonical_spend_day(row[0])
            for row in conn.execute(
                "SELECT spend_day FROM model_call_daily_spend_tombstones"
            ).fetchall()
        )
        if invalid_spend_day:
            gaps.append("invalid_spend_day:model_call_daily_spend_tombstones")

        disposition_table = "model_call_ledger_reconciliation_dispositions"
        if disposition_table in cls._table_names(conn):
            invalid_dispositions = 0
            for row in conn.execute(
                "SELECT disposition_key, disposition, known_row_count, recorded_at "
                "FROM model_call_ledger_reconciliation_dispositions"
            ).fetchall():
                try:
                    valid = (
                        str(row["disposition_key"] or "")
                        == _UNRECOVERABLE_TOMBSTONE_DISPOSITION_KEY
                        and str(row["disposition"] or "")
                        == _UNRECOVERABLE_TOMBSTONE_DISPOSITION
                        and _nonnegative_int(
                            row["known_row_count"],
                            label="persisted reconciliation disposition row count",
                        )
                        == int(row["known_row_count"])
                        and _is_canonical_timestamp(row["recorded_at"])
                    )
                except (TypeError, ValueError, ModelCallLedgerInvariantError):
                    valid = False
                if not valid:
                    invalid_dispositions += 1
            if invalid_dispositions:
                gaps.append("invalid_reconciliation_disposition")

        missing_run_subjects = int(
            conn.execute(
                "SELECT COUNT(*) FROM model_call_runs r WHERE ("
                "SELECT COUNT(*) FROM model_call_run_subjects s WHERE s.run_id=r.run_id) <> 1"
            ).fetchone()[0]
            or 0
        )
        if missing_run_subjects:
            gaps.append("unattributed_model_call_runs")
        raw_run_ids = int(
            conn.execute("SELECT COUNT(*) FROM model_call_runs").fetchone()[0] or 0
        )
        if raw_run_ids:
            raw_run_ids = sum(
                not _is_canonical_run_id(row[0])
                for row in conn.execute("SELECT run_id FROM model_call_runs").fetchall()
            )
        if raw_run_ids:
            gaps.append("raw_model_call_run_ids")
        raw_entry_ids = sum(
            not _is_canonical_entry_id(row[0])
            for row in conn.execute("SELECT entry_id FROM model_call_entries").fetchall()
        )
        if raw_entry_ids:
            gaps.append("raw_model_call_entry_ids")
        missing_entry_subjects = int(
            conn.execute(
                "SELECT COUNT(*) FROM model_call_entries e WHERE NOT EXISTS ("
                "SELECT 1 FROM model_call_entry_subjects s WHERE s.entry_id=e.entry_id)"
            ).fetchone()[0]
            or 0
        )
        if missing_entry_subjects:
            gaps.append("unattributed_model_call_entries")

        stale_run_versions = int(
            conn.execute(
                "SELECT COUNT(*) FROM model_call_runs WHERE schema_version<>?",
                (SCHEMA_VERSION,),
            ).fetchone()[0]
            or 0
        )
        if stale_run_versions:
            gaps.append("stale_model_call_run_schema_version")

        invalid_run_budget = 0
        for row in conn.execute("SELECT cost_budget FROM model_call_runs").fetchall():
            value = row[0]
            if value is None:
                continue
            try:
                _nonnegative_finite_float(value, label="persisted run cost budget")
            except ModelCallLedgerInvariantError:
                invalid_run_budget += 1
        if invalid_run_budget:
            gaps.append("invalid_monetary_state:model_call_runs")

        invalid_input_digest = 0
        invalid_legacy_fingerprint = 0
        invalid_numeric_state = 0
        invalid_cost_state = 0
        invalid_lifecycle_state = 0
        invalid_error_code = 0
        unsafe_metadata = 0
        allowed_states = _TERMINAL_STATES | {"reserved"}
        entry_rows = conn.execute("SELECT * FROM model_call_entries").fetchall()
        for row in entry_rows:
            state = str(row["lifecycle_state"] or "")
            if state not in allowed_states:
                invalid_lifecycle_state += 1
                continue
            if not _is_digest_reference(row["input_digest"]):
                invalid_input_digest += 1
            fingerprint = row["legacy_fingerprint"]
            if fingerprint is not None and not _is_digest_reference(fingerprint):
                invalid_legacy_fingerprint += 1
            if str(row["error_code"] or "") not in _SAFE_ERROR_CODES:
                invalid_error_code += 1
            if not (
                _is_safe_operation(row["operation"])
                and _is_safe_provider_label(row["provider"])
                and _is_safe_model_label(row["model"])
                and _is_safe_cache_status(row["cache_status"])
                and _is_safe_price_version(row["price_version"])
                and (
                    not str(row["provider_usage_id"] or "")
                    or _is_opaque_metadata_reference(row["provider_usage_id"], "provider_usage")
                )
                and (
                    not str(row["request_id"] or "")
                    or _is_opaque_metadata_reference(row["request_id"], "request")
                )
                and _is_safe_metered_usage_receipt(row["metered_usage_receipt"])
            ):
                unsafe_metadata += 1
            try:
                reserved_input = _nonnegative_int(
                    row["reserved_input_tokens"], label="persisted reserved input tokens"
                )
                reserved_output = _nonnegative_int(
                    row["reserved_output_tokens"], label="persisted reserved output tokens"
                )
                _nonnegative_int(row["retry_attempt"], label="persisted retry attempt")
                _nonnegative_int(row["latency_ms"], label="persisted latency")
                if int(row["request_dispatched"]) not in {0, 1}:
                    raise ModelCallLedgerInvariantError("persisted dispatch state must be binary")
                input_price = _nonnegative_finite_float(
                    row["input_price"], label="persisted input price"
                )
                output_price = _nonnegative_finite_float(
                    row["output_price"], label="persisted output price"
                )
                reserved_cost = _nonnegative_finite_float(
                    row["reserved_cost"], label="persisted reserved cost"
                )
                refund_cost = _nonnegative_finite_float(
                    row["refund_cost"], label="persisted refund cost"
                )
                for column in (
                    "actual_input_tokens",
                    "actual_output_tokens",
                    "actual_total_tokens",
                ):
                    if row[column] is not None:
                        _nonnegative_int(row[column], label=f"persisted {column}")
                if row["actual_cost"] is not None:
                    _nonnegative_finite_float(row["actual_cost"], label="persisted actual cost")
                if state != "legacy_observed":
                    expected_reserved = _cost(
                        reserved_input,
                        reserved_output,
                        input_price,
                        output_price,
                    )
                    if not _money_equal(reserved_cost, expected_reserved):
                        raise ModelCallLedgerInvariantError("persisted reserved cost does not match price snapshot")
                request_dispatched = int(row["request_dispatched"])
                no_actual_usage = (
                    row["actual_input_tokens"] is None
                    and row["actual_output_tokens"] is None
                    and row["actual_total_tokens"] is None
                    and row["actual_cost"] is None
                )
                no_provider_usage = not any(
                    str(row[column] or "").strip()
                    for column in ("provider_usage_id", "metered_usage_receipt", "request_id")
                )
                dispatched_timestamp_is_valid = _is_canonical_timestamp(row["dispatched_at"])
                settled_timestamp_is_valid = _is_canonical_timestamp(row["settled_at"])
                if state == "reserved":
                    if (
                        not _money_equal(refund_cost, 0.0)
                        or not no_actual_usage
                        or not no_provider_usage
                        or row["settled_at"] is not None
                        or (
                            request_dispatched == 0
                            and row["dispatched_at"] is not None
                        )
                        or (
                            request_dispatched == 1
                            and not dispatched_timestamp_is_valid
                        )
                    ):
                        raise ModelCallLedgerInvariantError(
                            "reserved entry must remain an unconsumed exact reservation"
                        )
                if state in {"settled", "incurred_overrun"}:
                    if (
                        row["actual_input_tokens"] is None
                        or row["actual_output_tokens"] is None
                        or row["actual_total_tokens"] is None
                        or row["actual_cost"] is None
                        or not _is_safe_metered_usage_receipt(row["metered_usage_receipt"])
                        or not str(row["metered_usage_receipt"] or "").strip()
                        or not (
                            _is_opaque_metadata_reference(
                                row["provider_usage_id"], "provider_usage"
                            )
                            or _is_opaque_metadata_reference(row["request_id"], "request")
                        )
                        or request_dispatched != 1
                        or not dispatched_timestamp_is_valid
                        or not settled_timestamp_is_valid
                    ):
                        raise ModelCallLedgerInvariantError("settled entry lacks durable metering facts")
                    actual_input = _nonnegative_int(
                        row["actual_input_tokens"], label="persisted actual input tokens"
                    )
                    actual_output = _nonnegative_int(
                        row["actual_output_tokens"], label="persisted actual output tokens"
                    )
                    if _nonnegative_int(
                        row["actual_total_tokens"], label="persisted actual total tokens"
                    ) != actual_input + actual_output:
                        raise ModelCallLedgerInvariantError("persisted actual token total is inconsistent")
                    expected_actual = _cost(actual_input, actual_output, input_price, output_price)
                    if not _money_equal(float(row["actual_cost"]), expected_actual):
                        raise ModelCallLedgerInvariantError("persisted actual cost does not match price snapshot")
                    actual_exceeds_reservation = _money_exceeds(
                        float(row["actual_cost"]),
                        reserved_cost,
                    )
                    if state == "settled" and actual_exceeds_reservation:
                        raise ModelCallLedgerInvariantError(
                            "settled entry exceeds its pre-dispatch reservation"
                        )
                    if state == "incurred_overrun" and not actual_exceeds_reservation:
                        raise ModelCallLedgerInvariantError(
                            "overrun entry does not exceed its pre-dispatch reservation"
                        )
                    expected_refund = max(0.0, reserved_cost - float(row["actual_cost"]))
                    if state == "settled" and (
                        not _money_equal(refund_cost, expected_refund)
                        or not _money_equal(refund_cost + float(row["actual_cost"]), reserved_cost)
                    ):
                        raise ModelCallLedgerInvariantError(
                            "settled entry refund does not exactly conserve the reservation"
                        )
                    if state == "incurred_overrun" and not _money_equal(refund_cost, 0.0):
                        raise ModelCallLedgerInvariantError(
                            "overrun entry cannot carry a refund"
                        )
                if state == "released":
                    if (
                        request_dispatched != 0
                        or row["dispatched_at"] is not None
                        or not settled_timestamp_is_valid
                        or not no_actual_usage
                        or not no_provider_usage
                        or not _money_equal(refund_cost, reserved_cost)
                    ):
                        raise ModelCallLedgerInvariantError(
                            "released entry cannot carry dispatch or incurred cost"
                        )
                if state == "legacy_observed":
                    if (
                        request_dispatched != 0
                        or row["dispatched_at"] is not None
                        or not settled_timestamp_is_valid
                        or not _money_equal(reserved_cost, 0.0)
                        or row["actual_cost"] is not None
                        or not _money_equal(refund_cost, 0.0)
                    ):
                        raise ModelCallLedgerInvariantError(
                            "legacy observation cannot carry billable runtime cost"
                        )
                if state == "incurred_unknown":
                    if (
                        request_dispatched != 1
                        or not dispatched_timestamp_is_valid
                        or not settled_timestamp_is_valid
                        or row["actual_input_tokens"] is not None
                        or row["actual_output_tokens"] is not None
                        or row["actual_total_tokens"] is None
                        or row["actual_cost"] is None
                        or not _money_equal(float(row["actual_cost"]), reserved_cost)
                        or not _money_equal(refund_cost, 0.0)
                        or not no_provider_usage
                        or _nonnegative_int(
                            row["actual_total_tokens"],
                            label="persisted incurred total tokens",
                        )
                        != reserved_input + reserved_output
                    ):
                        raise ModelCallLedgerInvariantError(
                            "incurred entry must retain its full pre-dispatch reservation"
                        )
                if state == "usage_unverified":
                    if (
                        request_dispatched != 1
                        or not dispatched_timestamp_is_valid
                        or not settled_timestamp_is_valid
                        or not no_actual_usage
                        or not no_provider_usage
                        or not _money_equal(refund_cost, 0.0)
                    ):
                        raise ModelCallLedgerInvariantError(
                            "usage-unverified entry has an inconsistent lifecycle"
                        )
            except (ModelCallLedgerInvariantError, TypeError, ValueError, OverflowError):
                invalid_numeric_state += 1
                invalid_cost_state += 1
        if invalid_input_digest:
            gaps.append("invalid_digest:model_call_entries.input_digest")
        if invalid_legacy_fingerprint:
            gaps.append("invalid_digest:model_call_entries.legacy_fingerprint")
        if invalid_lifecycle_state:
            gaps.append("invalid_lifecycle_state:model_call_entries")
        if invalid_error_code:
            gaps.append("invalid_error_code:model_call_entries")
        if unsafe_metadata:
            gaps.append("unsafe_metadata:model_call_entries")
        if invalid_numeric_state:
            gaps.append("invalid_numeric_state:model_call_entries")
        if invalid_cost_state:
            gaps.append("invalid_monetary_state:model_call_entries")

        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            gaps.append("foreign_key_data")

        for table in ("model_call_daily_spend_tombstones", "model_call_run_spend_tombstones"):
            invalid_tombstones = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} "  # nosec B608
                    "WHERE effective_cost < 0 OR deleted_entry_count < 0"
                ).fetchone()[0]
                or 0
            )
            if invalid_tombstones:
                gaps.append(f"invalid_tombstone:{table}")
        invalid_run_tombstone_digest = int(
            conn.execute(
                "SELECT COUNT(*) FROM model_call_run_spend_tombstones "
                "WHERE length(run_id_digest)<>64 "
                "OR run_id_digest GLOB '*[^0-9a-f]*'"
            ).fetchone()[0]
            or 0
        )
        if invalid_run_tombstone_digest:
            gaps.append("invalid_digest:model_call_run_spend_tombstones.run_id_digest")
        return gaps

    @classmethod
    def _runtime_schema_gaps(
        cls,
        conn: sqlite3.Connection,
        *,
        allow_retired_prompt_tables: bool = False,
    ) -> list[str]:
        gaps = cls._schema_gaps(
            conn,
            required_tables=_RUNTIME_REQUIRED_TABLES,
            required_columns=_RUNTIME_REQUIRED_COLUMNS,
            column_contracts=_RUNTIME_REQUIRED_COLUMN_CONTRACTS,
            primary_keys=_RUNTIME_REQUIRED_PRIMARY_KEYS,
            unique_columns=_RUNTIME_REQUIRED_UNIQUE_COLUMNS,
            index_columns=_RUNTIME_REQUIRED_INDEX_COLUMNS,
            index_tables=_RUNTIME_REQUIRED_INDEX_TABLES,
            foreign_keys=_RUNTIME_REQUIRED_FOREIGN_KEYS,
            optional_tables=(
                _RUNTIME_OPTIONAL_TABLES
                | (_RETIRED_PROMPT_STORAGE_TABLES if allow_retired_prompt_tables else frozenset())
            ),
            allowed_extra_index_tables=(
                _RETIRED_PROMPT_STORAGE_TABLES if allow_retired_prompt_tables else frozenset()
            ),
        )
        if not gaps:
            gaps.extend(cls._runtime_data_gaps(conn))
        return gaps

    @classmethod
    def _require_current_runtime_data_integrity(
        cls,
        conn: sqlite3.Connection,
        *,
        operation: str,
    ) -> None:
        """Revalidate mutable financial facts while a write transaction holds lock.

        Constructor validation is intentionally fail-closed, but another
        process could tamper with a SQLite row after construction.  Budget
        decisions must validate the live rows inside their ``BEGIN IMMEDIATE``
        transaction, otherwise NaN/negative values can turn a comparison into
        a free-credit bypass.
        """
        gaps = cls._runtime_data_gaps(conn)
        if gaps:
            raise ModelCallLedgerInvariantError(
                f"model-call ledger runtime facts invalid before {operation}: "
                + ", ".join(sorted(gaps))
            )

    @classmethod
    def _reconciliation_preflight_gaps(
        cls,
        conn: sqlite3.Connection,
        *,
        allow_retired_prompt_tables: bool = False,
    ) -> list[str]:
        """Reject unsupported existing state before a reconciliation writes DDL.

        Missing privacy-support tables are an expected old-schema condition and
        may be created by the backup-gated path.  Any *present* support table,
        however, is a data owner and must already match the exact contract;
        the sole exception is the explicitly enumerated raw-id tombstone v1
        shape that the reconciler replaces in one transaction.
        """
        meter_column = "metered_usage_receipt"
        tables = cls._table_names(conn)
        base_columns: dict[str, Iterable[str]] = {
            "model_call_runs": _RUNTIME_REQUIRED_COLUMNS["model_call_runs"],
            "model_call_entries": _RUNTIME_REQUIRED_COLUMNS["model_call_entries"]
            - {meter_column},
        }
        base_contracts: dict[str, Mapping[str, tuple[str, bool, str | None]]] = {
            "model_call_runs": _RUNTIME_REQUIRED_COLUMN_CONTRACTS["model_call_runs"],
            "model_call_entries": {
                name: contract
                for name, contract in _RUNTIME_REQUIRED_COLUMN_CONTRACTS[
                    "model_call_entries"
                ].items()
                if name != meter_column
            },
        }
        primary_keys: dict[str, tuple[str, ...]] = {
            "model_call_runs": _RUNTIME_REQUIRED_PRIMARY_KEYS["model_call_runs"],
            "model_call_entries": _RUNTIME_REQUIRED_PRIMARY_KEYS["model_call_entries"],
        }
        required_tables = {"model_call_runs", "model_call_entries"}
        retired_tombstone = cls._is_supported_retired_run_tombstone_schema(conn)
        for table in _RUNTIME_REQUIRED_TABLES - required_tables:
            if table not in tables:
                continue
            if table == "model_call_run_spend_tombstones" and retired_tombstone:
                continue
            required_tables.add(table)
            base_columns[table] = _RUNTIME_REQUIRED_COLUMNS[table]
            base_contracts[table] = _RUNTIME_REQUIRED_COLUMN_CONTRACTS[table]
            primary_keys[table] = _RUNTIME_REQUIRED_PRIMARY_KEYS[table]
        if "model_call_ledger_reconciliation_dispositions" in tables:
            disposition_table = "model_call_ledger_reconciliation_dispositions"
            base_columns[disposition_table] = _RUNTIME_REQUIRED_COLUMNS[disposition_table]
            base_contracts[disposition_table] = _RUNTIME_REQUIRED_COLUMN_CONTRACTS[
                disposition_table
            ]
            primary_keys[disposition_table] = _RUNTIME_REQUIRED_PRIMARY_KEYS[
                disposition_table
            ]
        # Existing ledgers may have an older shape for a known auxiliary
        # owner (notably the retired run-tombstone table).  Let the
        # backup-gated migration reach that narrowly supported conversion,
        # while still rejecting every unknown table/index/trigger/view.  Full
        # runtime validation below requires the exact current contract before
        # any normal read or write is allowed.
        base_indexes = dict(_RUNTIME_REQUIRED_INDEX_COLUMNS)
        return cls._schema_gaps(
            conn,
            required_tables=required_tables,
            required_columns=base_columns,
            column_contracts=base_contracts,
            primary_keys=primary_keys,
            unique_columns={
                "model_call_entries": _RUNTIME_REQUIRED_UNIQUE_COLUMNS["model_call_entries"]
            },
            index_columns=base_indexes,
            index_tables=_RUNTIME_REQUIRED_INDEX_TABLES,
            foreign_keys={
                table: foreign_key
                for table, foreign_key in _RUNTIME_REQUIRED_FOREIGN_KEYS.items()
                if table in base_columns
            },
            optional_tables=(
                (_RUNTIME_REQUIRED_TABLES - required_tables)
                | _RUNTIME_OPTIONAL_TABLES
                | (_RETIRED_PROMPT_STORAGE_TABLES if allow_retired_prompt_tables else frozenset())
            ),
            allowed_extra_columns={"model_call_entries": {meter_column}},
            allowed_extra_index_tables=(
                _RETIRED_PROMPT_STORAGE_TABLES if allow_retired_prompt_tables else frozenset()
            ),
            allow_missing_indexes=True,
        )

    def _validate_runtime_schema(self) -> None:
        """Reject an old or malformed ledger instead of upgrading it during use."""
        with self._connect() as conn:
            gaps = self._runtime_schema_gaps(conn)
        if gaps:
            raise ModelCallLedgerInvariantError(
                "model-call ledger schema requires backup-gated reconciliation: "
                + ", ".join(sorted(gaps))
            )

    @classmethod
    def _retired_cascading_run_tombstone_shape(cls, conn: sqlite3.Connection) -> bool:
        """Whether this DB contains the early shape that may already have lost spend."""
        if "model_call_run_spend_tombstones" not in cls._table_names(conn):
            return False
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(model_call_run_spend_tombstones)").fetchall()
        }
        old_columns = {"run_id", "effective_cost", "deleted_entry_count", "updated_at"}
        return columns == old_columns and bool(
            conn.execute("PRAGMA foreign_key_list(model_call_run_spend_tombstones)").fetchone()
        )

    @classmethod
    def _is_supported_retired_run_tombstone_schema(cls, conn: sqlite3.Connection) -> bool:
        """Return whether the sole supported v1 tombstone shape is present.

        This is deliberately narrower than "has a run_id column".  A retired
        raw-id table may be rebuilt only when its complete visible/hidden
        column, PK, FK, UNIQUE, and CHECK surface is the audited v1 contract.
        Any other table reaches normal exact-schema validation and blocks
        before the backup-gated reconciler changes a byte.
        """
        table = "model_call_run_spend_tombstones"
        if table not in cls._table_names(conn):
            return False
        rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()  # nosec B608
        expected = {
            "run_id": ("TEXT", False, None),
            "effective_cost": ("REAL", True, None),
            "deleted_entry_count": ("INTEGER", True, None),
            "updated_at": ("TEXT", True, None),
        }
        if {str(row[1]) for row in rows} != set(expected):
            return False
        if any(len(row) > 6 and int(row[6] or 0) != 0 for row in rows):
            return False
        for row in rows:
            contract = expected[str(row[1])]
            if (
                str(row[2] or "").upper() != contract[0]
                or bool(row[3]) != contract[1]
                or row[4] != contract[2]
            ):
                return False
        primary_key = tuple(
            str(row[1])
            for row in sorted(rows, key=lambda row: int(row[5]) or 2**31)
            if int(row[5]) > 0
        )
        if primary_key != ("run_id",):
            return False
        for index in conn.execute(f"PRAGMA index_list({table})").fetchall():  # nosec B608
            if not bool(index[2]):
                continue
            if str(index[3] if len(index) > 3 else "").lower() != "pk":
                return False
        actual_foreign_keys = Counter(
            (
                str(row[3]),
                str(row[2]),
                str(row[4]),
                str(row[6]).upper(),
                str(row[5]).upper(),
                str(row[7]).upper(),
            )
            for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()  # nosec B608
        )
        allowed_foreign_keys = Counter(
            [
                (
                    "run_id",
                    "model_call_runs",
                    "run_id",
                    "CASCADE",
                    "NO ACTION",
                    "NONE",
                )
            ]
        )
        if actual_foreign_keys != Counter() and actual_foreign_keys != allowed_foreign_keys:
            return False
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return not bool(
            table_sql
            and re.search(
                r"\b(?:CHECK\s*\(|DEFERRABLE\b)",
                str(table_sql[0] or ""),
                flags=re.IGNORECASE,
            )
        )

    @classmethod
    def _run_tombstone_requires_private_scrub(cls, conn: sqlite3.Connection) -> bool:
        """Whether reconciliation would release a reversible retired run key.

        An old tombstone-only database may have no surviving ``model_call_runs``
        row at all.  Its `run_id` column is still caller-controlled private
        material, so it needs the same WAL-to-DELETE and secure-delete path as
        a live run-id rekey before the old table is dropped.
        """
        if "model_call_run_spend_tombstones" not in cls._table_names(conn):
            return False
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(model_call_run_spend_tombstones)").fetchall()
        }
        return "run_id" in columns
