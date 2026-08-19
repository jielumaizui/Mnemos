#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Replay knowledge delivery routing decisions without touching live data by default."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.delivery_router import (  # noqa: E402
    KnowledgeDeliveryRouter,
    SCHEMA_VERSION,
)

REPLAY_SCHEMA_VERSION = "mnemos.delivery_replay.v1"

SAMPLE_CANDIDATES = [
    {
        "source": "predictive_push",
        "subject": "delivery-router-smoke",
        "channel": "predictive_push",
        "target": "README.md",
        "evidence_refs": ["README.md"],
        "task_fit_score": 0.8,
        "requested_level": "hint",
        "task_key": "replay",
        "cooldown_key": "delivery-router-smoke",
        "metadata": {"sample": True},
    }
]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    candidates = _load_candidates(args.input)

    if args.apply:
        db_path = Path(args.db_path).expanduser() if args.db_path else None
        database_dir = db_path.parent if db_path else None
        result = _run_replay(candidates, db_path=db_path, database_dir=database_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="mnemos-delivery-replay-") as tmp:
            tmp_path = Path(tmp)
            result = _run_replay(
                candidates,
                db_path=tmp_path / "delivery_events.db",
                database_dir=tmp_path,
            )
            result["dry_run"] = True

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        _print_human(result)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay Mnemos delivery decisions against JSON/JSONL candidates."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="JSON list, JSON object with candidates, or JSONL candidate file.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write replay events to the configured delivery DB instead of a temporary DB.",
    )
    parser.add_argument(
        "--db-path",
        default="",
        help="Delivery DB path used only with --apply.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser.parse_args(argv)


def _load_candidates(path: Path | None) -> list[Mapping[str, Any]]:
    if path is None:
        return list(SAMPLE_CANDIDATES)
    text = path.expanduser().read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        return [
            _ensure_mapping(json.loads(line))
            for line in text.splitlines()
            if line.strip()
        ]

    payload = json.loads(text)
    if isinstance(payload, dict) and "candidates" in payload:
        payload = payload["candidates"]
    if not isinstance(payload, list):
        raise ValueError("--input must contain a JSON list or {'candidates': [...]}")
    return [_ensure_mapping(item) for item in payload]


def _ensure_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("delivery replay candidates must be JSON objects")
    return value


def _run_replay(
    candidates: list[Mapping[str, Any]],
    *,
    db_path: Path | None,
    database_dir: Path | None,
) -> dict[str, Any]:
    router = KnowledgeDeliveryRouter(db_path=db_path, database_dir=database_dir)
    result = router.replay_candidates(candidates)
    result["schema_version"] = REPLAY_SCHEMA_VERSION
    result["delivery_schema_version"] = SCHEMA_VERSION
    result["dry_run"] = False
    return result


def _print_human(result: Mapping[str, Any]) -> None:
    print(f"schema_version: {result['schema_version']}")
    print(f"dry_run: {result.get('dry_run', False)}")
    print(f"count: {result.get('count', 0)}")
    print(f"counters: {json.dumps(result.get('counters', {}), ensure_ascii=False)}")
    for decision in result.get("decisions", []):
        print(
            "- {subject}: {decision}/{level} ({reason})".format(
                subject=decision.get("subject", ""),
                decision=decision.get("decision", ""),
                level=decision.get("delivered_level", ""),
                reason=decision.get("reason", ""),
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())
