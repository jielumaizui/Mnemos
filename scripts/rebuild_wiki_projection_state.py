#!/usr/bin/env python3
"""Full Wiki projection rebuild, idempotence comparison, and receipt closure."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive_graph import CognitiveGraphStore, CognitiveGraphUpdater  # noqa: E402
from core.config import get_config  # noqa: E402
from core.db_utils import render_sql  # noqa: E402
from core.kia.kg_event_handler import KGEventHandler  # noqa: E402
from core.kia.relation_endpoint_quality import is_derived_kg_scan_path  # noqa: E402
from core.embeddings import EmbeddingIndexManager  # noqa: E402
from core.embeddings.cache import EmbeddingCache  # noqa: E402
from core.embeddings.rate_limiter import SiliconFlowRateLimiter  # noqa: E402
from core.embeddings.siliconflow_client import (  # noqa: E402
    SiliconFlowEmbeddingClient,
)
from core.event_outcome import HandlerOutcome  # noqa: E402
from core.mnemos_bus import Event, EventBus  # noqa: E402
from core.ops.config_scope import use_config  # noqa: E402
from core.ops.offline_migration_lock import offline_migration_lock  # noqa: E402
from core.wiki_derived_projection import DerivedProjectionLifecycle  # noqa: E402
from core.wiki_metrics import WikiMetrics  # noqa: E402
from core.wiki_projection_lifecycle import WikiProjectionLedger  # noqa: E402
from core.wiki_navigation import rebuild_navigation  # noqa: E402
from scripts.wiki_projection_ann_audit import (  # noqa: E402
    compare_hnsw_indexes,
    relation_index_integrity,
    relation_label_map,
    wiki_label_map,
)
from scripts import wiki_projection_rebuild_state as rebuild_state  # noqa: E402


MAX_REBUILD_CYCLES = 6


class _IsolatedRuntimeConfig:
    """Preserve production settings while rebinding every durable path."""

    def __init__(
        self,
        base: Any,
        *,
        database_dir: Path,
        wiki_dir: Path,
        snapshot_path_replacements: tuple[tuple[str, str], ...],
    ) -> None:
        self._base = base
        self.database_dir = database_dir
        self.mnemos_dir = database_dir
        self.data_dir = database_dir
        self.wiki_dir = wiki_dir
        self.snapshot_path_replacements = snapshot_path_replacements

    def get(self, key: str, default: Any = None) -> Any:
        getter = getattr(self._base, "get", None)
        return getter(key, default) if callable(getter) else default


def _sqlite_backup(source: Path, destination: Path) -> bool:
    """Create a consistent SQLite backup if the source database exists."""

    if not source.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(source), timeout=30) as src, sqlite3.connect(
        str(destination), timeout=30
    ) as dst:
        src.backup(dst)
    return True


def _guarded_sqlite_state(
    path: Path,
    table_markers: dict[str, str],
    *,
    table_digest_columns: dict[str, tuple[str, ...] | None] | None = None,
) -> dict[str, Any]:
    """Capture mutation-sensitive state without creating or migrating a database."""

    resolved = path.expanduser().resolve(strict=False)
    if not resolved.is_file():
        return {"path": str(resolved), "exists": False, "tables": {}}
    tables: dict[str, Any] = {}
    with sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=30) as conn:
        existing = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table, marker in table_markers.items():
            if table not in existing:
                tables[table] = {"exists": False, "rows": 0, "max": None}
                continue
            column_rows = conn.execute(
                render_sql(
                    "PRAGMA table_info({table})",
                    identifiers={"table": table},
                )
            ).fetchall()
            ordered_columns = tuple(str(row[1]) for row in column_rows)
            columns = set(ordered_columns)
            maximum = (
                conn.execute(
                    render_sql(
                        "SELECT MAX({marker}) FROM {table}",
                        identifiers={"marker": marker, "table": table},
                    )
                ).fetchone()[0]
                if marker in columns
                else None
            )
            table_state: dict[str, Any] = {
                "exists": True,
                "rows": int(
                    conn.execute(
                        render_sql(
                            "SELECT COUNT(*) FROM {table}",
                            identifiers={"table": table},
                        )
                    ).fetchone()[0]
                ),
                "max": maximum,
                "schema_sha256": hashlib.sha256(
                    json.dumps(column_rows, ensure_ascii=False, default=str).encode(
                        "utf-8"
                    )
                ).hexdigest(),
            }
            digest_specs = table_digest_columns or {}
            if table in digest_specs:
                requested = digest_specs[table]
                digest_columns = tuple(requested or ordered_columns)
                missing_columns = sorted(set(digest_columns) - columns)
                selected_columns = tuple(
                    column for column in digest_columns if column in columns
                )
                row_digests: list[bytes] = []
                if selected_columns:
                    query = render_sql(
                        "SELECT {columns} FROM {table}",
                        identifiers={"table": table},
                        identifier_lists={"columns": selected_columns},
                    )
                    for row in conn.execute(query):
                        encoded = json.dumps(
                            row,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            default=str,
                        ).encode("utf-8")
                        row_digests.append(hashlib.sha256(encoded).digest())
                digest = hashlib.sha256()
                for row_digest in sorted(row_digests):
                    digest.update(row_digest)
                table_state.update(
                    {
                        "digest_columns": list(digest_columns),
                        "digest_missing_columns": missing_columns,
                        "content_sha256": digest.hexdigest(),
                    }
                )
            tables[table] = table_state
    return {"path": str(resolved), "exists": True, "tables": tables}


def _runtime_isolation_guard_state(cfg: Any) -> dict[str, Any]:
    """Protect every production ledger reachable from projection consumers."""

    event_root = next(
        (
            Path(value).expanduser()
            for name in ("mnemos_dir", "database_dir", "data_dir")
            if (value := getattr(cfg, name, None)) is not None
        ),
        Path.home() / ".mnemos",
    )
    event_tables = {
        "events": "id",
        "dead_letters": "id",
        "handler_receipts": "id",
        "event_trace_claims": "claimed_at",
        "event_deferred_keys": "created_at",
        "event_resolved_deferred_keys": "resolved_at",
        "event_object_provenance": "created_at",
        "event_subject_links": "trace_id",
        "event_subject_tombstones": "tombstoned_at",
        "event_subject_deletion_receipts": "created_at",
    }
    projection_tables = {
        "wiki_pages": "updated_at",
        "wiki_mutations": "sequence_no",
        "projection_receipts": "updated_at",
        "derived_projection_generations": "updated_at",
        "derived_projection_generation_items": "updated_at",
        "wiki_projection_material_effects": "completed_at",
        "wiki_subject_deletion_receipts": "created_at",
    }
    producer_consumer_tables = {
        "cognitive_data_consumer_heads": "updated_at",
        "cognitive_data_consumptions": "created_at",
        "cognitive_data_events": "created_at",
        "cognitive_data_reconciliations": "created_at",
        "cognitive_feedback_command_attempts": "created_at",
        "cognitive_state_effect_receipts": "created_at",
        "cognitive_state_heads": "updated_at",
        "cognitive_state_migration_quarantine": "created_at",
        "cognitive_state_outbox": "created_at",
        "cognitive_state_revisions": "created_at",
        "runtime_flow_events": "created_at",
        "runtime_flow_receipts": "created_at",
        "runtime_flow_registry": "updated_at",
        "typed_search_state_exclusions": "created_at",
        "typed_search_state_headers": "created_at",
        "typed_search_state_revision_bindings": "created_at",
    }
    trusted_push_tables = {
        "evidence_ledger": "created_at",
        "formal_cognitive_mutations": "created_at",
        "journal_events": "created_at",
        "proposal_revisions": "created_at",
        "proposals": "updated_at",
        "trusted_markdown_effect_intents": "created_at",
        "user_decisions": "created_at",
    }
    model_call_tables = {
        "model_call_runs": "created_at",
        "model_call_entries": "settled_at",
        "model_call_run_subjects": "created_at",
        "model_call_entry_subjects": "created_at",
        "model_call_frozen_subjects": "frozen_at",
        "model_call_daily_spend_tombstones": "updated_at",
        "model_call_run_spend_tombstones": "updated_at",
    }
    embedding_cache_tables = {"embedding_cache": "last_used_at"}
    getter = getattr(cfg, "get", None)
    configured_trusted_db = (
        getter("trusted_push.db_path", None) if callable(getter) else None
    )
    trusted_push_db = (
        Path(str(configured_trusted_db)).expanduser()
        if configured_trusted_db
        else Path(cfg.database_dir) / "trusted_push.db"
    )
    return {
        "events": _guarded_sqlite_state(event_root / "events.db", event_tables),
        "wiki_projection": _guarded_sqlite_state(
            Path(cfg.database_dir) / "wiki_projection.db",
            projection_tables,
        ),
        "producer_consumer": _guarded_sqlite_state(
            trusted_push_db.parent / "producer_consumer_ledger.db",
            producer_consumer_tables,
        ),
        "trusted_push": _guarded_sqlite_state(
            trusted_push_db,
            trusted_push_tables,
        ),
        "model_call_ledger": _guarded_sqlite_state(
            Path(cfg.database_dir) / "model_call_ledger.db",
            model_call_tables,
            table_digest_columns={table: None for table in model_call_tables},
        ),
        "embedding_cache": _guarded_sqlite_state(
            Path(cfg.database_dir) / "embedding_cache.db",
            embedding_cache_tables,
            table_digest_columns={
                "embedding_cache": (
                    "content_hash",
                    "model_version",
                    "token_count",
                    "created_at",
                    "last_used_at",
                    "hit_count",
                    "last_hit_at",
                )
            },
        ),
    }


def _backup_state(paths: Iterable[Path], backup_dir: Path) -> dict[str, Any]:
    """Back up every mutable projection artifact before destructive rebuild."""

    copied: list[str] = []
    for path in paths:
        target = backup_dir / path.name
        if path.suffix == ".db" and _sqlite_backup(path, target):
            copied.append(str(target))
        elif path.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied.append(str(target))
    return {"backup_dir": str(backup_dir), "artifacts": copied}


def _read_only_wiki_mutations(db_path: Path) -> list[dict[str, Any]]:
    """Read an immutable Wiki mutation ledger without initializing it."""

    resolved = db_path.expanduser().resolve(strict=True)
    with sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise ValueError(f"Wiki mutation ledger integrity failed: {resolved}")
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(wiki_mutations)")
        }
        required = {"sequence_no", "page_id", "page_path", "tombstone"}
        missing = sorted(required - columns)
        if missing:
            raise ValueError(
                "Wiki mutation ledger lacks comparator columns: " + ", ".join(missing)
            )
        rows = conn.execute(
            "SELECT * FROM wiki_mutations ORDER BY sequence_no"
        ).fetchall()
    return [dict(row) for row in rows]


def _translated_active_page_paths(
    mutations: list[dict[str, Any]],
    *,
    source_wiki_dir: Path,
    target_wiki_dir: Path,
) -> list[Path]:
    """Translate one ledger's active page identities into an isolated Vault."""

    source_root = source_wiki_dir.expanduser().resolve(strict=False)
    target_root = target_wiki_dir.expanduser().resolve(strict=False)
    translated: list[Path] = []
    for mutation in _latest_page_mutations(mutations).values():
        if bool(mutation.get("tombstone")):
            continue
        source_path = Path(str(mutation["page_path"])).expanduser().resolve(
            strict=False
        )
        try:
            relative = source_path.relative_to(source_root)
        except ValueError as exc:
            raise ValueError(
                f"active Wiki mutation path escapes the source Vault: {source_path}"
            ) from exc
        target_path = target_root / relative
        if not target_path.is_file():
            raise FileNotFoundError(
                f"active Wiki mutation is absent from comparator prestate: {target_path}"
            )
        translated.append(target_path)
    return sorted(translated)


def _vault_markdown_paths(vault_dir: Path) -> list[Path]:
    """Return the same user-visible Markdown denominator as lifecycle scans."""

    root = vault_dir.expanduser().resolve(strict=True)
    return sorted(
        path
        for path in root.rglob("*.md")
        if not any(part.startswith(".") for part in path.relative_to(root).parts)
    )


def _seed_isolated_embedding_cache(
    *,
    source_database_dir: Path,
    target_database_dir: Path,
) -> dict[str, Any]:
    """Seed provider results only; never copy a settled projection artifact."""

    source = source_database_dir / "embedding_cache.db"
    target = target_database_dir / "embedding_cache.db"
    if not source.is_file():
        return {"copied": False, "source": str(source), "target": str(target)}
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(
        source.resolve().as_uri() + "?mode=ro", uri=True, timeout=30
    ) as src, sqlite3.connect(str(target), timeout=30) as dst:
        src.backup(dst)
    return {"copied": True, "source": str(source), "target": str(target)}


def _new_isolated_embedding_client(cfg: Any) -> SiliconFlowEmbeddingClient:
    """Bind cache, provider ledger, and limiter to one isolated runtime root."""

    cache = EmbeddingCache(
        db_path=Path(cfg.database_dir) / "embedding_cache.db",
        config=cfg,
    )
    return SiliconFlowEmbeddingClient(
        cache=cache,
        limiter=SiliconFlowRateLimiter(),
        config=cfg,
    )


@dataclass(frozen=True)
class _ControlledProjectionRuntime:
    """Projection dependencies bound to one explicit durable runtime root."""

    kg_handler: KGEventHandler
    embedding_client: SiliconFlowEmbeddingClient
    event_bus: EventBus
    lifecycle: DerivedProjectionLifecycle


@contextmanager
def _controlled_projection_runtime(
    cfg: Any,
    *,
    ledger: WikiProjectionLedger,
) -> Iterator[_ControlledProjectionRuntime]:
    """Disable event recovery/dispatch while a deterministic rebuild is active."""

    database_dir = Path(cfg.database_dir)
    wiki_dir = Path(cfg.wiki_dir)
    embedding_client = _new_isolated_embedding_client(cfg)
    event_bus: EventBus | None = None
    try:
        event_bus = EventBus(
            config=cfg,
            run_startup_maintenance=False,
            recover_pending=False,
            enqueue_published_events=False,
        )
        lifecycle = DerivedProjectionLifecycle(
            wiki_dir,
            ledger=ledger,
            event_bus=event_bus,
        )
        kg_handler = KGEventHandler(
            db_path=database_dir / "knowledge_graph.db",
            wiki_base=wiki_dir,
            embedding_index_dir=database_dir / "embedding_index",
            embedding_client=embedding_client,
            config=cfg,
            projection_lifecycle=lifecycle,
            emit_projection_runtime_consumption=False,
        )
        yield _ControlledProjectionRuntime(
            kg_handler=kg_handler,
            embedding_client=embedding_client,
            event_bus=event_bus,
            lifecycle=lifecycle,
        )
    finally:
        try:
            if event_bus is not None:
                event_bus.close()
        finally:
            embedding_client.close()


_table_snapshot = rebuild_state._table_snapshot
_directory_snapshot = rebuild_state._directory_snapshot
_reset_projection_artifacts = rebuild_state._reset_projection_artifacts
_embedding_snapshot = rebuild_state._embedding_snapshot
_relation_embedding_semantic_comparison = (
    rebuild_state._relation_embedding_semantic_comparison
)
_relation_embedding_coverage = rebuild_state._relation_embedding_coverage
_projection_state = rebuild_state._projection_state
_semantic_projection_hash = rebuild_state._semantic_projection_hash
_materialize_incremental_mutation = rebuild_state._materialize_incremental_mutation
_mutation_prefix_snapshot = rebuild_state._mutation_prefix_snapshot
_verified_resume_baseline = rebuild_state._verified_resume_baseline


def _run_full_projection_consumers_after_kg(
    cfg: Any,
    *,
    input_page_count: int,
    kg_result: dict[str, Any],
    embedding_client: Any | None = None,
    publish_moc_mutations: bool = True,
) -> dict[str, Any]:
    """Finish a clean full projection pass after its KG state is verified."""

    wiki_dir = Path(cfg.wiki_dir)
    database_dir = Path(cfg.database_dir)
    relation_coverage = _relation_embedding_coverage(
        database_dir / "knowledge_graph.db",
        database_dir / "embedding_index" / "relation_index.bin",
    )
    if not relation_coverage["ok"]:
        raise RuntimeError(f"relation embedding projection is incomplete: {relation_coverage}")
    navigation = rebuild_navigation(
        wiki_dir,
        publish_mutations=publish_moc_mutations,
    )
    metrics_store = WikiMetrics(
        db_path=str(database_dir / "wiki_metrics.db"), wiki_dir=str(wiki_dir)
    )
    try:
        metrics = metrics_store.scan_all_pages()
    finally:
        metrics_store.close()
    cognitive = CognitiveGraphUpdater(
        store=CognitiveGraphStore(db_path=str(database_dir / "cognitive_graph.db"))
    ).reconcile()
    search_manager = EmbeddingIndexManager(
        wiki_base=wiki_dir,
        index_dir=database_dir / "embedding_index",
        client=embedding_client,
        config=cfg,
    )
    search_index = search_manager.build_index(force_full=True)
    if search_index.get("status") == "no_client":
        raise RuntimeError("Wiki search index full rebuild requires an available embedding client")
    search_coverage = search_manager.audit_coverage()
    if not search_coverage["ok"]:
        raise RuntimeError(f"Wiki search index coverage is incomplete: {search_coverage}")
    return {
        "kg": kg_result,
        "input_page_count": int(input_page_count),
        "relation_embedding_coverage": relation_coverage,
        "moc_navigation": {
            key: value for key, value in navigation.items() if key != "page_to_nav"
        },
        "metrics": metrics,
        "cognitive": cognitive,
        "wiki_search_index": search_index,
        "wiki_search_coverage": search_coverage,
    }


def _run_full_projection_cycle(
    cfg: Any,
    *,
    kg_handler: KGEventHandler,
    page_paths: list[Path],
    embedding_client: Any | None = None,
    publish_moc_mutations: bool = True,
) -> dict[str, Any]:
    """Run each projection's clean full-rebuild entrypoint once."""

    effective_page_paths = list(page_paths)
    kg = kg_handler.reconcile_pages(effective_page_paths)
    if kg.get("status") != "ok":
        raise RuntimeError(f"KG projection rebuild failed: {kg.get('errors')}")
    return _run_full_projection_consumers_after_kg(
        cfg,
        input_page_count=len(effective_page_paths),
        kg_result=kg,
        embedding_client=embedding_client,
        publish_moc_mutations=publish_moc_mutations,
    )


def _run_incremental_projection_cycle(
    cfg: Any,
    *,
    kg_handler: KGEventHandler,
    mutations: list[dict[str, Any]],
    source_wiki_dir: Path | None = None,
    rebuild_moc: bool = True,
    publish_moc_mutations: bool = True,
    materialize_mutations: bool = False,
    embedding_client: Any | None = None,
) -> dict[str, Any]:
    """Replay the actual per-mutation consumer entrypoints, with no full-scan fallback."""

    wiki_dir = Path(cfg.wiki_dir)
    database_dir = Path(cfg.database_dir)
    projection_ledger = database_dir / "wiki_projection.db"
    derived_output_cursor = rebuild_state._wiki_mutation_cursor(projection_ledger)
    metrics_store = WikiMetrics(
        db_path=str(database_dir / "wiki_metrics.db"), wiki_dir=str(wiki_dir)
    )
    cognitive = CognitiveGraphUpdater(
        store=CognitiveGraphStore(db_path=str(database_dir / "cognitive_graph.db"))
    )
    consumer_counts = {
        "kg": 0,
        "cognitive": 0,
        "metrics": 0,
        "metrics_superseded_missing": 0,
        "derived_inputs_regenerated": 0,
    }
    effective_mutations = sorted(
        mutations,
        key=lambda item: int(item.get("sequence_no", 0)),
    )

    def projection_path(value: Any) -> str:
        if not str(value or ""):
            return ""
        path = Path(str(value or "")).expanduser()
        if source_wiki_dir is None:
            return str(path)
        try:
            relative = path.resolve(strict=False).relative_to(
                source_wiki_dir.resolve(strict=False)
            )
        except ValueError:
            return str(path)
        return str(wiki_dir / relative)

    def consume_downstream(payload: dict[str, Any]) -> None:
        cognitive_outcome = cognitive.on_wiki_page_updated(
            Event("wiki_page_updated", "projection_rebuild", payload)
        )
        if cognitive_outcome.disposition not in {"ack", "noop"}:
            raise RuntimeError(
                f"incremental cognitive projection failed: {cognitive_outcome.reason}"
            )
        consumer_counts["cognitive"] += 1
        metrics_result = metrics_store.reconcile_page_lifecycle(
            page_path=payload["page_path"],
            previous_path=payload["previous_path"],
            mutation_type=str(payload["mutation_type"]),
        )
        if metrics_result.get("status") == "page_not_found":
            consumer_counts["metrics_superseded_missing"] += 1
            return
        metrics_outcome = HandlerOutcome.from_result(
            metrics_result,
            consumer="wiki_metrics",
        )
        if metrics_outcome.disposition not in {"ack", "noop"}:
            raise RuntimeError(
                f"incremental metrics projection failed: {metrics_outcome.reason}"
            )
        consumer_counts["metrics"] += 1

    regenerated_outputs: list[dict[str, Any]] = []
    changed_source_paths: set[Path] = set()
    dependency_closure: dict[str, Any] = {"required": False}
    try:
        with kg_handler.deferred_page_update_replay():
            for mutation in effective_mutations:
                source_page = Path(str(mutation["page_path"]))
                source_previous = Path(str(mutation.get("previous_path") or ""))
                classification_root = source_wiki_dir or wiki_dir
                page_is_derived = is_derived_kg_scan_path(
                    source_page, classification_root
                )
                previous_is_derived = bool(
                    str(mutation.get("previous_path") or "")
                    and is_derived_kg_scan_path(source_previous, classification_root)
                )
                moved_into_derived = bool(
                    mutation["mutation_type"] == "move"
                    and page_is_derived
                    and not previous_is_derived
                )
                if materialize_mutations and page_is_derived and not moved_into_derived:
                    regenerated_outputs.append(mutation)
                    consumer_counts["derived_inputs_regenerated"] += 1
                    continue
                candidate_dependency_changed = (
                    not (page_is_derived and previous_is_derived)
                    if mutation["mutation_type"] == "move"
                    else not page_is_derived
                )
                if candidate_dependency_changed:
                    changed_source_paths.add(
                        Path(projection_path(mutation["page_path"]))
                    )
                if materialize_mutations and source_wiki_dir is not None:
                    _materialize_incremental_mutation(
                        mutation,
                        target_path=Path(projection_path(mutation["page_path"])),
                        previous_path=Path(
                            projection_path(mutation.get("previous_path", ""))
                        ),
                        preserve_managed_target=moved_into_derived,
                    )
                payload = {
                    "mutation_id": mutation["mutation_id"],
                    "page_id": mutation["page_id"],
                    "page_revision": mutation["page_revision"],
                    "page_path": projection_path(mutation["page_path"]),
                    "previous_path": projection_path(mutation.get("previous_path", "")),
                    "mutation_type": mutation["mutation_type"],
                    "tombstone": bool(mutation.get("tombstone")),
                }
                kg_outcome = HandlerOutcome.from_result(
                    kg_handler.on_page_updated(payload), consumer="knowledge_graph"
                )
                if kg_outcome.disposition not in {"ack", "noop"}:
                    raise RuntimeError(
                        f"incremental KG projection failed: {kg_outcome.reason}"
                    )
                consumer_counts["kg"] += 1
                consume_downstream(payload)
        if changed_source_paths:
            closure_paths = _vault_markdown_paths(wiki_dir)
            closure_result = kg_handler.reconcile_pages(
                closure_paths, replace_existing=True
            )
            if closure_result.get("status") != "ok":
                raise RuntimeError(
                    f"incremental KG dependency closure failed: {closure_result['errors']}"
                )
            dependency_closure = {
                "required": True,
                "reason": "global relation candidate dependency changed",
                "changed_source_pages": len(changed_source_paths),
                "closure_page_count": len(closure_paths),
                "full_scan_fallback": False,
                "projection": closure_result,
            }
        for mutation in regenerated_outputs:
            consume_downstream(
                {
                    "mutation_id": mutation["mutation_id"],
                    "page_id": mutation["page_id"],
                    "page_revision": mutation["page_revision"],
                    "page_path": projection_path(mutation["page_path"]),
                    "previous_path": projection_path(mutation.get("previous_path", "")),
                    "mutation_type": mutation["mutation_type"],
                    "tombstone": bool(mutation.get("tombstone")),
                }
            )
        consumer_counts["canonical_derived_outputs"] = (
            rebuild_state._consume_derived_mutations_after(
                projection_ledger, after_sequence=derived_output_cursor,
                wiki_dir=wiki_dir, consume=consume_downstream,
            )
        )
    finally:
        metrics_store.close()

    relation_coverage = _relation_embedding_coverage(
        database_dir / "knowledge_graph.db",
        database_dir / "embedding_index" / "relation_index.bin",
    )
    if not relation_coverage["ok"]:
        raise RuntimeError(f"relation embedding projection is incomplete: {relation_coverage}")
    navigation = (
        rebuild_navigation(wiki_dir, publish_mutations=publish_moc_mutations)
        if rebuild_moc
        else {
            "indexed_pages": 0,
            "changed_pages": 0,
            "proposed_pages": 0,
            "mode": "isolated_final_navigation_snapshot",
        }
    )
    search_manager = EmbeddingIndexManager(
        wiki_base=wiki_dir,
        index_dir=database_dir / "embedding_index",
        client=embedding_client,
        config=cfg,
    )
    search_index = search_manager.build_index(force_full=False)
    if search_index.get("status") == "no_client":
        raise RuntimeError("Wiki search incremental rebuild requires an embedding client")
    search_coverage = search_manager.audit_coverage()
    if not search_coverage["ok"]:
        raise RuntimeError(f"Wiki search index coverage is incomplete: {search_coverage}")
    return {
        "input_page_count": len(mutations),
        "effective_page_count": len(effective_mutations),
        "consumer_counts": consumer_counts,
        "kg_dependency_closure": dependency_closure,
        "relation_embedding_coverage": relation_coverage,
        "moc_navigation": {
            key: value for key, value in navigation.items() if key != "page_to_nav"
        },
        "wiki_search_index": search_index,
        "wiki_search_coverage": search_coverage,
    }


def _isolated_incremental_comparator(
    cfg: Any,
    *,
    backup_dir: Path,
    mutations: list[dict[str, Any]],
    full_state: dict[str, Any],
    full_reference_cfg: Any | None = None,
) -> dict[str, Any]:
    """Compare a clean current full path with a clean prestate-plus-delta path."""

    source_wiki = Path(cfg.wiki_dir)
    reference_cfg = full_reference_cfg or cfg
    reference_db = Path(reference_cfg.database_dir)
    prestate_wiki = backup_dir / "wiki-prestate"
    target_root = backup_dir / "clean-incremental-comparator"
    target_db = target_root / "database"
    target_wiki = target_root / "wiki"
    target_index = target_db / "embedding_index"
    if not source_wiki.is_dir():
        raise FileNotFoundError(f"isolated comparator source Wiki is missing: {source_wiki}")
    if not prestate_wiki.is_dir():
        raise FileNotFoundError(
            f"isolated comparator prestate is missing: {prestate_wiki}"
        )
    if target_root.exists():
        shutil.rmtree(target_root)
    target_db.mkdir(parents=True)
    target_index.mkdir(parents=True)
    shutil.copytree(prestate_wiki, target_wiki)
    embedding_cache_seed = _seed_isolated_embedding_cache(
        source_database_dir=reference_db,
        target_database_dir=target_db,
    )
    baseline_mutations = _read_only_wiki_mutations(backup_dir / "wiki_projection.db")
    baseline_page_paths = _vault_markdown_paths(target_wiki)

    isolated_cfg = _IsolatedRuntimeConfig(
        cfg,
        database_dir=target_db,
        wiki_dir=target_wiki,
        snapshot_path_replacements=(
            (str(source_wiki.resolve(strict=False)), "$WIKI"),
            (str(Path(cfg.database_dir).resolve(strict=False)), "$DATABASE"),
        ),
    )
    protected_before = _runtime_isolation_guard_state(cfg)
    try:
        with _controlled_projection_runtime(
            isolated_cfg,
            ledger=WikiProjectionLedger(target_db / "wiki_projection.db"),
        ) as runtime:
            with use_config(isolated_cfg):
                baseline_projection = _run_full_projection_cycle(
                    isolated_cfg,
                    kg_handler=runtime.kg_handler,
                    page_paths=baseline_page_paths,
                    embedding_client=runtime.embedding_client,
                    publish_moc_mutations=False,
                )
                projection = _run_incremental_projection_cycle(
                    isolated_cfg,
                    kg_handler=runtime.kg_handler,
                    mutations=mutations,
                    source_wiki_dir=source_wiki,
                    rebuild_moc=True,
                    publish_moc_mutations=False,
                    materialize_mutations=True,
                    embedding_client=runtime.embedding_client,
                )
    finally:
        protected_after = _runtime_isolation_guard_state(cfg)
        if protected_after != protected_before:
            raise RuntimeError(
                "isolated comparator mutated a protected production ledger: "
                f"before={protected_before}, after={protected_after}"
            )
    isolation_guard = {
        "equal": True,
        "before": protected_before,
        "after": protected_after,
    }
    state = _projection_state(isolated_cfg)
    embedding_comparison = _relation_embedding_semantic_comparison(
        reference_db / "knowledge_graph.db",
        target_db / "knowledge_graph.db",
    )
    relation_ann_comparison = compare_hnsw_indexes(
        reference_db / "embedding_index" / "relation_index.bin",
        target_index / "relation_index.bin",
        expected_labels=relation_label_map(
            reference_db / "knowledge_graph.db"
        ),
        actual_labels=relation_label_map(target_db / "knowledge_graph.db"),
    )
    relation_ann_integrity = {
        "full": relation_index_integrity(
            reference_db / "knowledge_graph.db",
            reference_db / "embedding_index" / "relation_index.bin",
        ),
        "incremental": relation_index_integrity(
            target_db / "knowledge_graph.db",
            target_index / "relation_index.bin",
        ),
    }
    wiki_ann_comparison = compare_hnsw_indexes(
        reference_db / "embedding_index" / "wiki_index.bin",
        target_index / "wiki_index.bin",
        expected_labels=wiki_label_map(
            reference_db / "embedding_index" / "wiki_meta.json"
        ),
        actual_labels=wiki_label_map(target_index / "wiki_meta.json"),
    )
    return {
        "isolated": True,
        "baseline_mode": "clean_prestate_full_then_materialized_delta",
        "production_isolation_guard": isolation_guard,
        "full_reference_database_dir": str(reference_db),
        "target_root": str(target_root),
        "prestate_artifacts": [],
        "embedding_cache_seed": embedding_cache_seed,
        "baseline_mutation_count": len(baseline_mutations),
        "baseline_page_count": len(baseline_page_paths),
        "baseline_projection": baseline_projection,
        "mutation_count": len(mutations),
        "projection": projection,
        "state": state,
        "full_state_sha256": full_state["sha256"],
        "incremental_state_sha256": state["sha256"],
        "full_state_semantic_sha256": full_state["semantic_sha256"],
        "incremental_state_semantic_sha256": state["semantic_sha256"],
        "relation_embedding_semantics": embedding_comparison,
        "relation_ann_semantics": relation_ann_comparison,
        "relation_ann_integrity": relation_ann_integrity,
        "wiki_ann_semantics": wiki_ann_comparison,
        "equal": (
            full_state["semantic_sha256"] == state["semantic_sha256"]
            and embedding_comparison["equal"]
            and relation_ann_comparison["equal"]
            and relation_ann_integrity["full"]["ok"]
            and relation_ann_integrity["incremental"]["ok"]
            and wiki_ann_comparison["equal"]
        ),
    }


def _latest_page_mutations(mutations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Reduce a causal mutation stream to the latest state for every page identity."""

    latest: dict[str, dict[str, Any]] = {}
    for mutation in mutations:
        latest[str(mutation["page_id"])] = mutation
    return latest


def _active_page_paths(mutations: list[dict[str, Any]]) -> list[Path]:
    """Return existing active Wiki files from the ledger's latest page states."""

    latest = _latest_page_mutations(mutations)
    return sorted(
        Path(str(mutation["page_path"]))
        for mutation in latest.values()
        if not bool(mutation.get("tombstone"))
        and Path(str(mutation["page_path"])).is_file()
    )


def _incremental_page_paths(scan: dict[str, Any]) -> list[Path]:
    """Return only live files emitted by the latest mutation scan."""

    return sorted(
        Path(str(mutation["page_path"]))
        for mutation in scan["mutations"]
        if mutation["mutation_type"] != "delete"
        and Path(str(mutation["page_path"])).is_file()
    )


def _full_and_incremental_states(
    cycles: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Select the clean full pass and its immediate incremental replay."""

    if len(cycles) < 2:
        raise ValueError("full rebuild did not execute an incremental replay pass")
    return cycles[0]["state"], cycles[1]["state"]


def _record_rebuild_receipt(
    ledger: WikiProjectionLedger, *, mutation_id: str, consumer: str, **kwargs: Any
) -> None:
    """Fill only a missing projection watermark; completed evidence is immutable."""

    if ledger.terminal_projection_receipt(mutation_id, consumer) is not None:
        return
    existing = ledger.projection_receipt(mutation_id, consumer)
    if existing is not None and str(existing.get("event_trace_id") or ""):
        kwargs["event_trace_id"] = str(existing["event_trace_id"])
    ledger.record_projection_receipt(
        mutation_id=mutation_id, consumer=consumer, **kwargs
    )


def _complete_rebuild_under_controlled_runtime(
    *,
    cfg: Any,
    database_dir: Path,
    wiki_dir: Path,
    ledger: WikiProjectionLedger,
    payload: dict[str, Any],
    resume: bool,
    initial_mutations: list[dict[str, Any]],
    first_cycle_mode: str,
    resume_replay_after_sequence: int | None,
    baseline_sequence: int,
    backup_dir: Path,
) -> dict[str, Any]:
    """Run every mutable production cycle without background event recovery."""

    payload["lifecycle_replay"] = {
        "mode": (
            "verified_resume_after_current_state_kg_rebuild"
            if resume
            else "current_state_full_rebuild"
        ),
        "reason": (
            "resume preserves the completed KG pass and finishes every remaining "
            "full consumer before replaying the recorded lifecycle delta"
            if resume
            else "historical move/delete replay is intentionally replaced by rebuilding "
            "from the current Vault manifest; partial history replay can delete live state"
        ),
    }

    cycles: list[dict[str, Any]] = []
    page_paths = _active_page_paths(initial_mutations)
    forced_resume_mutations = (
        [
            mutation
            for mutation in initial_mutations
            if int(mutation.get("sequence_no", 0))
            > int(resume_replay_after_sequence)
        ]
        if resume and resume_replay_after_sequence is not None
        else []
    )
    payload["resume_replay"] = {
        "enabled": bool(resume and resume_replay_after_sequence is not None),
        "after_sequence": resume_replay_after_sequence,
        "mutation_count": len(forced_resume_mutations),
    }
    pending_mutations: list[dict[str, Any]] = []
    previous_state: dict[str, Any] | None = None
    converged = False
    with _controlled_projection_runtime(cfg, ledger=ledger) as runtime:
        for cycle_number in range(1, MAX_REBUILD_CYCLES + 1):
            if cycle_number == 1:
                if resume:
                    cycle = _run_full_projection_consumers_after_kg(
                        cfg,
                        input_page_count=len(page_paths),
                        kg_result={
                            "status": "ok",
                            "mode": "verified_resume",
                            "pages_processed": 0,
                        },
                        embedding_client=runtime.embedding_client,
                    )
                else:
                    cycle = _run_full_projection_cycle(
                        cfg,
                        kg_handler=runtime.kg_handler,
                        page_paths=page_paths,
                        embedding_client=runtime.embedding_client,
                    )
            else:
                cycle = _run_incremental_projection_cycle(
                    cfg,
                    kg_handler=runtime.kg_handler,
                    mutations=pending_mutations,
                    embedding_client=runtime.embedding_client,
                )
            state = _projection_state(cfg)
            scan = ledger.reconcile_vault(wiki_dir)
            cycles.append(
                {
                    "cycle": cycle_number,
                    "input_mode": (
                        first_cycle_mode if cycle_number == 1 else "incremental_delta"
                    ),
                    "input_page_count": cycle["input_page_count"],
                    "projection": cycle,
                    "state": state,
                    "vault_scan": scan,
                }
            )
            state_stable = bool(
                previous_state and previous_state["sha256"] == state["sha256"]
            )
            if state_stable and int(scan["recorded_mutations"]) == 0:
                converged = True
                break
            scan_mutations = list(scan["mutations"])
            if cycle_number == 1 and forced_resume_mutations:
                pending_by_id = {
                    str(mutation["mutation_id"]): mutation
                    for mutation in (*forced_resume_mutations, *scan_mutations)
                }
                pending_mutations = sorted(
                    pending_by_id.values(),
                    key=lambda mutation: int(mutation.get("sequence_no", 0)),
                )
            else:
                pending_mutations = scan_mutations
            previous_state = state

    payload["cycles"] = cycles
    payload["idempotent"] = converged
    if not converged:
        payload["error"] = (
            "full projection rebuild did not reach state and filesystem saturation "
            f"within {MAX_REBUILD_CYCLES} cycles"
        )
        return payload

    mutations = ledger.list_mutations()
    try:
        first_state, second_state = _full_and_incremental_states(cycles)
    except ValueError as exc:
        payload["error"] = str(exc)
        return payload
    payload["first_state"] = first_state
    payload["second_state"] = second_state
    payload["comparison"] = {
        "mode": "clean_full_rebuild_then_actual_mutation_handlers",
        "clean_reset": True,
        "incremental_full_scan_fallback": False,
        "incremental_mutation_count": cycles[1]["input_page_count"],
        "full_state_sha256": first_state["sha256"],
        "incremental_state_sha256": second_state["sha256"],
        "equal": first_state["sha256"] == second_state["sha256"],
    }
    if not payload["comparison"]["equal"]:
        payload["ok"] = False
        payload["error"] = (
            "immediate incremental replay differs from clean full state; "
            "resume from the now-reconciled projection state before running "
            "the isolated comparator"
        )
        return payload
    delta_mutations = [
        mutation
        for mutation in mutations
        if int(mutation["sequence_no"]) > baseline_sequence
    ]
    payload["isolated_comparator"] = _isolated_incremental_comparator(
        cfg,
        backup_dir=backup_dir,
        mutations=delta_mutations,
        full_state=first_state,
    )
    if not payload["isolated_comparator"]["equal"]:
        payload["ok"] = False
        payload["error"] = (
            "isolated incremental comparator differs from clean full state; "
            "no rebuild receipts were recorded"
        )
        return payload
    rebuild_id = "wiki-rebuild-" + second_state["sha256"][:24]
    for mutation in mutations:
        mutation_id = str(mutation["mutation_id"])
        kg_noop = is_derived_kg_scan_path(Path(str(mutation["page_path"])), wiki_dir)
        _record_rebuild_receipt(
            ledger,
            mutation_id=mutation_id,
            consumer="knowledge_graph",
            outcome="noop" if kg_noop else "ack",
            reason=(
                "derived page excluded from KG re-ingestion"
                if kg_noop
                else "real KG batch consumer reached filesystem saturation"
            ),
            event_trace_id=rebuild_id,
            metadata={"snapshot": second_state["tables"]["kg_relations"]["sha256"]},
        )
        _record_rebuild_receipt(
            ledger,
            mutation_id=mutation_id,
            consumer="cognitive_graph",
            outcome=(
                "ack" if mutation["mutation_type"] in {"move", "delete"} else "noop"
            ),
            reason=(
                "real cognitive lifecycle consumer replay verified"
                if mutation["mutation_type"] in {"move", "delete"}
                else "Wiki create/update creates no cognitive self relation"
            ),
            event_trace_id=rebuild_id,
            metadata={
                "snapshot": second_state["tables"]["cognitive_relations"]["sha256"]
            },
        )
        _record_rebuild_receipt(
            ledger,
            mutation_id=mutation_id,
            consumer="relation_embeddings",
            outcome="noop" if kg_noop else "ack",
            reason=(
                "derived page has no relation embedding input"
                if kg_noop
                else "relation embeddings and HNSW index rebuilt from clean KG state"
            ),
            event_trace_id=rebuild_id,
            metadata={
                "embedding_snapshot": second_state["tables"]["relation_embeddings"][
                    "sha256"
                ],
                "index_snapshot": second_state["tables"]["relation_hnsw"]["sha256"],
            },
        )
        _record_rebuild_receipt(
            ledger,
            mutation_id=mutation_id,
            consumer="wiki_search_index",
            outcome="ack",
            reason="full page embedding search index rebuilt and incrementally replayed",
            event_trace_id=rebuild_id,
            metadata={
                "meta_snapshot": second_state["tables"]["wiki_search_meta"]["sha256"],
                "index_snapshot": second_state["tables"]["wiki_search_hnsw"]["sha256"],
            },
        )
        _record_rebuild_receipt(
            ledger,
            mutation_id=mutation_id,
            consumer="wiki_metrics",
            outcome="ack",
            reason="full Wiki metrics rebuild verified",
            event_trace_id=rebuild_id,
            metadata={"snapshot": second_state["tables"]["wiki_metrics"]["sha256"]},
        )
        _record_rebuild_receipt(
            ledger,
            mutation_id=mutation_id,
            consumer="moc_navigation",
            outcome="ack",
            reason="deterministic MOC navigation rebuilt from the current Vault manifest",
            event_trace_id=rebuild_id,
            metadata={
                "snapshot": second_state["tables"]["moc_navigation"]["sha256"]
            },
        )
    payload["rebuild_id"] = rebuild_id
    payload["receipt_mutations"] = len(mutations)
    payload["reconciliation"] = ledger.reconciliation_report()
    payload["ok"] = bool(
        payload["idempotent"]
        and payload["comparison"]["equal"]
        and payload["isolated_comparator"]["equal"]
        and payload["reconciliation"]["ok"]
    )
    return payload


def rebuild(
    *,
    apply: bool,
    backup_dir: Path | None = None,
    resume: bool = False,
    resume_replay_after_sequence: int | None = None,
) -> dict[str, Any]:
    """Run preview read-only or hold the required lock for every applied rebuild."""

    cfg = get_config()
    database_dir = Path(cfg.database_dir)
    if not apply:
        return _rebuild_under_offline_lock(
            cfg=cfg,
            apply=apply,
            backup_dir=backup_dir,
            resume=resume,
            resume_replay_after_sequence=resume_replay_after_sequence,
        )
    with offline_migration_lock(database_dir):
        return _rebuild_under_offline_lock(
            cfg=cfg,
            apply=apply,
            backup_dir=backup_dir,
            resume=resume,
            resume_replay_after_sequence=resume_replay_after_sequence,
        )


def _rebuild_under_offline_lock(
    *,
    cfg: Any,
    apply: bool,
    backup_dir: Path | None,
    resume: bool,
    resume_replay_after_sequence: int | None,
) -> dict[str, Any]:
    """Back up, clean-rebuild, replay, and compare under the caller-owned lock."""

    database_dir = Path(cfg.database_dir)
    wiki_dir = Path(cfg.wiki_dir)
    ledger_path = database_dir / "wiki_projection.db"
    before = _projection_state(cfg)
    payload: dict[str, Any] = {
        "schema_version": "mnemos.wiki_projection_full_rebuild.v2",
        "applied": apply,
        "resume": resume,
        "resume_replay_after_sequence": resume_replay_after_sequence,
        "before": before,
    }
    if resume_replay_after_sequence is not None and not resume:
        raise ValueError("resume replay checkpoint requires --resume")
    if resume:
        if backup_dir is None:
            raise ValueError("rebuild resume requires the original explicit backup directory")
        payload["resume_validation"] = _verified_resume_baseline(
            backup_dir=backup_dir,
            live_ledger_path=ledger_path,
        )
        if resume_replay_after_sequence is not None:
            replay_after = int(resume_replay_after_sequence)
            baseline = int(payload["resume_validation"]["baseline_sequence"])
            live_sequence = int(payload["resume_validation"]["live_sequence"])
            if replay_after < baseline or replay_after > live_sequence:
                raise ValueError(
                    "resume replay checkpoint must be within the verified "
                    "backup-prefix and live-ledger sequence range"
                )
    if not apply:
        return payload
    ledger = WikiProjectionLedger(ledger_path)
    if resume:
        assert backup_dir is not None
        baseline_sequence = int(payload["resume_validation"]["baseline_sequence"])
        payload["backup"] = {
            "backup_dir": str(backup_dir.expanduser().resolve(strict=False)),
            "wiki_prestate": str(
                backup_dir.expanduser().resolve(strict=False) / "wiki-prestate"
            ),
            "reused": True,
        }
        payload["clean_projection_reset"] = {
            "skipped": True,
            "reason": "resume preserves the already rebuilt and coverage-verified KG",
        }
        payload["provenance_repair"] = {
            "skipped": True,
            "reason": "resume does not rewrite an immutable lifecycle ledger",
        }
        payload["pointer_repair"] = {
            "skipped": True,
            "reason": "backup-prefix validation replaced mutable pointer repair",
        }
        payload["initial_vault_scan"] = {
            "skipped": True,
            "reason": "the interrupted rebuild already completed its initial scan",
        }
        initial_mutations = ledger.list_mutations()
        first_cycle_mode = "clean_full_resume_after_kg"
    else:
        if backup_dir is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_dir = database_dir / "backups" / f"root007-projections-{stamp}"
        payload["backup"] = _backup_state(
            (
                database_dir / "knowledge_graph.db",
                database_dir / "cognitive_graph.db",
                database_dir / "wiki_metrics.db",
                ledger_path,
                database_dir / "embedding_index" / "relation_index.bin",
                database_dir / "embedding_index" / "wiki_index.bin",
                database_dir / "embedding_index" / "wiki_meta.json",
            ),
            backup_dir,
        )
        wiki_prestate = backup_dir / "wiki-prestate"
        if not wiki_prestate.exists():
            shutil.copytree(wiki_dir, wiki_prestate)
        payload["backup"]["wiki_prestate"] = str(wiki_prestate)
        payload["clean_projection_reset"] = _reset_projection_artifacts(cfg)
        payload["provenance_repair"] = {
            "cleared_synthetic_rebuild_traces": (
                ledger.repair_synthetic_rebuild_event_traces()
            )
        }
        payload["pointer_repair"] = {
            "restored_from_latest_mutation": (
                ledger.repair_current_pointers_from_history()
            )
        }
        pre_rebuild_mutations = ledger.list_mutations()
        baseline_sequence = max(
            (int(item["sequence_no"]) for item in pre_rebuild_mutations), default=0
        )
        payload["initial_vault_scan"] = ledger.reconcile_vault(wiki_dir)
        initial_mutations = ledger.list_mutations()
        first_cycle_mode = "clean_full"
    assert backup_dir is not None
    return _complete_rebuild_under_controlled_runtime(
        cfg=cfg,
        database_dir=database_dir,
        wiki_dir=wiki_dir,
        ledger=ledger,
        payload=payload,
        resume=resume,
        initial_mutations=initial_mutations,
        first_cycle_mode=first_cycle_mode,
        resume_replay_after_sequence=resume_replay_after_sequence,
        baseline_sequence=baseline_sequence,
        backup_dir=backup_dir,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for clean projection rebuild and comparator evidence."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", default="")
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "resume an interrupted clean rebuild from its original backup; "
            "never backs up or resets projection state again"
        ),
    )
    parser.add_argument(
        "--resume-replay-after-sequence",
        type=int,
        default=None,
        help=(
            "with --resume, replay every lifecycle mutation after this verified "
            "sequence even when the Vault scan has no new filesystem delta"
        ),
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = rebuild(
        apply=args.apply,
        backup_dir=Path(args.backup_dir).expanduser() if args.backup_dir else None,
        resume=args.resume,
        resume_replay_after_sequence=args.resume_replay_after_sequence,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            "Wiki projection full rebuild: "
            f"applied={result['applied']} idempotent={result.get('idempotent')} "
            f"gap={result.get('reconciliation', {}).get('projection_gap')}"
        )
    return 0 if (not args.apply or result.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
