"""Sync command for Mnemos CLI."""

import json
import logging
import sqlite3
import time
from typing import List

from core.cli.helpers import _get_config, _get_sqlite_conn
from core.sync_framework.agent_source import parse_discovered_session
from core.ops.durable_io import read_native_bytes

# Constants extracted from magic numbers
RECENT_SECONDS = 3600

logger = logging.getLogger(__name__)


def cmd_sync(args):
    """同步层管理"""
    if args.sync_cmd == "status":
        try:
            import sqlite3

            db_path = _get_config().database_dir / "sync_log.db"
            if db_path.exists():
                with _get_sqlite_conn()(
                    str(db_path), timeout=10
                ) as conn:  # [P1-FIX] 使用 sqlite_conn 确保连接关闭
                    cursor = conn.execute("""
                        SELECT agent_name, COUNT(*), MAX(synced_at)
                        FROM sync_log
                        WHERE date(synced_at) >= date('now', '-7 days')
                        GROUP BY agent_name
                    """)
                    rows = cursor.fetchall()
                    if rows:
                        print("最近7天同步统计:")
                        for agent, count, last_sync in rows:
                            print(f"  {agent}: {count}条 | 最近: {last_sync}")
                    else:
                        print("最近7天无同步记录")
            else:
                print("同步数据库不存在")
        except (OSError, sqlite3.Error) as e:
            print(f"状态查询失败: {e}")

    elif args.sync_cmd == "retry-failed":
        try:
            from core.sync_framework.sync_engine import SyncEngine

            engine = SyncEngine()
            result = engine.retry_failed()
            print(f"重试完成: {result}")
        except (OSError, ValueError, AttributeError) as e:
            print(f"重试失败: {e}")

    elif args.sync_cmd == "backfill":
        _cmd_sync_backfill(args)

    elif args.sync_cmd == "audit":
        _cmd_sync_audit(args)

    else:
        print("用法: mnemos sync {status|retry-failed|backfill|audit}")


def _compress_ranges(numbers: List[int]) -> str:
    """将连续整数列表压缩为范围字符串，如 [1,2,3,5,7] -> '1-3,5,7'"""
    if not numbers:
        return ""
    numbers = sorted(numbers)
    ranges = []
    start = end = numbers[0]
    for n in numbers[1:]:
        if n == end + 1:
            end = n
        else:
            ranges.append(f"{start}-{end}" if start != end else str(start))
            start = end = n
    ranges.append(f"{start}-{end}" if start != end else str(start))
    return ",".join(ranges)


def _get_backfill_status(config) -> dict:
    """[P0-1] 读取历史回填状态文件"""
    state_path = config.data_dir / "backfill_state.json"
    try:
        return json.loads(  # type: ignore[no-any-return]
            read_native_bytes(state_path).decode("utf-8")
        )
    except (json.JSONDecodeError, OSError, TypeError, UnicodeError):
        return {}


def _write_backfill_status(config, status: str, stats: dict | None = None) -> None:
    """[P0-1] 写入历史回填状态文件"""
    state_path = config.data_dir / "backfill_state.json"
    data = {"status": status, "updated_at": time.time()}
    if stats:
        data["stats"] = stats
    try:
        state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        logger.warning("[sync] (OSError, ValueError) suppressed", exc_info=True)


class _BackfillParams:
    """历史回填参数聚合"""

    def __init__(self, args, config):
        self.source_filter = getattr(args, "source", None)
        self.since_hours = getattr(args, "since", 0) or 0
        self.max_turns = getattr(args, "max_turns", 0) or config.get(
            "sync.backfill_max_turns_per_session", 0
        )
        self.max_sessions = getattr(args, "max_sessions", 0) or 0
        self.dry_run = getattr(args, "dry_run", False)

    @property
    def is_full_history_scope(self) -> bool:
        """Only an unrestricted all-source write may claim global completion."""
        return (
            not self.dry_run
            and not self.since_hours
            and not self.max_turns
            and not self.max_sessions
            and (not self.source_filter or self.source_filter == "all")
        )


def _prepare_backfill_params(args, config) -> _BackfillParams:
    return _BackfillParams(args, config)


def _filter_sources(agents, source_filter):
    if source_filter and source_filter != "all":
        return [a for a in agents if a.name == source_filter]
    return agents


def _canonical_discovered_sessions(engine, sessions):
    """Deduplicate source discovery aliases through SyncEngine's canonical resolver."""
    by_id = {}
    for session_info in sessions:
        canonical = engine.canonicalize_session_info(session_info)
        canonical_id = canonical.session_id
        existing = by_id.get(canonical_id)
        if existing is None or (canonical.mtime or 0) > (existing.mtime or 0):
            by_id[canonical_id] = canonical
    return list(by_id.values())


def _init_total_stats() -> dict:
    return {
        "agents": 0,
        "sessions": 0,
        "turns": 0,
        "synced": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "noise": 0,
        "skipped_empty": 0,
        "missing_turns": 0,
        "skipped_complete": 0,
        "partial_sessions": 0,
    }


def _filter_sessions_by_mtime(sessions, recent_seconds: float, now: float):
    """按修改时间过滤并排序 sessions（最新的在前）。"""
    filtered = []
    for si in sessions:
        try:
            mtime = si.source_path.stat().st_mtime
        except OSError:
            mtime = 0
        if recent_seconds and (now - mtime) > recent_seconds:
            continue
        filtered.append((mtime, si))
    filtered.sort(key=lambda x: x[0], reverse=True)
    return filtered


def _compute_missing_turns(turns, existing_turns, max_turns: int):
    """Return one bounded work batch and the full unresolved session denominator."""
    all_numbers = {t.turn_number for t in turns}
    missing = sorted(all_numbers - set(existing_turns))
    selected_turns = turns
    if max_turns and len(turns) > max_turns:
        selected_turns = turns[-max_turns:]
    missing_set = set(missing)
    turns_to_sync = [turn for turn in selected_turns if turn.turn_number in missing_set]
    return turns_to_sync, missing


def _update_total_stats_from_results(total_stats: dict, results, agent_synced_ref: list) -> None:
    for r in results:
        if r.action == "new":
            total_stats["synced"] += 1
            agent_synced_ref[0] += 1
        elif r.action == "updated":
            total_stats["updated"] += 1
            agent_synced_ref[0] += 1
        elif r.action in ("skipped", "skipped_l1"):
            total_stats["skipped"] += 1
        elif r.action == "noise":
            total_stats["noise"] += 1
        elif r.action == "failed":
            total_stats["failed"] += 1


def _sync_session_turns(
    source,
    session_info,
    turns_to_sync,
    engine,
    duplicate_cache,
):
    """同步单个 session 的缺洞 turns，返回 SyncResult 列表。"""
    results = []
    for idx, turn in enumerate(turns_to_sync, 1):
        result = engine.sync_single_turn(
            source,
            session_info,
            turn,
            incremental=False,
            check_backend_duplicate=duplicate_cache is None,
            backend_duplicate_cache=duplicate_cache,
        )
        results.append(result)
        if len(turns_to_sync) >= 50 and (idx % 50 == 0 or idx == len(turns_to_sync)):
            print(
                f"    progress {idx}/{len(turns_to_sync)} "
                f"(turn={turn.turn_number}, action={result.action})",
                flush=True,
            )
    return results


def _enqueue_distillation(engine, source, session_info, complete_turns) -> bool:
    try:
        bound_turns = engine.bind_session_raw_identities(
            source,
            session_info,
            complete_turns,
        )
        engine.enqueue_session_for_distillation(source, session_info, bound_turns)
        return True
    except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error) as e:
        logger.warning(
            "[sync backfill] 蒸馏入队失败 %s/%s: %s",
            source.name,
            session_info.session_id,
            e,
            exc_info=True,
        )
        return False


def _process_single_session(
    source,
    session_info,
    engine,
    params: _BackfillParams,
    total_stats: dict,
    duplicate_cache_ref: list,
) -> tuple[int, int, int]:
    """处理单个 session，返回 (parsed_turns, missing_turns, synced_count)。"""
    try:
        # Discovery aliases must resolve before every existing-turn lookup,
        # sync-log write, Raw write, and eventual distillation handoff.
        session_info = engine.canonicalize_session_info(session_info)
        turns = parse_discovered_session(source, session_info)
        if not turns:
            total_stats["skipped_empty"] += 1
            return 0, 0, 0

        turns = sorted(turns, key=lambda t: t.turn_number)
        existing_turns = engine.get_synced_turns_for_session(source.name, session_info)
        turns_to_sync, missing_turns = _compute_missing_turns(
            turns, existing_turns, params.max_turns
        )

        parsed_turns = len(turns)
        missing_count = len(missing_turns)
        total_stats["sessions"] += 1
        total_stats["turns"] += len(turns_to_sync if not params.dry_run else turns)
        if missing_turns:
            total_stats["missing_turns"] += len(missing_turns)

        if params.dry_run:
            if missing_turns:
                ranges = _compress_ranges(missing_turns)
                print(
                    f"  [dry-run] {source.name}/{session_info.session_id}: "
                    f"{len(turns)} turns, missing {len(missing_turns)} ({ranges})"
                )
            return parsed_turns, missing_count, 0

        if not turns_to_sync:
            if missing_turns:
                # A caller-limited batch can contain no currently selectable
                # gap while the canonical session still has older unresolved
                # turns.  That is partial, never complete.
                total_stats["partial_sessions"] += 1
                return parsed_turns, missing_count, 0
            # Sync-log completeness alone is not terminal.  A previous run may
            # have crashed after committing Raw/sync-log but before obtaining
            # the durable complete-session handoff.  Rebind Raw identities and
            # retry the idempotent handoff before publishing ``done``.
            if _enqueue_distillation(engine, source, session_info, turns):
                total_stats["skipped_complete"] += 1
            else:
                total_stats["failed"] += 1
                total_stats["partial_sessions"] += 1
            return parsed_turns, missing_count, 0

        if duplicate_cache_ref[0] is None and len(duplicate_cache_ref) == 1:
            duplicate_cache_ref[0] = engine.build_backend_duplicate_cache(source.name)

        ranges = _compress_ranges(missing_turns)
        print(
            f"  {source.name}/{session_info.session_id}: "
            f"sync missing {len(turns_to_sync)}/{len(turns)} turns ({ranges})",
            flush=True,
        )

        results = _sync_session_turns(source, session_info, turns_to_sync, engine, duplicate_cache_ref[0])
        agent_synced = [0]
        _update_total_stats_from_results(total_stats, results, agent_synced)
        if len(turns_to_sync) == len(missing_turns) and not any(
            result.action == "failed" for result in results
        ):
            # A handoff represents the whole canonical session, never one
            # caller-limited tail batch.
            if not _enqueue_distillation(engine, source, session_info, turns):
                total_stats["failed"] += 1
                total_stats["partial_sessions"] += 1
        elif missing_turns:
            total_stats["partial_sessions"] += 1
        return parsed_turns, missing_count, agent_synced[0]
    except (OSError, ValueError, AttributeError) as e:
        total_stats["failed"] += 1
        print(f"  ✗ {source.name}/{session_info.session_id}: {e}")
        return 0, 0, 0


def _backfill_single_source(
    source,
    engine,
    params: _BackfillParams,
    now: float,
    total_stats: dict,
) -> dict:
    """回填单个 Agent source，返回该 source 的统计。"""
    sessions = _canonical_discovered_sessions(engine, source.discover_sessions())
    if not sessions:
        return {"scanned": 0, "parsed": 0, "missing": 0, "synced": 0}

    total_stats["agents"] += 1
    recent_seconds = params.since_hours * RECENT_SECONDS if params.since_hours else 0
    sessions_with_mtime = _filter_sessions_by_mtime(sessions, recent_seconds, now)
    if params.max_sessions:
        sessions_with_mtime = sessions_with_mtime[:params.max_sessions]

    agent_parsed_turns = 0
    agent_missing_turns = 0
    agent_synced = 0
    duplicate_cache_ref: list = [None]

    for _mtime, session_info in sessions_with_mtime:
        parsed, missing, synced = _process_single_session(
            source, session_info, engine, params, total_stats, duplicate_cache_ref
        )
        agent_parsed_turns += parsed
        agent_missing_turns += missing
        agent_synced += synced

    print(
        f"  {source.name}: 扫描 {len(sessions_with_mtime)} sessions, "
        f"解析 {agent_parsed_turns} turns, 待补 {agent_missing_turns} turns, 同步 {agent_synced}"
    )
    return {
        "scanned": len(sessions_with_mtime),
        "parsed": agent_parsed_turns,
        "missing": agent_missing_turns,
        "synced": agent_synced,
    }


def _print_total_stats(total_stats: dict) -> None:
    print()
    print("回填统计:")
    print(f"  Agent 源: {total_stats['agents']}")
    print(f"  Sessions: {total_stats['sessions']}")
    print(f"  Turns: {total_stats['turns']}")
    if not total_stats.get("dry_run"):
        print(f"  Synced(new): {total_stats['synced']}")
        print(f"  Updated: {total_stats['updated']}")
        print(f"  Skipped: {total_stats['skipped']}")
        print(f"  Noise: {total_stats['noise']}")
        print(f"  Failed: {total_stats['failed']}")


def _cmd_sync_backfill(args):
    """历史回填：全量/大批量扫描 Agent 历史会话 — P0-4 直接调用 SyncEngine，绕过 CaptureQueue"""
    from core.sync_framework.sync_engine import SyncEngine
    from core.sync_framework.registry import SourceRegistry

    config = _get_config()
    params = _prepare_backfill_params(args, config)

    SourceRegistry.register_builtin_agents()
    agents = _filter_sources(SourceRegistry.auto_discover(), params.source_filter)
    if not agents:
        print("未发现任何 Agent 源")
        return

    print(f"历史回填: 发现 {len(agents)} 个 Agent 源")
    if params.since_hours:
        print(f"  时间范围: 最近 {params.since_hours} 小时")
    else:
        print("  时间范围: 全部历史")
    print(f"  每 session 最大 turn 数: {params.max_turns if params.max_turns else '无限制'}")
    print(f"  每 source 最大 session 数: {params.max_sessions if params.max_sessions else '无限制'}")
    if params.dry_run:
        print("  [dry-run] 只统计，不入库")
    print()

    _write_backfill_status(
        config,
        "running",
        {
            "agents": len(agents),
            "dry_run": params.dry_run,
            "full_history_scope": params.is_full_history_scope,
        },
    )

    engine = SyncEngine()
    total_stats = _init_total_stats()
    now = time.time()

    for source in agents:
        _backfill_single_source(source, engine, params, now, total_stats)

    engine.close()
    total_stats["dry_run"] = params.dry_run
    total_stats["full_history_scope"] = params.is_full_history_scope
    if params.dry_run:
        completion_status = "dry_run"
    elif not params.is_full_history_scope:
        completion_status = "partial"
    elif total_stats["failed"]:
        completion_status = "failed"
    else:
        completion_status = "done"
    _write_backfill_status(config, completion_status, total_stats)
    _print_total_stats(total_stats)
    print(f"  Missing turns: {total_stats['missing_turns']}")
    print(f"  Skipped(complete): {total_stats['skipped_complete']}")
    print(f"  Skipped(empty): {total_stats['skipped_empty']}")
    if completion_status == "partial":
        print("  范围受限：本次只完成选定批次，不能声明历史回填完成。")


def _cmd_sync_audit(args):
    """同步完整性审计：扫描各 Agent 的 session 缺洞情况"""
    from core.sync_framework.sync_engine import SyncEngine
    from core.sync_framework.registry import SourceRegistry

    source_filter = getattr(args, "source", None)
    config = _get_config()
    config.database_dir / "sync_log.db"

    SourceRegistry.register_builtin_agents()
    agents = SourceRegistry.auto_discover()
    if source_filter and source_filter != "all":
        agents = [a for a in agents if a.name == source_filter]
    if not agents:
        print("未发现任何 Agent 源")
        return

    engine = SyncEngine()
    total_sessions = 0
    sessions_with_gaps = 0
    largest_gap = {
        "session_id": "",
        "parsed_turns": 0,
        "synced_turns": 0,
        "missing_turns": 0,
        "missing_ranges": "",
    }

    for source in agents:
        sessions = _canonical_discovered_sessions(engine, source.discover_sessions())
        if not sessions:
            continue
        agent_sessions = 0
        agent_gaps = 0
        for session_info in sessions:
            try:
                session_info = engine.canonicalize_session_info(session_info)
                turns = parse_discovered_session(source, session_info)
                if not turns:
                    continue
                all_turn_numbers = sorted({t.turn_number for t in turns})
                synced_turns = engine.get_synced_turns_for_session(source.name, session_info)
                missing = sorted(set(all_turn_numbers) - set(synced_turns))
                total_sessions += 1
                agent_sessions += 1
                if missing:
                    sessions_with_gaps += 1
                    agent_gaps += 1
                    if len(missing) > largest_gap["missing_turns"]:
                        largest_gap = {
                            "session_id": session_info.session_id,
                            "parsed_turns": len(all_turn_numbers),
                            "synced_turns": len(synced_turns),
                            "missing_turns": len(missing),
                            "missing_ranges": _compress_ranges(missing),
                        }
            except (OSError, ValueError, AttributeError) as e:
                print(f"  ✗ {source.name}/{session_info.session_id}: {e}")

        print(f"{source.name}: {agent_sessions} sessions, {agent_gaps} with gaps")

    engine.close()
    print()
    print("同步完整性审计结果:")
    print(f"  总 sessions: {total_sessions}")
    print(f"  有缺洞的 sessions: {sessions_with_gaps}")
    if largest_gap["missing_turns"] > 0:
        print("  最大缺洞:")
        print(f"    session_id: {largest_gap['session_id']}")
        print(f"    parsed_turns: {largest_gap['parsed_turns']}")
        print(f"    synced_turns: {largest_gap['synced_turns']}")
        print(f"    missing_turns: {largest_gap['missing_turns']}")
        print(f"    missing_ranges: {largest_gap['missing_ranges']}")
