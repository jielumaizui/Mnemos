from __future__ import annotations

#!/usr/bin/env python3
"""
健康检查定时任务（OpenClaw P5 Config快照 + P6 Heartbeat）
每天下午3点执行（与 scheduler.py 一致）
"""

import os
import sys
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict
from core.config import get_config

sys.path.insert(0, str(Path(__file__).parent.parent))

# ========== P5: Config 健康快照 ==========

SENSITIVE_PATHS = [
    "config/",
    "core/job_scheduler.py",
    "core/credential_pool.py",
    "core/wiki_metrics.py",
]


def check_git_uncommitted() -> Dict:
    """检查敏感文件是否有未提交修改（P5 Config健康快照）"""
    result = {
        "status": "ok",
        "uncommitted_files": [],
        "diff_summary": "",
        "last_commit": "",
    }

    try:
        # 获取上次提交哈希
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%H %ci"],
            capture_output=True, text=True, timeout=10
        )
        if proc.returncode == 0:
            result["last_commit"] = proc.stdout.strip()

        # 获取未提交文件列表
        proc = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True, text=True, timeout=10
        )
        if proc.returncode != 0:
            result["status"] = "error"
            result["error"] = f"git diff failed: {proc.stderr}"
            return result

        all_uncommitted = [f.strip() for f in proc.stdout.strip().split("\n") if f.strip()]

        # 筛选敏感文件
        sensitive_uncommitted = []
        for f in all_uncommitted:
            for pattern in SENSITIVE_PATHS:
                if f.startswith(pattern) or f == pattern:
                    sensitive_uncommitted.append(f)
                    break

        result["uncommitted_files"] = sensitive_uncommitted

        if sensitive_uncommitted:
            result["status"] = "warning"
            # 获取 diff 摘要（限制长度）
            proc = subprocess.run(
                ["git", "diff", "--stat"] + sensitive_uncommitted,
                capture_output=True, text=True, timeout=10
            )
            if proc.returncode == 0:
                result["diff_summary"] = proc.stdout.strip()[:500]

        # 同时检查未跟踪的敏感文件
        proc = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=10
        )
        if proc.returncode == 0:
            untracked = [f.strip() for f in proc.stdout.strip().split("\n") if f.strip()]
            sensitive_untracked = []
            for f in untracked:
                for pattern in SENSITIVE_PATHS:
                    if f.startswith(pattern) or f == pattern:
                        sensitive_untracked.append(f)
                        break
            if sensitive_untracked:
                result.setdefault("untracked_files", []).extend(sensitive_untracked)
                if result["status"] == "ok":
                    result["status"] = "warning"

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)

    return result


# ========== P6: 数据库健康检查 ==========

def check_database() -> Dict:
    """检查数据库健康（P6 Heartbeat扩展版）"""
    results = {}

    db_files = {
        "ai_sync_log": get_config().data_dir / "ai_sync_log.db",
        "live_sync": get_config().data_dir / "live_sync.db",
        "job_scheduler": get_config().data_dir / "job_scheduler.db",
        "wiki_metrics": get_config().data_dir / "wiki_metrics.db",
        "skill_telemetry": get_config().data_dir / "skill_telemetry.db",
    }

    for db_name, db_path in db_files.items():
        info = {"path": str(db_path)}

        if not db_path.exists():
            info["status"] = "missing"
            results[db_name] = info
            continue

        # 文件元信息
        stat = db_path.stat()
        info["size_mb"] = round(stat.st_size / (1024 * 1024), 2)
        info["mtime"] = datetime.fromtimestamp(stat.st_mtime).isoformat()
        info["age_hours"] = round((datetime.now().timestamp() - stat.st_mtime) / 3600, 1)

        # SQLite 健康检测
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            cursor = conn.cursor()

            # 表列表
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cursor.fetchall()]
            info["tables"] = tables

            # WAL 模式
            cursor.execute("PRAGMA journal_mode")
            journal_mode = cursor.fetchone()[0]
            info["journal_mode"] = journal_mode

            # 完整性检查
            cursor.execute("PRAGMA integrity_check")
            integrity = cursor.fetchone()[0]
            info["integrity"] = integrity

            conn.close()

            if integrity == "ok":
                info["status"] = "ok"
            else:
                info["status"] = "error"
                info["error"] = f"integrity check failed: {integrity}"

        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower():
                info["status"] = "locked"
                info["error"] = "database is locked"
            else:
                info["status"] = "error"
                info["error"] = str(e)
        except Exception as e:
            info["status"] = "error"
            info["error"] = str(e)

        results[db_name] = info

    return results


def check_wiki_metrics() -> Dict:
    """Wiki Metrics 运营指标（精简版）"""
    db_path = get_config().data_dir / "wiki_metrics.db"
    if not db_path.exists():
        return {"status": "missing"}

    try:
        conn = sqlite3.connect(str(db_path, timeout=10), timeout=10)
        cursor = conn.cursor()

        stats = {"status": "ok"}

        # 总量
        cursor.execute("SELECT COUNT(*) FROM page_metrics")
        stats["total_pages"] = cursor.fetchone()[0]

        # 阶段分布
        cursor.execute(
            "SELECT knowledge_stage, COUNT(*) FROM page_metrics GROUP BY knowledge_stage"
        )
        stats["stage_distribution"] = {row[0]: row[1] for row in cursor.fetchall()}

        # 状态分布
        cursor.execute("SELECT status, COUNT(*) FROM page_metrics GROUP BY status")
        stats["status_distribution"] = {row[0]: row[1] for row in cursor.fetchall()}

        # 平均质量
        cursor.execute("SELECT AVG(quality_score) FROM page_metrics WHERE quality_score > 0")
        stats["avg_quality"] = round(cursor.fetchone()[0] or 0, 1)

        # 平均热力
        cursor.execute("SELECT AVG(heat_score) FROM page_metrics WHERE heat_score > 0")
        stats["avg_heat"] = round(cursor.fetchone()[0] or 0, 1)

        # 本月新增
        month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0).isoformat()
        cursor.execute(
            "SELECT COUNT(*) FROM page_metrics WHERE created_at >= ?",
            (month_start,)
        )
        stats["new_this_month"] = cursor.fetchone()[0]

        conn.close()
        return stats
    except Exception as e:
        return {"status": "error", "error": str(e)}


def check_wiki() -> Dict:
    """检查 Wiki 目录健康（递归，支持 workspace 隔离）"""
    wiki_path = Path(get_config().wiki_dir)

    if not wiki_path.exists():
        return {"status": "error", "error": "Wiki directory not found"}

    try:
        stats = {}
        total = 0
        skip_dirs = {".archive", "docs", ".git"}

        # 递归扫描所有 .md 文件，跳过归档和索引目录
        for md_file in wiki_path.rglob("*.md"):
            rel_parts = md_file.relative_to(wiki_path).parts
            if any(part in skip_dirs for part in rel_parts):
                continue
            if md_file.name == "index.md":
                continue

            # 按直接父目录分组（如 claude/sources、claude/entities、threads）
            category = "/".join(rel_parts[:-1]) if len(rel_parts) > 1 else "root"
            stats[category] = stats.get(category, 0) + 1
            total += 1

        return {
            "status": "ok",
            "total_md_files": total,
            "by_directory": stats
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def generate_health_report() -> str:
    """生成 Markdown 健康报告（用于写入 Memos）"""
    now = datetime.now().isoformat()
    lines = [f"# Health Check Report | {now[:19]}", ""]

    # P5 Config
    lines.append("## Config Health (P5)")
    git_check = check_git_uncommitted()
    if git_check["status"] == "ok":
        lines.append("Status: OK — 无敏感文件未提交修改")
    elif git_check["status"] == "warning":
        lines.append(f"**Status: WARNING** — 发现 {len(git_check.get('uncommitted_files', []))} 个敏感文件未提交")
        for f in git_check.get("uncommitted_files", []):
            lines.append(f"- `{f}`")
        if git_check.get("diff_summary"):
            lines.append(f"```\n{git_check['diff_summary'][:300]}\n```")
    else:
        lines.append(f"**Status: ERROR** — {git_check.get('error', 'unknown')}")
    lines.append("")

    # P6 Database
    lines.append("## Database Health (P6)")
    db_health = check_database()
    for db_name, info in db_health.items():
        status_emoji = "OK" if info.get("status") == "ok" else ("LOCKED" if info.get("status") == "locked" else "ERR")
        lines.append(f"- **{db_name}**: {status_emoji} | {info.get('size_mb', '?')} MB | journal={info.get('journal_mode', '?')}")
        if info.get("status") != "ok" and info.get("status") != "missing":
            lines.append(f"  - Error: {info.get('error', 'unknown')}")
    lines.append("")

    # P6 运营指标
    lines.append("## Wiki Metrics Stats")
    metrics_stats = check_wiki_metrics()
    if metrics_stats.get("status") == "ok":
        lines.append(f"- Total pages: {metrics_stats['total_pages']}")
        lines.append(f"- New this month: {metrics_stats['new_this_month']}")
        lines.append(f"- Avg quality: {metrics_stats['avg_quality']}/100")
        lines.append(f"- Avg heat: {metrics_stats['avg_heat']}")
        dist = metrics_stats.get("stage_distribution", {})
        if dist:
            dist_str = ", ".join(f"{k}={v}" for k, v in sorted(dist.items()))
            lines.append(f"- Stage distribution: {dist_str}")
    else:
        lines.append(f"Error: {metrics_stats.get('error', 'unknown')}")
    lines.append("")

    # Wiki 目录
    lines.append("## Wiki Directory")
    wiki_health = check_wiki()
    if wiki_health.get("status") == "ok":
        lines.append(f"Total .md files: {wiki_health['total_md_files']}")
        for d, c in wiki_health.get("by_directory", {}).items():
            lines.append(f"- {d}: {c}")
    else:
        lines.append(f"Error: {wiki_health.get('error', 'unknown')}")

    lines.append("")
    lines.append("---")
    lines.append("Tags: `system=health-report, agent=claude, type=heartbeat`")

    return "\n".join(lines)


def main():
    print(f"[{datetime.now().isoformat()}] Starting health check...")

    # 终端输出（保留原有格式）
    db_health = check_database()
    wiki_health = check_wiki()

    print("\n=== P5 Config Health ===")
    git_check = check_git_uncommitted()
    print(f"Status: {git_check['status']}")
    if git_check.get("uncommitted_files"):
        for f in git_check["uncommitted_files"]:
            print(f"  UNCOMMITTED: {f}")

    print("\n=== P6 Database Health ===")
    for db_name, result in db_health.items():
        status = result.get("status", "?")
        emoji = "OK" if status == "ok" else ("MISSING" if status == "missing" else "ERR")
        print(f"  [{db_name}] {emoji} | {result.get('size_mb', '?')} MB")
        if status not in ("ok", "missing"):
            print(f"    Error: {result.get('error', 'unknown')}")

    print("\n=== Wiki Metrics Stats ===")
    metrics_stats = check_wiki_metrics()
    if metrics_stats.get("status") == "ok":
        print(f"  Total pages: {metrics_stats['total_pages']}")
        print(f"  New this month: {metrics_stats['new_this_month']}")
        print(f"  Avg quality: {metrics_stats['avg_quality']}")
        print(f"  Avg heat: {metrics_stats['avg_heat']}")

    print("\n=== Wiki Health ===")
    print(f"Status: {wiki_health.get('status')}")
    if wiki_health.get('status') == 'ok':
        print(f"Total .md files: {wiki_health.get('total_md_files')}")

    # 生成完整报告（可用于写入 Memos）
    report = generate_health_report()
    print("\n=== Full Report (first 500 chars) ===")
    print(report[:500])

    # 保存到本地日志
    log_dir = get_config().data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"health_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    log_file.write_text(report, encoding="utf-8")
    print(f"\nReport saved to: {log_file}")

    print(f"\n[{datetime.now().isoformat()}] Done")


if __name__ == "__main__":
    from typing import Dict  # 延迟导入避免循环
    main()
