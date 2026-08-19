#!/usr/bin/env python3

from __future__ import annotations

"""
健康检查定时任务（OpenClaw P5 Config快照 + P6 Heartbeat）
每天下午3点执行（与 scheduler.py 一致）
"""

import ast
import logging
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.config import get_config  # noqa: E402
from core.db_utils import sqlite_artifact_exists  # noqa: E402
from core.db_utils import sqlite_artifact_path  # noqa: E402
from core.db_utils import validate_sql_identifier  # noqa: E402
from core.ops.keyring_doctor import build_keyring_doctor_report  # noqa: E402
from core.ops.keyring_doctor import probe_keyring  # noqa: E402
from core.privacy.secret_inventory import SCHEMA_VERSION as SECRET_INVENTORY_SCHEMA_VERSION  # noqa: E402
from core.privacy.secret_inventory import build_secret_inventory  # noqa: E402

# ========== P5: Config 健康快照 ==========

SENSITIVE_PATHS = [
    "config/",
    "core/job_scheduler.py",
    "core/llm_key_pool.py",
    "core/wiki_metrics.py",
]


def _run_git(args: List[str], timeout: int = 10) -> subprocess.CompletedProcess:
    """运行 git 命令并返回 CompletedProcess。"""
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _filter_sensitive(names: List[str]) -> List[str]:
    """从文件名列表中筛选出敏感路径。"""
    sensitive: List[str] = []
    for name in names:
        for pattern in SENSITIVE_PATHS:
            if name.startswith(pattern) or name == pattern:
                sensitive.append(name)
                break
    return sensitive


def _git_last_commit() -> str:
    proc = _run_git(["log", "-1", "--format=%H %ci"])
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _git_uncommitted_files() -> List[str]:
    proc = _run_git(["diff", "--name-only"])
    if proc.returncode != 0:
        raise RuntimeError(f"git diff failed: {proc.stderr}")
    return [f.strip() for f in proc.stdout.strip().split("\n") if f.strip()]


def _git_diff_summary(files: List[str]) -> str:
    if not files:
        return ""
    proc = _run_git(["diff", "--stat", *files])
    return proc.stdout.strip()[:500] if proc.returncode == 0 else ""


def _git_untracked_files() -> List[str]:
    proc = _run_git(["ls-files", "--others", "--exclude-standard"])
    if proc.returncode != 0:
        return []
    return [f.strip() for f in proc.stdout.strip().split("\n") if f.strip()]


def check_git_uncommitted() -> Dict:
    """检查敏感文件是否有未提交修改（P5 Config健康快照）"""
    result: Dict[str, Any] = {
        "status": "ok",
        "uncommitted_files": [],
        "diff_summary": "",
        "last_commit": "",
    }

    try:
        result["last_commit"] = _git_last_commit()
        all_uncommitted = _git_uncommitted_files()
        sensitive_uncommitted = _filter_sensitive(all_uncommitted)
        result["uncommitted_files"] = sensitive_uncommitted

        if sensitive_uncommitted:
            result["status"] = "warning"
            result["diff_summary"] = _git_diff_summary(sensitive_uncommitted)

        sensitive_untracked = _filter_sensitive(_git_untracked_files())
        if sensitive_untracked:
            result.setdefault("untracked_files", []).extend(sensitive_untracked)
            if result["status"] == "ok":
                result["status"] = "warning"

    except (OSError, subprocess.SubprocessError) as e:
        result["status"] = "error"
        result["error"] = str(e)

    return result


# ========== P6: 数据库健康检查 ==========


def check_database() -> Dict:
    """检查数据库健康（P6 Heartbeat扩展版）"""
    results = {}

    db_files = {
        "ai_sync_log": get_config().database_dir / "ai_sync_log.db",
        "live_sync": get_config().database_dir / "live_sync.db",
        "wiki_metrics": get_config().database_dir / "wiki_metrics.db",
        "skill_telemetry": get_config().database_dir / "skill_telemetry.db",
    }

    for db_name, db_path in db_files.items():
        info: Dict[str, Any] = {"path": str(db_path)}

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
        # [P0-FIX] 使用 try/finally 确保连接无论是否异常都关闭，防止 fd 泄漏
        conn = None
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
        except (sqlite3.Error, OSError) as e:
            info["status"] = "error"
            info["error"] = str(e)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except (
                    OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, sqlite3.Error,
                    subprocess.SubprocessError
                ):
                    logger.debug("数据库连接关闭失败", exc_info=True)

        results[db_name] = info

    return results


def _db_age_days(mtime: float) -> float:
    """根据文件 mtime 计算数据库文件年龄（天）。"""
    return round((datetime.now().timestamp() - mtime) / 86400, 1)


def _record_age_days(conn: sqlite3.Connection, table: str, timestamp_col: str) -> Optional[float]:
    """计算某表最老记录距今天数；无记录返回 None。"""
    try:
        validate_sql_identifier(table)
        validate_sql_identifier(timestamp_col)
        row = conn.execute(
            " ".join(["SELECT MIN(", timestamp_col, ") FROM", table])
        ).fetchone()
        if not row or row[0] is None:
            return None
        oldest = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        delta = datetime.now() - oldest.replace(tzinfo=None)
        return round(delta.total_seconds() / 86400, 1)
    except (sqlite3.Error, ValueError, OSError):
        # 数据库或时间解析异常时保守返回 None，不影响整体健康检查
        return None


def check_retention_databases() -> Dict[str, Dict[str, Any]]:
    """高存留风险数据库的体积与最老记录年龄检查（P0 4.1 database bloat）。"""
    from core.runtime_paths import RuntimePaths

    config = get_config()
    runtime_paths = RuntimePaths.from_config(config)
    db_checks: dict[str, tuple[str | Path, str, str]] = {
        "observations": ("observations.db", "observations", "updated_at"),
        "reflections": ("reflections.db", "reflection_records", "created_at"),
        "user_signals": ("user_signals.db", "behavior_prompt_signals", "timestamp"),
        "application_signals": ("application_signals.db", "application_signals", "created_at"),
        "wiki_metrics_query_log": ("wiki_metrics.db", "query_log", "created_at"),
        "mnemos_search_sessions": ("mnemos.db", "search_sessions", "created_at"),
        "link_probe_queue": ("link_probe.db", "link_probe_queue", "first_seen"),
        "model_call_ledger": (
            runtime_paths.model_call_ledger_db,
            "model_call_entries",
            "created_at",
        ),
        "knowledge_graph": ("knowledge_graph.db", "relations", "updated_at"),
    }

    results: Dict[str, Dict[str, Any]] = {}
    database_dir = config.database_dir
    for name, (filename, table, timestamp_col) in db_checks.items():
        db_path = filename if isinstance(filename, Path) else database_dir / filename
        info: Dict[str, Any] = {"path": str(db_path)}
        if not sqlite_artifact_exists(db_path):
            info["status"] = "missing"
            results[name] = info
            continue

        stat_result = sqlite_artifact_path(db_path).stat()
        info["size_mb"] = round(stat_result.st_size / (1024 * 1024), 2)
        info["file_age_days"] = _db_age_days(stat_result.st_mtime)

        conn = None
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            info["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
            info["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
            info["oldest_record_age_days"] = _record_age_days(conn, table, timestamp_col)
            info["status"] = "ok" if info["integrity"] == "ok" else "error"
        except sqlite3.OperationalError as e:
            info["status"] = "locked" if "database is locked" in str(e).lower() else "error"
            info["error"] = str(e)
        except (sqlite3.Error, OSError) as e:
            info["status"] = "error"
            info["error"] = str(e)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except (
                    OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, sqlite3.Error,
                    subprocess.SubprocessError
                ):
                    logger.debug("数据库连接关闭失败", exc_info=True)
        results[name] = info
    return results


def check_wiki_metrics() -> Dict:
    """Wiki Metrics 运营指标（精简版）"""
    db_path = get_config().database_dir / "wiki_metrics.db"
    if not sqlite_artifact_exists(db_path):
        return {"status": "missing"}

    # [P0-FIX] str(db_path, timeout=10) 是非法语法（str 不接受 timeout 参数）
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        cursor = conn.cursor()

        stats: Dict[str, Any] = {"status": "ok"}

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
        cursor.execute("SELECT COUNT(*) FROM page_metrics WHERE created_at >= ?", (month_start,))
        stats["new_this_month"] = cursor.fetchone()[0]

        return stats
    except (OSError, ValueError, TypeError, sqlite3.Error) as e:
        return {"status": "error", "error": str(e)}
    finally:
        if conn is not None:
            try:
                conn.close()
            except (
                OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, sqlite3.Error,
                subprocess.SubprocessError
            ):
                logger.debug("数据库连接关闭失败", exc_info=True)


def check_wiki() -> Dict:
    """检查 Wiki 目录健康（递归，支持 workspace 隔离）"""
    wiki_path = Path(get_config().wiki_dir)

    if not wiki_path.exists():
        return {"status": "error", "error": "Wiki directory not found"}

    try:
        stats: Dict[str, Any] = {}
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

        return {"status": "ok", "total_md_files": total, "by_directory": stats}
    except (OSError, UnicodeError, ValueError, TypeError) as e:
        return {"status": "error", "error": str(e)}


def _scan_for_pickle(root_paths: List[Path]) -> List[Tuple[str, int, str]]:
    """扫描生产代码中是否仍在使用 pickle（测试目录除外）。"""
    findings: List[Tuple[str, int, str]] = []
    risky_names = {"loads", "dumps", "load", "dump"}
    for root in root_paths:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = {alias.name for alias in node.names}
                    module = getattr(node, "module", None)
                    if "pickle" in names or module == "pickle":
                        findings.append((str(path), node.lineno, "pickle import"))
                elif isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                        if func.value.id == "pickle" and func.attr in risky_names:
                            findings.append((str(path), node.lineno, f"pickle.{func.attr}()"))
    return findings


def _scan_for_weak_hash(root_paths: List[Path]) -> List[Tuple[str, int, str]]:
    """扫描 hashlib.md5/sha1 是否未标记 usedforsecurity=False。"""
    findings: List[Tuple[str, int, str]] = []
    for root in root_paths:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
                    continue
                if func.value.id != "hashlib" or func.attr not in ("md5", "sha1"):
                    continue
                if any(kw.arg == "usedforsecurity" for kw in node.keywords):
                    continue
                findings.append((str(path), node.lineno, f"hashlib.{func.attr}()"))
    return findings


def _check_sensitive_permissions(paths: List[Path]) -> List[str]:
    """检查敏感文件/目录权限是否过于宽松。"""
    violations: List[str] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            mode = path.stat().st_mode
        except OSError:
            continue
        if path.is_dir():
            if mode & stat.S_IRWXO or mode & stat.S_IRWXG:
                violations.append(f"{path}: dir mode={oct(mode & 0o777)}")
        elif path.is_file():
            if mode & stat.S_IROTH or mode & stat.S_IWOTH or mode & stat.S_IRGRP or mode & stat.S_IWGRP:
                violations.append(f"{path}: file mode={oct(mode & 0o777)}")
    return violations


def _secret_inventory_from_config(config: Any) -> Dict[str, Any]:
    data = getattr(config, "_data", {})
    if not isinstance(data, Mapping):
        return {
            "schema_version": SECRET_INVENTORY_SCHEMA_VERSION,
            "findings": [],
            "plaintext_count": 0,
            "reference_count": 0,
            "error": "config object has no loaded mapping data",
        }
    inventory = build_secret_inventory(data)
    inventory["error"] = None
    return inventory


def _plaintext_api_key_risks_from_inventory(inventory: Dict[str, Any]) -> List[str]:
    """Return API key field paths for the retained health JSON field."""
    risks: List[str] = []
    for finding in inventory.get("findings", []):
        path = str(finding.get("path", ""))
        if "api_key" in path.lower() and finding.get("status") == "plaintext-risk":
            risks.append(path)
    return risks


def _historical_key_rows(credential_db: Path) -> Dict[str, Any]:
    """检查 credential_pool.db 是否仍存旧版 enc/plaintext 密钥。"""
    result: Dict[str, Any] = {"enc_rows": 0, "plaintext_rows": 0, "keyref_rows": 0}
    if not credential_db.exists():
        return result
    try:
        with sqlite3.connect(str(credential_db), timeout=5) as conn:
            rows = conn.execute("SELECT api_key FROM credentials").fetchall()
    except sqlite3.OperationalError:
        return result
    for (api_key,) in rows:
        if not isinstance(api_key, str):
            continue
        if api_key.startswith("enc:"):
            result["enc_rows"] += 1
        elif api_key.startswith("keyref:"):
            result["keyref_rows"] += 1
        elif api_key:
            result["plaintext_rows"] += 1
    return result


def _permission_repair_actions(violations: List[str]) -> List[str]:
    """生成可人工复核的权限修复命令。"""
    actions: List[str] = []
    for violation in violations:
        path_text = violation.split(":", 1)[0]
        mode = "700" if ": dir mode=" in violation else "600"
        actions.append(f"chmod {mode} {path_text}")
    return actions


def _probe_keyring() -> Dict[str, Any]:
    """探测 keyring backend，并保留不可用原因。"""
    return probe_keyring()


def check_security(config: Any | None = None) -> Dict[str, Any]:
    """安全健康总览（S47 安全加固）。"""
    result: Dict[str, Any] = {
        "status": "ok",
        "warnings": [],
        "repair_actions": [],
    }

    project_root = Path(__file__).parent.parent
    scan_roots = [
        project_root / "core",
        project_root / "integrations",
        project_root / "daemon",
        project_root / "scripts",
    ]

    # 1. pickle 使用情况
    pickle_findings = _scan_for_pickle(scan_roots)
    result["pickle_findings"] = pickle_findings
    if pickle_findings:
        result["status"] = "error"

    # 2. 弱哈希未标记
    weak_hash_findings = _scan_for_weak_hash(scan_roots)
    result["weak_hash_findings"] = weak_hash_findings
    if weak_hash_findings:
        result["status"] = "error"

    # 3. 敏感文件权限（配置、数据库目录及文件）
    cfg = config or get_config()
    sensitive_paths = [
        cfg.config_path,
        cfg.config_path.parent,
        cfg.database_dir,
        cfg.data_dir / "logs",
        cfg.database_dir / "logs",
    ]
    permission_violations = _check_sensitive_permissions(sensitive_paths)
    result["permission_violations"] = permission_violations
    if permission_violations:
        result["status"] = "warning"
        result["warnings"].append("sensitive file or directory permissions are too broad")
        result["repair_actions"].extend(_permission_repair_actions(permission_violations))

    # 4. 明文 secret inventory（同时保留既有 API key 风险字段）
    secret_inventory = _secret_inventory_from_config(cfg)
    result["secret_inventory"] = secret_inventory
    if secret_inventory.get("error"):
        result["status"] = "warning"
        result["warnings"].append("config secret inventory could not read config")
    plaintext_secret_count = int(secret_inventory.get("plaintext_count", 0) or 0)
    if plaintext_secret_count:
        result["status"] = "warning"
        result["warnings"].append("config may contain plaintext secret-like values")

    plaintext_risks = _plaintext_api_key_risks_from_inventory(secret_inventory)
    result["plaintext_api_key_risks"] = plaintext_risks
    if plaintext_risks:
        result["status"] = "warning"
        result["warnings"].append("config may contain plaintext API key fields")

    # 5. credential_pool 旧版密钥
    credential_db = cfg.database_dir / "credential_pool.db"
    legacy_keys = _historical_key_rows(credential_db)
    result["legacy_key_rows"] = legacy_keys
    if legacy_keys["enc_rows"] or legacy_keys["plaintext_rows"]:
        result["status"] = "warning"
        result["warnings"].append("credential_pool.db contains legacy enc/plaintext key rows")

    # 6. keyring 可用性
    keyring_info = _probe_keyring()
    keyring_report = build_keyring_doctor_report(
        cfg,
        keyring_info=keyring_info,
        secret_inventory=secret_inventory,
    )
    keyring_detail = keyring_report["keyring"]
    result["keyring"] = keyring_report
    result["keyring_available"] = keyring_detail["available"]
    result["keyring_backend"] = keyring_detail["backend"]
    result["keyring_error"] = keyring_detail["error"]
    result["keyring_status"] = keyring_report["status"]
    result["keyring_policy"] = keyring_report["policy"]
    result["keyring_risk_level"] = keyring_report["risk_level"]
    result["keyring_env_fallback_accepted"] = keyring_report["env_fallback_accepted"]
    result["keyring_safe_but_not_best"] = keyring_report["safe_but_not_best"]
    result["keyring_requires_user_choice"] = keyring_report["requires_user_choice"]
    if not keyring_info["available"]:
        if result["status"] == "ok":
            result["status"] = "warning"
        result["warnings"].extend(keyring_report["warnings"])
        result["repair_actions"].extend(keyring_report["repair_actions"])

    return result


def _render_config_section(git_check: Dict) -> List[str]:
    """渲染 Config Health (P5) 区块。"""
    lines = ["## Config Health (P5)"]
    if git_check["status"] == "ok":
        lines.append("Status: OK — 无敏感文件未提交修改")
    elif git_check["status"] == "warning":
        lines.append(
            f"**Status: WARNING** — 发现 {len(git_check.get('uncommitted_files', []))} 个敏感文件未提交"
        )
        for f in git_check.get("uncommitted_files", []):
            lines.append(f"- `{f}`")
        if git_check.get("diff_summary"):
            lines.append(f"```\n{git_check['diff_summary'][:300]}\n```")
    else:
        lines.append(f"**Status: ERROR** — {git_check.get('error', 'unknown')}")
    lines.append("")
    return lines


def _render_database_section(db_health: Dict) -> List[str]:
    """渲染 Database Health (P6) 区块。"""
    lines = ["## Database Health (P6)"]
    for db_name, info in db_health.items():
        status_emoji = (
            "OK"
            if info.get("status") == "ok"
            else ("LOCKED" if info.get("status") == "locked" else "ERR")
        )
        lines.append(
            f"- **{db_name}**: {status_emoji} | {info.get('size_mb', '?')} MB | journal={info.get('journal_mode', '?')}"
        )
        if info.get("status") != "ok" and info.get("status") != "missing":
            lines.append(f"  - Error: {info.get('error', 'unknown')}")
    lines.append("")
    return lines


def _render_retention_section(retention_db_health: Dict) -> List[str]:
    """渲染 Retention Database Health 区块。"""
    lines = ["## Retention Database Health (P0 4.1)"]
    for db_name, info in retention_db_health.items():
        status = info.get("status", "?")
        emoji = "OK" if status == "ok" else ("MISSING" if status == "missing" else "ERR")
        oldest = info.get("oldest_record_age_days")
        oldest_str = f"oldest={oldest}d" if oldest is not None else "oldest=N/A"
        lines.append(
            f"- **{db_name}**: {emoji} | {info.get('size_mb', '?')} MB | {oldest_str} | "
            f"journal={info.get('journal_mode', '?')}"
        )
        if status not in ("ok", "missing") and info.get("error"):
            lines.append(f"  - Error: {info['error']}")
    lines.append("")
    return lines


def _render_wiki_metrics_section(metrics_stats: Dict) -> List[str]:
    """渲染 Wiki Metrics Stats 区块。"""
    lines = ["## Wiki Metrics Stats"]
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
    return lines


def _render_wiki_directory_section(wiki_health: Dict) -> List[str]:
    """渲染 Wiki Directory 区块。"""
    lines = ["## Wiki Directory"]
    if wiki_health.get("status") == "ok":
        lines.append(f"Total .md files: {wiki_health['total_md_files']}")
        for d, c in wiki_health.get("by_directory", {}).items():
            lines.append(f"- {d}: {c}")
    else:
        lines.append(f"Error: {wiki_health.get('error', 'unknown')}")
    lines.append("")
    return lines


def _render_security_section(sec: Dict) -> List[str]:
    """渲染 Security Health (S47) 区块。"""
    lines = ["## Security Health (S47)"]
    lines.append(f"Status: {sec['status'].upper()}")
    lines.append(f"- keyring available: {sec.get('keyring_available', False)}")
    if sec.get("keyring_status"):
        lines.append(f"- keyring status: {sec['keyring_status']}")
    if sec.get("keyring_risk_level"):
        lines.append(f"- keyring risk: {sec['keyring_risk_level']}")
    lines.append(
        f"- keyring env fallback accepted: {sec.get('keyring_env_fallback_accepted', False)}"
    )
    lines.append(
        f"- keyring safe but not best: {sec.get('keyring_safe_but_not_best', False)}"
    )
    if sec.get("keyring_backend"):
        lines.append(f"- keyring backend: {sec['keyring_backend']}")
    if sec.get("keyring_error"):
        lines.append(f"- keyring error: {sec['keyring_error']}")
    lines.append(
        f"- legacy credential rows: enc={sec['legacy_key_rows']['enc_rows']}, "
        f"plaintext={sec['legacy_key_rows']['plaintext_rows']}, "
        f"keyref={sec['legacy_key_rows']['keyref_rows']}"
    )
    lines.append(f"- pickle findings: {len(sec['pickle_findings'])}")
    lines.append(f"- weak hash findings: {len(sec['weak_hash_findings'])}")
    lines.append(f"- permission violations: {len(sec['permission_violations'])}")
    lines.append(f"- plaintext api key risks: {len(sec['plaintext_api_key_risks'])}")
    secret_inventory = sec.get("secret_inventory", {})
    lines.append(
        "- secret inventory plaintext risks: "
        f"{int(secret_inventory.get('plaintext_count', 0) or 0)}"
    )
    for finding in sec.get("pickle_findings", [])[:5]:
        lines.append(f"  - pickle: {finding[0]}:{finding[1]}")
    for finding in sec.get("weak_hash_findings", [])[:5]:
        lines.append(f"  - weak hash: {finding[0]}:{finding[1]}")
    for violation in sec.get("permission_violations", [])[:5]:
        lines.append(f"  - permission: {violation}")
    for finding in secret_inventory.get("findings", [])[:5]:
        if finding.get("status") == "plaintext-risk":
            lines.append(f"  - secret: {finding.get('path')} plaintext-risk")
    for warning in sec.get("warnings", [])[:5]:
        lines.append(f"  - warning: {warning}")
    for action in sec.get("repair_actions", [])[:5]:
        lines.append(f"  - repair: {action}")
    lines.append("")
    lines.append("---")
    lines.append("Tags: `system=health-report, agent=claude, type=heartbeat`")
    return lines


def generate_health_report() -> str:
    """生成 Markdown 健康报告（用于写入 L1 storage）"""
    now = datetime.now().isoformat()
    lines = [f"# Health Check Report | {now[:19]}", ""]

    lines.extend(_render_config_section(check_git_uncommitted()))
    lines.extend(_render_database_section(check_database()))
    lines.extend(_render_retention_section(check_retention_databases()))
    lines.extend(_render_wiki_metrics_section(check_wiki_metrics()))
    lines.extend(_render_wiki_directory_section(check_wiki()))
    lines.extend(_render_security_section(check_security()))

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

    print("\n=== Retention Database Health (P0 4.1) ===")
    for db_name, result in check_retention_databases().items():
        status = result.get("status", "?")
        emoji = "OK" if status == "ok" else ("MISSING" if status == "missing" else "ERR")
        oldest = result.get("oldest_record_age_days")
        oldest_str = f"oldest={oldest}d" if oldest is not None else "oldest=N/A"
        print(f"  [{db_name}] {emoji} | {result.get('size_mb', '?')} MB | {oldest_str}")
        if status not in ("ok", "missing") and result.get("error"):
            print(f"    Error: {result['error']}")

    print("\n=== Wiki Metrics Stats ===")
    metrics_stats = check_wiki_metrics()
    if metrics_stats.get("status") == "ok":
        print(f"  Total pages: {metrics_stats['total_pages']}")
        print(f"  New this month: {metrics_stats['new_this_month']}")
        print(f"  Avg quality: {metrics_stats['avg_quality']}")
        print(f"  Avg heat: {metrics_stats['avg_heat']}")

    print("\n=== Wiki Health ===")
    print(f"Status: {wiki_health.get('status')}")
    if wiki_health.get("status") == "ok":
        print(f"Total .md files: {wiki_health.get('total_md_files')}")

    print("\n=== Security Health ===")
    sec = check_security()
    print(f"Status: {sec['status']}")
    print(f"  keyring_available: {sec.get('keyring_available')}")
    print(f"  legacy_key_rows: {sec['legacy_key_rows']}")
    print(f"  pickle_findings: {len(sec['pickle_findings'])}")
    print(f"  weak_hash_findings: {len(sec['weak_hash_findings'])}")
    print(f"  permission_violations: {len(sec['permission_violations'])}")
    print(f"  plaintext_api_key_risks: {len(sec['plaintext_api_key_risks'])}")
    print(
        "  secret_inventory_plaintext_risks: "
        f"{int(sec.get('secret_inventory', {}).get('plaintext_count', 0) or 0)}"
    )

    # 生成完整报告（可用于写入 backend）
    report = generate_health_report()
    print("\n=== Full Report (first 500 chars) ===")
    print(report[:500])

    # 保存到本地日志
    log_dir = get_config().database_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"health_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    log_file.write_text(report, encoding="utf-8")
    print(f"\nReport saved to: {log_file}")

    print(f"\n[{datetime.now().isoformat()}] Done")


if __name__ == "__main__":
    main()
