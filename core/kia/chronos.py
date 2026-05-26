"""
Chronos — 时间之神 — KIA 步骤调度中心

事件驱动 + 按需触发 + 并行执行架构（ADR-020）。

16 个步骤按触发方式分为：
- 事件触发（实时响应，不经调度器）：connect_worker, iteration_tracker, task_classifier, aegis
- 定时触发（按 cron 节奏运行）：immune, dna, entropy, profile, capsule, dark_knowledge, shadow_page, stress_test
- 条件触发（满足条件才执行）：skill_flywheel
- 被动调用（工具函数）：time_parser
- 调度中心自身：knowledge_sched

同时保留原有任务调度/提醒功能（ScheduledTask）。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ============================================================
# 原有任务调度/提醒功能（保留）
# ============================================================

@dataclass
class ScheduledTask:
    """调度任务"""
    task_id: str
    task_type: str
    subtype: str
    due_date: str
    reminder_date: str
    is_periodic: bool
    period: Optional[str]
    status: str
    context: str
    created_at: str
    reminded_at: Optional[str] = None


# ============================================================
# Trigger 类层次
# ============================================================

class Trigger:
    """触发条件基类"""

    def is_due(self) -> bool:
        raise NotImplementedError

    def update_last_run(self, ts: Optional[str] = None) -> None:
        pass

    def describe(self) -> str:
        return self.__class__.__name__


class CronTrigger(Trigger):
    """Cron 表达式触发器"""

    def __init__(self, cron: str, last_run: Optional[str] = None):
        self.cron = cron
        self._last_run: Optional[datetime] = (
            datetime.fromisoformat(last_run) if last_run else None
        )
        parts = cron.split()
        self.minute = parts[0] if len(parts) > 0 else "*"
        self.hour = parts[1] if len(parts) > 1 else "*"
        self.day_of_month = parts[2] if len(parts) > 2 else "*"
        self.month = parts[3] if len(parts) > 3 else "*"
        self.day_of_week = parts[4] if len(parts) > 4 else "*"

    def is_due(self) -> bool:
        now = datetime.now()
        if self._last_run:
            # 同一分钟内不重复触发
            if (now - self._last_run).total_seconds() < 60:
                return False

        return self._matches(now)

    def _matches(self, now: datetime) -> bool:
        if not self._field_matches(self.minute, now.minute, 0, 59):
            return False
        if not self._field_matches(self.hour, now.hour, 0, 23):
            return False
        if not self._field_matches(self.day_of_month, now.day, 1, 31):
            return False
        if not self._field_matches(self.month, now.month, 1, 12):
            return False
        if not self._field_matches(self.day_of_week, now.weekday() + 1, 0, 7):
            return False
        return True

    @staticmethod
    def _field_matches(field: str, value: int, min_val: int, max_val: int) -> bool:
        if field == "*":
            return True
        # 简单步长：*/N
        if field.startswith("*/"):
            step = int(field[2:])
            return value % step == 0
        # 枚举：1,3,5
        if "," in field:
            return value in [int(x) for x in field.split(",")]
        # 范围：1-5
        if "-" in field:
            lo, hi = field.split("-", 1)
            return int(lo) <= value <= int(hi)
        # 精确值
        try:
            return value == int(field)
        except ValueError:
            return False

    def update_last_run(self, ts: Optional[str] = None) -> None:
        self._last_run = datetime.fromisoformat(ts) if ts else datetime.now()

    def describe(self) -> str:
        return f"cron:{self.cron}"


class EventTrigger(Trigger):
    """事件触发器 — 由事件总线直接调用，不经调度器 tick"""

    def __init__(self, event_type: str):
        self.event_type = event_type

    def is_due(self) -> bool:
        return False  # 事件触发步骤不参与 tick 调度

    def describe(self) -> str:
        return f"event:{self.event_type}"


class ConditionTrigger(Trigger):
    """条件触发器 — 检查 predicate 是否满足"""

    def __init__(self, predicate: Callable[[], bool], description: str = ""):
        self.predicate = predicate
        self._description = description

    def is_due(self) -> bool:
        try:
            return self.predicate()
        except Exception:
            return False

    def describe(self) -> str:
        return f"condition:{self._description}"


class PassiveTrigger(Trigger):
    """被动触发器 — 永远不主动触发"""

    def is_due(self) -> bool:
        return False

    def describe(self) -> str:
        return "passive"


# ============================================================
# ScheduledStep
# ============================================================

@dataclass
class ScheduledStep:
    """KIA 调度步骤定义"""
    name: str
    func: Callable[[], Dict]
    trigger: Trigger
    deps: List[str] = field(default_factory=list)
    timeout: int = 300
    enabled: bool = True
    consecutive_failures: int = 0


# ============================================================
# KnowledgeScheduler — KIA 步骤调度中心
# ============================================================

class KnowledgeScheduler:
    """
    KIA 步骤调度中心。

    职责：
    1. 注册所有 KIA 步骤及其触发条件
    2. 按触发条件筛选待执行步骤
    3. 处理依赖关系（拓扑排序）
    4. 无依赖步骤并行执行，有依赖步骤串行等待
    5. 记录执行日志和性能指标
    6. 连续 3 次失败自动禁用步骤

    注意：本调度器只管理**定时触发**和**条件触发**的步骤。
    **事件触发**的步骤由事件总线直接调用，不经过调度器。
    """

    MAX_CONSECUTIVE_FAILURES = 3

    def __init__(self, max_workers: int = 4, db_path: Optional[str] = None):
        # 调度器
        self.steps: Dict[str, ScheduledStep] = {}
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self._results: Dict[str, Dict] = {}
        self._lock = threading.Lock()

        # 任务调度/提醒（原有功能）
        if db_path:
            self.DB_PATH = Path(db_path).expanduser()
        else:
            from core.config import get_config
            self.DB_PATH = get_config().data_dir / "live_sync.db"
        self._init_task_db()

    # ----------------------------------------------------------
    # 任务调度/提醒数据库（原有功能）
    # ----------------------------------------------------------

    def _init_task_db(self):
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_scheduled_tasks (
                    task_id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    subtype TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    reminder_date TEXT NOT NULL,
                    is_periodic INTEGER DEFAULT 0,
                    period TEXT,
                    status TEXT DEFAULT 'pending',
                    context TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reminded_at TIMESTAMP,
                    completed_at TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_kst_status
                ON knowledge_scheduled_tasks(status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_kst_reminder
                ON knowledge_scheduled_tasks(reminder_date)
            """)
            # 步骤执行日志
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scheduler_step_log (
                    step_name TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    duration_sec REAL,
                    status TEXT NOT NULL,
                    error TEXT,
                    PRIMARY KEY (step_name, started_at)
                )
            """)

    # ----------------------------------------------------------
    # 步骤注册
    # ----------------------------------------------------------

    def register(self, step: ScheduledStep) -> None:
        self.steps[step.name] = step
        logger.debug(f"注册步骤: {step.name} ({step.trigger.describe()})")

    def register_all_default_steps(self, wiki_base: Optional[str] = None) -> None:
        """注册 ADR-020 定义的 16 个默认步骤"""
        if wiki_base is None:
            from core.config import get_config
            wiki_base = str(get_config().wiki_dir)

        # --- 定时触发步骤 ---
        self.register(ScheduledStep(
            name="knowledge_immune",
            func=lambda: self._run_kia_module("hygieia", "KnowledgeImmuneSystem", "full_scan", wiki_base=wiki_base),
            trigger=CronTrigger("0 2 * * *"),
            timeout=300,
        ))
        self.register(ScheduledStep(
            name="knowledge_dna",
            func=lambda: self._run_kia_module("genos", "DNAEngine", "compute_and_save_all", wiki_base=wiki_base),
            trigger=CronTrigger("0 3 * * *"),
            timeout=300,
        ))
        self.register(ScheduledStep(
            name="entropy_engine",
            func=lambda: self._run_kia_module("eris", "EntropyEngine", "scan", wiki_base=wiki_base),
            trigger=CronTrigger("0 4 * * *"),
            timeout=300,
        ))
        self.register(ScheduledStep(
            name="knowledge_profile",
            func=lambda: self._run_kia_module("metis", "ProfileGenerator", "generate_and_report", wiki_base=wiki_base),
            trigger=CronTrigger("0 5 * * *"),
            timeout=300,
        ))
        self.register(ScheduledStep(
            name="time_capsule",
            func=lambda: self._run_kia_module("aion", "TimeCapsule", "scan_for_auto_reminders", wiki_base=wiki_base),
            trigger=CronTrigger("0 * * * *"),
            timeout=60,
        ))
        self.register(ScheduledStep(
            name="dark_knowledge",
            func=lambda: self._run_kia_module("erebus", "DarkKnowledgeMiner", "mine_all", wiki_base=wiki_base),
            trigger=CronTrigger("0 6 * * 0"),
            timeout=600,
        ))
        self.register(ScheduledStep(
            name="shadow_page",
            func=lambda: self._run_kia_module("hecate", "ShadowPageManager", "sync_all_inbox", wiki_base=wiki_base),
            trigger=CronTrigger("0 7 * * 0"),
            timeout=600,
        ))
        self.register(ScheduledStep(
            name="stress_test",
            func=lambda: self._run_kia_module("stress_test", "StressTestEngine", "batch_test", wiki_base=wiki_base),
            trigger=CronTrigger("0 8 * * 0"),
            timeout=600,
        ))

        # --- 条件触发步骤 ---
        self.register(ScheduledStep(
            name="skill_flywheel",
            func=lambda: self._run_kia_module("ixion", "SkillFlywheel", "run_cycle", wiki_base=wiki_base),
            trigger=ConditionTrigger(
                predicate=self._flywheel_predicate,
                description="profile_signals>=50",
            ),
            deps=["knowledge_profile"],
            timeout=300,
        ))

        # --- 事件触发步骤（注册但不参与 tick） ---
        self.register(ScheduledStep(
            name="connect_worker",
            func=lambda: {"status": "event_only"},
            trigger=EventTrigger("page.created"),
        ))
        self.register(ScheduledStep(
            name="iteration_tracker",
            func=lambda: {"status": "event_only"},
            trigger=EventTrigger("page.modified"),
        ))
        self.register(ScheduledStep(
            name="task_classifier",
            func=lambda: {"status": "event_only"},
            trigger=EventTrigger("session.start"),
        ))
        self.register(ScheduledStep(
            name="kia_guard",
            func=lambda: {"status": "event_only"},
            trigger=EventTrigger("message.exchanged"),
        ))

        # --- 被动调用步骤 ---
        self.register(ScheduledStep(
            name="time_parser",
            func=lambda: {"status": "passive"},
            trigger=PassiveTrigger(),
        ))

        # --- 调度中心自身 ---
        self.register(ScheduledStep(
            name="knowledge_sched",
            func=self._run_sched_maintenance,
            trigger=CronTrigger("*/5 * * * *"),
            timeout=60,
        ))

        # --- 强制复盘检查 ---
        self.register(ScheduledStep(
            name="forced_retrospective",
            func=self._run_forced_retrospective,
            trigger=CronTrigger("*/5 * * * *"),
            timeout=30,
        ))

        # --- ScorerV2 训练队列检查（每小时，数据积累阶段） ---
        self.register(ScheduledStep(
            name="scorer_training",
            func=self._run_scorer_training,
            trigger=CronTrigger("0 * * * *"),
            timeout=60,
        ))

    def _flywheel_predicate(self) -> bool:
        """skill_flywheel 条件：画像信号数 >= 50"""
        try:
            from core.persona.psyche import get_signal_store
            stats = get_signal_store().get_signal_stats(days=90)
            total = sum(v for v in stats.values() if v > 0)
            return total >= 50
        except Exception:
            return False

    def _run_kia_module(self, module_name: str, class_name: str,
                        method_name: str, wiki_base: str = None) -> Dict:
        """通用 KIA 模块执行器"""
        try:
            import importlib
            mod = importlib.import_module(f"core.kia.{module_name}")
            cls = getattr(mod, class_name)
            instance = cls(wiki_base=wiki_base)
            method = getattr(instance, method_name)
            result = method()
            if isinstance(result, dict):
                return result
            return {"status": "ok", "result": str(result)}
        except Exception as e:
            logger.error(f"KIA 模块执行失败 {module_name}.{class_name}.{method_name}: {e}")
            return {"status": "error", "error": str(e)}

    def _run_sched_maintenance(self) -> Dict:
        """调度器自身维护：清理过期任务、检查步骤健康"""
        self.cleanup_old_tasks()
        return {"status": "ok", "steps_registered": len(self.steps)}

    def _run_forced_retrospective(self) -> Dict:
        """强制复盘检查：到期预约直接打开 Obsidian，系统提醒走权重"""
        try:
            from core.app.forced_retrospective import ForcedRetrospective
            fr = ForcedRetrospective()
            decisions = fr.check_due_reminders()
            forced = sum(1 for d in decisions if d.should_force_open)
            reminded = sum(1 for d in decisions if not d.should_force_open)
            return {
                "status": "ok",
                "forced_open": forced,
                "dialog_reminder": reminded,
            }
        except Exception as e:
            logger.error(f"强制复盘检查失败: {e}")
            return {"status": "error", "error": str(e)}

    def _run_scorer_training(self) -> Dict:
        """ScorerV2 训练队列检查：每小时检查是否有足够数据开始训练"""
        try:
            from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2
            scorer = AdaptiveScorerV2(domain="mnemos")
            trained = scorer.process_training_queue()
            status = scorer.get_status()
            return {
                "status": "ok",
                "trained_samples": trained,
                "ready_samples": status.get("ready_samples", 0),
                "mode": status.get("mode", "unknown"),
                "note": "数据积累阶段，算法实现待 ready_samples >= 20 后启动",
            }
        except Exception as e:
            logger.error(f"ScorerV2 训练检查失败: {e}")
            return {"status": "error", "error": str(e)}

    # ----------------------------------------------------------
    # tick — 调度器主循环
    # ----------------------------------------------------------

    def tick(self) -> Dict[str, Dict]:
        """
        一次调度 tick。

        1. 筛选满足触发条件的步骤
        2. 拓扑排序（依赖优先）
        3. 无依赖步骤并行执行
        4. 有依赖步骤串行等待
        5. 记录结果、处理失败

        Returns:
            {step_name: result_dict}
        """
        # 1. 筛选满足触发条件的步骤（排除事件触发和被动触发）
        ready = [
            s for s in self.steps.values()
            if s.enabled and s.trigger.is_due()
        ]

        if not ready:
            return {}

        logger.info(f"调度 tick: {len(ready)} 个步骤待执行")

        # 2. 拓扑排序
        ordered = self._topological_sort(ready)

        # 3. 分离无依赖和有依赖
        parallel = [s for s in ordered if not s.deps]
        sequential = [s for s in ordered if s.deps]

        results: Dict[str, Dict] = {}

        # 4. 并行执行无依赖步骤
        if parallel:
            futures = {
                self.executor.submit(self._run_step, step): step
                for step in parallel
            }
            for future in as_completed(futures):
                step = futures[future]
                try:
                    results[step.name] = future.result(timeout=step.timeout)
                except Exception as e:
                    results[step.name] = {"status": "error", "error": str(e)}
                    self._handle_step_failure(step, e)

        # 5. 串行执行有依赖步骤
        for step in sequential:
            deps_ok = all(d in results and results[d].get("status") != "error" for d in step.deps)
            if not deps_ok:
                results[step.name] = {
                    "status": "skipped",
                    "reason": "dependencies_not_met",
                }
                continue
            try:
                results[step.name] = self._run_step(step)
            except Exception as e:
                results[step.name] = {"status": "error", "error": str(e)}
                self._handle_step_failure(step, e)

        # 6. 更新全局结果缓存
        with self._lock:
            self._results.update(results)

        return results

    def _run_step(self, step: ScheduledStep) -> Dict:
        """执行单个步骤，包装日志和计时"""
        start = datetime.now()
        try:
            result = step.func()
            if not isinstance(result, dict):
                result = {"status": "ok", "result": str(result)}
            result["_meta"] = {
                "duration_sec": (datetime.now() - start).total_seconds(),
                "timestamp": start.isoformat(),
            }
            # 成功则重置失败计数
            step.consecutive_failures = 0
            step.trigger.update_last_run()
            logger.info(f"步骤 {step.name} 完成 ({result.get('status')}), "
                       f"耗时 {result['_meta']['duration_sec']:.1f}s")
            # 记录到 SQLite
            self._log_step_execution(step.name, start, result)
            return result
        except Exception as e:
            duration = (datetime.now() - start).total_seconds()
            self._handle_step_failure(step, e)
            return {
                "status": "error",
                "error": str(e),
                "_meta": {
                    "duration_sec": duration,
                    "timestamp": start.isoformat(),
                },
            }

    def _handle_step_failure(self, step: ScheduledStep, error: Exception) -> None:
        """处理步骤失败：累计失败计数，3 次后自动禁用"""
        step.consecutive_failures += 1
        logger.warning(f"步骤 {step.name} 失败 ({step.consecutive_failures}/{self.MAX_CONSECUTIVE_FAILURES}): {error}")

        if step.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            step.enabled = False
            logger.error(f"步骤 {step.name} 连续 {self.MAX_CONSECUTIVE_FAILURES} 次失败，已自动禁用")

    def _log_step_execution(self, step_name: str, started_at: datetime, result: Dict) -> None:
        """记录步骤执行日志到 SQLite"""
        try:
            with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO scheduler_step_log
                    (step_name, started_at, duration_sec, status, error)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    step_name,
                    started_at.isoformat(),
                    result.get("_meta", {}).get("duration_sec", 0),
                    result.get("status", "unknown"),
                    result.get("error"),
                ))
        except Exception as e:
            logger.debug(f"步骤日志写入失败: {e}")

    def _topological_sort(self, steps: List[ScheduledStep]) -> List[ScheduledStep]:
        """拓扑排序：确保依赖步骤先执行"""
        step_map = {s.name: s for s in steps}
        visited = set()
        result = []

        def visit(name: str):
            if name in visited:
                return
            visited.add(name)
            step = step_map.get(name)
            if step and step.deps:
                for dep in step.deps:
                    visit(dep)
            if step is not None:
                result.append(step)

        for s in steps:
            visit(s.name)

        return result

    # ----------------------------------------------------------
    # 事件触发入口
    # ----------------------------------------------------------

    def trigger_event(self, event_type: str, payload: Dict = None) -> Dict:
        """
        事件触发入口 — 由事件总线调用。

        根据事件类型直接调用对应的 KIA 模块。
        """
        payload = payload or {}
        event_step_map = {
            "page.created": ("charon", "ConnectWorker", "process_page"),
            "page.modified": ("proteus", "IterationTracker", "record_change"),
            "session.start": ("dike", "TaskClassifier", "classify"),
            "message.exchanged": ("aegis", "KIAGuard", "check_message"),
        }

        if event_type not in event_step_map:
            return {"status": "unknown_event", "event_type": event_type}

        module_name, class_name, method_name = event_step_map[event_type]
        step = self.steps.get(event_step_map[event_type][0].replace("_", ""), None)

        try:
            import importlib
            from core.config import get_config
            mod = importlib.import_module(f"core.kia.{module_name}")
            cls = getattr(mod, class_name)
            instance = cls(wiki_base=str(get_config().wiki_dir))
            method = getattr(instance, method_name)

            # 根据事件类型传递参数
            if event_type == "page.created":
                result = method(payload.get("page_path", ""))
            elif event_type == "page.modified":
                result = method(payload.get("page_path", ""))
            elif event_type == "session.start":
                result = method(payload.get("user_message", ""))
            elif event_type == "message.exchanged":
                result = method(payload.get("message", ""), payload.get("context", ""))
            else:
                result = method()

            if isinstance(result, dict):
                return result
            return {"status": "ok", "result": str(result)}

        except Exception as e:
            logger.error(f"事件触发执行失败 {event_type}: {e}")
            return {"status": "error", "error": str(e)}

    # ----------------------------------------------------------
    # 步骤管理
    # ----------------------------------------------------------

    def enable_step(self, name: str) -> bool:
        step = self.steps.get(name)
        if step:
            step.enabled = True
            step.consecutive_failures = 0
            return True
        return False

    def disable_step(self, name: str) -> bool:
        step = self.steps.get(name)
        if step:
            step.enabled = False
            return True
        return False

    def get_step_status(self) -> Dict[str, Dict]:
        """获取所有步骤状态"""
        status = {}
        for name, step in self.steps.items():
            status[name] = {
                "trigger": step.trigger.describe(),
                "enabled": step.enabled,
                "consecutive_failures": step.consecutive_failures,
                "timeout": step.timeout,
                "deps": step.deps,
            }
        return status

    def get_last_results(self) -> Dict[str, Dict]:
        with self._lock:
            return dict(self._results)

    # ----------------------------------------------------------
    # 原有任务调度/提醒功能（完整保留）
    # ----------------------------------------------------------

    def schedule(self, task_type: str, subtype: str,
                 due_date: datetime, context: str = "",
                 is_periodic: bool = False, period: Optional[str] = None) -> str:
        task_id = f"{task_type}-{subtype}-{due_date.strftime('%Y%m%d')}"
        days_until = (due_date - datetime.now()).days
        if days_until <= 7:
            reminder_days = 1
        elif days_until <= 30:
            reminder_days = 3
        else:
            reminder_days = 7
        reminder_date = due_date - timedelta(days=reminder_days)

        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO knowledge_scheduled_tasks
                (task_id, task_type, subtype, due_date, reminder_date,
                 is_periodic, period, status, context, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (
                task_id, task_type, subtype,
                due_date.isoformat(), reminder_date.isoformat(),
                1 if is_periodic else 0, period,
                context, datetime.now().isoformat()
            ))
        return task_id

    def get_pending_reminders(self) -> List[ScheduledTask]:
        now = datetime.now().isoformat()
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            cursor = conn.execute("""
                SELECT task_id, task_type, subtype, due_date, reminder_date,
                       is_periodic, period, status, context, created_at, reminded_at
                FROM knowledge_scheduled_tasks
                WHERE status = 'pending'
                  AND reminder_date <= ?
                ORDER BY reminder_date ASC
            """, (now,))
            return [self._row_to_task(row) for row in cursor.fetchall()]

    def mark_reminded(self, task_id: str):
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("""
                UPDATE knowledge_scheduled_tasks
                SET status = 'reminded', reminded_at = ?
                WHERE task_id = ?
            """, (datetime.now().isoformat(), task_id))

    def mark_completed(self, task_id: str):
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("""
                UPDATE knowledge_scheduled_tasks
                SET status = 'completed', completed_at = ?
                WHERE task_id = ?
            """, (datetime.now().isoformat(), task_id))

    def cancel(self, task_id: str):
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("""
                UPDATE knowledge_scheduled_tasks
                SET status = 'cancelled'
                WHERE task_id = ?
            """, (task_id,))

    def startup_compensation(self) -> List[ScheduledTask]:
        now = datetime.now().isoformat()
        missed = []

        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            cursor = conn.execute("""
                SELECT task_id, task_type, subtype, due_date, reminder_date,
                       is_periodic, period, status, context, created_at, reminded_at
                FROM knowledge_scheduled_tasks
                WHERE status = 'pending'
                  AND reminder_date <= ?
                ORDER BY reminder_date ASC
            """, (now,))
            missed.extend(self._row_to_task(row) for row in cursor.fetchall())

            three_days_ago = (datetime.now() - timedelta(days=3)).isoformat()
            cursor = conn.execute("""
                SELECT task_id, task_type, subtype, due_date, reminder_date,
                       is_periodic, period, status, context, created_at, reminded_at
                FROM knowledge_scheduled_tasks
                WHERE status = 'reminded'
                  AND reminded_at <= ?
                ORDER BY reminded_at ASC
            """, (three_days_ago,))
            missed.extend(self._row_to_task(row) for row in cursor.fetchall())

        return missed

    def format_reminder(self, task: ScheduledTask) -> str:
        due = datetime.fromisoformat(task.due_date.replace('Z', '+00:00'))
        days_until = (due - datetime.now()).days
        lines = [
            f"**任务提醒**",
            f"",
            f"类型：{task.task_type}/{task.subtype}",
            f"执行日期：{task.due_date[:10]}（还有 {days_until} 天）",
        ]
        if task.is_periodic:
            lines.append(f"周期：{task.period}")
        if task.context:
            lines.append(f"上下文：{task.context}")
        lines.append("")
        lines.append("知识库已装载相关经验，请查看。")
        return "\n".join(lines)

    def list_all(self, status: Optional[str] = None) -> List[ScheduledTask]:
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            if status:
                cursor = conn.execute("""
                    SELECT task_id, task_type, subtype, due_date, reminder_date,
                           is_periodic, period, status, context, created_at, reminded_at
                    FROM knowledge_scheduled_tasks
                    WHERE status = ?
                    ORDER BY due_date ASC
                """, (status,))
            else:
                cursor = conn.execute("""
                    SELECT task_id, task_type, subtype, due_date, reminder_date,
                           is_periodic, period, status, context, created_at, reminded_at
                    FROM knowledge_scheduled_tasks
                    ORDER BY due_date ASC
                """)
            return [self._row_to_task(row) for row in cursor.fetchall()]

    def cleanup_old_tasks(self, days: int = 30):
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("""
                DELETE FROM knowledge_scheduled_tasks
                WHERE status IN ('completed', 'cancelled')
                  AND completed_at <= ?
            """, (cutoff,))

    def _row_to_task(self, row) -> ScheduledTask:
        return ScheduledTask(
            task_id=row[0],
            task_type=row[1],
            subtype=row[2],
            due_date=row[3],
            reminder_date=row[4],
            is_periodic=bool(row[5]),
            period=row[6],
            status=row[7],
            context=row[8],
            created_at=row[9],
            reminded_at=row[10],
        )


# ============================================================
# 便捷函数
# ============================================================

def schedule_task(task_type: str, subtype: str,
                  due_date: datetime, context: str = "",
                  is_periodic: bool = False, period: Optional[str] = None) -> str:
    scheduler = KnowledgeScheduler()
    return scheduler.schedule(task_type, subtype, due_date, context, is_periodic, period)


def check_reminders() -> List[ScheduledTask]:
    scheduler = KnowledgeScheduler()
    return scheduler.get_pending_reminders()
