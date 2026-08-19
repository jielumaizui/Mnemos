#!/usr/bin/env python3
"""Audit or repair historical pipeline ownership/receipt gaps.

Dry-run is the default. ``--apply`` performs only additive/retryable repairs:
it migrates Amphora's session-only schema, requeues unproven legacy ``done``
rows that still have source messages, creates Capture outboxes, attaches typed
Amphora receipts, corrects prematurely consumed recap states, and removes only
the proven duplicate worker raw rows superseded by a committed document capture.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _capture_gap_rows(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    with sqlite3.connect(str(db_path), timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        mapped: set[int] = set()
        if _table_exists(conn, "capture_distillation_handoffs"):
            for row in conn.execute("SELECT event_ids_json FROM capture_distillation_handoffs"):
                try:
                    mapped.update(int(value) for value in json.loads(row[0] or "[]"))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
        rows = conn.execute(
            "SELECT * FROM capture_events WHERE status='done' ORDER BY source_agent, session_id, turn_number"
        ).fetchall()
        return [dict(row) for row in rows if int(row["id"]) not in mapped]


def _capture_handoff_snapshot(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"counts": {}, "nonterminal_handoffs": 0}
    with sqlite3.connect(str(db_path), timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "capture_distillation_handoffs"):
            return {"counts": {}, "nonterminal_handoffs": 0}
        counts = {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM capture_distillation_handoffs GROUP BY status"
            )
        }
    terminal = counts.get("committed", 0) + counts.get("intentional_skip", 0)
    return {"counts": counts, "nonterminal_handoffs": sum(counts.values()) - terminal}


def _document_worker_duplicate_rows(
    raw_db_path: Path,
    capture_db_path: Path,
) -> list[dict[str, Any]]:
    """Find worker raw rows that explicitly point at an existing producer revision."""
    if not raw_db_path.exists() or not capture_db_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with sqlite3.connect(str(raw_db_path), timeout=30) as raw_conn:
        raw_conn.row_factory = sqlite3.Row
        if not _table_exists(raw_conn, "raw_turns") or not _table_exists(
            raw_conn, "raw_turn_revisions"
        ):
            return []
        duplicates = raw_conn.execute(
            """
            SELECT event_id, source_agent, session_id, turn_number,
                   current_revision_id, metadata_json
            FROM raw_turns
            WHERE origin='sync_engine'
              AND (source_agent LIKE 'file_ingestor:%' OR source_agent='document_processor')
            ORDER BY source_agent, session_id, turn_number
            """
        ).fetchall()
        with sqlite3.connect(str(capture_db_path), timeout=30) as capture_conn:
            capture_conn.row_factory = sqlite3.Row
            for duplicate in duplicates:
                try:
                    metadata = json.loads(duplicate["metadata_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    metadata = {}
                canonical_revision_id = str(metadata.get("raw_event_id") or "")
                if not canonical_revision_id:
                    continue
                canonical = raw_conn.execute(
                    """
                    SELECT t.event_id, t.turn_number, t.metadata_json
                    FROM raw_turn_revisions r
                    JOIN raw_turns t ON t.event_id=r.logical_event_id
                    WHERE r.revision_id=?
                    """,
                    (canonical_revision_id,),
                ).fetchone()
                if canonical is None or str(canonical["event_id"]) == str(duplicate["event_id"]):
                    continue
                try:
                    canonical_metadata = json.loads(canonical["metadata_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    canonical_metadata = {}
                capture = capture_conn.execute(
                    """
                    SELECT id, status FROM capture_events
                    WHERE source_agent=? AND session_id=? AND turn_number=?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (
                        duplicate["source_agent"],
                        duplicate["session_id"],
                        canonical["turn_number"],
                    ),
                ).fetchone()
                handoff_status = ""
                if capture is not None and _table_exists(
                    capture_conn, "capture_distillation_handoffs"
                ):
                    handoff = capture_conn.execute(
                        """
                        SELECT status FROM capture_distillation_handoffs h
                        WHERE EXISTS (
                            SELECT 1 FROM json_each(h.event_ids_json)
                            WHERE CAST(value AS INTEGER)=?
                        )
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (int(capture["id"]),),
                    ).fetchone()
                    handoff_status = str(handoff["status"] or "") if handoff else ""
                edge_count = 0
                if _table_exists(raw_conn, "raw_provenance_edges"):
                    edge_count = int(
                        raw_conn.execute(
                            """
                            SELECT COUNT(*) FROM raw_provenance_edges e
                            JOIN raw_turn_revisions r ON r.revision_id=e.source_revision_id
                            WHERE r.logical_event_id=?
                            """,
                            (duplicate["event_id"],),
                        ).fetchone()[0]
                    )
                capture_status = str(capture["status"] or "") if capture else ""
                rows.append(
                    {
                        "duplicate_event_id": str(duplicate["event_id"]),
                        "duplicate_revision_id": str(duplicate["current_revision_id"] or ""),
                        "canonical_event_id": str(canonical["event_id"]),
                        "canonical_revision_id": canonical_revision_id,
                        "source_agent": str(duplicate["source_agent"]),
                        "session_id": str(duplicate["session_id"]),
                        "asset_id": str(canonical_metadata.get("asset_id") or ""),
                        "capture_status": capture_status,
                        "handoff_status": handoff_status,
                        "provenance_edges": edge_count,
                        "safe_to_remove": bool(
                            canonical_metadata.get("asset_id")
                            and capture_status == "done"
                            and handoff_status in {"committed", "intentional_skip"}
                            and edge_count == 0
                        ),
                    }
                )
    return rows


def _remove_safe_document_worker_duplicates(
    raw_db_path: Path,
    capture_db_path: Path,
) -> tuple[int, int]:
    """Delete only redundant worker rows whose canonical receipt is fully committed."""
    rows = _document_worker_duplicate_rows(raw_db_path, capture_db_path)
    safe = [row for row in rows if row["safe_to_remove"]]
    if not safe:
        return 0, len(rows)
    event_ids = [row["duplicate_event_id"] for row in safe]
    with sqlite3.connect(str(raw_db_path), timeout=30) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for event_id in event_ids:
            conn.execute("DELETE FROM raw_access_log WHERE event_id=?", (event_id,))
            conn.execute("DELETE FROM raw_metrics WHERE event_id=?", (event_id,))
            if _table_exists(conn, "raw_native_contract_observations"):
                conn.execute(
                    "DELETE FROM raw_native_contract_observations WHERE logical_event_id=?",
                    (event_id,),
                )
            conn.execute("DELETE FROM raw_turn_revisions WHERE logical_event_id=?", (event_id,))
            conn.execute("DELETE FROM raw_turns WHERE event_id=?", (event_id,))
        if _table_exists(conn, "raw_lifecycle_state"):
            conn.execute(
                """
                INSERT INTO raw_lifecycle_state (key, value, updated_at)
                VALUES ('document_ingest_ownership_reconciliation', ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (
                    json.dumps(
                        {
                            "removed_duplicate_event_ids": event_ids,
                            "canonical_revision_ids": [
                                row["canonical_revision_id"] for row in safe
                            ],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
        conn.commit()
    remaining = _document_worker_duplicate_rows(raw_db_path, capture_db_path)
    return len(safe), len(remaining)


def _amphora_snapshot(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"exists": False, "counts": {}, "legacy_session_unique": False}
    with sqlite3.connect(str(db_path), timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        table = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='distillation_tasks'"
        ).fetchone()
        if not table:
            return {"exists": True, "counts": {}, "legacy_session_unique": False}
        counts = {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM distillation_tasks GROUP BY status"
            )
        }
        columns = {row[1] for row in conn.execute("PRAGMA table_info(distillation_tasks)")}
        if "terminal_reason" not in columns:
            unproven = int(conn.execute("""
                    SELECT COUNT(*) FROM distillation_tasks
                    WHERE status='done' AND COALESCE(output_path, '')=''
                    """).fetchone()[0])
            inventory: dict[str, Any] = {
                "schema_version": "legacy_session_schema",
                "inventory_hash": "",
                "object_count": unproven,
                "uncovered_count": unproven,
                "objects": [],
            }
        else:
            from core.kia import amphora

            inventory = amphora.build_historical_provenance_inventory()
            unproven = int(inventory["uncovered_count"])
        return {
            "exists": True,
            "counts": counts,
            "legacy_session_unique": "SESSION_ID TEXT UNIQUE" in str(table[0]).upper(),
            "unproven_terminal_count": unproven,
            "legacy_provenance_inventory": inventory,
        }


def _recap_gaps(db_path: Path, wiki_dir: Path) -> list[dict[str, str]]:
    if not db_path.exists():
        return []
    with sqlite3.connect(str(db_path), timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "retrospective_sessions"):
            return []
        columns = {row[1] for row in conn.execute("PRAGMA table_info(retrospective_sessions)")}
        receipt_sql = "completion_receipt" if "completion_receipt" in columns else "'{}'"
        rows = conn.execute(f"""
            SELECT recap_id, task_id, state, finalized_page, {receipt_sql} AS completion_receipt
            FROM retrospective_sessions WHERE state IN ('finalized', 'consumed')
            """).fetchall()  # nosec B608
        gaps = []
        for row in rows:
            page = str(row["finalized_page"] or "")
            exists = bool(page and (wiki_dir / page).exists())
            try:
                receipt = json.loads(row["completion_receipt"] or "{}")
            except (json.JSONDecodeError, TypeError):
                receipt = {}
            consumed_without_receipt = row["state"] == "consumed" and not (
                isinstance(receipt, dict)
                and receipt.get("status") == "committed"
                and receipt.get("terminal") is True
            )
            if consumed_without_receipt or not exists:
                gaps.append(
                    {
                        "recap_id": str(row["recap_id"]),
                        "task_id": str(row["task_id"]),
                        "state": str(row["state"]),
                        "page_path": page,
                        "page_exists": str(exists).lower(),
                    }
                )
        return gaps


def audit() -> dict[str, Any]:
    cfg = get_config()
    capture_db = cfg.database_dir / "capture_queue.db"
    amphora_db = cfg.database_dir / "distill_queue.db"
    recap_db = cfg.database_dir / "recap_tasks.db"
    raw_db = cfg.database_dir / "raw_events.db"
    capture_gaps = _capture_gap_rows(capture_db)
    capture_sessions = {(str(row["source_agent"]), str(row["session_id"])) for row in capture_gaps}
    recap_gaps = _recap_gaps(recap_db, cfg.wiki_dir)
    amphora = _amphora_snapshot(amphora_db)
    handoffs = _capture_handoff_snapshot(capture_db)
    document_duplicates = _document_worker_duplicate_rows(raw_db, capture_db)
    unsafe_document_duplicates = sum(
        1 for row in document_duplicates if not row["safe_to_remove"]
    )
    return {
        "schema_version": "mnemos.pipeline_receipt_reconciliation.v1",
        "mode": "dry_run",
        "capture": {
            "done_events_without_handoff": len(capture_gaps),
            "sessions_without_handoff": len(capture_sessions),
            **handoffs,
        },
        "amphora": amphora,
        "recap": {"premature_or_missing_page_states": len(recap_gaps)},
        "document_ingest": {
            "duplicate_worker_raw_rows": len(document_duplicates),
            "safe_to_remove": len(document_duplicates) - unsafe_document_duplicates,
            "blocked": unsafe_document_duplicates,
        },
        "reconciliation_gap": (
            len(capture_gaps)
            + int(handoffs["nonterminal_handoffs"])
            + int(amphora.get("unproven_terminal_count", 0))
            + len(recap_gaps)
            + len(document_duplicates)
        ),
    }


def _repair_capture_handoffs(
    *,
    amphora_manifest: dict[str, Any] | None = None,
    backup_dir: Path | None = None,
) -> tuple[int, int, int]:
    from core.kia.amphora import (
        build_historical_provenance_inventory,
        enqueue_with_receipt,
        reconcile_historical_task_provenance,
    )
    from core.sync_framework.capture_handoff import build_messages_revision
    from core.sync_framework.capture_queue import CaptureQueue

    queue = CaptureQueue()
    rows = _capture_gap_rows(queue.db_path)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        try:
            row["payload"] = json.loads(row.get("payload_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            row["payload"] = {}
        grouped[(str(row["source_agent"]), str(row["session_id"]))].append(row)

    created = committed = blocked = 0
    for (source_agent, session_id), events in grouped.items():
        current_inventory = build_historical_provenance_inventory()
        uncovered = [
            item
            for item in current_inventory["objects"]
            if not item["covered"]
            and str(item["row"].get("source_agent") or "") == source_agent
            and str(item["row"].get("session_id") or "") == session_id
        ]
        reviewed: dict[str, Any] | None = None
        if uncovered:
            if amphora_manifest is None or backup_dir is None:
                blocked += 1
                continue
            reviewed_matches = [
                item
                for item in amphora_manifest.get("objects", [])
                if str(item.get("primary_key") or "")
                == str(uncovered[0]["primary_key"])
                and str(item.get("object_hash") or "")
                == str(uncovered[0]["object_hash"])
            ]
            if len(uncovered) != 1 or len(reviewed_matches) != 1:
                blocked += 1
                continue
            reviewed = reviewed_matches[0]
        revision = build_messages_revision(events)
        handoff = queue.create_distillation_handoff(
            source_agent,
            session_id,
            events,
            enabled=True,
            input_revision=revision,
        )
        created += 1
        if handoff["status"] == "intentional_skip":
            committed += 1
            continue
        try:
            if reviewed is not None:
                if amphora_manifest is None or backup_dir is None:
                    raise ValueError(
                        "reviewed Amphora migration requires its manifest and backup directory"
                    )
                receipt = reconcile_historical_task_provenance(
                    session_id=session_id,
                    messages=handoff["messages"],
                    meta=handoff["meta"],
                    reviewed_task_id=str(reviewed["primary_key"]),
                    expected_old_input_revision=str(
                        reviewed["old_input_revision"]
                    ),
                    expected_object_hash=str(reviewed["object_hash"]),
                    expected_inventory_hash=str(amphora_manifest["inventory_hash"]),
                    backup_dir=backup_dir,
                )
            else:
                receipt = enqueue_with_receipt(
                    session_id=session_id,
                    messages=handoff["messages"],
                    meta=handoff["meta"],
                )
            queue.commit_distillation_handoff(
                handoff["receipt_id"],
                downstream_receipt_id=receipt.receipt_id,
                downstream_task_id=receipt.task_id,
            )
            committed += 1
        except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
            queue.fail_distillation_handoff(handoff["receipt_id"], str(exc))
    queue.close()
    return created, committed, blocked


def _repair_recap_states() -> int:
    cfg = get_config()
    db_path = cfg.database_dir / "recap_tasks.db"
    gaps = _recap_gaps(db_path, cfg.wiki_dir)
    if not gaps:
        return 0
    with sqlite3.connect(str(db_path), timeout=30) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(retrospective_sessions)")}
        if "completion_receipt" not in columns:
            conn.execute(
                "ALTER TABLE retrospective_sessions ADD COLUMN completion_receipt TEXT DEFAULT '{}'"
            )
        for gap in gaps:
            page_exists = gap["page_exists"] == "true"
            state = "finalized" if page_exists else "retryable_failed"
            receipt = {
                "schema_version": "mnemos.retrospective_persist_receipt.v1",
                "status": "committed" if page_exists else "retryable_failed",
                "terminal": page_exists,
                "page_path": gap["page_path"],
                "terminal_reason": (
                    "historical_page_verified_consumption_state_corrected"
                    if page_exists
                    else "historical_final_state_has_no_committed_page"
                ),
            }
            conn.execute(
                """
                UPDATE retrospective_sessions
                SET state=?, completion_receipt=?, updated_at=datetime('now')
                WHERE recap_id=?
                """,
                (state, json.dumps(receipt, ensure_ascii=False), gap["recap_id"]),
            )
    return len(gaps)


def apply_repairs(
    *,
    amphora_manifest: dict[str, Any] | None = None,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    before = audit()
    amphora_schema_migrated = 0
    if before["amphora"].get("legacy_session_unique"):
        from core.kia import amphora

        amphora._init_db()
        amphora_schema_migrated = 1
        before = audit()
    amphora_uncovered = int(
        before["amphora"].get("unproven_terminal_count", 0)
    )
    missing_migration_authority = bool(
        amphora_uncovered and (amphora_manifest is None or backup_dir is None)
    )
    if int(before["capture"].get("done_events_without_handoff", 0)):
        handoffs_created, handoffs_committed, handoffs_blocked = (
            _repair_capture_handoffs(
                amphora_manifest=amphora_manifest,
                backup_dir=backup_dir,
            )
        )
    else:
        handoffs_created = handoffs_committed = handoffs_blocked = 0
    recaps_repaired = _repair_recap_states()
    cfg = get_config()
    document_duplicates_removed, document_duplicates_remaining = (
        _remove_safe_document_worker_duplicates(
            cfg.database_dir / "raw_events.db",
            cfg.database_dir / "capture_queue.db",
        )
    )
    after = audit()
    after["mode"] = "apply"
    after["applied"] = {
        "amphora_requeued": 0,
        "amphora_schema_migrated": amphora_schema_migrated,
        "amphora_migration_blocked": max(
            handoffs_blocked,
            int(missing_migration_authority),
        ),
        "capture_handoffs_created": handoffs_created,
        "capture_handoffs_committed": handoffs_committed,
        "recap_states_repaired": recaps_repaired,
        "document_worker_duplicates_removed": document_duplicates_removed,
        "document_worker_duplicates_remaining": document_duplicates_remaining,
    }
    after["before_reconciliation_gap"] = before["reconciliation_gap"]
    return after


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply additive reconciliation")
    parser.add_argument(
        "--amphora-manifest",
        type=Path,
        help="reviewed dry-run Amphora object inventory JSON",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="new backup root required for Amphora object migration",
    )
    args = parser.parse_args()
    manifest = None
    if args.amphora_manifest is not None:
        manifest = json.loads(args.amphora_manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("Amphora manifest must be a JSON object")
        if isinstance(manifest.get("amphora"), dict):
            nested = manifest["amphora"].get("legacy_provenance_inventory")
            if not isinstance(nested, dict):
                raise ValueError("audit report lacks an Amphora legacy inventory")
            manifest = nested
    result = (
        apply_repairs(
            amphora_manifest=manifest,
            backup_dir=args.backup_dir,
        )
        if args.apply
        else audit()
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if (not args.apply or result["reconciliation_gap"] == 0) else 2


if __name__ == "__main__":
    raise SystemExit(main())
