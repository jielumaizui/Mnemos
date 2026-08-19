"""Dry-run-first reconciliation for committed cognition episode projections."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Iterator

from core.cognitive.cognition_episode_dispatch import COMMAND_TYPE, CognitionEpisodeDispatchOwner
from core.cognitive.cognition_episode_event_schema import (
    initialize_cognition_episode_event_schema,
    inspect_cognition_episode_event_schema,
)
from core.cognitive.cognition_episode_dispatch_audit import (
    build_report as audit_event_dispatch,
)
from core.cognitive.cognition_episode_projection_schema import (
    initialize_cognition_episode_projection_schema,
    inspect_cognition_episode_projection_schema,
)
from core.cognitive.state_contract import sha256_json
from core.event_bus_lease import (
    initialize_event_bus_lease_schema,
    inspect_event_bus_lease_schema,
)
from core.evidence.evidence_graph_direction_audit import (
    build_report as audit_evidence_direction,
)
from core.frontmatter import fm_get, parse_frontmatter
from core.migrations.model_call_ledger_reconcile.runtime import runtime_writers_are_inactive
from core.ops.exclusive_file_lock import exclusive_file_lock
from core.utils import read_text_value
from core.wiki_projection_lifecycle import (
    WikiProjectionLedger,
    resolve_wiki_projection_db_path,
)

SCHEMA_VERSION = "mnemos.cognition_episode_reconciliation.v1"


def _paths(config: Any) -> dict[str, Path]:
    database_dir = Path(config.database_dir).expanduser().resolve(strict=False)
    return {
        "database_dir": database_dir,
        "state": database_dir / "producer_consumer_ledger.db",
        "evidence_graph": database_dir / "evidence_graph.db",
        "cognitive_graph": Path(
            getattr(config, "cognitive_graph_db_path", None) or database_dir / "cognitive_graph.db"
        )
        .expanduser()
        .resolve(strict=False),
        "wiki_projection": resolve_wiki_projection_db_path(config)
        .expanduser()
        .resolve(strict=False),
        "events": (Path(config.mnemos_dir) / "events.db").expanduser().resolve(strict=False),
        "wiki": Path(config.wiki_dir).expanduser().resolve(strict=False),
    }


def _connect_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve(strict=True)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _logical_snapshot_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with _connect_read_only(path) as conn:
        for statement in conn.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _pending_revision_ids(state_db: Path) -> list[str]:
    if not state_db.is_file():
        return []
    with _connect_read_only(state_db) as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if not {"cognitive_state_outbox", "cognitive_state_effect_receipts"} <= tables:
            return []
        return [
            str(row[0])
            for row in conn.execute(
                """SELECT DISTINCT o.revision_id
                   FROM cognitive_state_outbox AS o
                   LEFT JOIN cognitive_state_effect_receipts AS r
                     ON r.command_id=o.command_id
                   WHERE o.command_type=? AND r.command_id IS NULL
                   ORDER BY o.revision_id""",
                (COMMAND_TYPE,),
            ).fetchall()
        ]


def _repair_revision_ids(config: Any, state_db: Path) -> list[str]:
    """Return committed receipts whose fixed target oracle no longer verifies."""

    if not state_db.is_file():
        return []
    from core.cognitive.cognition_episode_projection_receipt import (
        CognitionEpisodeProjectionProof,
        CognitionEpisodeProjectionTargets,
        verify_cognition_episode_projection,
    )
    from core.cognitive.state_store import CognitiveStateStore

    state = CognitiveStateStore(config)
    targets = CognitionEpisodeProjectionTargets.from_config(
        config,
        state_db_path=state.db_path,
    )
    with _connect_read_only(state_db) as conn:
        rows = conn.execute(
            """SELECT o.command_id, o.revision_id, r.consumer_id,
                      r.target_effect_id, r.before_hash, r.after_hash
               FROM cognitive_state_outbox AS o
               JOIN cognitive_state_effect_receipts AS r
                 ON r.command_id=o.command_id
               WHERE o.command_type=? AND r.status='committed'
               ORDER BY o.revision_id, r.consumer_id""",
            (COMMAND_TYPE,),
        ).fetchall()
    repair: set[str] = set()
    for row in rows:
        revision_id = str(row["revision_id"])
        command = state.command(str(row["command_id"]))
        revision = state.revision(revision_id)
        if command is None or revision is None:
            repair.add(revision_id)
            continue
        proof = CognitionEpisodeProjectionProof(
            consumer_id=str(row["consumer_id"]),
            revision_id=revision_id,
            effect_id=str(row["target_effect_id"]),
            before_hash=str(row["before_hash"]),
            after_hash=str(row["after_hash"]),
        )
        try:
            verify_cognition_episode_projection(
                targets=targets,
                command=command,
                revision=revision,
                proof=proof,
            )
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error):
            repair.add(revision_id)
    return sorted(repair)


def _bound_wiki_page_counts(wiki_dir: Path, revision_ids: list[str]) -> dict[str, int]:
    counts = {revision_id: 0 for revision_id in revision_ids}
    if not wiki_dir.is_dir() or not counts:
        return counts
    for page in sorted(wiki_dir.rglob("*.md")):
        content = read_text_value(page)
        frontmatter, _body = parse_frontmatter(content)
        revision_id = str(fm_get(frontmatter, "cognition_episode_revision_id") or "")
        if revision_id in counts:
            counts[revision_id] += 1
    return counts


def _classify_direction_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Classify only reversals whose swapped endpoint types satisfy the frozen contract."""

    relation = str(candidate["relation_type"])
    current_source_type = str(candidate["source_type"])
    current_target_type = str(candidate["target_type"])
    source_type = current_target_type
    target_type = current_source_type
    rank = {
        "memory": 0,
        "knowledge": 0,
        "raw_revision_span": 0,
        "observation": 1,
        "mirror": 2,
        "claim": 2,
        "belief": 2,
        "reflection": 3,
        "insight": 4,
        "decision": 4,
        "prediction": 5,
        "action": 6,
        "outcome": 7,
    }
    valid = False
    if relation == "observed_in":
        valid = source_type == "observation" and target_type in {
            "raw_revision_span",
            "memory",
            "knowledge",
        }
    elif relation == "generated_from":
        valid = source_type == "insight" and target_type == "reflection"
    elif relation in {"derived_from", "based_on"}:
        source_rank = rank.get(source_type)
        target_rank = rank.get(target_type)
        valid = source_rank is not None and target_rank is not None and source_rank >= target_rank
    else:
        exact = {
            "predicted_from": ({"prediction"}, {"decision"}),
            "implements": ({"action"}, {"decision"}),
            "measures": ({"outcome"}, {"action", "prediction"}),
            "contains": (
                {"episode"},
                {
                    "observation",
                    "claim",
                    "belief",
                    "decision",
                    "prediction",
                    "action",
                    "outcome",
                },
            ),
        }
        if relation in exact:
            sources, targets = exact[relation]
            valid = source_type in sources and target_type in targets
    return {
        **candidate,
        "classification": "reverse" if valid else "unclassified",
        "reversed_source_id": str(candidate["target_id"]) if valid else "",
        "reversed_target_id": str(candidate["source_id"]) if valid else "",
    }


def _apply_direction_rebuild(
    db_path: Path,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reverse an exact reviewed candidate set in one fail-closed transaction."""

    if any(item["classification"] != "reverse" for item in candidates):
        raise RuntimeError("unclassified evidence direction candidate cannot be rebuilt")
    applied: list[dict[str, Any]] = []
    if not candidates:
        return applied
    with sqlite3.connect(str(db_path), timeout=60) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        try:
            for candidate in candidates:
                edge = conn.execute(
                    "SELECT * FROM evidence_edges WHERE id=?",
                    (int(candidate["edge_id"]),),
                ).fetchone()
                if edge is None or any(
                    (
                        str(edge["source_id"]) != str(candidate["source_id"]),
                        str(edge["target_id"]) != str(candidate["target_id"]),
                        str(edge["relation_type"]) != str(candidate["relation_type"]),
                    )
                ):
                    raise RuntimeError("reviewed evidence direction candidate drifted")
                reversed_edge = conn.execute(
                    """SELECT * FROM evidence_edges
                       WHERE source_id=? AND target_id=? AND relation_type=?""",
                    (
                        str(candidate["reversed_source_id"]),
                        str(candidate["reversed_target_id"]),
                        str(candidate["relation_type"]),
                    ),
                ).fetchone()
                action = "reversed"
                if reversed_edge is None:
                    conn.execute(
                        """UPDATE evidence_edges SET source_id=?, target_id=?
                           WHERE id=?""",
                        (
                            str(candidate["reversed_source_id"]),
                            str(candidate["reversed_target_id"]),
                            int(candidate["edge_id"]),
                        ),
                    )
                else:
                    comparable = (
                        "confidence",
                        "evidence",
                        "access_control",
                    )
                    if any(str(edge[name]) != str(reversed_edge[name]) for name in comparable):
                        raise RuntimeError(
                            "reversed evidence edge conflicts with an existing target edge"
                        )
                    conn.execute(
                        "DELETE FROM evidence_edges WHERE id=?",
                        (int(candidate["edge_id"]),),
                    )
                    action = "deduplicated"
                applied.append(
                    {
                        "edge_id": int(candidate["edge_id"]),
                        "action": action,
                        "source_id": str(candidate["reversed_source_id"]),
                        "target_id": str(candidate["reversed_target_id"]),
                        "relation_type": str(candidate["relation_type"]),
                    }
                )
            conn.commit()
        except (RuntimeError, KeyError, TypeError, ValueError, sqlite3.Error):
            conn.rollback()
            raise
    return applied


def build_reconciliation_plan(config: Any) -> dict[str, Any]:
    """Return a content-addressed plan without creating or mutating any target."""

    paths = _paths(config)
    event_audit = audit_event_dispatch(
        database_dir=paths["database_dir"],
        event_db_path=paths["events"],
        wiki_dir=paths["wiki"],
        cognitive_graph_db_path=paths["cognitive_graph"],
        wiki_projection_db_path=paths["wiki_projection"],
    )
    direction_audit = audit_evidence_direction(paths["evidence_graph"])
    direction_candidates = [
        _classify_direction_candidate(dict(candidate))
        for candidate in direction_audit["runtime"]["legacy_direction_candidates"]
    ]
    pending = _pending_revision_ids(paths["state"])
    repair_revision_ids = _repair_revision_ids(config, paths["state"])
    page_counts = _bound_wiki_page_counts(paths["wiki"], pending)
    projection_schema = inspect_cognition_episode_projection_schema(
        evidence_db_path=paths["evidence_graph"],
        cognitive_graph_db_path=paths["cognitive_graph"],
        wiki_projection_db_path=paths["wiki_projection"],
    )
    event_lease_schema = inspect_event_bus_lease_schema(paths["events"])
    episode_event_schema = inspect_cognition_episode_event_schema(paths["events"])
    actions: list[str] = []
    if projection_schema["evidence_graph"]["gaps"]:
        actions.append("initialize_evidence_projection_schema")
    if projection_schema["cognitive_graph"]["gaps"]:
        actions.append("initialize_cognitive_graph_projection_schema")
    if projection_schema["wiki"]["gaps"]:
        actions.append("initialize_wiki_projection_schema")
    if event_lease_schema["gaps"]:
        actions.append("initialize_event_bus_lease_schema")
    if episode_event_schema["gaps"]:
        actions.append("initialize_cognition_episode_event_schema")
    if pending:
        actions.append("dispatch_pending_cognition_episode")
    if repair_revision_ids:
        actions.append("repair_missing_target_effects")
    if direction_candidates and all(
        item["classification"] == "reverse" for item in direction_candidates
    ):
        actions.append("rebuild_legacy_evidence_edge_directions")
    blockers: list[str] = []
    if event_audit["gaps"]["static_contract_gap"]:
        blockers.append("event_dispatch_static_contract_gap")
    if direction_audit["gaps"]["static_contract_gap"]:
        blockers.append("evidence_direction_static_contract_gap")
    if any(item["classification"] == "unclassified" for item in direction_candidates):
        blockers.append("unclassified_legacy_direction_candidates")
    if any(
        str(gap).startswith("duplicate_terminal_receipts:") for gap in episode_event_schema["gaps"]
    ):
        blockers.append("duplicate_terminal_receipts_require_explicit_classification")
    if any(count == 0 for count in page_counts.values()):
        blockers.append("pending_revision_without_bound_wiki_page")
    snapshot_hashes = {
        name: _logical_snapshot_hash(path)
        for name, path in paths.items()
        if name not in {"database_dir", "wiki"} and path.is_file()
    }
    inventory = {
        "schema_version": SCHEMA_VERSION,
        "target_paths": {
            key: str(value) for key, value in paths.items() if key not in {"database_dir", "wiki"}
        },
        "snapshot_hashes": snapshot_hashes,
        "pending_revision_ids": pending,
        "repair_revision_ids": repair_revision_ids,
        "bound_wiki_page_counts": page_counts,
        "actions": actions,
        "blockers": blockers,
        "event_gaps": dict(event_audit["gaps"]),
        "direction_gaps": dict(direction_audit["gaps"]),
        "direction_rebuild_candidates": direction_candidates,
        "projection_schema_gaps": {
            "evidence_graph": list(projection_schema["evidence_graph"]["gaps"]),
            "cognitive_graph": list(projection_schema["cognitive_graph"]["gaps"]),
            "wiki": list(projection_schema["wiki"]["gaps"]),
        },
        "event_lease_schema_gaps": list(event_lease_schema["gaps"]),
        "cognition_episode_event_schema_gaps": list(episode_event_schema["gaps"]),
    }
    return {
        **inventory,
        "inventory_hash": sha256_json(inventory),
        "apply_required": bool(actions),
        "backup_targets": [
            str(path)
            for name, path in paths.items()
            if name not in {"database_dir", "wiki"} and path.is_file()
        ],
        "event_dispatch_audit": event_audit,
        "evidence_direction_audit": direction_audit,
    }


def _backup_database(source: Path, destination: Path) -> dict[str, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(source), timeout=60) as source_conn:
        with sqlite3.connect(str(destination), timeout=60) as target_conn:
            source_conn.backup(target_conn)
    with _connect_read_only(destination) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise RuntimeError(f"backup integrity check failed: {source}")
    return {
        "source": str(source),
        "backup": str(destination),
        "source_snapshot_hash": _logical_snapshot_hash(source),
        "backup_snapshot_hash": _logical_snapshot_hash(destination),
        "integrity_check": integrity,
    }


def _write_backups(
    paths: dict[str, Path],
    backup_dir: Path,
    inventory_hash: str,
) -> dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=False)
    rows: list[dict[str, str]] = []
    absent_targets: list[str] = []
    for name in (
        "state",
        "evidence_graph",
        "cognitive_graph",
        "wiki_projection",
        "events",
    ):
        source = paths[name]
        if source.is_file():
            row = _backup_database(source, backup_dir / f"{name}.sqlite3")
            if row["source_snapshot_hash"] != row["backup_snapshot_hash"]:
                raise RuntimeError(f"backup logical snapshot mismatch: {source}")
            rows.append(row)
        else:
            absent_targets.append(str(source))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "inventory_hash": inventory_hash,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "databases": rows,
        "absent_targets": absent_targets,
        "integrity_ok": all(row["integrity_check"] == "ok" for row in rows),
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _remove_sqlite_family(path: Path) -> None:
    for target in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if target.is_file():
            target.unlink()


def _restore_backups(manifest: dict[str, Any]) -> None:
    backed_up_sources = {str(row["source"]) for row in manifest["databases"]}
    for absent in manifest["absent_targets"]:
        if absent not in backed_up_sources:
            _remove_sqlite_family(Path(absent))
    for row in manifest["databases"]:
        source = Path(row["source"])
        backup = Path(row["backup"])
        _remove_sqlite_family(source)
        source.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(backup), timeout=60) as backup_conn:
            with sqlite3.connect(str(source), timeout=60) as target_conn:
                backup_conn.backup(target_conn)
        if _logical_snapshot_hash(source) != str(row["source_snapshot_hash"]):
            raise RuntimeError(f"rollback snapshot mismatch: {source}")


@contextmanager
def _migration_lock(database_dir: Path) -> Iterator[None]:
    with exclusive_file_lock(
        database_dir / ".cognition_episode_projection_reconcile.lock",
        unavailable_message="cognition episode reconciliation lock is held",
    ):
        yield


def _wait_for_projection(owner: CognitionEpisodeDispatchOwner, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = [
            command
            for command in owner.state.pending_commands()
            if command["command_type"] == COMMAND_TYPE
        ]
        if not pending:
            return
        time.sleep(0.02)
    raise RuntimeError("cognition episode projection did not reach terminal receipts")


def apply_reconciliation(
    config: Any,
    *,
    expected_inventory_hash: str,
    backup_dir: Path,
    daemon_check: Callable[[Path], bool] = runtime_writers_are_inactive,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Back up every target, project pending revisions, then independently audit."""

    plan = build_reconciliation_plan(config)
    if expected_inventory_hash != plan["inventory_hash"]:
        raise ValueError("reviewed inventory hash does not match current targets")
    if plan["blockers"]:
        raise RuntimeError("reconciliation plan has blockers: " + ",".join(plan["blockers"]))
    backup_dir = Path(backup_dir).expanduser().resolve(strict=False)
    if backup_dir.exists():
        raise FileExistsError("backup directory must not already exist")
    paths = _paths(config)
    if not daemon_check(paths["database_dir"]):
        raise RuntimeError("Mnemos runtime writers must be conclusively stopped before apply")

    with _migration_lock(paths["database_dir"]):
        current = build_reconciliation_plan(config)
        if current["inventory_hash"] != expected_inventory_hash:
            raise RuntimeError("reconciliation inventory drifted after lock acquisition")
        if not daemon_check(paths["database_dir"]):
            raise RuntimeError("Mnemos runtime writers restarted before apply")
        backup_manifest = _write_backups(
            paths,
            backup_dir,
            expected_inventory_hash,
        )
        bus = None
        direction_rebuild_receipts: list[dict[str, Any]] = []
        try:
            from core.cognitive_graph.store import CognitiveGraphStore
            from core.evidence.evidence_graph import EvidenceGraph
            from core.mnemos_bus import EventBus

            if not paths["evidence_graph"].is_file():
                EvidenceGraph(str(paths["evidence_graph"]))
            if not paths["cognitive_graph"].is_file():
                CognitiveGraphStore(
                    str(paths["cognitive_graph"]),
                    ownership_config=config,
                )
            if not paths["wiki_projection"].is_file():
                WikiProjectionLedger(paths["wiki_projection"])
            initialize_cognition_episode_projection_schema(
                evidence_db_path=paths["evidence_graph"],
                cognitive_graph_db_path=paths["cognitive_graph"],
                wiki_projection_db_path=paths["wiki_projection"],
            )
            if paths["events"].is_file():
                initialize_event_bus_lease_schema(paths["events"])
                initialize_cognition_episode_event_schema(paths["events"])
            direction_rebuild_receipts = _apply_direction_rebuild(
                paths["evidence_graph"],
                current["direction_rebuild_candidates"],
            )
            evidence_graph = EvidenceGraph(str(paths["evidence_graph"]))
            cognitive_graph = CognitiveGraphStore(
                str(paths["cognitive_graph"]),
                ownership_config=config,
            )
            bus = EventBus(config=config)
            owner = CognitionEpisodeDispatchOwner(
                config=config,
                event_bus=bus,
                cognitive_graph_store=cognitive_graph,
                evidence_graph=evidence_graph,
            )
            owner.subscribe()
            for revision_id in current["repair_revision_ids"]:
                owner.reconcile_revision(revision_id)
            bus.start_dispatch()
            owner.publish_pending()
            _wait_for_projection(owner, timeout)
            bus.stop_dispatch()
            bus.close()
            bus = None
            event_audit = audit_event_dispatch(
                database_dir=paths["database_dir"],
                event_db_path=paths["events"],
                wiki_dir=paths["wiki"],
                cognitive_graph_db_path=paths["cognitive_graph"],
                wiki_projection_db_path=paths["wiki_projection"],
            )
            direction_audit = audit_evidence_direction(paths["evidence_graph"])
            if not event_audit["ok"] or not direction_audit["ok"]:
                raise RuntimeError("post-apply cognition projection audit failed")
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error):
            if bus is not None:
                bus.stop_dispatch()
                bus.close()
            _restore_backups(backup_manifest)
            raise
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "inventory_hash": expected_inventory_hash,
        "backup_manifest": backup_manifest,
        "direction_rebuild_receipts": direction_rebuild_receipts,
        "event_dispatch_audit": event_audit,
        "evidence_direction_audit": direction_audit,
    }
