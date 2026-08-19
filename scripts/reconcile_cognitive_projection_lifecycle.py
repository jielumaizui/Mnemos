#!/usr/bin/env python3
"""Plan or apply the COG-050 L2.4-L5 projection reconciliation.

Dry-run is the default.  It renders every owned projection into an isolated
shadow vault from read-only canonical stores, then emits an exact create,
update, and delete allowlist.  Apply requires the reviewed plan hash and a new
backup directory, holds the shared offline-migration lock, revalidates the
plan, backs up every overwritten/deleted page plus ``wiki_projection.db``, and
only then publishes through the typed derived-projection lifecycle.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.vaults.vault_sync import (
    _canonical_source_hashes,
    _sqlite_artifact_fallback_hash,
    _sqlite_logical_image,
    sync_all_projections,
)
from core.wiki_derived_projection import DerivedProjectionLifecycle
from core.wiki_projection_lifecycle import WikiProjectionLedger
from scripts.audit_cognitive_projection_lifecycle import audit_live_projection_state


SCHEMA_VERSION = "mnemos.cognitive_projection_reconciliation.v1"
OWNED_SCOPE_ROOTS = (
    Path("L2.4-KG"),
    Path("L4-Reflections/Reflections"),
    Path("L4-Reflections/Shifts"),
    Path("L4-Reflections/KnowledgeUpdates"),
    Path("L4-Reflections/Reports"),
    Path("L5-Feedback/user-persona-history"),
)
OWNED_EXACT_FILES = frozenset({Path("L5-Feedback/user-persona.md")})


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def publish(self, event: Any) -> str:
        self.events.append(event)
        return str(event.trace_id)


class _ConfigProxy:
    def __init__(self, source: Any, *, database_dir: Path, wiki_dir: Path):
        self._source = source
        self.database_dir = database_dir
        self.data_dir = database_dir
        self.mnemos_dir = database_dir
        self.wiki_dir = wiki_dir

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)

    def get(self, key: str, default: Any = None) -> Any:
        getter = getattr(self._source, "get", None)
        return getter(key, default) if callable(getter) else default

    def vault_dir(self, name: str) -> Path:
        if name == "mnemos":
            return self.wiki_dir
        if name == "raw":
            return self.mnemos_dir / "raw"
        target = getattr(self._source, "vault_dir", None)
        if callable(target):
            return Path(target(name))
        raise KeyError(name)


@contextmanager
def _projection_dispatch_runtime(
    *,
    config: Any,
    ledger: WikiProjectionLedger,
    wiki_dir: Path,
):
    """Bind one apply run to its exact ledger and required consumers."""

    from core.cognitive_graph import CognitiveGraphStore, CognitiveGraphUpdater
    from core.mnemos_bus import EventBus
    from daemon.wiki_projection_handlers import register_wiki_projection_handlers

    bus = EventBus(
        config=config,
        run_startup_maintenance=False,
        recover_pending=False,
    )
    graph_store = None
    try:
        if bus.projection_db_path.resolve(strict=False) != ledger.db_path.resolve(
            strict=False
        ):
            raise RuntimeError("projection EventBus is bound to a different ledger")
        lifecycle = DerivedProjectionLifecycle(
            wiki_dir,
            ledger=ledger,
            event_bus=bus,
        )
        register_wiki_projection_handlers(
            bus,
            config,
            projection_lifecycle=lifecycle,
        )
        graph_store = CognitiveGraphStore(
            db_path=str(config.database_dir / "cognitive_graph.db"),
            ownership_config=config,
        )
        CognitiveGraphUpdater(store=graph_store, bus=bus).subscribe()
        bus.start_dispatch()
        yield lifecycle, bus
    finally:
        bus.close()
        close_store = getattr(graph_store, "close", None)
        if callable(close_store):
            close_store()


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _is_visible_markdown(relative: Path) -> bool:
    return relative.suffix.lower() == ".md" and not any(
        part.startswith(".") for part in relative.parts
    )


def _is_owned(relative: Path) -> bool:
    if relative in OWNED_EXACT_FILES:
        return True
    if relative.parent == Path("L3-Observations"):
        return True
    return any(relative == root or root in relative.parents for root in OWNED_SCOPE_ROOTS)


def _owned_inventory(vault_dir: Path) -> dict[str, dict[str, Any]]:
    inventory: dict[str, dict[str, Any]] = {}
    if not vault_dir.is_dir():
        return inventory
    for path in vault_dir.rglob("*.md"):
        if not path.is_file():
            continue
        relative = path.relative_to(vault_dir)
        if not _is_visible_markdown(relative) or not _is_owned(relative):
            continue
        data = path.read_bytes()
        inventory[relative.as_posix()] = {
            "sha256": _sha256_bytes(data),
            "bytes": len(data),
        }
    return dict(sorted(inventory.items()))


def _sqlite_snapshot_hash(path: Path) -> str:
    if not path.is_file():
        return "missing"
    try:
        connection = sqlite3.connect(
            f"file:{path.resolve(strict=True)}?mode=ro",
            uri=True,
            timeout=30,
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            return _sha256_bytes(_sqlite_logical_image(connection))
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        return _sha256_bytes(_sqlite_artifact_fallback_hash(path))


def _production_target_state_hash(vault_dir: Path, projection_db: Path) -> str:
    link_context: list[tuple[str, str]] = []
    if vault_dir.is_dir():
        for path in vault_dir.rglob("*.md"):
            if not path.is_file():
                continue
            relative = path.relative_to(vault_dir)
            if not _is_visible_markdown(relative) or _is_owned(relative):
                continue
            link_context.append(
                (relative.as_posix(), _sha256_bytes(path.read_bytes()))
            )
    return _sha256_json(
        {
            "owned_pages": _owned_inventory(vault_dir),
            "non_projection_link_context": sorted(link_context),
            "projection_db": _sqlite_snapshot_hash(projection_db),
        }
    )


def _build_sparse_shadow(source_vault: Path, shadow_vault: Path) -> int:
    """Copy non-owned Markdown into an isolated, containment-safe shadow Vault."""

    linked = 0
    if not source_vault.is_dir():
        return linked
    for source in source_vault.rglob("*.md"):
        if not source.is_file():
            continue
        relative = source.relative_to(source_vault)
        if not _is_visible_markdown(relative) or _is_owned(relative):
            continue
        target = shadow_vault / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        linked += 1
    return linked


def _operation_plan(
    current: Mapping[str, Mapping[str, Any]],
    desired: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    operations: list[dict[str, Any]] = []
    counts = {"create": 0, "update": 0, "delete": 0, "unchanged": 0}
    for relative in sorted(set(current) | set(desired)):
        before = current.get(relative)
        after = desired.get(relative)
        if before is None:
            action = "create"
        elif after is None:
            action = "delete"
        elif before["sha256"] != after["sha256"]:
            action = "update"
        else:
            counts["unchanged"] += 1
            continue
        counts[action] += 1
        operations.append(
            {
                "action": action,
                "relative_path": relative,
                "current_sha256": str((before or {}).get("sha256") or "missing"),
                "desired_sha256": str((after or {}).get("sha256") or "missing"),
                "current_bytes": int((before or {}).get("bytes") or 0),
                "desired_bytes": int((after or {}).get("bytes") or 0),
            }
        )
    return operations, counts


def _render_desired(
    *,
    config: Any,
    source_vault: Path,
    database_dir: Path,
    work_root: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    shadow_vault = work_root / "shadow-vault"
    linked = _build_sparse_shadow(source_vault, shadow_vault)
    ledger = WikiProjectionLedger(work_root / "wiki_projection.db")
    bus = _RecordingBus()
    lifecycle = DerivedProjectionLifecycle(
        shadow_vault,
        ledger=ledger,
        event_bus=bus,
    )
    proxy = _ConfigProxy(
        config,
        database_dir=database_dir,
        wiki_dir=shadow_vault,
    )
    summary = sync_all_projections(
        vault_dir=shadow_vault,
        raw_dir=work_root / "raw",
        commit=False,
        config=proxy,
        lifecycle=lifecycle,
    )
    if summary.get("status") != "ok":
        raise RuntimeError(
            "isolated projection render failed: "
            + json.dumps(summary, ensure_ascii=False, sort_keys=True)
        )
    return _owned_inventory(shadow_vault), {
        "linked_non_projection_pages": linked,
        "event_count": len(bus.events),
        "layers": {
            name: summary[name]
            for name in ("kg", "observation", "reflection", "persona")
        },
    }


def build_reconciliation_plan(
    *,
    config: Any,
    database_dir: Path,
    wiki_dir: Path,
) -> dict[str, Any]:
    """Build a mutation-free, exact COG-050 production plan."""

    database_dir = database_dir.expanduser().resolve(strict=False)
    wiki_dir = wiki_dir.expanduser().resolve(strict=False)
    projection_db = database_dir / "wiki_projection.db"
    sources_before = _canonical_source_hashes(database_dir)
    target_before = _production_target_state_hash(wiki_dir, projection_db)
    current = _owned_inventory(wiki_dir)
    with tempfile.TemporaryDirectory(prefix="mnemos-cog050-plan-") as raw_root:
        desired, render = _render_desired(
            config=config,
            source_vault=wiki_dir,
            database_dir=database_dir,
            work_root=Path(raw_root),
        )
    sources_after = _canonical_source_hashes(database_dir)
    target_after = _production_target_state_hash(wiki_dir, projection_db)
    if sources_before != sources_after:
        raise RuntimeError("dry-run changed a canonical source database")
    if target_before != target_after:
        raise RuntimeError("dry-run changed the production projection target")

    operations, counts = _operation_plan(current, desired)
    plan_material = {
        "schema_version": SCHEMA_VERSION,
        "database_dir": str(database_dir),
        "wiki_dir": str(wiki_dir),
        "source_hashes": sources_after,
        "target_state_hash": target_after,
        "desired_manifest": desired,
        "operations": operations,
    }
    return {
        **plan_material,
        "mode": "dry_run",
        "ok": True,
        "plan_hash": _sha256_json(plan_material),
        "counts": counts,
        "current_page_count": len(current),
        "desired_page_count": len(desired),
        "production_mutation_count": 0,
        "render": render,
    }


def _backup_sqlite(source: Path, destination: Path) -> dict[str, str]:
    if not source.is_file():
        return {"source": str(source), "status": "missing"}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(source), timeout=60) as source_conn:
        with sqlite3.connect(str(destination), timeout=60) as destination_conn:
            source_conn.backup(destination_conn)
            integrity = str(destination_conn.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise RuntimeError("wiki_projection.db backup failed integrity_check")
    return {
        "source": str(source),
        "backup": str(destination),
        "status": "backed_up",
        "snapshot_hash": _sqlite_snapshot_hash(destination),
    }


def _create_backup(
    *,
    plan: Mapping[str, Any],
    wiki_dir: Path,
    projection_db: Path,
    backup_dir: Path,
) -> dict[str, Any]:
    if backup_dir.exists():
        raise FileExistsError("backup directory must not already exist")
    backup_dir.mkdir(parents=True, exist_ok=False)
    files_root = backup_dir / "wiki-preimage"
    files: list[dict[str, Any]] = []
    for operation in plan["operations"]:
        if operation["action"] not in {"update", "delete"}:
            continue
        relative = Path(str(operation["relative_path"]))
        if relative.is_absolute() or not _is_owned(relative):
            raise ValueError(f"unsafe projection backup target: {relative}")
        source = wiki_dir / relative
        if not source.is_file():
            raise RuntimeError(f"reviewed projection preimage disappeared: {source}")
        destination = files_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied_hash = _sha256_bytes(destination.read_bytes())
        if copied_hash != operation["current_sha256"]:
            raise RuntimeError(f"projection backup hash mismatch: {relative}")
        files.append(
            {
                "relative_path": relative.as_posix(),
                "sha256": copied_hash,
                "backup": str(destination),
            }
        )
    ledger = _backup_sqlite(projection_db, backup_dir / "wiki_projection.sqlite3")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "plan_hash": plan["plan_hash"],
        "wiki_dir": str(wiki_dir),
        "projection_db": ledger,
        "files": files,
        "created_paths": [
            row["relative_path"]
            for row in plan["operations"]
            if row["action"] == "create"
        ],
    }
    manifest_path = backup_dir / "backup-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {**manifest, "manifest_path": str(manifest_path)}


def _verify_applied_manifest(
    wiki_dir: Path,
    desired: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, str]]:
    actual = _owned_inventory(wiki_dir)
    mismatches: list[dict[str, str]] = []
    for relative in sorted(set(actual) | set(desired)):
        current_hash = str((actual.get(relative) or {}).get("sha256") or "missing")
        desired_hash = str((desired.get(relative) or {}).get("sha256") or "missing")
        if current_hash != desired_hash:
            mismatches.append(
                {
                    "relative_path": relative,
                    "actual_sha256": current_hash,
                    "desired_sha256": desired_hash,
                }
            )
    return mismatches


def apply_reconciliation(
    *,
    config: Any,
    database_dir: Path,
    wiki_dir: Path,
    expected_plan_hash: str,
    backup_dir: Path,
    receipt_timeout: float = 30.0,
) -> dict[str, Any]:
    """Apply only an exact reviewed plan under the shared offline lock."""

    from core.ops.offline_migration_lock import offline_migration_lock

    database_dir = database_dir.expanduser().resolve(strict=False)
    wiki_dir = wiki_dir.expanduser().resolve(strict=False)
    projection_db = database_dir / "wiki_projection.db"
    with offline_migration_lock(database_dir):
        plan = build_reconciliation_plan(
            config=config,
            database_dir=database_dir,
            wiki_dir=wiki_dir,
        )
        if plan["plan_hash"] != expected_plan_hash:
            raise RuntimeError("reviewed projection plan hash does not match locked state")
        backup = _create_backup(
            plan=plan,
            wiki_dir=wiki_dir,
            projection_db=projection_db,
            backup_dir=backup_dir.expanduser().resolve(strict=False),
        )
        source_before = _canonical_source_hashes(database_dir)
        ledger = WikiProjectionLedger(projection_db)
        proxy = _ConfigProxy(
            config,
            database_dir=database_dir,
            wiki_dir=wiki_dir,
        )
        with _projection_dispatch_runtime(
            config=proxy,
            ledger=ledger,
            wiki_dir=wiki_dir,
        ) as (lifecycle, _bus):
            summary = sync_all_projections(
                vault_dir=wiki_dir,
                raw_dir=(
                    backup_dir.expanduser().resolve(strict=False)
                    / "apply-scratch"
                    / "raw"
                ),
                commit=False,
                config=proxy,
                lifecycle=lifecycle,
            )
            source_after = _canonical_source_hashes(database_dir)
            if source_before != source_after:
                raise RuntimeError(
                    "projection apply changed a canonical source database"
                )
            mismatches = _verify_applied_manifest(
                wiki_dir,
                plan["desired_manifest"],
            )

            deadline = time.monotonic() + max(0.0, float(receipt_timeout))
            live = audit_live_projection_state(
                wiki_dir=wiki_dir,
                projection_db=projection_db,
            )
            while (
                live.get("required_consumer_receipt_gap", 0)
                and time.monotonic() < deadline
            ):
                time.sleep(0.1)
                live = audit_live_projection_state(
                    wiki_dir=wiki_dir,
                    projection_db=projection_db,
                )
        ok = bool(
            summary.get("status") == "ok"
            and not mismatches
            and live.get("initialized")
            and live.get("projection_binding_gap", 0) == 0
            and live.get("stale_projection", 0) == 0
            and live.get("required_consumer_receipt_gap", 0) == 0
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply",
            "ok": ok,
            "plan_hash": plan["plan_hash"],
            "counts": plan["counts"],
            "backup": backup,
            "sync": summary,
            "manifest_mismatches": mismatches,
            "live": live,
            "canonical_source_hashes": source_after,
        }


def build_parser() -> argparse.ArgumentParser:
    """Build the dry-run/apply reconciliation command-line parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-dir", type=Path)
    parser.add_argument("--wiki-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-hash")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--receipt-timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    """Run reconciliation planning or an explicitly authorized apply."""

    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        from core.config import get_config

        base_config = get_config()
        database_dir = Path(args.database_dir or base_config.database_dir)
        wiki_dir = Path(args.wiki_dir or base_config.wiki_dir)
        config = _ConfigProxy(
            base_config,
            database_dir=database_dir,
            wiki_dir=wiki_dir,
        )
        if args.apply:
            if not args.expected_plan_hash or args.backup_dir is None:
                raise ValueError(
                    "--apply requires --expected-plan-hash and --backup-dir"
                )
            result = apply_reconciliation(
                config=config,
                database_dir=database_dir,
                wiki_dir=wiki_dir,
                expected_plan_hash=str(args.expected_plan_hash),
                backup_dir=args.backup_dir,
                receipt_timeout=args.receipt_timeout,
            )
        else:
            if args.expected_plan_hash or args.backup_dir is not None:
                raise ValueError("apply-only arguments require --apply")
            result = build_reconciliation_plan(
                config=config,
                database_dir=database_dir,
                wiki_dir=wiki_dir,
            )
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        LookupError,
        sqlite3.Error,
    ) as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply" if args.apply else "dry_run",
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        output = args.output.expanduser().resolve(strict=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
