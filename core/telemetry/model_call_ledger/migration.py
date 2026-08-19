"""Backup-gated reconciliation authority for the local model-call ledger.

This module is intentionally separate from :mod:`.api`: normal provider
callers can reserve and settle a paid request, but cannot open a legacy
database or manufacture historical observations.  The reconciliation command
receives opaque in-process proof/receipt tokens and a short-lived session;
the token registries below are lexical so a copied object cannot become an
authorization by itself.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat as stat_module
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeAlias

from core.db_utils import render_sql

from .contracts import SCHEMA_VERSION, _SAFE_ERROR_CODES, ModelCallLedgerInvariantError
from .normalization import (
    _canonical_run_id,
    _canonical_timestamp,
    _hash_text,
    _is_canonical_entry_id,
    _is_canonical_run_id,
    _is_digest_reference,
    _json_hash,
    _new_canonical_entry_id,
    _nonnegative_int,
    _normalize_model_label,
    _normalize_operation,
    _normalize_provider_label,
    _utc_now,
)
from .schema_reconciliation import LedgerSchemaReconciliation
from .schema_validation import LedgerSchemaValidation
from .state import (
    LedgerState,
    require_delete_journal_mode_for_private_scrub,
    require_secure_delete_for_private_scrub,
)
from .subjects_retention import LedgerSubjectsRetention


CanonicalInspection: TypeAlias = tuple[str, set[str], dict[str, int]]


def _private_sqlite_backup_identity(path: Path) -> str:
    """Return a stable in-memory identity for one mode-0600 SQLite backup.

    This is a local integrity binding for a recovery copy, not encryption or
    a user-facing secret store.  It deliberately never reaches plans, health
    output, or persisted ledger rows.
    """
    candidate = Path(path).expanduser()
    try:
        initial = candidate.lstat()
    except OSError as exc:
        raise ModelCallLedgerInvariantError("private ledger backup is missing") from exc
    if stat_module.S_ISLNK(initial.st_mode) or not stat_module.S_ISREG(initial.st_mode):
        raise ModelCallLedgerInvariantError(
            "private ledger backup must be a regular non-symlink file"
        )
    if initial.st_mode & 0o077:
        raise ModelCallLedgerInvariantError(
            "private ledger backup permissions are not mode-0600"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise ModelCallLedgerInvariantError(
            "private ledger backup cannot be opened safely"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != initial.st_dev
            or opened.st_ino != initial.st_ino
            or not stat_module.S_ISREG(opened.st_mode)
        ):
            raise ModelCallLedgerInvariantError(
                "private ledger backup changed before identity check"
            )
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        finished = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        final = candidate.lstat()
    except OSError as exc:
        raise ModelCallLedgerInvariantError(
            "private ledger backup changed during identity check"
        ) from exc
    stable = (
        initial.st_dev,
        initial.st_ino,
        initial.st_size,
        initial.st_mtime_ns,
        initial.st_ctime_ns,
    )
    if stable != (
        finished.st_dev,
        finished.st_ino,
        finished.st_size,
        finished.st_mtime_ns,
        finished.st_ctime_ns,
    ) or stable != (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
        final.st_ctime_ns,
    ):
        raise ModelCallLedgerInvariantError("private ledger backup changed during identity check")
    return "sha256:" + digest.hexdigest()


def _verified_private_sqlite_backup(path: Path) -> bool:
    try:
        _private_sqlite_backup_identity(path)
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=15)
        try:
            return str(conn.execute("PRAGMA integrity_check").fetchone()[0]) == "ok"
        finally:
            conn.close()
    except (ModelCallLedgerInvariantError, OSError, sqlite3.Error):
        return False


def _reconciliation_source_signature(path: Path) -> str:
    """Bind authorization to filesystem/schema facts without reading row bodies."""
    stat = Path(path).stat()
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=15)
    try:
        pragma_values = {
            name: conn.execute(f"PRAGMA {name}").fetchone()[0]
            for name in (
                "schema_version",
                "user_version",
                "application_id",
                "page_count",
                "freelist_count",
                "journal_mode",
                "encoding",
            )
        }
        tables = sorted(
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        )
    finally:
        conn.close()
    return _json_hash(
        {
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "pragmas": pragma_values,
            "tables": tables,
        }
    )


def _build_backup_reconciliation_authority() -> tuple[
    Callable[[Path], object],
    Callable[[object], Path],
    Callable[[object, Path], object],
    Callable[[object, object | None], object],
    Callable[[object], Path],
    Callable[[object], bool],
    Callable[[object], None],
    Callable[[object, Path], None],
    Callable[[object, Path], None],
]:
    """Build one-shot lexical proof, receipt and authorization registries."""
    proofs: dict[object, dict[str, Any]] = {}
    receipts: dict[object, dict[str, Any]] = {}
    authorizations: dict[object, dict[str, Any]] = {}

    def _lookup(store: dict[object, dict[str, Any]], token: object) -> dict[str, Any] | None:
        try:
            return store.get(token)
        except TypeError:
            return None

    def _proof_facts(proof: object) -> dict[str, Any]:
        facts = _lookup(proofs, proof)
        if facts is None:
            raise ModelCallLedgerInvariantError(
                "model-call ledger reconciliation requires a registered pre-backup proof"
            )
        return facts

    def _authorization_facts(authorization: object) -> dict[str, Any]:
        facts = _lookup(authorizations, authorization)
        if facts is None:
            raise ModelCallLedgerInvariantError(
                "model-call schema reconciliation requires a registered verified-backup authorization"
            )
        return facts

    def prepare(source_db: Path) -> object:
        requested_source = Path(source_db).expanduser()
        try:
            requested_stat = requested_source.lstat()
        except FileNotFoundError:
            source_existed = False
            source = requested_source.resolve(strict=False)
        except OSError as exc:
            raise ModelCallLedgerInvariantError(
                "model-call ledger source cannot be inspected safely"
            ) from exc
        else:
            if stat_module.S_ISLNK(requested_stat.st_mode) or not stat_module.S_ISREG(
                requested_stat.st_mode
            ):
                raise ModelCallLedgerInvariantError(
                    "model-call ledger source must be a regular non-symlink file"
                )
            source_existed = True
            source = requested_source.resolve(strict=True)
        proof = object()
        proofs[proof] = {
            "source_db": source,
            "source_signature": (
                _reconciliation_source_signature(source) if source_existed else "missing"
            ),
            "source_existed": source_existed,
        }
        return proof

    def proof_source(proof: object) -> Path:
        return Path(_proof_facts(proof)["source_db"])

    def write_backup(proof: object, backup_db: Path) -> object:
        proof_facts = _proof_facts(proof)
        if not bool(proof_facts["source_existed"]):
            raise ModelCallLedgerInvariantError(
                "private ledger backup requires a registered existing-source pre-backup proof"
            )
        source = Path(proof_facts["source_db"])
        requested_target = Path(backup_db).expanduser()
        try:
            target_stat = requested_target.lstat()
        except OSError as exc:
            raise ModelCallLedgerInvariantError("private ledger backup target is missing") from exc
        if stat_module.S_ISLNK(target_stat.st_mode) or not stat_module.S_ISREG(target_stat.st_mode):
            raise ModelCallLedgerInvariantError(
                "private ledger backup target must be a new mode-0600 regular file"
            )
        target = requested_target.resolve(strict=True)
        if (
            not source.is_file()
            or not target.is_file()
            or target_stat.st_mode & 0o077
            or target_stat.st_size != 0
        ):
            raise ModelCallLedgerInvariantError(
                "private ledger backup target must be a new mode-0600 regular file"
            )
        try:
            source_uri = source.resolve().as_uri() + "?mode=ro"
            src = sqlite3.connect(source_uri, uri=True, timeout=30)
            dst = sqlite3.connect(str(target), timeout=30)
            try:
                src.backup(dst)
                integrity = str(dst.execute("PRAGMA integrity_check").fetchone()[0])
            finally:
                dst.close()
                src.close()
        except (OSError, sqlite3.Error) as exc:
            raise ModelCallLedgerInvariantError("private ledger SQLite backup failed") from exc
        if integrity != "ok" or not _verified_private_sqlite_backup(target):
            raise ModelCallLedgerInvariantError("private ledger backup integrity verification failed")
        backup_identity = _private_sqlite_backup_identity(target)
        source_signature_after_backup = _reconciliation_source_signature(source)
        if source_signature_after_backup != proof_facts["source_signature"]:
            raise ModelCallLedgerInvariantError(
                "model-call ledger source changed while the private backup was created"
            )
        receipt = object()
        receipts[receipt] = {
            "source_db": source,
            "backup_db": target,
            "backup_identity": backup_identity,
            "source_signature_before_backup": proof_facts["source_signature"],
            "source_signature_after_backup": source_signature_after_backup,
        }
        return receipt

    def issue(proof: object, backup_receipt: object | None) -> object:
        proof_facts = _proof_facts(proof)
        source = Path(proof_facts["source_db"])
        source_existed = bool(proof_facts["source_existed"])
        receipt_facts: dict[str, Any] | None = None
        backup_identity = ""
        if source_existed:
            receipt_facts = _lookup(receipts, backup_receipt) if backup_receipt is not None else None
            if (
                receipt_facts is None
                or Path(receipt_facts["source_db"]) != source
                or receipt_facts["source_signature_before_backup"]
                != proof_facts["source_signature"]
                or receipt_facts["source_signature_after_backup"]
                != proof_facts["source_signature"]
            ):
                raise ModelCallLedgerInvariantError(
                    "existing model-call ledger requires the exact private-backup receipt"
                )
            backup = Path(receipt_facts["backup_db"])
            if (
                not source.is_file()
                or not _verified_private_sqlite_backup(backup)
                or _private_sqlite_backup_identity(backup)
                != str(receipt_facts.get("backup_identity") or "")
            ):
                raise ModelCallLedgerInvariantError(
                    "existing model-call ledger requires an integrity-checked private backup"
                )
            source_signature_after_backup = _reconciliation_source_signature(source)
            if source_signature_after_backup != proof_facts["source_signature"]:
                raise ModelCallLedgerInvariantError(
                    "model-call ledger source changed while the private backup was created"
                )
            backup_identity = str(receipt_facts.get("backup_identity") or "")
        else:
            if source.exists() or backup_receipt is not None:
                raise ModelCallLedgerInvariantError(
                    "new model-call ledger reconciliation cannot claim a nonexistent backup source"
                )
            source_signature_after_backup = "missing"
            backup = None
        authorization = object()
        authorizations[authorization] = {
            "source_db": source,
            "backup_db": backup,
            "backup_identity": backup_identity,
            "source_signature_before_backup": proof_facts["source_signature"],
            "source_signature_after_backup": source_signature_after_backup,
            "source_existed": source_existed,
            "schema_reconciled": False,
            "post_reconciliation_signature": "",
        }
        proofs.pop(proof, None)
        if backup_receipt is not None:
            receipts.pop(backup_receipt, None)
        return authorization

    def validated_source(authorization: object) -> Path:
        facts = _authorization_facts(authorization)
        source = Path(facts["source_db"])
        if bool(facts["schema_reconciled"]):
            if not source.is_file():
                raise ModelCallLedgerInvariantError("model-call ledger source changed during reconciliation")
            if _reconciliation_source_signature(source) != facts["post_reconciliation_signature"]:
                raise ModelCallLedgerInvariantError("model-call ledger source changed during reconciliation")
        elif bool(facts["source_existed"]):
            backup = facts["backup_db"]
            if not source.is_file() or backup is None:
                raise ModelCallLedgerInvariantError("model-call ledger source changed after backup")
            if (
                not _verified_private_sqlite_backup(Path(backup))
                or _private_sqlite_backup_identity(Path(backup))
                != str(facts.get("backup_identity") or "")
            ):
                raise ModelCallLedgerInvariantError("model-call ledger backup is no longer verified")
            if _reconciliation_source_signature(source) != facts["source_signature_after_backup"]:
                raise ModelCallLedgerInvariantError("model-call ledger source changed after backup")
        elif source.exists():
            raise ModelCallLedgerInvariantError(
                "model-call ledger source appeared after backup authorization"
            )
        return source

    def schema_reconciled(authorization: object) -> bool:
        return bool(_authorization_facts(authorization)["schema_reconciled"])

    def revoke(authorization: object) -> None:
        try:
            authorizations.pop(authorization, None)
        except TypeError:
            return

    def mark_schema_complete(authorization: object, db_path: Path) -> None:
        facts = _authorization_facts(authorization)
        facts["schema_reconciled"] = True
        facts["post_reconciliation_signature"] = _reconciliation_source_signature(db_path)

    def advance_source_signature(authorization: object, db_path: Path) -> None:
        facts = _authorization_facts(authorization)
        if not bool(facts["schema_reconciled"]):
            raise ModelCallLedgerInvariantError("model-call reconciliation authorization expired")
        facts["post_reconciliation_signature"] = _reconciliation_source_signature(db_path)

    return (
        prepare,
        proof_source,
        write_backup,
        issue,
        validated_source,
        schema_reconciled,
        revoke,
        mark_schema_complete,
        advance_source_signature,
    )


(
    _prepare_backup,
    _proof_source,
    _write_verified_backup,
    _issue_authorization,
    _validated_source,
    _schema_reconciled,
    _revoke_authorization,
    _mark_schema_complete,
    _advance_source_signature,
) = _build_backup_reconciliation_authority()
del _build_backup_reconciliation_authority


class LedgerReconciliationSession:
    """Short-lived private migration session bound to a verified backup."""

    def __init__(self, authorization: object, *, config: Any | None = None):
        db_path = _validated_source(authorization)
        self._authorization = authorization
        self._state = LedgerState(db_path, config=config, reconciliation_only=True)
        self._validation = LedgerSchemaValidation(self._state)
        self._schema = LedgerSchemaReconciliation(self._state)
        self._retention = LedgerSubjectsRetention(self._state)
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise ModelCallLedgerInvariantError("model-call reconciliation session is closed")

    def _source(self) -> Path:
        self._ensure_open()
        return _validated_source(self._authorization)

    @property
    def canonical_path(self) -> Path:
        """The still-authorized canonical ledger source path."""
        return self._source()

    def reconcile_privacy_schema(
        self,
        *,
        discard_unattributable_legacy: bool = False,
        discard_unrecoverable_run_tombstone_history: bool = False,
    ) -> dict[str, int]:
        """Upgrade exactly one validated source to the current privacy schema."""
        db_path = self._source()
        if not db_path.exists():
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._schema._bootstrap_schema()
            self._validation._validate_runtime_schema()
            self._state.runtime_schema_validated = True
            _mark_schema_complete(self._authorization, db_path)
            return {
                "created_schema": 1,
                "backfilled_run_subjects": 0,
                "backfilled_entry_subjects": 0,
                "discarded_unattributable_legacy_entries": 0,
                "unrecoverable_run_tombstone_history_discarded": 0,
                "rekeyed_run_ids": 0,
                "rekeyed_entry_ids": 0,
                "redacted_error_codes": 0,
                "redacted_metadata_rows": 0,
                "redacted_timestamp_rows": 0,
            }

        with self._state.connect() as conn:
            preflight_gaps = self._validation._reconciliation_preflight_gaps(
                conn, allow_retired_prompt_tables=True
            )
            if preflight_gaps:
                raise ModelCallLedgerInvariantError(
                    "cannot reconcile an unsupported model-call ledger: "
                    + ", ".join(sorted(preflight_gaps))
                )
            raw_run_id_present = any(
                not _is_canonical_run_id(row[0])
                for row in conn.execute("SELECT run_id FROM model_call_runs").fetchall()
            )
            raw_entry_id_present = any(
                not _is_canonical_entry_id(row[0])
                for row in conn.execute("SELECT entry_id FROM model_call_entries").fetchall()
            )
            entry_columns_before = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(model_call_entries)").fetchall()
            }
            unsafe_metadata_present = bool(
                self._schema._metadata_reconciliation_updates(
                    conn,
                    has_metered_usage_receipt="metered_usage_receipt" in entry_columns_before,
                )
            )
            unsafe_timestamps_present = bool(self._schema._timestamp_reconciliation_updates(conn))
            unsafe_error_code_present = any(
                str(row[0] or "") not in _SAFE_ERROR_CODES
                for row in conn.execute(
                    "SELECT error_code FROM model_call_entries WHERE error_code<>''"
                ).fetchall()
            )
            raw_tombstone_present = self._validation._run_tombstone_requires_private_scrub(conn)
            if "model_call_entry_subjects" in self._validation._table_names(conn):
                unattributed_legacy_present = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM model_call_entries e WHERE NOT EXISTS ("
                        "SELECT 1 FROM model_call_entry_subjects s WHERE s.entry_id=e.entry_id)"
                    ).fetchone()[0]
                    or 0
                )
            else:
                unattributed_legacy_present = int(
                    conn.execute("SELECT COUNT(*) FROM model_call_entries").fetchone()[0] or 0
                )
            private_scrub_required = (
                raw_run_id_present
                or raw_entry_id_present
                or unsafe_metadata_present
                or unsafe_timestamps_present
                or unsafe_error_code_present
                or raw_tombstone_present
                or unattributed_legacy_present > 0
            )
            if private_scrub_required:
                require_delete_journal_mode_for_private_scrub(conn)
                require_secure_delete_for_private_scrub(conn)
            conn.execute("BEGIN IMMEDIATE")
            entry_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(model_call_entries)").fetchall()
            }
            if "metered_usage_receipt" not in entry_columns:
                conn.execute(
                    "ALTER TABLE model_call_entries "
                    "ADD COLUMN metered_usage_receipt TEXT NOT NULL DEFAULT ''"
                )
            unrecoverable_history_discarded = self._schema._rebuild_run_spend_tombstones_without_fk(
                conn,
                discard_unrecoverable_cascade_history=discard_unrecoverable_run_tombstone_history,
            )
            self._schema._create_base_indexes(conn)
            self._schema._create_subject_schema(conn)
            rekeyed_run_ids = self._schema._rekey_model_call_run_ids_to_opaque(conn)
            rekeyed_entry_ids = self._schema._rekey_model_call_entry_ids_to_opaque(conn)
            metadata_updates = self._schema._metadata_reconciliation_updates(
                conn, has_metered_usage_receipt=True
            )
            timestamp_updates = self._schema._timestamp_reconciliation_updates(conn)
            if metadata_updates:
                require_secure_delete_for_private_scrub(conn)
                conn.executemany(
                    "UPDATE model_call_entries SET operation=?, provider=?, model=?, "
                    "cache_status=?, provider_usage_id=?, request_id=?, "
                    "metered_usage_receipt=?, price_version=? WHERE entry_id=?",
                    metadata_updates,
                )
            if timestamp_updates:
                require_secure_delete_for_private_scrub(conn)
                for statement, params in timestamp_updates:
                    conn.execute(statement, params)
            unsafe_error_entry_ids = [
                str(row[0])
                for row in conn.execute(
                    "SELECT entry_id, error_code FROM model_call_entries WHERE error_code<>''"
                ).fetchall()
                if str(row[1] or "") not in _SAFE_ERROR_CODES
            ]
            if unsafe_error_entry_ids:
                require_secure_delete_for_private_scrub(conn)
                conn.executemany(
                    "UPDATE model_call_entries SET error_code='error_redacted' WHERE entry_id=?",
                    [(entry_id,) for entry_id in unsafe_error_entry_ids],
                )

            unmapped_entries = conn.execute(
                """
                SELECT e.entry_id, e.run_id, e.lifecycle_state
                FROM model_call_entries e
                WHERE NOT EXISTS (
                    SELECT 1 FROM model_call_entry_subjects s WHERE s.entry_id=e.entry_id
                )
                ORDER BY e.entry_id
                """
            ).fetchall()
            unattributable: list[sqlite3.Row] = []
            entry_backfills = 0
            for row in unmapped_entries:
                if str(row["lifecycle_state"]) != "legacy_observed":
                    raise ModelCallLedgerInvariantError(
                        "nonlegacy model-call entries lack immutable entry-level subject attribution"
                    )
                unattributable.append(row)
            if unattributable and not discard_unattributable_legacy:
                raise ModelCallLedgerInvariantError(
                    "unattributable legacy model-call entries require explicit discard"
                )
            discarded = 0
            if unattributable:
                entry_ids = [str(row["entry_id"]) for row in unattributable]
                conn.execute(
                    render_sql(
                        "DELETE FROM model_call_entries "
                        "WHERE entry_id IN ({entry_ids})",
                        placeholder_counts={"entry_ids": len(entry_ids)},
                    ),
                    entry_ids,
                )
                discarded = len(entry_ids)

            unmapped_runs = conn.execute(
                """
                SELECT r.run_id
                FROM model_call_runs r
                WHERE NOT EXISTS (
                    SELECT 1 FROM model_call_run_subjects s WHERE s.run_id=r.run_id
                )
                ORDER BY r.run_id
                """
            ).fetchall()
            run_backfills = 0
            for row in unmapped_runs:
                run_id = str(row["run_id"])
                bindings = [
                    (str(item["scope_kind"]), str(item["subject_hash"]))
                    for item in conn.execute(
                        """
                        SELECT DISTINCT s.scope_kind, s.subject_hash
                        FROM model_call_entry_subjects s
                        JOIN model_call_entries e ON e.entry_id=s.entry_id
                        WHERE e.run_id=?
                        ORDER BY s.scope_kind, s.subject_hash
                        """,
                        (run_id,),
                    ).fetchall()
                ]
                entry_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM model_call_entries WHERE run_id=?", (run_id,)
                    ).fetchone()[0]
                    or 0
                )
                if entry_count == 0:
                    conn.execute("DELETE FROM model_call_runs WHERE run_id=?", (run_id,))
                    continue
                if not bindings:
                    raise ModelCallLedgerInvariantError(
                        "model-call run contains entries without subject attribution"
                    )
                root_binding = (
                    bindings[0]
                    if len(bindings) == 1
                    else self._retention._subject_binding(
                        ("source", f"reconciled-multi-subject-run:{_hash_text(run_id)[:24]}")
                    )
                )
                if root_binding is None:
                    raise ModelCallLedgerInvariantError("reconciled run root is invalid")
                conn.execute(
                    "INSERT INTO model_call_run_subjects("
                    "run_id, scope_kind, subject_hash, created_at) VALUES (?, ?, ?, ?)",
                    (run_id, root_binding[0], root_binding[1], _utc_now()),
                )
                run_backfills += 1
            conn.execute(
                "DELETE FROM model_call_runs WHERE NOT EXISTS ("
                "SELECT 1 FROM model_call_entries e WHERE e.run_id=model_call_runs.run_id)"
            )
            conn.execute(
                "UPDATE model_call_runs SET schema_version=? WHERE schema_version<>?",
                (SCHEMA_VERSION, SCHEMA_VERSION),
            )
            runtime_gaps = self._validation._runtime_schema_gaps(
                conn, allow_retired_prompt_tables=True
            )
            if runtime_gaps:
                raise ModelCallLedgerInvariantError(
                    "reconciled model-call ledger did not satisfy runtime schema: "
                    + ", ".join(sorted(runtime_gaps))
                )
            conn.commit()

        with self._state.connect() as conn:
            post_commit_gaps = self._validation._runtime_schema_gaps(
                conn, allow_retired_prompt_tables=True
            )
        if post_commit_gaps:
            raise ModelCallLedgerInvariantError(
                "reconciled model-call ledger changed before canonical retired cleanup: "
                + ", ".join(sorted(post_commit_gaps))
            )
        self._state.runtime_schema_validated = True
        _mark_schema_complete(self._authorization, db_path)
        return {
            "created_schema": 0,
            "backfilled_run_subjects": run_backfills,
            "backfilled_entry_subjects": entry_backfills,
            "discarded_unattributable_legacy_entries": discarded,
            "unattributable_legacy_entry_count": len(unattributable),
            "unrecoverable_run_tombstone_history_discarded": int(
                unrecoverable_history_discarded
            ),
            "rekeyed_run_ids": rekeyed_run_ids,
            "rekeyed_entry_ids": rekeyed_entry_ids,
            "redacted_error_codes": len(unsafe_error_entry_ids),
            "redacted_metadata_rows": len(metadata_updates),
            "redacted_timestamp_rows": len(timestamp_updates),
        }

    def import_historical_observation(self, record: Any) -> bool:
        """Import one attributable metadata-only historical observation."""
        db_path = self._source()
        if not _schema_reconciled(self._authorization):
            raise ModelCallLedgerInvariantError(
                "historical import requires completed backup-gated schema reconciliation"
            )
        fingerprint = str(getattr(record, "fingerprint", "") or "").strip()
        if not fingerprint:
            fingerprint = _json_hash(
                {
                    "operation": getattr(record, "operation", ""),
                    "provider": getattr(record, "provider", ""),
                    "model": getattr(record, "model", ""),
                    "input_digest": getattr(record, "input_digest", ""),
                    "created_at": getattr(record, "created_at", ""),
                }
            )
        if not _is_digest_reference(fingerprint):
            raise ModelCallLedgerInvariantError("legacy fingerprint must be a canonical one-way digest")
        normalized_input_digest = str(getattr(record, "input_digest", "") or "").strip()
        if not _is_digest_reference(normalized_input_digest):
            raise ModelCallLedgerInvariantError(
                "historical input digest must be a canonical one-way digest"
            )
        normalized_input_tokens = _nonnegative_int(
            getattr(record, "input_tokens", 0), label="historical input tokens"
        )
        normalized_output_tokens = _nonnegative_int(
            getattr(record, "output_tokens", 0), label="historical output tokens"
        )
        normalized_latency_ms = _nonnegative_int(
            getattr(record, "latency_ms", 0), label="historical latency"
        )
        normalized_created_at = _canonical_timestamp(getattr(record, "created_at", ""))
        normalized_operation = _normalize_operation(
            getattr(record, "operation", ""), historical=True
        )
        normalized_provider = _normalize_provider_label(
            getattr(record, "provider", ""), historical=True
        )
        normalized_model = _normalize_model_label(getattr(record, "model", ""), historical=True)
        subject_scope = getattr(record, "subject_scope", None)
        if subject_scope is None:
            raise ModelCallLedgerInvariantError(
                "legacy observation requires explicit preserved subject attribution"
            )
        run_id = _canonical_run_id(f"legacy:{_hash_text(fingerprint)[:32]}")
        entry_id = _new_canonical_entry_id()
        binding = self._retention._subject_binding(subject_scope)
        if binding is None:
            raise ModelCallLedgerInvariantError("legacy subject attribution disappeared")
        with self._state.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._validation._require_current_runtime_data_integrity(
                conn, operation="historical observation import"
            )
            existing_run = conn.execute(
                "SELECT run_id FROM model_call_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing_run is None:
                now = _utc_now()
                conn.execute(
                    "INSERT INTO model_call_runs(run_id, cost_budget, created_at, schema_version) "
                    "VALUES (?, NULL, ?, ?)",
                    (run_id, now, SCHEMA_VERSION),
                )
                conn.execute(
                    "INSERT INTO model_call_run_subjects("
                    "run_id, scope_kind, subject_hash, created_at) VALUES (?, ?, ?, ?)",
                    (run_id, binding[0], binding[1], now),
                )
            elif self._retention._run_binding(conn, run_id) != binding:
                raise ModelCallLedgerInvariantError(
                    "legacy fingerprint was reused with a different subject attribution"
                )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO model_call_entries(
                    entry_id, run_id, operation, provider, model, input_digest,
                    reserved_input_tokens, reserved_output_tokens, reserved_cost,
                    actual_input_tokens, actual_output_tokens, actual_total_tokens,
                    actual_cost, latency_ms, price_version, cache_status, retry_attempt,
                    input_price, output_price, lifecycle_state, error_code,
                    legacy_fingerprint, created_at, settled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, NULL, ?, ?, 'legacy', 0,
                          ?, ?, 'legacy_observed', ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    run_id,
                    normalized_operation,
                    normalized_provider,
                    normalized_model,
                    normalized_input_digest,
                    normalized_input_tokens,
                    normalized_output_tokens,
                    normalized_input_tokens,
                    normalized_output_tokens,
                    normalized_input_tokens + normalized_output_tokens,
                    normalized_latency_ms,
                    "legacy-observation-unbillable-v1",
                    0.0,
                    0.0,
                    "" if bool(getattr(record, "success", True)) else "legacy_failure",
                    fingerprint,
                    normalized_created_at,
                    normalized_created_at,
                ),
            )
            if cursor.rowcount:
                conn.execute(
                    "INSERT INTO model_call_entry_subjects("
                    "entry_id, scope_kind, subject_hash, created_at) VALUES (?, ?, ?, ?)",
                    (entry_id, binding[0], binding[1], _utc_now()),
                )
            conn.commit()
        _advance_source_signature(self._authorization, db_path)
        return bool(cursor.rowcount)

    def require_cleanup_ready(self) -> None:
        """Prove that only counted retired owners remain before their deletion."""
        self._source()
        if not _schema_reconciled(self._authorization):
            raise ModelCallLedgerInvariantError(
                "canonical retired cleanup requires completed schema reconciliation"
            )
        with self._state.connect() as conn:
            gaps = self._validation._runtime_schema_gaps(
                conn, allow_retired_prompt_tables=True
            )
        if gaps:
            raise ModelCallLedgerInvariantError(
                "canonical retired cleanup requires a valid reconciled ledger: "
                + ", ".join(sorted(gaps))
            )

    def assert_runtime_valid(self, conn: sqlite3.Connection) -> None:
        """Check the final canonical transaction before its cleanup commit."""
        self._source()
        gaps = self._validation._runtime_schema_gaps(conn)
        if gaps:
            raise ModelCallLedgerInvariantError(
                "canonical retired cleanup did not satisfy runtime schema: "
                + ", ".join(sorted(gaps))
            )

    def complete_canonical_cleanup(self) -> None:
        """Advance authorization only after cleanup's final schema proof."""
        # Cleanup intentionally changes the source signature by dropping the
        # last retired owner.  Do not call ``_source()`` here: that validator
        # quite correctly rejects a changed signature until this method
        # records the post-cleanup generation.  The preceding
        # ``require_cleanup_ready()`` and transaction-local
        # ``assert_runtime_valid()`` establish the authorization and exact
        # final schema; the lexical token remains live until ``close()``.
        self._ensure_open()
        source = self._state.db_path
        with self._state.connect() as conn:
            gaps = self._validation._runtime_schema_gaps(conn)
        if gaps:
            raise ModelCallLedgerInvariantError(
                "canonical retired cleanup did not satisfy runtime schema: "
                + ", ".join(sorted(gaps))
            )
        _advance_source_signature(self._authorization, source)

    def close(self) -> None:
        if self._closed:
            return
        _revoke_authorization(self._authorization)
        self._closed = True


class LedgerReconciliation:
    """The only internal entry point for backup-gated ledger migration."""

    @staticmethod
    def prepare_backup(source_db: Path) -> object:
        return _prepare_backup(source_db)

    @staticmethod
    def proof_source(proof: object) -> Path:
        return _proof_source(proof)

    @staticmethod
    def write_verified_backup(proof: object, backup_db: Path) -> object:
        return _write_verified_backup(proof, backup_db)

    @staticmethod
    def backup_identity(path: Path) -> str:
        return _private_sqlite_backup_identity(path)

    @staticmethod
    def open_after_verified_backup(
        prepared_backup: object,
        backup_receipt: object | None,
        *,
        config: Any | None = None,
    ) -> LedgerReconciliationSession:
        authorization = _issue_authorization(prepared_backup, backup_receipt)
        try:
            return LedgerReconciliationSession(authorization, config=config)
        except BaseException:
            # Issuing an authorization consumes its proof/receipt.  If the
            # session cannot be constructed, immediately revoke that
            # otherwise unreachable token rather than retaining an in-memory
            # migration capability for the process lifetime.
            _revoke_authorization(authorization)
            raise

    @staticmethod
    def inspect_canonical(path: Path) -> CanonicalInspection:
        """Read canonical deduplication state without opening an old schema for write."""
        counts = {
            "canonical_entry_count": 0,
            "canonical_unattributable_legacy_count": 0,
            "canonical_unattributable_billable_count": 0,
            "canonical_privacy_schema_missing": 0,
            "canonical_unrecoverable_run_tombstone_history": 0,
        }
        candidate = Path(path).expanduser()
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            return "missing", set(), counts
        except OSError:
            return "invalid", set(), counts
        if stat_module.S_ISLNK(metadata.st_mode) or not stat_module.S_ISREG(metadata.st_mode):
            return "invalid", set(), counts
        try:
            conn = sqlite3.connect(candidate.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
            conn.row_factory = sqlite3.Row
            try:
                tables = LedgerSchemaValidation._table_names(conn)
                if not {"model_call_runs", "model_call_entries"}.issubset(tables):
                    return "invalid", set(), counts
                counts["canonical_entry_count"] = int(
                    conn.execute("SELECT COUNT(*) FROM model_call_entries").fetchone()[0] or 0
                )
                preflight_gaps = LedgerSchemaValidation._reconciliation_preflight_gaps(
                    conn, allow_retired_prompt_tables=True
                )
                if preflight_gaps:
                    return "invalid", set(), counts
                counts["canonical_unrecoverable_run_tombstone_history"] = int(
                    LedgerSchemaValidation._retired_cascading_run_tombstone_shape(conn)
                )
                runtime_gaps = LedgerSchemaValidation._runtime_schema_gaps(
                    conn, allow_retired_prompt_tables=True
                )
                if runtime_gaps:
                    counts["canonical_privacy_schema_missing"] = 1
                    has_entry_subjects = "model_call_entry_subjects" in tables
                    legacy_predicate = "e.lifecycle_state='legacy_observed'"
                    billable_predicate = "e.lifecycle_state<>'legacy_observed'"
                    if has_entry_subjects:
                        for label, predicate in (
                            ("canonical_unattributable_legacy_count", legacy_predicate),
                            ("canonical_unattributable_billable_count", billable_predicate),
                        ):
                            counts[label] = int(
                                conn.execute(
                                    render_sql(
                                        "SELECT COUNT(*) FROM model_call_entries e "
                                        "WHERE {predicate} AND NOT EXISTS ("
                                        "SELECT 1 FROM model_call_entry_subjects s "
                                        "WHERE s.entry_id=e.entry_id)",
                                        fixed_fragments={
                                            "predicate": (
                                                predicate,
                                                {
                                                    legacy_predicate,
                                                    billable_predicate,
                                                },
                                            )
                                        },
                                    )
                                ).fetchone()[0]
                                or 0
                            )
                    else:
                        counts["canonical_unattributable_legacy_count"] = int(
                            conn.execute(
                                "SELECT COUNT(*) FROM model_call_entries "
                                "WHERE lifecycle_state='legacy_observed'"
                            ).fetchone()[0]
                            or 0
                        )
                        counts["canonical_unattributable_billable_count"] = int(
                            conn.execute(
                                "SELECT COUNT(*) FROM model_call_entries "
                                "WHERE lifecycle_state<>'legacy_observed'"
                            ).fetchone()[0]
                            or 0
                        )
                    if counts["canonical_unattributable_billable_count"]:
                        return "privacy_reconciliation_required", set(), counts
                    if counts["canonical_unattributable_legacy_count"]:
                        return "unattributable_legacy_required", set(), counts
                    return "privacy_reconciliation_required", set(), counts
                counts["canonical_unattributable_legacy_count"] = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM model_call_entries e
                        WHERE e.lifecycle_state='legacy_observed' AND NOT EXISTS (
                            SELECT 1 FROM model_call_entry_subjects s WHERE s.entry_id=e.entry_id
                        )
                        """
                    ).fetchone()[0]
                    or 0
                )
                counts["canonical_unattributable_billable_count"] = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM model_call_entries e
                        WHERE e.lifecycle_state<>'legacy_observed' AND NOT EXISTS (
                            SELECT 1 FROM model_call_entry_subjects s WHERE s.entry_id=e.entry_id
                        )
                        """
                    ).fetchone()[0]
                    or 0
                )
                if counts["canonical_unattributable_billable_count"]:
                    return "privacy_reconciliation_required", set(), counts
                if counts["canonical_unattributable_legacy_count"]:
                    return "unattributable_legacy_required", set(), counts
                rows = conn.execute(
                    """
                    SELECT e.legacy_fingerprint
                    FROM model_call_entries e
                    WHERE e.legacy_fingerprint IS NOT NULL AND e.legacy_fingerprint<>''
                      AND EXISTS (
                          SELECT 1 FROM model_call_entry_subjects s WHERE s.entry_id=e.entry_id
                      )
                    """
                ).fetchall()
                return "ready", {str(row[0]) for row in rows}, counts
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            return "invalid", set(), counts


__all__ = [
    "CanonicalInspection",
    "LedgerReconciliation",
    "LedgerReconciliationSession",
]
