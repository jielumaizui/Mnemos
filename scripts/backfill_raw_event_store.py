#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill canonical raw_events.db from native agent transcripts.

This does not write Obsidian raw markdown and does not call LLM/embedding APIs.
It only reads AgentSource parsers and upserts turns into RawEventStore.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.agent_kit.source_support_manifest import (
    AgentSourceSupportManifestError,
    build_native_source_snapshot,
    get_agent_source_support_manifest,
    build_source_support_runtime_report,
)
from core.sync_framework.agent_source import (
    AgentSource,
    SessionInfo,
    Turn,
    canonicalize_session_info,
    parse_discovered_session,
)
from core.sync_framework.raw_event_store import RawEventStore
from core.sync_framework.registry import PathDiscover, SourceRegistry
from core.sync_framework.source_support import (
    SOURCE_CAPABILITY_RECOVERABLE_ERRORS,
    build_native_raw_metadata,
)

logger = logging.getLogger(__name__)


def _filter_sources(agents: Iterable[AgentSource], source_filter: str) -> list[AgentSource]:
    if source_filter and source_filter != "all":
        wanted = {item.strip() for item in source_filter.split(",") if item.strip()}
        return [agent for agent in agents if agent.name in wanted]
    return list(agents)


def _session_mtime(session: SessionInfo) -> float:
    if session.mtime is not None:
        return float(session.mtime)
    try:
        return session.source_path.stat().st_mtime
    except OSError:
        return 0.0


def _filter_sessions(
    sessions: list[SessionInfo],
    *,
    since_hours: int,
    max_sessions: int,
) -> list[SessionInfo]:
    now = time.time()
    recent_seconds = since_hours * 3600 if since_hours else 0
    canonical_sessions: dict[str, SessionInfo] = {}
    for session in sessions:
        canonical_id = canonicalize_session_info(session).session_id
        existing = canonical_sessions.get(canonical_id)
        if existing is None or _session_mtime(session) > _session_mtime(existing):
            canonical_sessions[canonical_id] = session
    ranked = sorted(
        ((_session_mtime(session), session) for session in canonical_sessions.values()),
        key=lambda item: item[0],
        reverse=True,
    )
    selected = [
        session
        for mtime, session in ranked
        if not recent_seconds or now - mtime <= recent_seconds
    ]
    if max_sessions:
        selected = selected[:max_sessions]
    return selected


def _source_metadata(
    source: AgentSource,
    session: SessionInfo,
    turn: Turn,
) -> Dict[str, Any]:
    """Build native Raw metadata through the shared fail-closed contract."""
    return build_native_raw_metadata(source, session, turn)


def _resolved_source_roots(source: AgentSource) -> list[Path]:
    """Capture observed roots as evidence, never as a second source definition."""
    roots: list[Path] = []
    try:
        observed_roots = getattr(source, "observed_roots", None)
        if callable(observed_roots):
            roots.extend(Path(root) for root in observed_roots())
        data_dir = source.data_dir
    except AgentSourceSupportManifestError:
        raise
    except SOURCE_CAPABILITY_RECOVERABLE_ERRORS:
        data_dir = None
    if not roots and data_dir is not None:
        roots.append(Path(data_dir))
    if not roots:
        try:
            discovered = PathDiscover.find(source.name)
        except AgentSourceSupportManifestError:
            raise
        except SOURCE_CAPABILITY_RECOVERABLE_ERRORS:
            discovered = None
        if discovered is not None:
            roots.append(Path(discovered))
    return list(dict.fromkeys(roots))


def _source_files(session: SessionInfo, turn: Turn) -> list[str]:
    files = [str(path) for path in (turn.source_files or [])]
    if not files and session.source_path:
        files.append(str(session.source_path))
    return files


def _backfill_turn(
    store: RawEventStore,
    source: AgentSource,
    session: SessionInfo,
    turn: Turn,
) -> str:
    files = _source_files(session, turn)
    return store.upsert_turn(
        source_agent=source.name,
        session_id=canonicalize_session_info(session).session_id,
        turn_number=turn.turn_number,
        user_content=turn.user_content,
        assistant_content=turn.assistant_content,
        model_tag=source.model_tag,
        timestamp=turn.timestamp,
        metadata=_source_metadata(source, session, turn),
        tool_calls=turn.tool_calls,
        tool_results=turn.tool_results,
        reasoning=turn.reasoning,
        attachments=turn.attachments,
        raw_event_refs=turn.raw_event_refs,
        source_files=files,
        source_path=str(session.source_path) if session.source_path else None,
        completeness=dict(turn.completeness or {}),
        full_content_hash=(turn.metadata or {}).get("full_content_hash"),
        origin="sync_engine",
    )


def _backfill_source(
    source: AgentSource,
    store: RawEventStore,
    *,
    since_hours: int,
    max_sessions: int,
    max_turns_per_session: int,
    dry_run: bool,
) -> Dict[str, Any]:
    stats = {
        "sessions": 0,
        "turns": 0,
        "written": 0,
        "empty": 0,
        "failed": 0,
    }
    sessions = _filter_sessions(
        source.discover_sessions(),
        since_hours=since_hours,
        max_sessions=max_sessions,
    )
    for session in sessions:
        stats["sessions"] += 1
        try:
            turns = sorted(
                parse_discovered_session(source, session),
                key=lambda item: item.turn_number,
            )
            if max_turns_per_session and len(turns) > max_turns_per_session:
                turns = turns[-max_turns_per_session:]
            if not turns:
                stats["empty"] += 1
                continue
            stats["turns"] += len(turns)
            if dry_run:
                continue
            for turn in turns:
                _backfill_turn(store, source, session, turn)
                stats["written"] += 1
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            stats["failed"] += 1
            logger.warning(
                "backfill failed for %s/%s: %s",
                source.name,
                session.session_id,
                exc,
                exc_info=True,
            )
    return stats


def run_backfill(args: argparse.Namespace, *, emit_progress: bool = True) -> Dict[str, Any]:
    """Run one backfill and return a standalone runtime-evidence report."""
    SourceRegistry.register_builtin_agents()
    agents = _filter_sources(SourceRegistry.auto_discover(), args.source)
    store = RawEventStore(db_path=Path(args.db_path).expanduser() if args.db_path else None)
    manifest = get_agent_source_support_manifest()
    summary: Dict[str, Any] = {
        "dry_run": args.dry_run,
        "support_manifest_hash": manifest.manifest_hash,
        "agents": {},
        "unmanifested_sources": [],
    }
    try:
        for source in agents:
            try:
                manifest.require_active_source(source.name)
            except AgentSourceSupportManifestError:
                summary["unmanifested_sources"].append(source.name)
                summary["agents"][source.name] = {
                    "sessions": 0,
                    "turns": 0,
                    "written": 0,
                    "empty": 0,
                    "failed": 1,
                    "native_source_snapshot": None,
                }
                logger.error("backfill rejected undeclared native source %s", source.name)
                continue
            stats = _backfill_source(
                source,
                store,
                since_hours=args.since_hours,
                max_sessions=args.max_sessions,
                max_turns_per_session=args.max_turns_per_session,
                dry_run=args.dry_run,
            )
            snapshot = build_native_source_snapshot(
                source.name,
                resolved_roots=_resolved_source_roots(source),
                cursor={
                    "kind": "backfill",
                    "since_hours": args.since_hours,
                    "max_sessions": args.max_sessions,
                    "max_turns_per_session": args.max_turns_per_session,
                },
                native_denominator={
                    "sessions": int(stats["sessions"]),
                    "turns": int(stats["turns"]),
                },
                manifest=manifest,
            )
            summary["agents"][source.name] = {
                **stats,
                "native_source_snapshot": snapshot.to_dict(),
            }
            if emit_progress:
                print(
                    f"{source.name}: sessions={stats['sessions']} turns={stats['turns']} "
                    f"written={stats['written']} empty={stats['empty']} failed={stats['failed']}",
                    flush=True,
                )
    finally:
        store.close()
    return build_source_support_runtime_report(
        summary,
        producer="scripts.backfill_raw_event_store",
        manifest=manifest,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default="all",
        help="Agent source filter: all or comma-separated names, e.g. kimi,claude",
    )
    parser.add_argument("--since-hours", type=int, default=0, help="Only sessions touched recently")
    parser.add_argument("--max-sessions", type=int, default=0, help="Limit sessions per source")
    parser.add_argument(
        "--max-turns-per-session",
        type=int,
        default=0,
        help="Limit to the latest N turns per session",
    )
    parser.add_argument("--db-path", default="", help="Override raw_events.db path")
    parser.add_argument("--dry-run", action="store_true", help="Parse and count without writing")
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary")
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    parser = build_parser()
    args = parser.parse_args()
    summary = run_backfill(args, emit_progress=not args.json)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
