#!/usr/bin/env python3
"""Audit committed cognition episode EventBus and target-effect closure."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.cognition_episode_dispatch_audit import build_report  # noqa: E402,F401
from core.config import get_config  # noqa: E402
from core.wiki_projection_lifecycle import resolve_wiki_projection_db_path  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-dir", type=Path)
    parser.add_argument("--event-db-path", type=Path)
    parser.add_argument("--wiki-dir", type=Path)
    parser.add_argument("--cognitive-graph-db-path", type=Path)
    parser.add_argument("--wiki-projection-db-path", type=Path)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    config = get_config()
    database_dir = args.database_dir or Path(config.database_dir)
    event_db_path = args.event_db_path or Path(config.mnemos_dir) / "events.db"
    wiki_dir = args.wiki_dir or Path(config.wiki_dir)
    cognitive_graph_db_path = args.cognitive_graph_db_path or Path(
        getattr(config, "cognitive_graph_db_path", None) or database_dir / "cognitive_graph.db"
    )
    wiki_projection_db_path = args.wiki_projection_db_path or resolve_wiki_projection_db_path(
        config
    )
    report = build_report(
        database_dir=database_dir,
        event_db_path=event_db_path,
        wiki_dir=wiki_dir,
        cognitive_graph_db_path=cognitive_graph_db_path,
        wiki_projection_db_path=wiki_projection_db_path,
    )
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            indent=None if args.json else 2,
        )
    )
    return 1 if args.strict and not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
