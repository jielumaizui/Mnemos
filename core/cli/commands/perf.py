"""Perf command for Mnemos CLI."""

import json
import logging

from core.cli.helpers import _get_config
from core.cli.helpers import _daemon_processes, _format_bytes, _get_sqlite_conn
from core.db_utils import sqlite_artifact_exists
from core.db_utils import sqlite_artifact_size

logger = logging.getLogger(__name__)


def _print_perf_header(config) -> None:
    """打印性能配置摘要。"""
    print("Mnemos 性能状态")
    print("=" * 40)
    print(f"性能档位:      {config.get('performance_tier', 'default')}")
    print(
        "Raw tail/源:   "
        f"{config.get('sync.raw_sync_sessions_per_source', 10)} sessions"
    )
    print(
        "Raw reconcile/源: "
        f"{config.get('sync.raw_sync_sessions_per_source', 10)} sessions"
    )
    print(
        "Raw 每批 turns: "
        f"{config.get('sync.raw_sync_turns_per_session', 100)}"
    )
    print(f"Capture workers: {config.get('capture.max_workers', 4)}")
    print()


def _print_daemon_processes() -> None:
    """打印 daemon 进程及其资源占用。"""
    import subprocess

    print("daemon 进程:")
    processes = _daemon_processes()
    if not processes:
        print("  未检测到运行中的 daemon")
        print()
        return

    for line in processes:
        pid = line.split()[0]
        if pid.isdigit():
            try:
                ps = subprocess.run(
                    ["ps", "-p", pid, "-o", "pid=,pcpu=,pmem=,rss=,etime="],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if ps.returncode == 0:
                    print(f"  {ps.stdout.strip()}  rss(KB)")
                    continue
            except (OSError, subprocess.SubprocessError):
                logger.warning(
                    "[perf] (OSError, subprocess.SubprocessError) suppressed", exc_info=True
                )
        print(f"  {line}")
    print()


def _format_path_size(label: str, path) -> str:
    """返回单一路径的体积描述。"""
    try:
        if path.is_dir():
            total = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
            suffix = ""
        elif sqlite_artifact_exists(path):
            total, encrypted = sqlite_artifact_size(path)
            suffix = " (encrypted)" if encrypted else ""
        else:
            total = 0
            suffix = ""
        return f"  {label}: {_format_bytes(total)}{suffix}"
    except (OSError, ValueError) as e:
        return f"  {label}: 统计失败 ({e})"


def _print_data_sizes(config) -> None:
    """打印各数据文件/目录体积。"""
    print("数据体积:")
    paths = [
        ("mnemos_dir", config.data_dir),
        ("events.db", config.database_dir / "events.db"),
        ("capture_queue.db", config.database_dir / "capture_queue.db"),
        ("knowledge_graph.db", config.database_dir / "knowledge_graph.db"),
        ("embedding_index", config.database_dir / "embedding_index"),
        ("daemon.log", config.database_dir / "daemon.log"),
    ]
    for label, path in paths:
        print(_format_path_size(label, path))
    print()


def _print_events_pressure(config) -> None:
    """打印 events.db 队列压力。"""
    import sqlite3

    events_db = config.database_dir / "events.db"
    if not sqlite_artifact_exists(events_db):
        print("  events.db: 不存在")
        return
    try:
        with _get_sqlite_conn()(str(events_db), timeout=5) as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM events WHERE status IN ('pending','processing')"
            ).fetchone()[0]
            dead = conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0]
        print(f"  events pending/processing: {pending}")
        print(f"  dead_letters: {dead}")
    except (OSError, sqlite3.Error) as e:
        print(f"  events.db: 读取失败 ({e})")


def _print_capture_queue_pressure(config) -> None:
    """打印 capture_queue.db 队列压力。"""
    import sqlite3

    cq_db = config.database_dir / "capture_queue.db"
    if not cq_db.exists():
        print("  capture_queue.db: 不存在")
        return
    try:
        with _get_sqlite_conn()(str(cq_db), timeout=5) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM capture_events GROUP BY status"
            ).fetchall()
        print("  capture_queue:")
        for status, count in rows:
            print(f"    - {status}: {count}")
    except (OSError, sqlite3.Error) as e:
        print(f"  capture_queue.db: 读取失败 ({e})")


def _print_queue_pressure(config) -> None:
    """打印整体队列压力。"""
    print("队列压力:")
    _print_events_pressure(config)
    _print_capture_queue_pressure(config)
    print()


def _print_raw_sync_state(config) -> None:
    """打印 Raw 同步最近状态。"""
    print("Raw 最近同步:")
    state_path = config.database_dir / "agent_source_coverage.json"
    if not state_path.exists():
        print("  尚无扫描游标")
        return
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        sources = data.get("sources", {}) if isinstance(data, dict) else {}
        if not isinstance(sources, dict) or not sources:
            print("  尚无 source coverage 记录")
            return
        for source_name, item in sorted(sources.items()):
            if not isinstance(item, dict):
                continue
            cursor = item.get("cursor", {})
            committed = cursor.get("raw_committed_turns", 0) if isinstance(cursor, dict) else 0
            print(
                f"  {source_name}: {item.get('status', 'unknown')} | "
                f"discovered={item.get('native_sessions', 0)} | raw_committed={committed}"
            )
    except (OSError, ValueError) as e:
        print(f"  读取失败: {e}")


def cmd_perf(args):
    """查看后台性能与队列压力"""
    config = _get_config()
    _print_perf_header(config)
    _print_daemon_processes()
    _print_data_sizes(config)
    _print_queue_pressure(config)
    _print_raw_sync_state(config)
