"""Dependency-light registration and event helpers for Chronos."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, List, Optional

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 600
TIMEOUT_SECONDS_2 = 30


class SchedulerSupportMixin:
    """Registration, step-state, and lightweight event operations."""

    # Structural contract supplied by ``KnowledgeScheduler``.  These are
    # annotations only: runtime dispatch still resolves to the concrete host.
    DB_PATH: Path
    steps: Dict[str, Any]
    _results: Dict[str, Dict[str, Any]]
    _lock: Any
    MAX_CONSECUTIVE_FAILURES: ClassVar[int]
    EVENT_TRIGGER_ROUTES: ClassVar[tuple[tuple[str, str], ...]]
    _scheduled_step_type: ClassVar[type[Any]]
    _cron_trigger_type: ClassVar[type[Any]]
    _condition_trigger_type: ClassVar[type[Any]]
    _event_trigger_type: ClassVar[type[Any]]
    _run_kia_module: Callable[..., Dict[str, Any]]
    _run_graph_build: Callable[[str], Dict[str, Any]]
    _run_heat_map: Callable[[str], Dict[str, Any]]
    _run_knowledge_profile: Callable[[str], Dict[str, Any]]
    _run_skill_flywheel: Callable[[str], Dict[str, Any]]
    _flywheel_predicate: Callable[[], bool]
    _run_connect_worker: Callable[[str], Dict[str, Any]]
    _run_knowledge_evolution: Callable[[str], Dict[str, Any]]
    _register_event_handlers: Callable[[], None]
    trigger_event: Callable[[str, Dict[str, Any]], Dict[str, Any]]

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
                    priority INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reminded_at TIMESTAMP,
                    completed_at TIMESTAMP
                )
            """)
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(knowledge_scheduled_tasks)")
            }
            if "priority" not in columns:
                conn.execute(
                    "ALTER TABLE knowledge_scheduled_tasks ADD COLUMN priority INTEGER DEFAULT 0"
                )
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_kst_status
                ON knowledge_scheduled_tasks(status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_kst_reminder
                ON knowledge_scheduled_tasks(reminder_date)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_kst_priority_reminder
                ON knowledge_scheduled_tasks(status, priority, reminder_date)
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
            # 步骤状态（持久化 last_run）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scheduler_step_state (
                    step_name TEXT PRIMARY KEY,
                    last_run TEXT
                )
            """)
            # Exactly-once execution intent/result journal.  An ``executing``
            # row is deliberately terminalized as an unknown-outcome dead
            # letter after restart; Chronos never guesses that an arbitrary
            # step is safe to invoke twice.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scheduler_step_effects (
                    effect_id TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL UNIQUE,
                    step_name TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    before_hash TEXT NOT NULL,
                    after_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('executing', 'committed', 'failed_terminal')
                    ),
                    reason_code TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL,
                    result_hash TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                )
            """)

    def register(self, step: Any) -> None:
        self.steps[step.name] = step
        # 恢复 last_run
        try:
            with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
                row = conn.execute(
                    "SELECT last_run FROM scheduler_step_state WHERE step_name = ?", (step.name,)
                ).fetchone()
                if row and row[0] and isinstance(step.trigger, self._cron_trigger_type):
                    step.trigger.update_last_run(row[0])
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.warning("恢复步骤 %s 上次运行时间失败", step.name, exc_info=True)
        logger.debug("注册步骤: %s (%s)", step.name, step.trigger.describe())

    def register_all_default_steps(
        self, wiki_base: Optional[str] = None, *, include_heavy_steps: bool = True
    ) -> None:
        """注册 ADR-020 定义的 16 个默认步骤"""
        ScheduledStep = self._scheduled_step_type
        CronTrigger = self._cron_trigger_type
        ConditionTrigger = self._condition_trigger_type
        if wiki_base is None:
            from core.config import get_config

            wiki_base = str(get_config().wiki_dir)

        # --- 定时触发步骤 ---
        self.register(
            ScheduledStep(
                name="knowledge_immune",
                func=lambda: self._run_kia_module(
                    "hygieia", "KnowledgeImmuneSystem", "full_scan", wiki_base=wiki_base
                ),
                trigger=CronTrigger("30 10 * * *"),  # 每天 10:30
                timeout=300,
            )
        )
        self.register(
            ScheduledStep(
                name="knowledge_dna",
                func=lambda: self._run_kia_module(
                    "genos", "DNAEngine", "scan_all_pages", wiki_base=wiki_base
                ),
                trigger=CronTrigger("0 11 * * *"),  # 每天 11:00
                timeout=300,
            )
        )
        self.register(
            ScheduledStep(
                name="graph_build",
                func=lambda: self._run_graph_build(wiki_base),
                trigger=CronTrigger("30 11 * * *"),  # 每天 11:30
                timeout=300,
                enabled=include_heavy_steps,
            )
        )
        self.register(
            ScheduledStep(
                name="entropy_engine",
                func=lambda: self._run_kia_module(
                    "eris", "EntropyEngine", "scan", wiki_base=wiki_base
                ),
                trigger=CronTrigger("0 12 * * *"),  # 每天 12:00
                timeout=300,
            )
        )
        self.register(
            ScheduledStep(
                name="heat_map",
                func=lambda: self._run_heat_map(wiki_base),
                trigger=CronTrigger("30 12 * * *"),  # 每天 12:30
                timeout=300,
            )
        )
        self.register(
            ScheduledStep(
                name="knowledge_profile",
                func=lambda: self._run_knowledge_profile(wiki_base),
                trigger=CronTrigger("0 13 * * *"),  # 每天 13:00
                timeout=300,
            )
        )
        self.register(
            ScheduledStep(
                name="time_capsule",
                func=lambda: self._run_kia_module(
                    "aion", "TimeCapsule", "scan_for_auto_reminders", wiki_base=wiki_base
                ),
                trigger=CronTrigger("0 20 * * *"),  # 每天 20:00
                timeout=60,
            )
        )
        self.register(
            ScheduledStep(
                name="shadow_page",
                func=lambda: self._run_kia_module(
                    "hecate", "ShadowPageManager", "batch_sync", wiki_base=wiki_base
                ),
                trigger=CronTrigger("30 13 * * *"),  # 每天 13:30
                timeout=TIMEOUT_SECONDS,
            )
        )
        self.register(
            ScheduledStep(
                name="stress_test",
                func=lambda: self._run_kia_module(
                    "stress_test", "StressTestEngine", "batch_test", wiki_base=wiki_base
                ),
                trigger=CronTrigger("0 14 * * *"),  # 每天 14:00
                timeout=TIMEOUT_SECONDS,
                enabled=include_heavy_steps,
            )
        )

        # --- 条件触发步骤 ---
        self.register(
            ScheduledStep(
                name="skill_flywheel",
                func=lambda: self._run_skill_flywheel(wiki_base),
                trigger=ConditionTrigger(
                    predicate=self._flywheel_predicate,
                    description="profile_signals>=50",
                ),
                deps=["knowledge_profile"],
                timeout=300,
            )
        )

        # --- 定时构图步骤（MOC + 实体页面全量更新） ---
        self.register(
            ScheduledStep(
                name="connect_worker",
                func=lambda: self._run_connect_worker(wiki_base),
                trigger=CronTrigger("30 14 * * *"),  # 每天 14:30
                timeout=TIMEOUT_SECONDS,
                enabled=include_heavy_steps,
            )
        )
        self.register(
            ScheduledStep(
                name="knowledge_evolution",
                func=lambda: self._run_knowledge_evolution(wiki_base),
                trigger=CronTrigger("0 15 * * *"),  # 每天 15:00
                timeout=300,
                enabled=include_heavy_steps,
            )
        )

        # --- 事件触发步骤（注册 EventBus 消费者） ---
        self._register_event_steps()
        self._register_event_handlers()

    def _register_event_steps(self) -> None:
        """将事件触发入口纳入 scheduler step registry。"""
        EventTrigger = self._event_trigger_type
        ScheduledStep = self._scheduled_step_type
        for step_name, event_type in self.EVENT_TRIGGER_ROUTES:
            def run_event_step(event_type: str = event_type) -> Dict:
                return self.trigger_event(event_type, {})

            self.register(
                ScheduledStep(
                    name=step_name,
                    func=run_event_step,
                    trigger=EventTrigger(event_type),
                    timeout=TIMEOUT_SECONDS_2,
                )
            )

    def _event_trigger_routes(self) -> List[tuple[str, str]]:
        routes = [
            (name, step.trigger.event_type)
            for name, step in self.steps.items()
            if isinstance(step.trigger, self._event_trigger_type)
        ]
        return routes or list(self.EVENT_TRIGGER_ROUTES)

    def _handle_step_failure(self, step: Any, error: Exception) -> None:
        """处理步骤失败：累计失败计数，3 次后自动禁用"""
        step.consecutive_failures += 1
        logger.warning(
            "步骤 %s 失败 (%s/%s): %s",
            step.name,
            step.consecutive_failures,
            self.MAX_CONSECUTIVE_FAILURES,
            error,
        )

        if step.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            step.enabled = False
            logger.error(
                "步骤 %s 连续 %s 次失败，已自动禁用", step.name, self.MAX_CONSECUTIVE_FAILURES
            )

    def _topological_sort(self, steps: List[Any]) -> List[Any]:
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

    def _trigger_page_created(self, payload: Dict) -> Dict:
        """页面创建事件：当前 Charon 只暴露批量构图入口。"""
        from core.kia.charon import run_connect_cycle

        result = run_connect_cycle(dry_run=bool(payload.get("dry_run", False)))
        return {"status": "ok", "event_type": "page.created", "result": result}

    def _trigger_session_start(self, payload: Dict) -> Dict:
        """会话开始事件：按 Dike 当前契约传入 messages 列表。"""
        from core.kia.dike import TaskClassifier

        user_message = payload.get("user_message") or payload.get("message") or ""
        messages = payload.get("messages")
        if not isinstance(messages, list):
            messages = [{"role": "user", "content": str(user_message)}]

        result = TaskClassifier().classify(messages)
        data = self._normalize_event_result(result)
        data.setdefault("status", "ok")
        data["event_type"] = "session.start"
        return data

    def _trigger_message_exchanged(self, payload: Dict) -> Dict:
        """消息交换事件：按 Aegis 当前 InProcessGuard.check 契约执行。"""
        from core.kia.aegis import InProcessGuard

        guard = payload.get("guard")
        if guard is None:
            guard = InProcessGuard(payload.get("knowledge"))

        alert = guard.check(
            str(payload.get("message") or payload.get("user_message") or ""),
            str(payload.get("ai_response") or payload.get("context") or ""),
        )
        return {
            "status": "ok",
            "event_type": "message.exchanged",
            "alert": self._normalize_event_result(alert) if alert else None,
        }

    @staticmethod
    def _normalize_event_result(result) -> Dict:
        if result is None:
            return {}
        if isinstance(result, dict):
            return result
        if is_dataclass(result):
            return asdict(result)  # type: ignore[arg-type]
        return {"result": str(result)}

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
