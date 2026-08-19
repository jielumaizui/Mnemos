#!/usr/bin/env python3
"""Read-only audit for exact Persona challenge presentation and feedback lineage."""

from __future__ import annotations

import argparse
import ast
import inspect
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.persona.challenge_feedback import (  # noqa: E402
    _presented_delivery_binding,
    record_persona_challenge_feedback,
)
from core.persona import challenge_feedback  # noqa: E402
from integrations.apollon import _analyze_blindspot_feedback  # noqa: E402


def audit_persona_challenge_feedback(
    state_db: Path,
    *,
    asset_db: Path | None = None,
) -> dict[str, Any]:
    state_path = Path(state_db).expanduser().resolve(strict=False)
    asset_path = Path(
        asset_db or state_path.parent / "user_cognitive_blindspots.db"
    ).expanduser().resolve(strict=False)
    payload: dict[str, Any] = {
        "schema_version": "mnemos.persona_challenge_feedback_audit.v1",
        "read_only": True,
        "seeded_by_audit": False,
        "state_db": str(state_path),
        "asset_db": str(asset_path),
        "feedback_denominator": 0,
        "feedback_without_delivery_ref": 0,
        "keyword_inferred_feedback": _keyword_inference_sites(),
        "accepted_as_validated": 0,
        "swallowed_feedback_persistence_error": _swallowed_persistence_sites(),
        "errors": [],
        "ok": False,
    }
    reactions = _current_reactions(state_path)
    for reaction in reactions:
        if (
            reaction.get("source_channel") != "delivery_feedback"
            or reaction.get("subject_ref", {}).get("type") != "persona_challenge"
        ):
            continue
        payload["feedback_denominator"] += 1
        delivery_ref = reaction.get("delivery_ref")
        display_ref = reaction.get("display_ref")
        subject_id = str(reaction.get("subject_ref", {}).get("id") or "")
        missing = (
            not isinstance(delivery_ref, dict)
            or delivery_ref.get("state") != "available"
            or str(delivery_ref.get("event_id") or "") != subject_id
            or not isinstance(display_ref, dict)
            or display_ref.get("state") != "available"
            or not str(display_ref.get("display_id") or "")
        )
        if not missing:
            try:
                _presented_delivery_binding(
                    state_path,
                    delivery_id=subject_id,
                    presentation_receipt_hash=str(display_ref["display_id"]),
                )
            except (OSError, RuntimeError, ValueError):
                missing = True
        payload["feedback_without_delivery_ref"] += int(missing)
        source_event = str(reaction.get("source_event_ref", {}).get("event_id") or "")
        payload["keyword_inferred_feedback"] += int(
            not source_event.startswith("persona-challenge-feedback-")
        )
    for asset in _current_blindspots(asset_path):
        if (
            asset.get("status") == "confirmed"
            and asset.get("user_reaction") == "accepted"
            and (
                asset.get("challenge_outcome") != "validated"
                or not asset.get("challenge_delivery_id")
                or not asset.get("challenge_presentation_receipt_hash")
                or not asset.get("challenge_feedback_event_id")
                or not asset.get("challenge_reaction_revision_id")
            )
        ):
            payload["accepted_as_validated"] += 1
    if not payload["feedback_denominator"]:
        payload["errors"].append("feedback_denominator_zero")
    for key in (
        "feedback_without_delivery_ref",
        "keyword_inferred_feedback",
        "accepted_as_validated",
        "swallowed_feedback_persistence_error",
    ):
        if payload[key]:
            payload["errors"].append(f"{key}:{payload[key]}")
    payload["ok"] = not payload["errors"]
    return payload


def _current_reactions(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        try:
            rows = conn.execute(
                """
                SELECT revision.payload_json
                FROM cognitive_state_heads AS head
                JOIN cognitive_state_revisions AS revision
                  ON revision.revision_id=head.revision_id
                WHERE head.object_type='user_reaction_event'
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    result = []
    for row in rows:
        try:
            value = json.loads(str(row[0]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def _current_blindspots(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        try:
            rows = conn.execute(
                """
                SELECT revision.payload_json
                FROM user_cognitive_blindspot_heads AS head
                JOIN user_cognitive_blindspot_revisions AS revision
                  ON revision.revision_id=head.revision_id
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    result = []
    for row in rows:
        try:
            value = json.loads(str(row[0]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def _keyword_inference_sites() -> int:
    source = inspect.getsource(_analyze_blindspot_feedback)
    forbidden = (
        "你说得对",
        "我不同意",
        "record_reaction",
        "_save_profile",
        "BlindSpotProfileManager",
    )
    return sum(int(value in source) for value in forbidden)


def _swallowed_persistence_sites() -> int:
    count = 0
    for function in (
        record_persona_challenge_feedback,
        challenge_feedback._apply_asset_outcome,
        _analyze_blindspot_feedback,
    ):
        tree = ast.parse(inspect.getsource(function))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if not any(isinstance(child, ast.Raise) for child in ast.walk(node)):
                count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", default="")
    parser.add_argument("--asset-db", default="")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.state_db:
        state_db = Path(args.state_db)
    else:
        from core.config import get_config

        state_db = Path(get_config().database_dir) / "producer_consumer_ledger.db"
    report = audit_persona_challenge_feedback(
        state_db,
        asset_db=Path(args.asset_db) if args.asset_db else None,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(report)
    return 0 if report["ok"] or not args.strict else 1


if __name__ == "__main__":
    raise SystemExit(main())
