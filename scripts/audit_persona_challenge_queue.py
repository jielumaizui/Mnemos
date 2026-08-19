#!/usr/bin/env python3
"""Read-only/runtime audit for the DecisionTrace-backed Persona challenge queue."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.persona.challenge_queue import (  # noqa: E402
    PERSONA_CHALLENGE_COMMAND,
    PERSONA_CHALLENGE_CONSUMER,
    PersonaChallengeQueueConsumer,
)
from core.cognitive.state_contract import sha256_json  # noqa: E402


class _ProbeConfig(SimpleNamespace):
    def get(self, _key: str, default: Any = None) -> Any:
        return default


def _empty_tick_business_writes() -> int:
    with tempfile.TemporaryDirectory(prefix="mnemos-persona-challenge-empty-") as tmp:
        root = Path(tmp)
        config = _ProbeConfig(database_dir=root, data_dir=root, mnemos_dir=root)
        before = tuple(root.rglob("*"))
        results = [PersonaChallengeQueueConsumer(config).run_once() for _ in range(24)]
        after = tuple(root.rglob("*"))
    if not all(
        result.get("status") == "noop"
        and result.get("reason") == "no_pending_decision_command"
        for result in results
    ):
        return 1
    return int(before != after)


def audit_persona_challenge_queue(db_path: Path) -> dict[str, Any]:
    resolved = db_path.expanduser().resolve(strict=False)
    payload: dict[str, Any] = {
        "schema_version": "mnemos.persona_challenge_queue_audit.v2",
        "db_path": str(resolved),
        "read_only": True,
        "seeded_by_audit": False,
        "empty_tick_business_writes": _empty_tick_business_writes(),
        "eligible_decision_command_consumed": 0,
        "challenge_command_replay_duplicates": 0,
        "challenge_without_decision_trace": 0,
        "challenge_command_count": 0,
        "pending_challenge_command_count": 0,
        "delivery_command_count": 0,
        "presented_challenge_without_canonical_revision": 0,
        "presented_challenge_from_shadow_only": 0,
        "stale_or_revoked_challenge": 0,
        "errors": [],
        "ok": False,
    }
    if not resolved.is_file():
        payload["errors"].append("cognitive_state_store_uninitialized")
        return payload
    with sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=30) as conn:
        required = {
            "cognitive_state_outbox",
            "cognitive_state_revisions",
            "cognitive_state_effect_receipts",
        }
        available = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = sorted(required - available)
        if missing:
            payload["errors"].append("missing_tables:" + ",".join(missing))
            return payload
        params = (PERSONA_CHALLENGE_CONSUMER, PERSONA_CHALLENGE_COMMAND)
        payload["challenge_command_count"] = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM cognitive_state_outbox
                WHERE consumer_id=? AND command_type=?
                """,
                params,
            ).fetchone()[0]
        )
        payload["eligible_decision_command_consumed"] = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM cognitive_state_outbox AS command
                JOIN cognitive_state_effect_receipts AS receipt
                  ON receipt.command_id=command.command_id
                WHERE command.consumer_id=?
                  AND command.command_type=?
                  AND receipt.status='committed'
                """,
                params,
            ).fetchone()[0]
        )
        payload["pending_challenge_command_count"] = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM cognitive_state_outbox AS command
                LEFT JOIN cognitive_state_effect_receipts AS receipt
                  ON receipt.command_id=command.command_id
                WHERE command.consumer_id=?
                  AND command.command_type=?
                  AND receipt.command_id IS NULL
                """,
                params,
            ).fetchone()[0]
        )
        payload["challenge_command_replay_duplicates"] = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT revision_id, consumer_id, command_type
                    FROM cognitive_state_outbox
                    WHERE consumer_id=? AND command_type=?
                    GROUP BY revision_id, consumer_id, command_type
                    HAVING COUNT(*) > 1
                )
                """,
                params,
            ).fetchone()[0]
        )
        payload["challenge_without_decision_trace"] = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM cognitive_state_outbox AS command
                LEFT JOIN cognitive_state_revisions AS revision
                  ON revision.revision_id=command.revision_id
                 AND revision.object_type='decision_trace'
                WHERE command.consumer_id=?
                  AND command.command_type=?
                  AND revision.revision_id IS NULL
                """,
                params,
            ).fetchone()[0]
        )
        delivery_rows = conn.execute(
            """
            SELECT command.command_id, command.revision_id, command.payload_hash,
                   command.created_at, revision.payload_hash AS decision_hash,
                   consumption.outcome
            FROM cognitive_state_outbox AS command
            JOIN cognitive_state_revisions AS revision
              ON revision.revision_id=command.revision_id
            JOIN cognitive_state_effect_receipts AS receipt
              ON receipt.command_id=command.command_id
            JOIN cognitive_data_consumptions AS consumption
              ON consumption.consumption_id=receipt.consumption_id
            WHERE command.consumer_id=?
              AND command.command_type=?
              AND receipt.status='committed'
            ORDER BY command.command_id
            """,
            params,
        ).fetchall()
    asset_db = resolved.parent / "user_cognitive_blindspots.db"
    asset_conn = None
    if asset_db.is_file():
        asset_conn = sqlite3.connect(f"file:{asset_db}?mode=ro", uri=True, timeout=30)
        asset_conn.row_factory = sqlite3.Row
        asset_conn.execute("PRAGMA query_only=ON")
    try:
        for row in delivery_rows:
            try:
                outcome = json.loads(str(row[5] or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            deliveries = outcome.get("delivery_commands")
            if not isinstance(deliveries, list):
                continue
            for delivery in deliveries:
                payload["delivery_command_count"] += 1
                missing_canonical = False
                shadow_only = False
                stale = False
                if not isinstance(delivery, dict):
                    payload["presented_challenge_without_canonical_revision"] += 1
                    continue
                asset_ref = delivery.get("asset_revision")
                decision_ref = delivery.get("decision_trace")
                challenge_ref = delivery.get("challenge")
                if (
                    not isinstance(asset_ref, dict)
                    or not asset_ref.get("asset_id")
                    or not asset_ref.get("revision_id")
                    or not asset_ref.get("content_hash")
                ):
                    missing_canonical = True
                if (
                    not isinstance(decision_ref, dict)
                    or str(decision_ref.get("revision_id") or "") != str(row[1])
                    or str(decision_ref.get("content_hash") or "") != str(row[4])
                ):
                    stale = True
                if (
                    not isinstance(challenge_ref, dict)
                    or not challenge_ref.get("content")
                    or not challenge_ref.get("content_hash")
                ):
                    stale = True
                if isinstance(asset_ref, dict) and asset_ref.get("source_kind") != (
                    "canonical_admitted_blindspot"
                ):
                    shadow_only = True
                current = None
                if not missing_canonical and asset_conn is not None:
                    try:
                        current = asset_conn.execute(
                            """
                            SELECT revision.*
                            FROM user_cognitive_blindspot_heads AS head
                            JOIN user_cognitive_blindspot_revisions AS revision
                              ON revision.revision_id=head.revision_id
                            WHERE head.asset_id=?
                            """,
                            (str(asset_ref["asset_id"]),),
                        ).fetchone()
                    except sqlite3.OperationalError:
                        current = None
                if not missing_canonical and current is None:
                    missing_canonical = True
                if current is not None:
                    try:
                        asset = json.loads(str(current["payload_json"]))
                        expected_asset_hash = sha256_json(asset)
                        expected_content_hash = sha256_json(
                            {
                                "asset_id": asset["asset_id"],
                                "asset_revision_id": asset["revision_id"],
                                "type": asset["type"],
                                "content": asset["description"],
                                "impact": asset["impact"],
                            }
                        )
                        expired = datetime.fromisoformat(
                            str(current["expires_at"]).replace("Z", "+00:00")
                        ) <= datetime.fromisoformat(str(row[3]).replace("Z", "+00:00"))
                        stale = stale or any(
                            (
                                str(current["revision_id"]) != str(asset_ref["revision_id"]),
                                str(asset_ref["content_hash"]) != expected_asset_hash,
                                str(current["status"]) not in {"suspected", "confirmed"},
                                expired,
                                str(challenge_ref.get("content") or "")
                                != str(asset["description"]),
                                str(challenge_ref.get("content_hash") or "")
                                != expected_content_hash,
                            )
                        )
                    except (KeyError, TypeError, ValueError):
                        stale = True
                payload["presented_challenge_without_canonical_revision"] += int(
                    missing_canonical
                )
                payload["presented_challenge_from_shadow_only"] += int(shadow_only)
                payload["stale_or_revoked_challenge"] += int(stale)
    finally:
        if asset_conn is not None:
            asset_conn.close()
    if payload["empty_tick_business_writes"]:
        payload["errors"].append("empty_tick_business_writes")
    if not payload["challenge_command_count"]:
        payload["errors"].append("production_challenge_command_denominator_zero")
    if not payload["eligible_decision_command_consumed"]:
        payload["errors"].append("eligible_decision_command_consumed_zero")
    for key in (
        "challenge_command_replay_duplicates",
        "challenge_without_decision_trace",
        "presented_challenge_without_canonical_revision",
        "presented_challenge_from_shadow_only",
        "stale_or_revoked_challenge",
    ):
        if payload[key]:
            payload["errors"].append(f"{key}:{payload[key]}")
    payload["ok"] = not payload["errors"]
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.db_path:
        db_path = Path(args.db_path)
    else:
        from core.config import get_config

        db_path = Path(get_config().database_dir) / "producer_consumer_ledger.db"
    payload = audit_persona_challenge_queue(db_path)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(payload)
    return 0 if payload["ok"] or not args.strict else 1


if __name__ == "__main__":
    raise SystemExit(main())
