"""Shared CLI helpers for Mnemos command handlers."""

import logging
import os
import shutil
import sqlite3
from pathlib import Path
from typing import List, Optional

from core.db_utils import sqlite_artifact_exists
from core.db_utils import sqlite_artifact_size
from core.db_utils import sqlite_conn, validate_sql_identifier
from core.config_persistence import historical_config_paths
from core.frontmatter import parse_frontmatter
from core.vaults.obsidian_registry import is_vault_registered

# Constants extracted from magic numbers
CUTOFF = 86400
FM = 2048

logger = logging.getLogger(__name__)


BYTES_PER_KB = 1024  # [P2-FIX] 字节转换常量


def _resolve_executable(name: str, fallback: Optional[str] = None) -> Optional[str]:
    """将命令名解析为绝对路径；未找到时返回存在的 fallback。"""
    resolved = shutil.which(name)
    if resolved:
        return resolved
    if fallback and Path(fallback).exists():
        return fallback
    return None


def _get_sqlite_conn():
    """Return sqlite_conn, preferring the one re-exported by mnemos_cli.

    This allows tests that patch ``mnemos_cli.sqlite_conn`` to affect helpers
    and command handlers without knowing their new module locations.
    """
    try:
        import mnemos_cli

        return mnemos_cli.sqlite_conn
    # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
    except (
        OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError
    ):
        return sqlite_conn


def _get_config():
    """Return get_config(), preferring the one re-exported by mnemos_cli."""
    try:
        import mnemos_cli

        return mnemos_cli.get_config()
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        from core.config import get_config

        return get_config()


def _format_bytes(size: int) -> str:
    """将字节数格式化为人类可读字符串（B/KB/MB/GB）。"""
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < BYTES_PER_KB or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= BYTES_PER_KB
    return f"{size}B"


def _check_vault_health(config, name: str) -> dict:
    """检查单个 vault 的目录与 Obsidian 注册状态。"""
    try:
        vault_dir = config.vault_dir(name)
    except KeyError:
        return {"name": name, "exists": False, "writable": False, "registered": False}
    exists = vault_dir.exists() and vault_dir.is_dir()
    writable = os.access(vault_dir, os.W_OK) if exists else False
    registered = is_vault_registered(vault_dir) if exists else False
    return {
        "name": name,
        "path": vault_dir,
        "exists": exists,
        "writable": writable,
        "registered": registered,
    }


def _print_vault_status(config):
    """打印 raw + mnemos 两个 vault 的健康状态。"""
    lines = []
    warnings = []
    for name in config.list_vaults():
        info = _check_vault_health(config, name)
        mark = "✓" if info["exists"] and info["writable"] else "✗"
        reg_mark = "✓" if info["registered"] else "⚠"
        lines.append(
            f"  {mark} {name}: {info['path']} "
            f"(exists={info['exists']}, writable={info['writable']})"
        )
        lines.append(f"    Obsidian 注册: {reg_mark}")
        if not info["exists"]:
            warnings.append(f"{name} vault 目录不存在: {info['path']}")
        elif not info["writable"]:
            warnings.append(f"{name} vault 目录不可写: {info['path']}")
        elif not info["registered"]:
            warnings.append(f"{name} vault 未注册到 Obsidian")
    return "\n".join(lines), warnings


def _get_cognitive_graph_stats(config=None) -> dict:
    """读取 cognitive graph 统计，失败时返回空字典。"""
    try:
        if config is None:
            config = _get_config()
        db_path = Path(config.cognitive_graph_db_path)
        if not db_path.is_file():
            return {}
        with sqlite3.connect(
            db_path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=5,
        ) as conn:
            existing = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required = {"cognitive_relations", "canonical_nodes", "sync_outbox"}
            if not required <= existing:
                return {}
            relations = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN stale = 1 THEN 1 ELSE 0 END) "
                "FROM cognitive_relations"
            ).fetchone()
            nodes = conn.execute("SELECT COUNT(*) FROM canonical_nodes").fetchone()
            outbox = conn.execute(
                "SELECT COUNT(*) FROM sync_outbox WHERE processed_at IS NULL"
            ).fetchone()
        return {
            "relations": int(relations[0] or 0),
            "relations_stale": int(relations[1] or 0),
            "canonical_nodes": int(nodes[0] or 0),
            "outbox_pending": int(outbox[0] or 0),
        }
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        logger.debug("读取 cognitive graph 统计失败: %s", exc)
        return {}


def _sqlite_group_counts(db_path: Path, table: str, group_cols: str):
    """获取 SQLite 表的分组计数（SQL 注入防护版）。

    [P0-FIX] 原实现通过 f-string 拼接 SQL，group_cols / where 可被注入。
    修复方案：
      1. 移除从未使用的 where 参数（调用方均不传）
      2. 对 table / group_cols 做白名单校验（仅允许字母、数字、下划线）
    """
    if not sqlite_artifact_exists(db_path):
        return []
    try:
        # 白名单校验：表名和列名只能包含字母、数字、下划线
        validate_sql_identifier(table)
        # group_cols 允许多列，用逗号分隔
        group_cols_clean = ", ".join(
            validate_sql_identifier(col.strip())
            for col in group_cols.split(",")
        )

        sql = " ".join([
            "SELECT",
            group_cols_clean + ", COUNT(*)",
            "FROM",
            table,
            "GROUP BY",
            group_cols_clean,
            "ORDER BY COUNT(*) DESC",
        ])
        with _get_sqlite_conn()(
            str(db_path), timeout=5
        ) as conn:  # [P1-FIX] 使用 sqlite_conn 确保连接关闭
            return conn.execute(sql).fetchall()
    except (OSError, sqlite3.Error, ValueError):
        logger.debug("读取 SQLite 统计失败: %s", db_path, exc_info=True)
        return []


def _daemon_processes() -> List[str]:
    """检测系统中正在运行的 mnemos_daemon 进程，返回进程列表。"""

    def _looks_like_daemon_cmd(cmd: str) -> bool:
        if "mnemos_daemon.py" not in cmd and "mnemos_daemon" not in cmd:
            return False
        noisy = ("pgrep", "grep", "mnemos_cli.py", "sed -n", "pytest")
        if any(token in cmd for token in noisy):
            return False
        try:
            import shlex

            tokens = shlex.split(cmd)
            return any(Path(t).name in ("mnemos_daemon.py", "mnemos_daemon") for t in tokens)
        except (ValueError, OSError):
            return "mnemos_daemon.py start" in cmd or "mnemos_daemon start" in cmd

    try:
        import subprocess
        import platform

        if platform.system() == "Darwin":
            # macOS: pgrep -a 只输出 PID，需要用 ps 获取完整命令行
            pgrep_cmd = _resolve_executable("pgrep", "/usr/bin/pgrep")
            ps_cmd = _resolve_executable("ps", "/bin/ps")
            if not pgrep_cmd or not ps_cmd:
                return []
            result = subprocess.run(
                [pgrep_cmd, "-f", "mnemos_daemon.py"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode not in (0, 1):
                return []
            lines = []
            for pid in result.stdout.splitlines():
                pid = pid.strip()
                if not pid.isdigit():
                    continue
                ps_result = subprocess.run(
                    [ps_cmd, "-p", pid, "-o", "args="],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if ps_result.returncode == 0:
                    cmd = ps_result.stdout.strip()
                    if _looks_like_daemon_cmd(cmd):
                        lines.append(f"{pid} {cmd}")
            return lines
        else:
            pgrep_cmd = _resolve_executable("pgrep", "/usr/bin/pgrep")
            if not pgrep_cmd:
                return []
            result = subprocess.run(
                [pgrep_cmd, "-af", "mnemos_daemon.py"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode not in (0, 1):
                return []
            lines = []
            for line in result.stdout.splitlines():
                if _looks_like_daemon_cmd(line):
                    lines.append(line)
            return lines
    except (OSError, subprocess.SubprocessError):
        return []


def _print_config_contract(config, warnings=None):
    print("配置契约:")
    print(f"  当前读取: {config.config_path}")
    print(f"  配置存在: {'是' if config.config_path.exists() else '否（使用代码默认值）'}")
    print(f"  数据目录: {config.data_dir}")
    mnemos_dir = getattr(config, "mnemos_dir", None)
    legacy_paths = (
        [path for path in historical_config_paths(Path(mnemos_dir)) if path.exists()]
        if mnemos_dir is not None
        else []
    )
    if legacy_paths:
        print("  旧 YAML: " + ", ".join(str(p) for p in legacy_paths))
        if warnings is not None:
            warnings.append("检测到旧 YAML 配置；运行时权威配置为 configs/main.json")
    services = config.get("daemon.services")
    if services:
        print("  daemon 服务:")
        for key in sorted(services):
            mark = "✓" if services[key] else "☐"
            print(f"    {mark} {key}")


def _print_runtime_health(config, warnings=None):
    print("运行态:")
    processes = _daemon_processes()
    print(f"  daemon 进程数: {len(processes)}")
    if len(processes) > 1 and warnings is not None:
        warnings.append(f"检测到重复 daemon 进程: {len(processes)}")

    log_path = config.data_dir / "daemon.log"
    if log_path.exists():
        log_size = log_path.stat().st_size
        print(f"  daemon.log: {_format_bytes(log_size)}")
        max_log = int(
            config.get("ops.daemon_log_max_bytes", 10 * BYTES_PER_KB * BYTES_PER_KB)
        )  # [P2-FIX] 使用常量
        if log_size > max_log and warnings is not None:
            warnings.append(f"daemon.log 超过阈值: {_format_bytes(log_size)}")
    else:
        print("  daemon.log: 未创建")

    events_db = config.database_dir / "events.db"
    if sqlite_artifact_exists(events_db):
        size, encrypted = sqlite_artifact_size(events_db)
        suffix = " (encrypted)" if encrypted else ""
        print(f"  events.db: {_format_bytes(size)}{suffix}")
        rows = _sqlite_group_counts(events_db, "events", "event_type, status")
        pending_total = sum(c for _, status, c in rows if status in ("pending", "processing"))
        print(f"  events pending/processing: {pending_total}")
        for event_type, status, count in rows[:5]:
            print(f"    - {event_type}/{status}: {count}")
        alert = int(config.get("event_bus.queue_depth_alert", 1000))
        if pending_total > alert and warnings is not None:
            warnings.append(f"events.db 积压超过阈值: {pending_total}")
    else:
        print("  events.db: 未创建")

    capture_db = config.database_dir / "capture_queue.db"
    if sqlite_artifact_exists(capture_db):
        rows = _sqlite_group_counts(capture_db, "capture_events", "status")
        pending = sum(c for status, c in rows if status in ("pending", "processing"))
        print(f"  capture_queue pending/processing: {pending}")
        for status, count in rows[:5]:
            print(f"    - {status}: {count}")


def _print_today_summary(config):
    """[C] 今日动态摘要 — 用户可见状态面板"""
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")
    sync_db = config.database_dir / "sync_log.db"

    print("今日动态:")

    _print_capture_summary(config, sync_db, today)
    _print_distillation_summary(config, sync_db, today)
    _print_wiki_today(config)
    _print_agent_access(config)
    _print_resource_status(config)


def _print_capture_summary(config, sync_db, today):
    """今日采集 turn 数（按 agent）"""
    capture_turns = 0
    agent_turns = {}
    if sqlite_artifact_exists(sync_db):
        try:
            with _get_sqlite_conn()(
                str(sync_db), timeout=5
            ) as conn:  # [P1-FIX] 使用 sqlite_conn 确保连接关闭
                rows = conn.execute(
                    "SELECT agent_name, COUNT(*) FROM sync_log WHERE date(synced_at) = ? GROUP BY agent_name",  # noqa: E501
                    (today,),
                ).fetchall()
                for agent, count in rows:
                    agent_turns[agent] = count
                    capture_turns += count
        except (OSError, sqlite3.Error):
            logger.warning("[helpers] (OSError, sqlite3.Error) suppressed", exc_info=True)

    if capture_turns:
        agent_detail = ", ".join(
            f"{a}={n}" for a, n in sorted(agent_turns.items(), key=lambda x: -x[1])[:5]
        )
        print(f"  采集: {capture_turns} turns ({agent_detail})")
    else:
        print("  采集: 今日暂无")


def _print_distillation_summary(config, sync_db, today):
    """最近 24 小时蒸馏情况"""
    distilled_sessions = 0
    wiki_pages_today = 0
    if sqlite_artifact_exists(sync_db):
        try:
            with _get_sqlite_conn()(
                str(sync_db), timeout=5
            ) as conn:  # [P1-FIX] 使用 sqlite_conn 确保连接关闭
                # 最近24小时蒸馏的 session 数
                rows = conn.execute(
                    "SELECT COUNT(DISTINCT session_id) FROM sync_log "
                    "WHERE distill_status = 'distilled' AND datetime(distilled_at) > datetime('now', '-1 day')",  # noqa: E501
                ).fetchall()
                distilled_sessions = rows[0][0] if rows else 0
                # 今日生成的 wiki 页面数（从 wiki_page_paths 推断）
                rows = conn.execute(
                    "SELECT COUNT(*) FROM sync_log "
                    "WHERE distill_status = 'distilled' AND date(distilled_at) = ? AND wiki_page_paths IS NOT NULL",  # noqa: E501
                    (today,),
                ).fetchall()
                wiki_pages_today = rows[0][0] if rows else 0
        except (OSError, sqlite3.Error):
            logger.warning("[helpers] (OSError, sqlite3.Error) suppressed", exc_info=True)

    if distilled_sessions:
        print(f"  蒸馏: 最近24h {distilled_sessions} sessions")
        if wiki_pages_today:
            print(f"  Wiki生成: 今日 {wiki_pages_today} pages")
    else:
        print("  蒸馏: 最近24h暂无")


def _infer_creation_time(path: Path, stat) -> float:
    """推断文件创建时间：优先 st_birthtime，其次 frontmatter，最后 mtime"""
    from datetime import datetime

    created = getattr(stat, "st_birthtime", None)
    if created is not None:
        return float(created)

    try:
        fm = parse_frontmatter(path.read_text(encoding="utf-8")[:FM])[0] or {}
        date_str = fm.get("created_at") or fm.get("distilled_at")
        if date_str:
            return datetime.fromisoformat(
                str(date_str).replace("Z", "+00:00")
            ).timestamp()
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        pass
    return float(stat.st_mtime)


def _print_wiki_today(config):
    """今日新增 Wiki 页面（优先用创建时间，避免修改旧页被计入）"""
    from datetime import datetime

    wiki_dir = config.wiki_dir
    wiki_today = 0
    if wiki_dir.exists():
        try:
            now = datetime.now().timestamp()
            cutoff = now - CUTOFF
            for p in wiki_dir.rglob("*.md"):
                if _infer_creation_time(p, p.stat()) > cutoff:
                    wiki_today += 1
        except (OSError, ValueError):
            logger.warning("[helpers] (OSError, ValueError) suppressed", exc_info=True)
    print(f"  Wiki: {wiki_today} pages (24h内新增)")


def _print_agent_access(config):
    """Agent 接入状态（主动/被动）"""
    try:
        from core.sync_framework.registry import SourceRegistry

        SourceRegistry.register_builtin_agents()
        passive = SourceRegistry.auto_discover()
        passive_names = {a.name for a in passive}
        # 主动接入：从配置判断
        active_names = set()
        if config.claude_code_enabled:
            active_names.add("claude")
        # OpenCode 主动配置检测
        opencode_config = config.to_dict().get("integrations", {}).get("opencode", {})
        if (
            opencode_config.get("enabled")
            or (config.config_path.parent / "opencode" / "mcp.json").exists()
        ):
            active_names.add("opencode")

        both = passive_names & active_names
        only_passive = passive_names - active_names
        only_active = active_names - passive_names

        parts = []
        if both:
            parts.append(f"双通道: {', '.join(sorted(both))}")
        if only_passive:
            parts.append(f"仅被动: {', '.join(sorted(only_passive))}")
        if only_active:
            parts.append(f"仅主动hook: {', '.join(sorted(only_active))}")
        if parts:
            print(f"  Agent: {'; '.join(parts)}")
        else:
            print("  Agent: 未检测到接入")
    except (ImportError, AttributeError):
        logger.debug("[helpers] (ImportError, AttributeError) suppressed", exc_info=True)


def _print_resource_status(config):
    """资源状态与 1 小时趋势"""
    try:
        from core.resource_budget import get_budget

        budget = get_budget()
        rstatus = budget.status()
        state_map = {"normal": "正常", "slowed": "降速", "throttled": "节流", "battery": "电池"}
        state = state_map.get(rstatus["state"], rstatus["state"])
        if rstatus["state"] != "normal":
            print(
                f"  ⚠️ 资源: {state} (CPU {rstatus['cpu']}, 内存 {rstatus['memory']}, 温度 {rstatus['thermal']})"  # noqa: E501
            )
        # [E] 显示 1 小时资源趋势（轻量性能基准）
        stats = budget.history_stats(hours=1.0)
        if stats and stats.get("samples", 0) >= 3:
            cpu_avg = stats["cpu_avg"]
            cpu_peak = stats["cpu_peak"]
            mem_avg = stats["mem_avg"]
            trend = ""
            if cpu_avg < 5.0:
                trend = " 后台空闲"
            elif cpu_avg < 30.0:
                trend = " 负载正常"
            else:
                trend = " 负载偏高"
            print(
                f"  1h趋势: CPU 均值{cpu_avg:.1f}% 峰值{cpu_peak:.1f}% | 内存 均值{mem_avg:.1f}%{trend}"
            )
    except (ImportError, AttributeError, KeyError):
        logger.debug("[helpers] (ImportError, AttributeError, KeyError) suppressed", exc_info=True)
