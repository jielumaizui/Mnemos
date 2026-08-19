#!/usr/bin/env python3
"""Audit the typed, replayable lifecycle for L2.4-L5 Wiki projections."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.trust.static_scan import scan_direct_writes
from core.wiki_derived_projection import (
    DerivedProjectionLifecycle,
    ProjectionPageSpec,
)
from core.wiki_projection_lifecycle import (
    DEFAULT_REQUIRED_CONSUMERS,
    WikiProjectionLedger,
)


AUDIT_SCHEMA_VERSION = "mnemos.cognitive_projection_lifecycle_audit.v1"
PROJECTION_ROOTS = (
    "L2.4-KG",
    "L3-Observations",
    "L4-Reflections",
    "L5-Feedback",
)
AUDITED_SCOPE_ROOTS = (
    Path("L2.4-KG"),
    Path("L4-Reflections/Reflections"),
    Path("L4-Reflections/Shifts"),
    Path("L4-Reflections/KnowledgeUpdates"),
    Path("L4-Reflections/Reports"),
    Path("L5-Feedback/user-persona-history"),
)
AUDITED_EXACT_FILES = frozenset({Path("L5-Feedback/user-persona.md")})
DIRECT_PROJECTION_MODULES = frozenset(
    {
        "core/cognitive/wiki_exporter.py",
        "core/kia/kg_exporter.py",
        "core/reflection/reflection_exporter.py",
        "core/persona/delphi.py",
        "core/persona/projection_runtime.py",
        "core/vaults/vault_sync.py",
    }
)
ZERO_BUDGET_METRICS = (
    "formal_projection_registered_as_report",
    "direct_projection_write",
    "projection_failure_swallowed",
    "vault_sync_canonical_delta",
    "stale_projection",
    "projection_binding_gap",
    "required_consumer_receipt_gap",
    "full_incremental_mismatch",
    "nth_file_failure_replay_gap",
    "publisher_failure_replay_gap",
    "binding_replay_gap",
    "vault_sync_mutating_callsite",
)


class _RecordingBus:
    def __init__(self, failures: int = 0):
        self.failures = failures
        self.events: list[Any] = []

    def publish(self, event: Any) -> str:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("injected projection publisher failure")
        self.events.append(event)
        return str(event.trace_id)


class _AuditConfig:
    def __init__(self, root: Path):
        self.mnemos_dir = root / "runtime"
        self.database_dir = root / "database"
        self.data_dir = self.database_dir
        self.wiki_dir = root / "vault"
        self.raw_dir = root / "raw"

    def get(self, _key: str, default: Any = None) -> Any:
        return default

    def vault_dir(self, name: str) -> Path:
        if name == "mnemos":
            return self.wiki_dir
        if name == "raw":
            return self.raw_dir
        raise KeyError(name)


def _page(vault: Path, name: str, body: str = "# Projection\n") -> ProjectionPageSpec:
    return ProjectionPageSpec(
        path=vault / "L3-Observations" / f"{name}.md",
        content=body,
        page_role="formal_derived:observation",
        canonical_revision=f"canonical:{name}",
        source_refs=(f"observation:{name}",),
    )


def _handler_swallows_projection_failure(path: Path, function_name: str) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != function_name:
            continue
        for candidate in ast.walk(node):
            if not isinstance(candidate, ast.Try):
                continue
            call_names = {
                _qualified_name(call.func).rsplit(".", 1)[-1]
                for call in ast.walk(candidate)
                if isinstance(call, ast.Call)
            }
            if not call_names.intersection(
                {
                    "export_batch",
                    "export_record",
                    "export_shifts",
                    "export_weekly_report",
                    "export_to_vault",
                    "publish_event",
                }
            ):
                continue
            for handler in candidate.handlers:
                if not any(isinstance(child, ast.Raise) for child in ast.walk(handler)):
                    count += 1
    return count


def _qualified_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _vault_sync_ast_contract(path: Path) -> dict[str, Any]:
    """Inspect executable Vault-sync calls without trusting text markers."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mutating = 0
    replay_calls: set[str] = set()
    read_only_call_count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _qualified_name(node.func)
        leaf = name.rsplit(".", 1)[-1]
        if leaf in {
            "ObservationEngine",
            "canonical_raw_engine_kwargs",
            "save_persona",
        }:
            mutating += 1
        if leaf == "run" and any(
            keyword.arg == "persist"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        ):
            mutating += 1
        if leaf in {
            "rebuild_observation_projection",
            "load_canonical_persona_versions_read_only",
        }:
            replay_calls.add(leaf)
        if any(
            keyword.arg == "read_only"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        ):
            read_only_call_count += 1
    canonical_delta_assignment = any(
        isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Store)
        and node.id == "canonical_delta"
        for node in ast.walk(tree)
    )
    uses_read_only_replay = bool(
        replay_calls
        == {
            "rebuild_observation_projection",
            "load_canonical_persona_versions_read_only",
        }
        and read_only_call_count >= 1
        and canonical_delta_assignment
    )
    return {
        "mutating_callsite_count": mutating,
        "uses_read_only_replay": uses_read_only_replay,
        "replay_calls": sorted(replay_calls),
        "read_only_call_count": read_only_call_count,
        "canonical_delta_assignment": canonical_delta_assignment,
    }


def _static_contract(repo_root: Path) -> dict[str, Any]:
    direct = scan_direct_writes(repo_root)
    projection_sites = [
        site for site in direct["sites"] if site.rel_path in DIRECT_PROJECTION_MODULES
    ]
    forbidden_sites = [
        site
        for site in projection_sites
        if site.category
        not in {"guarded_trusted_push", "guarded_projection_lifecycle"}
    ]
    registry_path = repo_root / "core/trust/static_sink_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))["entries"]
    report_entries = [
        sink_id
        for sink_id, entry in registry.items()
        if sink_id.split("::", 1)[0] in DIRECT_PROJECTION_MODULES
        and str(entry.get("category")) == "report"
    ]

    swallowed = _handler_swallows_projection_failure(
        repo_root / "core/cognitive/observation_engine.py",
        "_export_projection",
    ) + _handler_swallows_projection_failure(
        repo_root / "core/reflection/reflection_engine.py",
        "_export_reflection_projection",
    ) + _handler_swallows_projection_failure(
        repo_root / "core/kia/kg_event_handler.py",
        "_project_kg_to_vault",
    )
    vault_contract = _vault_sync_ast_contract(
        repo_root / "core/vaults/vault_sync.py"
    )
    lifecycle_source = (repo_root / "core/wiki_derived_projection.py").read_text(
        encoding="utf-8"
    )
    lifecycle_markers = {
        marker: marker in lifecycle_source
        for marker in (
            "derived_projection_generations",
            "derived_projection_generation_items",
            "WikiProjectionLedger",
            "publish_wiki_mutation",
            "atomic_write_text",
        )
    }
    role_markers = {
        rel_path: (
            "formal_derived:" in (repo_root / rel_path).read_text(encoding="utf-8")
            or "derived_report:" in (repo_root / rel_path).read_text(encoding="utf-8")
        )
        for rel_path in (
            "core/cognitive/wiki_exporter.py",
            "core/kia/kg_exporter.py",
            "core/reflection/reflection_exporter.py",
            "core/persona/projection_runtime.py",
        )
    }
    return {
        "formal_projection_registered_as_report": len(report_entries),
        "report_registry_entries": sorted(report_entries),
        "direct_projection_write": len(forbidden_sites),
        "direct_projection_sites": [site.sink_id for site in forbidden_sites],
        "projection_failure_swallowed": swallowed,
        "vault_sync_mutating_callsite": vault_contract["mutating_callsite_count"],
        "vault_sync_uses_read_only_replay": vault_contract["uses_read_only_replay"],
        "vault_sync_ast_contract": vault_contract,
        "lifecycle_markers": lifecycle_markers,
        "page_role_markers": role_markers,
        "static_scan_registry_stale_count": direct["registry_stale_count"],
        "static_scan_unknown_count": direct["unknown_count"],
    }


def _wait_for(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _consumer_receipt_probe(root: Path) -> dict[str, Any]:
    from core.mnemos_bus import EventBus, HandlerOutcome

    config = _AuditConfig(root)
    config.database_dir.mkdir(parents=True, exist_ok=True)
    config.mnemos_dir.mkdir(parents=True, exist_ok=True)
    ledger = WikiProjectionLedger(config.database_dir / "wiki_projection.db")
    bus = EventBus(
        root_dir=root / "events",
        config=config,
        run_startup_maintenance=False,
        recover_pending=False,
    )
    # Tests may globally redirect the EventBus projection ledger; this isolated
    # probe must bind the bus to the exact ledger that owns its mutation.
    bus._projection_db_path = ledger.db_path

    def noop_handler(target: str) -> Any:
        def handle(_event: Any) -> Any:
            return HandlerOutcome.noop(
                target,
                "derived projection is not a canonical ingestion source",
            )

        return handle

    for consumer in DEFAULT_REQUIRED_CONSUMERS:
        bus.subscribe(
            "wiki_page_updated",
            noop_handler(consumer),
            consumer_id=consumer,
        )
    bus.start_dispatch()
    try:
        lifecycle = DerivedProjectionLifecycle(
            config.wiki_dir,
            ledger=ledger,
            event_bus=bus,
        )
        page = _page(config.wiki_dir, "consumer-receipts")
        generation = lifecycle.publish_generation(
            projection_kind="observation",
            scope_root=config.wiki_dir / "L3-Observations",
            pages=[page],
            full=True,
        )
        mutation_id = generation.items[0].mutation_id
        _wait_for(lambda: not ledger.required_consumer_gaps(mutation_id))
        binding = lifecycle.binding_for_path(page.path)
        binding_gap = int(
            not binding
            or not binding.get("canonical_revision")
            or not binding.get("content_sha256")
            or not binding.get("event_trace_id")
            or binding.get("status") != "published"
        )
        return {
            "projection_binding_gap": binding_gap,
            "required_consumer_receipt_gap": len(
                ledger.required_consumer_gaps(mutation_id)
            ),
        }
    finally:
        bus.stop_dispatch()
        bus.close()


def _initialize_empty_canonical_stores(config: _AuditConfig) -> None:
    from core.cognitive.observation_store import ObservationStore
    from core.kia.knowledge_graph import KnowledgeGraph
    from core.persona.psyche import SignalStore
    from core.reflection.reflection_store import ReflectionStore

    config.database_dir.mkdir(parents=True, exist_ok=True)
    ObservationStore(str(config.database_dir / "observations.db"))
    ReflectionStore(str(config.database_dir / "reflections.db"))
    graph = KnowledgeGraph(
        db_path=str(config.database_dir / "knowledge_graph.db"),
        wiki_base=str(config.wiki_dir),
        config=config,
    )
    _ = graph.entity_manager
    graph.close()
    signals = SignalStore(
        config.database_dir / "user_signals.db",
        config=config,
        initialize_schema=True,
    )
    signals.close()


def _vault_sync_probe(root: Path) -> int:
    from core.vaults.vault_sync import sync_all_projections

    config = _AuditConfig(root)
    _initialize_empty_canonical_stores(config)
    ledger = WikiProjectionLedger(config.database_dir / "wiki_projection.db")
    lifecycle = DerivedProjectionLifecycle(
        config.wiki_dir,
        ledger=ledger,
        event_bus=_RecordingBus(),
    )
    summary = sync_all_projections(
        vault_dir=config.wiki_dir,
        raw_dir=config.raw_dir,
        commit=False,
        config=config,
        lifecycle=lifecycle,
    )
    return len(summary["canonical_delta"])


def _synthetic_contract() -> dict[str, Any]:
    metrics = {
        "vault_sync_canonical_delta": 1,
        "stale_projection": 1,
        "projection_binding_gap": 1,
        "required_consumer_receipt_gap": len(DEFAULT_REQUIRED_CONSUMERS),
        "full_incremental_mismatch": 1,
        "nth_file_failure_replay_gap": 1,
        "publisher_failure_replay_gap": 1,
        "binding_replay_gap": 1,
    }
    error = ""
    try:
        with tempfile.TemporaryDirectory(prefix="mnemos-projection-audit-") as raw_root:
            root = Path(raw_root)

            write_calls = 0

            def fail_second(authorization: Any, path: Path, content: str) -> None:
                nonlocal write_calls
                write_calls += 1
                if write_calls == 2:
                    raise OSError("injected second-file failure")
                DerivedProjectionLifecycle._atomic_publish(
                    authorization,
                    path,
                    content,
                )

            nth_ledger = WikiProjectionLedger(root / "nth.db")
            nth_vault = root / "nth-vault"
            nth = DerivedProjectionLifecycle(
                nth_vault,
                ledger=nth_ledger,
                event_bus=_RecordingBus(),
                file_writer=fail_second,
            )
            nth_pages = [_page(nth_vault, "attention"), _page(nth_vault, "time")]
            try:
                nth.publish_generation(
                    projection_kind="observation",
                    scope_root=nth_vault / "L3-Observations",
                    pages=nth_pages,
                    full=True,
                )
            except OSError:
                pass
            replay = nth.publish_generation(
                projection_kind="observation",
                scope_root=nth_vault / "L3-Observations",
                pages=nth_pages,
                full=True,
            )
            metrics["nth_file_failure_replay_gap"] = int(
                replay.status != "committed"
                or any(not page.path.is_file() for page in nth_pages)
                or len(nth_ledger.list_mutations()) != 2
            )

            event_ledger = WikiProjectionLedger(root / "event.db")
            event_vault = root / "event-vault"
            failing_bus = _RecordingBus(failures=1)
            event_lifecycle = DerivedProjectionLifecycle(
                event_vault,
                ledger=event_ledger,
                event_bus=failing_bus,
            )
            event_page = _page(event_vault, "attention")
            try:
                event_lifecycle.publish_generation(
                    projection_kind="observation",
                    scope_root=event_vault / "L3-Observations",
                    pages=[event_page],
                    full=True,
                )
            except RuntimeError:
                pass
            unpublished = event_ledger.unpublished_mutations()
            restarted = DerivedProjectionLifecycle(
                event_vault,
                ledger=WikiProjectionLedger(event_ledger.db_path),
                event_bus=failing_bus,
            )
            event_replay = restarted.publish_generation(
                projection_kind="observation",
                scope_root=event_vault / "L3-Observations",
                pages=[event_page],
                full=True,
            )
            metrics["publisher_failure_replay_gap"] = int(
                len(unpublished) != 1
                or event_replay.status != "committed"
                or len(event_ledger.list_mutations()) != 1
            )

            binding_vault = root / "binding-vault"
            binding_lifecycle = DerivedProjectionLifecycle(
                binding_vault,
                ledger=WikiProjectionLedger(root / "binding.db"),
                event_bus=_RecordingBus(),
            )
            binding_a = _page(binding_vault, "attention", "# State A\n")
            binding_b = ProjectionPageSpec(
                path=binding_a.path,
                content="# State B\n",
                page_role=binding_a.page_role,
                canonical_revision="canonical:attention:b",
                source_refs=binding_a.source_refs,
            )
            for page in (binding_a, binding_b, binding_a):
                binding_lifecycle.publish_generation(
                    projection_kind="observation",
                    scope_root=binding_vault / "L3-Observations",
                    pages=[page],
                    full=False,
                )
            binding = binding_lifecycle.binding_for_path(binding_a.path)
            metrics["binding_replay_gap"] = int(
                binding is None
                or binding.get("canonical_revision")
                != binding_a.canonical_revision
                or binding.get("content_sha256") != _sha256_path(binding_a.path)
            )

            stale_ledger = WikiProjectionLedger(root / "stale.db")
            stale_vault = root / "stale-vault"
            stale_lifecycle = DerivedProjectionLifecycle(
                stale_vault,
                ledger=stale_ledger,
                event_bus=_RecordingBus(),
            )
            stale_page = _page(stale_vault, "vanished-dimension")
            stale_lifecycle.publish_generation(
                projection_kind="observation",
                scope_root=stale_vault / "L3-Observations",
                pages=[stale_page],
                full=True,
            )
            stale_lifecycle.publish_generation(
                projection_kind="observation",
                scope_root=stale_vault / "L3-Observations",
                pages=[],
                full=True,
            )
            metrics["stale_projection"] = len(
                stale_lifecycle.stale_paths(
                    projection_kind="observation",
                    scope_root=stale_vault / "L3-Observations",
                )
            ) + int(stale_page.path.exists())

            full_vault = root / "full"
            incremental_vault = root / "incremental"
            full = DerivedProjectionLifecycle(
                full_vault,
                ledger=WikiProjectionLedger(root / "full.db"),
                event_bus=_RecordingBus(),
            )
            incremental = DerivedProjectionLifecycle(
                incremental_vault,
                ledger=WikiProjectionLedger(root / "incremental.db"),
                event_bus=_RecordingBus(),
            )
            full_page = _page(full_vault, "same", "# Stable bytes\n")
            incremental_page = _page(
                incremental_vault,
                "same",
                "# Stable bytes\n",
            )
            full.publish_generation(
                projection_kind="observation",
                scope_root=full_vault / "L3-Observations",
                pages=[full_page],
                full=True,
            )
            incremental.publish_generation(
                projection_kind="observation",
                scope_root=incremental_vault / "L3-Observations",
                pages=[incremental_page],
                full=False,
            )
            metrics["full_incremental_mismatch"] = int(
                full_page.path.read_bytes() != incremental_page.path.read_bytes()
            )

            metrics.update(_consumer_receipt_probe(root / "consumer"))
            metrics["vault_sync_canonical_delta"] = _vault_sync_probe(root / "sync")
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        LookupError,
        sqlite3.Error,
    ) as exc:
        error = f"{type(exc).__name__}: {exc}"
    return {
        "metrics": metrics,
        "error": error,
        "consumer_probe_mode": "isolated_typed_noop",
    }


def _latest_manifest_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT item.*
        FROM derived_projection_generation_items AS item
        WHERE item.rowid=(
            SELECT newer.rowid
            FROM derived_projection_generation_items AS newer
            WHERE newer.target_path=item.target_path
            ORDER BY newer.updated_at DESC, newer.rowid DESC
            LIMIT 1
        )
        """
    ).fetchall()


def _sha256_path(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _is_audited_projection_path(relative: Path) -> bool:
    if relative in AUDITED_EXACT_FILES:
        return True
    if relative.parent == Path("L3-Observations"):
        return True
    return any(
        relative == root or root in relative.parents
        for root in AUDITED_SCOPE_ROOTS
    )


def audit_live_projection_state(
    *,
    wiki_dir: Path,
    projection_db: Path,
) -> dict[str, Any]:
    """Measure live binding, byte, and required-consumer receipt residuals."""

    pages = [
        page
        for root_name in PROJECTION_ROOTS
        for page in sorted((wiki_dir / root_name).rglob("*.md"))
        if page.is_file()
        and not any(part.startswith(".") for part in page.relative_to(wiki_dir).parts)
        and _is_audited_projection_path(page.relative_to(wiki_dir))
    ]
    if not projection_db.is_file():
        return {
            "initialized": False,
            "page_count": len(pages),
            "projection_binding_gap": len(pages),
            "stale_projection": 0,
            "required_consumer_receipt_gap": 0,
        }
    connection = sqlite3.connect(f"file:{projection_db}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {
            "derived_projection_generation_items",
            "projection_receipts",
        }
        if not required <= tables:
            return {
                "initialized": False,
                "page_count": len(pages),
                "projection_binding_gap": len(pages),
                "stale_projection": 0,
                "required_consumer_receipt_gap": 0,
            }
        rows = [
            row
            for row in _latest_manifest_rows(connection)
            if (
                Path(str(row["target_path"])).is_relative_to(wiki_dir)
                and _is_audited_projection_path(
                    Path(str(row["target_path"])).relative_to(wiki_dir)
                )
            )
        ]
        by_path = {str(row["target_path"]): row for row in rows}
        binding_gap = 0
        consumer_gap = 0
        for page in pages:
            row = by_path.get(str(page.resolve(strict=False)))
            if (
                row is None
                or str(row["action"]) != "upsert"
                or str(row["status"]) != "published"
                or not str(row["canonical_revision"])
                or not str(row["mutation_id"])
                or not str(row["event_trace_id"])
                or str(row["content_sha256"]) != _sha256_path(page)
            ):
                binding_gap += 1
                continue
            outcomes = {
                str(receipt["consumer"]): str(receipt["outcome"])
                for receipt in connection.execute(
                    "SELECT consumer, outcome FROM projection_receipts WHERE mutation_id=?",
                    (str(row["mutation_id"]),),
                ).fetchall()
            }
            consumer_gap += sum(
                outcomes.get(consumer) not in {"ack", "noop"}
                for consumer in DEFAULT_REQUIRED_CONSUMERS
            )
        for row in rows:
            if str(row["action"]) != "delete" or not str(row["mutation_id"]):
                continue
            outcomes = {
                str(receipt["consumer"]): str(receipt["outcome"])
                for receipt in connection.execute(
                    "SELECT consumer, outcome FROM projection_receipts WHERE mutation_id=?",
                    (str(row["mutation_id"]),),
                ).fetchall()
            }
            consumer_gap += sum(
                outcomes.get(consumer) not in {"ack", "noop"}
                for consumer in DEFAULT_REQUIRED_CONSUMERS
            )
        stale = 0
        for row in rows:
            target = Path(str(row["target_path"]))
            action = str(row["action"])
            if action == "delete" and target.exists():
                stale += 1
            elif action == "upsert" and (
                not target.is_file()
                or _sha256_path(target) != str(row["content_sha256"])
            ):
                stale += 1
        return {
            "initialized": True,
            "page_count": len(pages),
            "manifest_item_count": len(rows),
            "projection_binding_gap": binding_gap,
            "stale_projection": stale,
            "required_consumer_receipt_gap": consumer_gap,
        }
    finally:
        connection.close()


def build_report(
    *,
    repo_root: Path = ROOT,
    production: bool = False,
    database_dir: Path | None = None,
    wiki_dir: Path | None = None,
) -> dict[str, Any]:
    """Build the static, isolated, and optional production audit report."""

    static = _static_contract(repo_root)
    synthetic = _synthetic_contract()
    metrics = {name: 0 for name in ZERO_BUDGET_METRICS}
    for name in metrics:
        if name in static:
            metrics[name] = int(static[name])
        if name in synthetic["metrics"]:
            metrics[name] = int(synthetic["metrics"][name])

    live: dict[str, Any] = {"checked": False}
    if production:
        if database_dir is None or wiki_dir is None:
            raise ValueError("production audit requires database_dir and wiki_dir")
        live = {
            "checked": True,
            **audit_live_projection_state(
                wiki_dir=wiki_dir,
                projection_db=database_dir / "wiki_projection.db",
            ),
        }
        for name in (
            "projection_binding_gap",
            "stale_projection",
            "required_consumer_receipt_gap",
        ):
            metrics[name] += int(live.get(name, 0))

    failures = sorted(name for name, value in metrics.items() if int(value) != 0)
    if synthetic["error"]:
        failures.append("synthetic_contract_error")
    if not static["vault_sync_uses_read_only_replay"]:
        failures.append("vault_sync_read_only_contract")
    if not all(static["lifecycle_markers"].values()):
        failures.append("lifecycle_contract_markers")
    if not all(static["page_role_markers"].values()):
        failures.append("page_role_contract_markers")
    if static["static_scan_registry_stale_count"]:
        failures.append("static_sink_registry_stale")
    if static["static_scan_unknown_count"]:
        failures.append("static_sink_registry_unknown")
    failures = sorted(set(failures))
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "ok": not failures,
        "production_checked": production,
        "zero_budget_metrics": list(ZERO_BUDGET_METRICS),
        "metrics": metrics,
        "static": static,
        "synthetic": synthetic,
        "live": live,
        "failures": failures,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the COG-050 audit."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--database-dir", type=Path)
    parser.add_argument("--wiki-dir", type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    """Run the audit and return a strict-gate compatible exit code."""

    args = build_parser().parse_args(list(argv) if argv is not None else None)
    database_dir = args.database_dir
    wiki_dir = args.wiki_dir
    if args.production and (database_dir is None or wiki_dir is None):
        from core.config import get_config

        config = get_config()
        database_dir = database_dir or Path(config.database_dir)
        wiki_dir = wiki_dir or Path(config.wiki_dir)
    report = build_report(
        production=args.production,
        database_dir=database_dir,
        wiki_dir=wiki_dir,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict and not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
