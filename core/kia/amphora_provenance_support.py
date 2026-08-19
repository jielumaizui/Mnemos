"""Object-level legacy Amphora provenance inventory and verified backups."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any, Callable, Literal, overload

from core.ops.durable_io import (
    DurableIOError,
    SecureImmutablePublishReceipt,
    ensure_private_directory,
    fsync_directory,
    fsync_regular_file,
    normalize_private_sqlite_copy,
    owned_sqlite_connection_pair,
    secure_cleanup_created_tree,
    secure_create_directory,
    secure_publish_immutable_bytes,
    secure_publish_immutable_text,
    secure_read_bytes,
    secure_regular_file_preimage,
    validate_secure_created_file_receipts,
    validate_private_sqlite_copy,
)
from core.ops.readiness_query_budget import connect_readonly_sqlite


@dataclass(frozen=True)
class AmphoraProvenanceContext:
    """Explicit owner dependencies for historical provenance reconciliation."""

    db_path: Callable[[], Path]
    normalize_messages: Callable[[Any], Any]
    messages_revision: Callable[[Any], str]
    conn_seconds: int
    legacy_provenance_reason: str
    migration_schema: str


def _sha256_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


@overload
def read_exact_regular_file_bytes(
    path: str | Path,
    *,
    purpose: str,
    required: Literal[True] = True,
) -> bytes: ...


@overload
def read_exact_regular_file_bytes(
    path: str | Path,
    *,
    purpose: str,
    required: Literal[False],
) -> bytes | None: ...


def read_exact_regular_file_bytes(
    path: str | Path,
    *,
    purpose: str,
    required: bool = True,
) -> bytes | None:
    """Read one exact regular leaf without following any path component."""

    candidate = Path(path).expanduser().absolute()
    if not candidate.name:
        if required:
            raise ValueError(f"{purpose} is missing")
        return None
    try:
        content = secure_read_bytes(candidate.parent, candidate.name)
    except (DurableIOError, OSError):
        raise ValueError(f"{purpose} is unsafe") from None
    if content is None and required:
        raise ValueError(f"{purpose} is missing")
    return content


@overload
def read_owned_message_asset_bytes(
    *,
    database_path: Path,
    messages_path: str | Path,
    purpose: str,
    required: Literal[True] = True,
) -> bytes: ...


@overload
def read_owned_message_asset_bytes(
    *,
    database_path: Path,
    messages_path: str | Path,
    purpose: str,
    required: Literal[False],
) -> bytes | None: ...


def read_owned_message_asset_bytes(
    *,
    database_path: Path,
    messages_path: str | Path,
    purpose: str,
    required: bool = True,
) -> bytes | None:
    """Read one Amphora message leaf from the queue-owned asset directory."""

    raw_path = str(messages_path or "")
    if not raw_path:
        if required:
            raise ValueError(f"{purpose} is missing")
        return None
    root = Path(database_path).expanduser().absolute().parent / "distill_messages"
    candidate = Path(raw_path).expanduser().absolute()
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise ValueError(f"{purpose} is outside owner") from None
    if len(relative.parts) != 1:
        raise ValueError(f"{purpose} is outside owner")
    try:
        content = secure_read_bytes(root, relative)
    except (DurableIOError, OSError):
        raise ValueError(f"{purpose} is unsafe") from None
    if content is None and required:
        raise ValueError(f"{purpose} is missing")
    return content


def _remove_created_backup_leaf(
    leaf: Path,
    *,
    creation_preimage: dict[str, object],
    created_file_preimages: dict[str, dict[str, object]],
) -> None:
    """Remove only the exact backup generation created by the current call."""

    secure_cleanup_created_tree(
        leaf.parent,
        created_files={
            Path(leaf.name, name): preimage for name, preimage in created_file_preimages.items()
        },
        created_directories={Path(leaf.name): creation_preimage},
    )


def _visible_message_projection(messages: object) -> list[dict[str, str]]:
    if not isinstance(messages, list) or not messages:
        raise ValueError("Amphora provenance messages must be a non-empty list")
    projected: list[dict[str, str]] = []
    for message in messages:
        if (
            not isinstance(message, dict)
            or not isinstance(message.get("role"), str)
            or not isinstance(message.get("content"), str)
        ):
            raise ValueError("Amphora provenance messages require exact role/content strings")
        projected.append({"role": str(message["role"]), "content": str(message["content"])})
    return projected


def _validated_provenance_backup(
    *,
    context: AmphoraProvenanceContext,
    manifest_path: Path,
    inventory_hash: str,
    reviewed_object: dict,
    expected_manifest_hash: str = "",
) -> str:
    manifest_path = Path(manifest_path).expanduser().absolute()
    try:
        manifest_bytes = read_exact_regular_file_bytes(
            manifest_path,
            purpose="Amphora provenance backup manifest",
        )
        document = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("Amphora provenance backup manifest is invalid") from exc
    if not isinstance(document, dict):
        raise ValueError("Amphora provenance backup manifest is invalid")
    declared_hash = str(document.get("manifest_hash") or "")
    manifest = {key: value for key, value in document.items() if key != "manifest_hash"}
    actual_hash = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    if not declared_hash or declared_hash != actual_hash:
        raise ValueError("Amphora provenance backup manifest hash mismatch")
    if expected_manifest_hash and declared_hash != expected_manifest_hash:
        raise ValueError("Amphora provenance backup receipt hash mismatch")
    if (
        manifest.get("schema_version") != "mnemos.amphora_provenance_backup.v1"
        or str(manifest.get("inventory_hash") or "") != inventory_hash
        or str(manifest.get("legacy_task_id") or "") != str(reviewed_object["primary_key"])
        or str(manifest.get("legacy_object_hash") or "") != str(reviewed_object["object_hash"])
    ):
        raise ValueError("Amphora provenance backup identity mismatch")
    database_backup = manifest.get("database_backup")
    messages_backup = manifest.get("messages_backup")
    if not isinstance(database_backup, dict) or not isinstance(messages_backup, dict):
        raise ValueError("Amphora provenance backup assets are invalid")
    backup_leaf = manifest_path.parent
    db_backup = (backup_leaf / "distill_queue.db").absolute()
    declared_db_backup = Path(str(database_backup.get("path") or "")).expanduser().absolute()
    if declared_db_backup != db_backup:
        raise ValueError("Amphora provenance database backup identity mismatch")
    db_backup_bytes = read_exact_regular_file_bytes(
        db_backup,
        purpose="Amphora provenance database backup",
    )
    if _sha256_bytes(db_backup_bytes) != str(database_backup.get("sha256") or ""):
        raise ValueError("Amphora provenance database backup hash mismatch")
    try:
        validate_private_sqlite_copy(db_backup)
    except DurableIOError:
        raise ValueError("Amphora provenance database backup is unsafe") from None
    with sqlite3.connect(str(db_backup), timeout=context.conn_seconds) as backup_conn:
        backup_conn.row_factory = sqlite3.Row
        integrity = str(backup_conn.execute("PRAGMA integrity_check").fetchone()[0])
        backed_up_row = backup_conn.execute(
            "SELECT * FROM distillation_tasks WHERE task_id=?",
            (str(reviewed_object["primary_key"]),),
        ).fetchone()
    if integrity != "ok" or str(database_backup.get("integrity_check") or "") != "ok":
        raise ValueError("Amphora provenance database backup integrity failed")
    if backed_up_row is None or dict(backed_up_row) != reviewed_object["row"]:
        raise ValueError("Amphora provenance database backup drifted from review")
    expected_messages = reviewed_object["messages_asset"]
    messages_path = (backup_leaf / "messages.json").absolute()
    declared_messages_path = Path(str(messages_backup.get("path") or "")).expanduser().absolute()
    if declared_messages_path != messages_path:
        raise ValueError("Amphora provenance messages backup identity mismatch")
    messages_bytes = read_exact_regular_file_bytes(
        messages_path,
        purpose="Amphora provenance messages backup",
        required=False,
    )
    if bool(expected_messages["exists"]):
        if (
            messages_bytes is None
            or len(messages_bytes) != int(expected_messages["size"])
            or _sha256_bytes(messages_bytes) != expected_messages["sha256"]
            or str(messages_backup.get("sha256") or "") != expected_messages["sha256"]
        ):
            raise ValueError("Amphora provenance messages backup hash mismatch")
    elif messages_bytes is not None or str(messages_backup.get("sha256") or ""):
        raise ValueError("Amphora provenance unexpected messages backup")
    return declared_hash


def _historical_provenance_inventory_in_connection(
    conn: sqlite3.Connection,
    *,
    context: AmphoraProvenanceContext,
) -> dict:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM distillation_tasks WHERE terminal_reason=? " "ORDER BY task_id",
        (context.legacy_provenance_reason,),
    ).fetchall()
    migration_table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' " "AND name='amphora_provenance_migrations'"
    ).fetchone()
    migrations = (
        {
            str(row["legacy_task_id"]): dict(row)
            for row in conn.execute("SELECT * FROM amphora_provenance_migrations")
        }
        if migration_table_exists
        else {}
    )
    objects: list[dict] = []
    for row in rows:
        row_payload = {key: row[key] for key in row.keys()}
        messages_path = str(row["messages_path"] or "")
        messages_bytes = read_owned_message_asset_bytes(
            database_path=context.db_path(),
            messages_path=messages_path,
            purpose="provenance messages asset",
            required=False,
        )
        messages_asset = {
            "path": str(messages_path),
            "exists": messages_bytes is not None,
            "size": len(messages_bytes) if messages_bytes is not None else 0,
            "sha256": _sha256_bytes(messages_bytes) if messages_bytes is not None else "",
        }
        identity = {
            "schema_version": context.migration_schema,
            "source_table": "distillation_tasks",
            "primary_key": str(row["task_id"]),
            "old_input_revision": str(row["input_revision"] or ""),
            "row": row_payload,
            "messages_asset": messages_asset,
        }
        object_hash = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    identity,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        migration = migrations.get(str(row["task_id"]))
        coverage_error = ""
        if not migration:
            coverage_error = "migration_receipt_missing"
        elif (
            migration.get("schema_version") != context.migration_schema
            or migration.get("legacy_input_revision") != str(row["input_revision"] or "")
            or migration.get("legacy_object_hash") != object_hash
        ):
            coverage_error = "migration_receipt_legacy_binding_mismatch"
        else:
            canonical = conn.execute(
                "SELECT * FROM distillation_tasks WHERE task_id=?",
                (str(migration.get("canonical_task_id") or ""),),
            ).fetchone()
            if canonical is None:
                coverage_error = "canonical_task_missing"
            else:
                canonical_payload = dict(canonical)
                try:
                    canonical_messages_bytes = read_owned_message_asset_bytes(
                        database_path=context.db_path(),
                        messages_path=str(canonical_payload.get("messages_path") or ""),
                        purpose="canonical provenance messages asset",
                    )
                    canonical_messages = json.loads(canonical_messages_bytes.decode("utf-8"))
                    canonical_meta = json.loads(str(canonical_payload.get("meta") or "{}"))
                except (
                    UnicodeError,
                    json.JSONDecodeError,
                    TypeError,
                    ValueError,
                ):
                    coverage_error = "canonical_task_asset_invalid"
                else:
                    provenance = (
                        canonical_meta.get("provenance_migration")
                        if isinstance(canonical_meta, dict)
                        else None
                    )
                    expected_provenance = {
                        "schema_version": context.migration_schema,
                        "migration_id": str(migration.get("migration_id") or ""),
                        "legacy_task_id": str(row["task_id"]),
                        "legacy_object_hash": object_hash,
                        "inventory_hash": str(migration.get("inventory_hash") or ""),
                        "backup_manifest_hash": str(migration.get("backup_manifest_hash") or ""),
                    }
                    if (
                        str(canonical_payload.get("input_revision") or "")
                        != str(migration.get("canonical_input_revision") or "")
                        or str(canonical_payload.get("handoff_receipt_id") or "")
                        != str(migration.get("handoff_receipt_id") or "")
                        or context.messages_revision(context.normalize_messages(canonical_messages))
                        != str(migration.get("canonical_input_revision") or "")
                        or not isinstance(canonical_meta, dict)
                        or str(canonical_meta.get("messages_revision") or "")
                        != str(migration.get("canonical_input_revision") or "")
                        or provenance != expected_provenance
                    ):
                        coverage_error = "canonical_task_binding_mismatch"
                if not coverage_error:
                    try:
                        _validated_provenance_backup(
                            context=context,
                            manifest_path=Path(str(migration.get("backup_manifest_path") or "")),
                            inventory_hash=str(migration.get("inventory_hash") or ""),
                            reviewed_object={
                                **identity,
                                "object_hash": object_hash,
                            },
                            expected_manifest_hash=str(migration.get("backup_manifest_hash") or ""),
                        )
                    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
                        coverage_error = "backup_receipt_binding_mismatch"
        covered = not coverage_error
        objects.append(
            {
                **identity,
                "object_hash": object_hash,
                "covered": covered,
                "coverage_error": coverage_error,
                "migration_id": str(migration.get("migration_id") or "") if migration else "",
            }
        )
    inventory_core = {
        "schema_version": context.migration_schema,
        "database": str(context.db_path().resolve(strict=False)),
        "objects": [
            {
                key: value
                for key, value in item.items()
                if key not in {"covered", "coverage_error", "migration_id"}
            }
            for item in objects
        ],
    }
    inventory_hash = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                inventory_core,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    return {
        **inventory_core,
        "inventory_hash": inventory_hash,
        "object_count": len(objects),
        "uncovered_count": sum(not item["covered"] for item in objects),
        "objects": objects,
    }


def build_historical_provenance_inventory(
    *,
    context: AmphoraProvenanceContext,
) -> dict:
    """Inventory every historical Amphora object by exact row and messages bytes."""

    path = context.db_path()
    if not path.is_file():
        core = {
            "schema_version": context.migration_schema,
            "database": str(path.resolve(strict=False)),
            "objects": [],
        }
        inventory_hash = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    core,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        return {
            **core,
            "inventory_hash": inventory_hash,
            "object_count": 0,
            "uncovered_count": 0,
        }
    conn = connect_readonly_sqlite(
        path,
        timeout_seconds=context.conn_seconds,
    )
    conn.row_factory = sqlite3.Row
    try:
        return _historical_provenance_inventory_in_connection(conn, context=context)
    finally:
        conn.close()


def _backup_historical_provenance_object(
    *,
    context: AmphoraProvenanceContext,
    backup_dir: Path,
    inventory: dict,
    reviewed_object: dict,
) -> tuple[Path, str]:
    try:
        backup_root = ensure_private_directory(Path(backup_dir))
    except DurableIOError:
        raise ValueError("Amphora provenance backup root is unsafe")
    primary_key = str(reviewed_object["primary_key"])
    if not primary_key or Path(primary_key).name != primary_key or primary_key in {".", ".."}:
        raise ValueError("Amphora provenance backup identity is invalid")
    leaf = backup_root / primary_key
    manifest_path = leaf / "backup_manifest.json"
    try:
        creation_preimage = secure_create_directory(backup_root, primary_key)
    except FileExistsError:
        manifest_hash = _validated_provenance_backup(
            context=context,
            manifest_path=manifest_path,
            inventory_hash=str(inventory["inventory_hash"]),
            reviewed_object=reviewed_object,
        )
        return manifest_path, manifest_hash
    created_file_preimages: dict[str, dict[str, object]] = {}
    try:
        db_backup = leaf / "distill_queue.db"
        descriptor = os.open(
            db_backup,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        try:
            metadata = os.fstat(descriptor)
            created_file_preimages[db_backup.name] = {
                "device": int(metadata.st_dev),
                "inode": int(metadata.st_ino),
            }
        finally:
            os.close(descriptor)
        with owned_sqlite_connection_pair(
            lambda: connect_readonly_sqlite(
                context.db_path(),
                timeout_seconds=context.conn_seconds,
            ),
            lambda: sqlite3.connect(
                str(db_backup),
                timeout=context.conn_seconds,
            ),
        ) as (source, target):
            source.backup(target)
            integrity = str(target.execute("PRAGMA integrity_check").fetchone()[0])
            target.row_factory = sqlite3.Row
            backed_up_row = target.execute(
                "SELECT * FROM distillation_tasks WHERE task_id=?",
                (primary_key,),
            ).fetchone()
        if integrity != "ok":
            raise RuntimeError("Amphora provenance backup integrity check failed")
        if backed_up_row is None or dict(backed_up_row) != reviewed_object["row"]:
            raise ValueError("Amphora provenance database backup drifted from review")
        normalize_private_sqlite_copy(db_backup)
        db_backup.chmod(0o600)
        fsync_regular_file(db_backup)
        db_preimage = secure_regular_file_preimage(leaf, db_backup.name)
        if db_preimage is None:
            raise RuntimeError("Amphora provenance database backup receipt missing")
        created_file_preimages[db_backup.name] = db_preimage
        db_backup_bytes = read_exact_regular_file_bytes(
            db_backup,
            purpose="Amphora provenance database backup",
        )

        expected_messages = reviewed_object["messages_asset"]
        source_messages_bytes = read_owned_message_asset_bytes(
            database_path=context.db_path(),
            messages_path=str(expected_messages.get("path") or ""),
            purpose="provenance messages asset",
            required=False,
        )
        if bool(expected_messages["exists"]) != (source_messages_bytes is not None):
            raise ValueError("Amphora provenance messages asset drifted from review")
        messages_backup = leaf / "messages.json"
        if source_messages_bytes is not None:
            if (
                len(source_messages_bytes) != int(expected_messages["size"])
                or _sha256_bytes(source_messages_bytes) != expected_messages["sha256"]
            ):
                raise ValueError("Amphora provenance messages asset drifted from review")
            messages_publication = secure_publish_immutable_bytes(
                leaf,
                messages_backup.name,
                source_messages_bytes,
                return_receipt=True,
            )
            if not isinstance(
                messages_publication,
                SecureImmutablePublishReceipt,
            ):
                raise RuntimeError("Amphora provenance messages backup receipt missing")
            if messages_publication.created:
                created_file_preimages[messages_backup.name] = messages_publication.preimage

        manifest = {
            "schema_version": "mnemos.amphora_provenance_backup.v1",
            "inventory_hash": inventory["inventory_hash"],
            "legacy_task_id": reviewed_object["primary_key"],
            "legacy_object_hash": reviewed_object["object_hash"],
            "database_backup": {
                "path": str(db_backup),
                "sha256": _sha256_bytes(db_backup_bytes),
                "integrity_check": integrity,
            },
            "messages_backup": {
                "path": str(messages_backup),
                "sha256": (
                    _sha256_bytes(source_messages_bytes)
                    if source_messages_bytes is not None
                    else ""
                ),
            },
        }
        manifest_hash = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        manifest_publication = secure_publish_immutable_text(
            leaf,
            manifest_path.name,
            json.dumps(
                {**manifest, "manifest_hash": manifest_hash},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
            return_receipt=True,
        )
        if not isinstance(
            manifest_publication,
            SecureImmutablePublishReceipt,
        ):
            raise RuntimeError("Amphora provenance manifest backup receipt missing")
        if manifest_publication.created:
            created_file_preimages[manifest_path.name] = manifest_publication.preimage
        fsync_directory(leaf)
        validate_secure_created_file_receipts(
            backup_root,
            {
                Path(primary_key, name): preimage
                for name, preimage in created_file_preimages.items()
            },
        )
        return manifest_path, manifest_hash
    except BaseException:
        _remove_created_backup_leaf(
            leaf,
            creation_preimage=creation_preimage,
            created_file_preimages=created_file_preimages,
        )
        raise
