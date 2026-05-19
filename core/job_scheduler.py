#!/usr/bin/env python3
"""
Job Scheduler - 多层安全调度系统 (Hermes H16)

增强特性：
- 依赖链管理：任务 A 完成后才能启动任务 B
- 超时熔断：任务运行超过阈值自动终止
- 失败重试：指数退避重试策略
- 状态持久化：SQLite 记录任务执行历史

与现有 config/scheduler.py 的集成：
    from core.job_scheduler import JobScheduler

    js = JobScheduler()
    js.register_job("heat_decay", cron="0 11 * * *", script="heat_decay.py")
    js.register_job("cold_demotion", cron="0 13 * * *", script="cold_demotion.py",
                    depends_on=["heat_decay"])  # 依赖 heat_decay
    js.run_due_jobs()
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
import signal
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set, Any, Callable
import re

from core.config import get_config


# ==================== 1. _LazyPath ====================

class _LazyPath:
    """Lazy path that resolves get_config() only on access."""
    __slots__ = ('_base', '_segments')

    def __init__(self, base: str = "data_dir", *segments):
        self._base = base
        self._segments = segments

    def __truediv__(self, other):
        return _LazyPath(self._base, *self._segments, other)

    def __rtruediv__(self, other):
        raise NotImplementedError

    def _resolve(self) -> Path:
        config = get_config()
        if self._base == "data_dir":
            result = config.data_dir
        elif self._base == "wiki_dir":
            result = config.wiki_dir
        else:
            result = config.data_dir
        for seg in self._segments:
            result = result / seg
        return result

    def __str__(self):
        return str(self._resolve())

    def __repr__(self):
        return f"LazyPath({self._base}:{'/'.join(self._segments)})"

    def __fspath__(self):
        return str(self._resolve())

    def __getattr__(self, name):
        return getattr(self._resolve(), name)

    def __hash__(self):
        return hash(self._resolve())

    def __eq__(self, other):
        return self._resolve() == other

    def __iter__(self):
        return iter(self._resolve())


DB_PATH = _LazyPath("data_dir", "job_scheduler.db")

# 项目根目录/scripts
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = _PROJECT_ROOT / "scripts"


# ==================== 2. 枚举与状态 ====================

class JobStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class JobTrigger(Enum):
    CRON = "cron"
    MANUAL = "manual"
    DEPENDENCY = "dependency"
    RETRY = "retry"


@dataclass
class JobConfig:
    """任务配置"""
    name: str
    script: str
    cron: Optional[str] = None
    description: str = ""
    # 依赖链
    depends_on: List[str] = field(default_factory=list)
    # 超时（秒）
    timeout_seconds: int = 300
    # 重试
    max_retries: int = 3
    retry_backoff_base: int = 60
    # 跳过策略
    skip_if_missed: bool = True
    # 标签
    tags: List[str] = field(default_factory=list)


@dataclass
class JobRun:
    """单次任务执行记录"""
    id: str
    job_name: str
    status: JobStatus
    trigger: JobTrigger
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    error_message: Optional[str] = None
    attempt: int = 1
    metadata: Dict[str, Any] = field(default_factory=dict)


# ==================== 3. Cron 解析器 ====================

class CronParser:
    """简单的 Cron 表达式解析器"""

    @staticmethod
    def is_due(cron_expr: str, now: Optional[datetime] = None) -> bool:
        """
        检查当前时间是否匹配 cron 表达式

        支持格式: "分 时 日 月 周"
        目前只支持精确匹配（不支持 */N 步长）
        """
        now = now or datetime.now(timezone.utc)
        parts = cron_expr.split()
        if len(parts) != 5:
            return False

        minute, hour, day, month, weekday = parts

        # 分钟
        if not CronParser._match_field(minute, now.minute):
            return False
        # 小时
        if not CronParser._match_field(hour, now.hour):
            return False
        # 日期
        if not CronParser._match_field(day, now.day):
            return False
        # 月份
        if not CronParser._match_field(month, now.month):
            return False
        # 星期 (0=周日) — 修复运算符优先级：加括号
        if weekday != "*":
            if str((now.weekday() + 1) % 7) != weekday and weekday != "*":
                return False

        return True

    @staticmethod
    def _match_field(expr: str, value: int) -> bool:
        """匹配单个字段"""
        if expr == "*":
            return True
        if "/" in expr:
            # 步长匹配，如 */4
            _, step = expr.split("/")
            return value % int(step) == 0
        return int(expr) == value

    @staticmethod
    def get_next_run(cron_expr: str, after: Optional[datetime] = None) -> Optional[datetime]:
        """获取下次执行时间（简化版：只支持小时级）"""
        after = after or datetime.now(timezone.utc)
        parts = cron_expr.split()
        if len(parts) != 5:
            return None

        minute_str, hour_str = parts[0], parts[1]

        # 简单的下一小时执行
        if minute_str == "0" and hour_str.startswith("*/"):
            step = int(hour_str.split("/")[1])
            next_hour = ((after.hour // step) + 1) * step
            if next_hour >= 24:
                next_hour = 0
                next_day = after + timedelta(days=1)
            else:
                next_day = after
            return next_day.replace(hour=next_hour, minute=0, second=0, microsecond=0)

        if minute_str != "*" and hour_str != "*":
            target = after.replace(hour=int(hour_str), minute=int(minute_str), second=0,
                                   microsecond=0)
            if target <= after:
                target += timedelta(days=1)
            return target

        return None


# ==================== 4. JobScheduler ====================

class JobScheduler:
    """
    多层安全调度器
    """

    def __init__(self, db_path: Optional[str] = None,
                 scripts_dir: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        Path(str(self.db_path)).parent.mkdir(parents=True, exist_ok=True)
        self.scripts_dir = Path(scripts_dir) if scripts_dir else SCRIPTS_DIR
        self._local = threading.local()
        self._init_db()

        # 注册的任务
        self._jobs: Dict[str, JobConfig] = {}
        # 锁
        self._run_lock = threading.Lock()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self.db_path), timeout=10, check_same_thread=False
            )
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS job_runs (
                id TEXT PRIMARY KEY,
                job_name TEXT NOT NULL,
                status TEXT NOT NULL,
                trigger TEXT NOT NULL,
                started_at TIMESTAMP,
                ended_at TIMESTAMP,
                duration_ms INTEGER,
                exit_code INTEGER,
                stdout TEXT,
                stderr TEXT,
                error_message TEXT,
                attempt INTEGER DEFAULT 1,
                metadata TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_jr_job ON job_runs(job_name);
            CREATE INDEX IF NOT EXISTS idx_jr_status ON job_runs(status);
            CREATE INDEX IF NOT EXISTS idx_jr_started ON job_runs(started_at);
        """)
        conn.commit()

    # ---- 任务注册 ----

    def register_job(self, name: str, script: str,
                     cron: Optional[str] = None,
                     description: str = "",
                     depends_on: Optional[List[str]] = None,
                     timeout_seconds: int = 300,
                     max_retries: int = 3,
                     skip_if_missed: bool = True,
                     tags: Optional[List[str]] = None):
        """注册定时任务"""
        self._jobs[name] = JobConfig(
            name=name,
            script=script,
            cron=cron,
            description=description,
            depends_on=depends_on or [],
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            skip_if_missed=skip_if_missed,
            tags=tags or [],
        )

    def get_job(self, name: str) -> Optional[JobConfig]:
        """获取任务配置"""
        return self._jobs.get(name)

    def list_jobs(self) -> List[JobConfig]:
        """列出所有任务"""
        return list(self._jobs.values())

    # ---- 依赖检查 ----

    def _check_dependencies(self, job: JobConfig) -> bool:
        """
        检查任务的依赖是否满足

        返回 True 表示所有依赖已成功完成。
        """
        if not job.depends_on:
            return True

        conn = self._get_conn()
        for dep_name in job.depends_on:
            # 获取依赖的最新执行记录
            row = conn.execute(
                """SELECT status FROM job_runs
                   WHERE job_name = ?
                   ORDER BY started_at DESC
                   LIMIT 1""",
                (dep_name,),
            ).fetchone()

            if not row:
                print(f"[Scheduler] 依赖 {dep_name} 未执行过")
                return False

            if row["status"] != JobStatus.SUCCESS.value:
                print(f"[Scheduler] 依赖 {dep_name} 状态为 {row['status']}，不满足")
                return False

        return True

    # ---- 执行记录 ----

    def _record_run(self, run: JobRun):
        """记录任务执行"""
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO job_runs
                (id, job_name, status, trigger, started_at, ended_at,
                 duration_ms, exit_code, stdout, stderr, error_message,
                 attempt, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run.id, run.job_name, run.status.value, run.trigger.value,
                run.started_at.isoformat() if run.started_at else None,
                run.ended_at.isoformat() if run.ended_at else None,
                run.duration_ms, run.exit_code,
                run.stdout, run.stderr, run.error_message,
                run.attempt,
                json.dumps(run.metadata, ensure_ascii=False),
            ),
        )
        conn.commit()

    def get_last_run(self, job_name: str) -> Optional[JobRun]:
        """获取任务最近一次执行"""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT * FROM job_runs
               WHERE job_name = ?
               ORDER BY started_at DESC
               LIMIT 1""",
            (job_name,),
        ).fetchone()
        if not row:
            return None
        return self._row_to_run(row)

    def get_run_history(self, job_name: str, limit: int = 10) -> List[JobRun]:
        """获取任务执行历史"""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM job_runs
               WHERE job_name = ?
               ORDER BY started_at DESC
               LIMIT ?""",
            (job_name, limit),
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def _row_to_run(self, row: sqlite3.Row) -> JobRun:
        return JobRun(
            id=row["id"],
            job_name=row["job_name"],
            status=JobStatus(row["status"]),
            trigger=JobTrigger(row["trigger"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
            duration_ms=row["duration_ms"],
            exit_code=row["exit_code"],
            stdout=row["stdout"] or "",
            stderr=row["stderr"] or "",
            error_message=row["error_message"],
            attempt=row["attempt"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )

    # ---- 任务执行 ----

    def run_job(self, job_name: str,
                trigger: JobTrigger = JobTrigger.MANUAL) -> JobRun:
        """
        执行单个任务（带超时和重试）

        Args:
            job_name: 任务名称
            trigger: 触发方式

        Returns:
            JobRun 执行记录
        """
        job = self._jobs.get(job_name)
        if not job:
            raise ValueError(f"未知任务: {job_name}")

        with self._run_lock:
            # 1. 检查依赖
            if not self._check_dependencies(job):
                run = JobRun(
                    id=f"run_{job_name}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
                    job_name=job_name,
                    status=JobStatus.SKIPPED,
                    trigger=trigger,
                    error_message="依赖未满足",
                )
                self._record_run(run)
                return run

            # 2. 执行（带重试）
            for attempt in range(1, job.max_retries + 1):
                run = self._execute_once(job, trigger, attempt)
                self._record_run(run)

                if run.status == JobStatus.SUCCESS:
                    return run

                if attempt < job.max_retries:
                    # 指数退避等待
                    backoff = job.retry_backoff_base * (2 ** (attempt - 1))
                    print(f"[Scheduler] {job_name} 第 {attempt} 次失败，{backoff}秒后重试...")
                    time.sleep(backoff)

            return run

    def _execute_once(self, job: JobConfig,
                      trigger: JobTrigger,
                      attempt: int) -> JobRun:
        """单次执行任务（带超时）"""
        import uuid
        run_id = f"run_{job.name}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:4]}"

        script_path = self.scripts_dir / job.script
        if not script_path.exists():
            return JobRun(
                id=run_id,
                job_name=job.name,
                status=JobStatus.FAILED,
                trigger=trigger,
                attempt=attempt,
                error_message=f"脚本不存在: {script_path}",
            )

        run = JobRun(
            id=run_id,
            job_name=job.name,
            status=JobStatus.RUNNING,
            trigger=trigger,
            started_at=datetime.now(timezone.utc),
            attempt=attempt,
        )

        try:
            # 使用 sys.executable 替代硬编码 python3，确保跨平台
            result = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True,
                text=True,
                timeout=job.timeout_seconds,
                cwd=str(self.scripts_dir),
            )

            run.ended_at = datetime.now(timezone.utc)
            run.duration_ms = int((run.ended_at - run.started_at).total_seconds() * 1000)
            run.exit_code = result.returncode
            run.stdout = result.stdout[-5000:]  # 限制长度
            run.stderr = result.stderr[-2000:]

            if result.returncode == 0:
                run.status = JobStatus.SUCCESS
            else:
                run.status = JobStatus.FAILED
                run.error_message = f"Exit code: {result.returncode}"

        except subprocess.TimeoutExpired:
            run.ended_at = datetime.now(timezone.utc)
            run.duration_ms = job.timeout_seconds * 1000
            run.status = JobStatus.TIMEOUT
            run.error_message = f"超时 ({job.timeout_seconds}s)"

        except Exception as e:
            run.ended_at = datetime.now(timezone.utc)
            if run.started_at:
                run.duration_ms = int((run.ended_at - run.started_at).total_seconds() * 1000)
            run.status = JobStatus.FAILED
            run.error_message = str(e)

        return run

    # ---- 批量调度 ----

    def run_due_jobs(self, now: Optional[datetime] = None) -> List[JobRun]:
        """
        运行所有到期的任务

        检查 cron 表达式，执行到期的任务。
        """
        now = now or datetime.now(timezone.utc)
        results = []

        for job in self._jobs.values():
            if not job.cron:
                continue

            if CronParser.is_due(job.cron, now):
                # 检查是否已运行过（分钟级去重）
                last_run = self.get_last_run(job.name)
                if last_run and last_run.started_at:
                    elapsed = (now - last_run.started_at).total_seconds()
                    if elapsed < 60:  # 1 分钟内不重复执行
                        continue

                result = self.run_job(job.name, JobTrigger.CRON)
                results.append(result)

        return results

    def run_job_chain(self, job_names: List[str]) -> List[JobRun]:
        """
        顺序执行任务链

        每个任务成功后才开始下一个。
        """
        results = []
        for name in job_names:
            result = self.run_job(name, JobTrigger.DEPENDENCY)
            results.append(result)
            if result.status != JobStatus.SUCCESS:
                print(f"[Scheduler] 任务链中断: {name} 失败")
                break
        return results

    # ---- 统计与报告 ----

    def get_stats(self, days: int = 7) -> Dict[str, Any]:
        """获取调度统计"""
        conn = self._get_conn()
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        rows = conn.execute(
            """SELECT job_name, status, COUNT(*)
               FROM job_runs
               WHERE started_at >= ?
               GROUP BY job_name, status""",
            (since,),
        ).fetchall()

        stats = {}
        for job_name, status, count in rows:
            if job_name not in stats:
                stats[job_name] = {}
            stats[job_name][status] = count

        return {
            "period_days": days,
            "jobs": stats,
            "total_registered": len(self._jobs),
        }

    def health_check(self) -> Dict[str, Any]:
        """调度器健康检查"""
        issues = []

        for job in self._jobs.values():
            last_run = self.get_last_run(job.name)
            if not last_run:
                issues.append(f"{job.name}: 从未执行")
                continue

            if last_run.status == JobStatus.FAILED:
                issues.append(f"{job.name}: 上次执行失败 ({last_run.error_message})")
            elif last_run.status == JobStatus.TIMEOUT:
                issues.append(f"{job.name}: 上次执行超时")

        return {
            "healthy": len(issues) == 0,
            "issues": issues,
            "registered_jobs": len(self._jobs),
        }


# ==================== 便捷函数 ====================

_default_scheduler: Optional[JobScheduler] = None
_scheduler_lock = threading.Lock()


def get_default_scheduler() -> JobScheduler:
    global _default_scheduler
    if _default_scheduler is None:
        with _scheduler_lock:
            if _default_scheduler is None:
                _default_scheduler = JobScheduler()
    return _default_scheduler


# ==================== CLI ====================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Job Scheduler CLI")
    parser.add_argument("--run", help="运行指定任务")
    parser.add_argument("--chain", nargs="+", help="运行任务链")
    parser.add_argument("--due", action="store_true", help="运行到期任务")
    parser.add_argument("--stats", action="store_true", help="统计")
    parser.add_argument("--health", action="store_true", help="健康检查")
    parser.add_argument("--history", help="查看任务历史")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    js = get_default_scheduler()

    # 注册默认任务（与现有 config/scheduler.py 兼容）
    js.register_job("heat_decay", "heat_decay.py", cron="0 11 * * *",
                    description="热力衰减")
    js.register_job("cold_demotion", "cold_demotion.py", cron="0 13 * * *",
                    description="冷降级", depends_on=["heat_decay"])

    if args.run:
        result = js.run_job(args.run)
        print(f"[{result.status.value}] {result.job_name} 耗时 {result.duration_ms}ms")
        if result.error_message:
            print(f"错误: {result.error_message}")
        return

    if args.chain:
        results = js.run_job_chain(args.chain)
        for r in results:
            print(f"[{r.status.value}] {r.job_name}")
        return

    if args.due:
        results = js.run_due_jobs()
        print(f"执行 {len(results)} 个到期任务")
        for r in results:
            print(f"  [{r.status.value}] {r.job_name}")
        return

    if args.stats:
        print(json.dumps(js.get_stats(args.days), indent=2, ensure_ascii=False))
        return

    if args.health:
        health = js.health_check()
        print(f"健康: {'✅' if health['healthy'] else '❌'}")
        for issue in health["issues"]:
            print(f"  ⚠️ {issue}")
        return

    if args.history:
        runs = js.get_run_history(args.history, limit=10)
        for r in runs:
            status_icon = "✅" if r.status == JobStatus.SUCCESS else "❌"
            print(f"{status_icon} {r.started_at.strftime('%m-%d %H:%M')} | "
                  f"{r.status.value} | {r.duration_ms}ms | attempt={r.attempt}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
