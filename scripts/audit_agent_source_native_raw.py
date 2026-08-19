#!/usr/bin/env python3
"""Independently compare all host-native logical events with canonical Raw."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agent_kit.native_raw_challenger import audit_native_to_raw
from core.agent_kit.source_support_manifest import get_agent_source_support_manifest
from core.config import get_config
from core.sync_framework.registry import SourceRegistry


def _default_raw_db_path() -> Path:
    config = get_config()
    configured = config.get("raw_event_store.db_path")
    return Path(configured or (config.database_dir / "raw_events.db")).expanduser()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=_default_raw_db_path())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    SourceRegistry.register_builtin_agents()
    manifest = get_agent_source_support_manifest()
    # The challenger is a host Agent cognitive-chain gate.  The registry also
    # correctly contains manifest-declared ingestion-only parsers; excluding
    # them here prevents a host-only auditor from reclassifying them as
    # undeclared failures.
    host_sources = [
        source
        for source in SourceRegistry.auto_discover()
        if manifest.require_active_source(source.name).is_host_agent
    ]
    report = audit_native_to_raw(
        host_sources,
        raw_db_path=Path(args.db_path),
        manifest=manifest,
    )
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)
    print(rendered if args.json else json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
