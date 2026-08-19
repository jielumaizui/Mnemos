"""Immutable scheduling contracts, trigger types, and recovery oracles."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any, Callable, Dict, List, Mapping, Optional

from core.cognitive.decision_trace import (
    MaterialActionObservation,
    MaterialActionPermit,
)
from core.cognitive.state_contract import sha256_json
from core.sync_framework.storage_backend import StorageError

# Constants extracted from magic numbers
CRON_DOW_DAYS = 7
CRON_TRIGGER__MATCHES_CRON_DOW_DAYS = 7
TIMEOUT_SECONDS = 600
TIMEOUT_SECONDS_2 = 30
STATS_DAYS = 90
KNOWLEDGE_SCHEDULER_DURATION_BUCKET_MONTH_DAYS = 30
KNOWLEDGE_SCHEDULER_CLEANUP_OLD_TASKS_DAYS = 30
KNOWLEDGE_SCHEDULER__ROW_TO_TASK_ROW = 7
KNOWLEDGE_SCHEDULER__REMINDER_DATE_FOR_DAYS_UNTIL_DAYS = 7
KNOWLEDGE_SCHEDULER__REMINDER_DATE_FOR_DAYS_UNTIL_DAYS_2 = 30
REMINDER_DAYS = 7
KNOWLEDGE_SCHEDULER_DURATION_BUCKET_WEEK_DAYS = 7
KNOWLEDGE_SCHEDULER_DURATION_BUCKET_QUARTER_DAYS = 90
KNOWLEDGE_SCHEDULER_DURATION_BUCKET_YEAR_DAYS = 365


# ============================================================
# 原有任务调度/提醒功能（保留）
# ============================================================

logger = logging.getLogger(__name__)
CHRONOS_OPERATION_ERRORS = (
    ImportError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    sqlite3.Error,
    StorageError,
)
CHRONOS_TASK_CREATE_ACTION = "create_scheduled_task"
CHRONOS_STEP_EXECUTE_ACTION = "execute_scheduled_step"
CHRONOS_OWNER = "chronos"
CHRONOS_EXECUTOR = "knowledge_scheduler"
CHRONOS_DECISION_CONTRACT_ID = "project-contract:chronos-material-actions"
CHRONOS_DECISION_CONTRACT_REVISION = "mnemos.chronos_material_actions.v1"
CHRONOS_DECISION_CONTRACT_TEXT = (
    "Chronos may create only an exact requested reminder task and may execute "
    "only an exact enabled step registered in the active scheduler."
)
CHRONOS_DECISION_PRODUCER_HASH = sha256_json(
    {
        "module": "core.kia.chronos",
        "producer": "KnowledgeScheduler",
        "version": CHRONOS_DECISION_CONTRACT_REVISION,
    }
)
CHRONOS_STEP_SUCCESS_STATUSES = frozenset(
    {
        "ok",
        "success",
        "completed",
        "complete",
        "no_change",
        "noop",
        "skipped",
        "passive",
        "not_applicable",
    }
)


def scheduled_task_material_action_binding(
    *,
    task_type: str,
    subtype: str,
    due_date: datetime,
    context: str = "",
    is_periodic: bool = False,
    period: Optional[str] = None,
    priority: int = 0,
) -> dict[str, str]:
    """Bind one scheduled-task creation to its complete semantic payload."""

    payload = {
        "schema_version": "mnemos.scheduled_task_input.v1",
        "task_type": str(task_type),
        "subtype": str(subtype),
        "due_date": due_date.isoformat(),
        "context": str(context),
        "is_periodic": bool(is_periodic),
        "period": str(period or ""),
        "priority": int(priority),
    }
    semantic_hash = sha256_json(payload)
    return {
        "target_ref": f"scheduled-task:{semantic_hash.split(':', 1)[1][:32]}",
        "input_hash": semantic_hash,
    }


def scheduled_step_material_action_binding(step: "ScheduledStep") -> dict[str, str]:
    """Bind one scheduled-step execution to its immutable callable contract."""

    func = step.func
    callable_ref = (
        f"{getattr(func, '__module__', type(func).__module__)}:"
        f"{getattr(func, '__qualname__', type(func).__qualname__)}"
    )
    payload = {
        "schema_version": "mnemos.scheduled_step_input.v1",
        "step_name": step.name,
        "callable_ref": callable_ref,
        "trigger": step.trigger.describe(),
        "dependencies": sorted(step.deps),
        "timeout": int(step.timeout),
    }
    return {
        "target_ref": f"scheduled-step:{step.name}",
        "input_hash": sha256_json(payload),
    }


class ScheduledTaskEffectOracle:
    """Read-only recovery oracle for one exact scheduled-task row."""

    owner = CHRONOS_OWNER
    executor_id = CHRONOS_EXECUTOR
    action_type = CHRONOS_TASK_CREATE_ACTION

    def __init__(
        self,
        db_path: Path,
        *,
        task_id: str,
        expected: Mapping[str, Any],
    ):
        self.db_path = Path(db_path)
        self.task_id = str(task_id)
        self.expected = dict(expected)

    def observe(
        self,
        permit: MaterialActionPermit,
    ) -> MaterialActionObservation | None:
        """Return the exact scheduled-task row already committed for recovery."""

        with sqlite3.connect(
            f"file:{self.db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM knowledge_scheduled_tasks WHERE task_id=?",
                (self.task_id,),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        if any(data.get(key) != value for key, value in self.expected.items()):
            raise RuntimeError(
                "existing scheduled task does not match its pending material command"
            )
        after_hash = sha256_json({"scheduled_task": data})
        observed_at = datetime.fromisoformat(str(data["created_at"]))
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return MaterialActionObservation(
            status="committed",
            before_hash=sha256_json({"scheduled_task": None}),
            after_hash=after_hash,
            evidence_refs=(
                f"target-after:{after_hash}",
                f"target-oracle:chronos-task:{self.task_id}:{after_hash}",
            ),
            outcome="observed scheduled task after restart",
            observed_at=observed_at.isoformat(),
        )


class ScheduledStepEffectOracle:
    """Read one exact at-most-once scheduled-step execution journal."""

    owner = CHRONOS_OWNER
    executor_id = CHRONOS_EXECUTOR
    action_type = CHRONOS_STEP_EXECUTE_ACTION

    def __init__(
        self,
        db_path: Path,
        *,
        step_name: str,
        input_hash: str,
    ):
        self.db_path = Path(db_path)
        self.step_name = str(step_name)
        self.input_hash = str(input_hash)
        self.last_result: Dict[str, Any] | None = None

    def observe(
        self,
        permit: MaterialActionPermit,
    ) -> MaterialActionObservation | None:
        """Return the exact at-most-once step effect already committed."""

        if (
            permit.target_ref != f"scheduled-step:{self.step_name}"
            or permit.input_hash != self.input_hash
        ):
            raise PermissionError("scheduled-step oracle does not match the exact command")
        with sqlite3.connect(
            f"file:{self.db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM scheduler_step_effects WHERE effect_id=?",
                (permit.effect_id,),
            ).fetchone()
        if row is None:
            self.last_result = None
            return None
        data = dict(row)
        if (
            str(data["command_id"]) != permit.command_id
            or str(data["step_name"]) != self.step_name
            or str(data["input_hash"]) != self.input_hash
        ):
            raise RuntimeError("scheduled-step effect journal does not match its command")
        try:
            parsed_result = json.loads(str(data["result_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("scheduled-step effect result is malformed") from exc
        if not isinstance(parsed_result, dict):
            raise RuntimeError("scheduled-step effect result must be an object")
        if sha256_json(parsed_result) != str(data["result_hash"]):
            raise RuntimeError("scheduled-step effect result hash mismatch")

        status = str(data["status"])
        before_hash = str(data["before_hash"])
        after_hash = str(data["after_hash"])
        reason_code = str(data["reason_code"] or "")
        evidence = [
            f"target-oracle:{permit.effect_id}:chronos-step:{self.step_name}",
            f"target-journal:chronos-step:{permit.effect_id}",
        ]
        if status == "executing":
            # The function may or may not have reached a foreign target.  The
            # only truthful, duplicate-safe recovery is an exhausted unknown
            # outcome.  A new attempt requires a superseding decision.
            self.last_result = {
                "status": "error",
                "error": "scheduled step outcome unknown after restart",
                "reason": "crash_window_dead_letter",
                "_meta": {"timestamp": str(data["started_at"])},
            }
            return MaterialActionObservation(
                status="dead_letter",
                before_hash=before_hash,
                after_hash=before_hash,
                evidence_refs=tuple(
                    (
                        *evidence,
                        f"attempted-effect:{permit.effect_id}",
                        f"retry-budget-exhausted:{permit.command_id}",
                    )
                ),
                reason_code="chronos_step_outcome_unknown_after_crash",
                retry_exhausted=True,
                outcome="scheduled step was not replayed after an ambiguous crash",
                observed_at=str(data["started_at"]),
            )

        self.last_result = dict(parsed_result)
        if status == "committed":
            evidence.append(f"target-after:{after_hash}")
        elif status == "failed_terminal":
            evidence.append(f"attempted-effect:{permit.effect_id}")
        else:
            raise RuntimeError("unsupported scheduled-step effect status")
        return MaterialActionObservation(
            status=status,
            before_hash=before_hash,
            after_hash=after_hash,
            evidence_refs=tuple(evidence),
            reason_code=reason_code,
            outcome=f"scheduled step completed with {parsed_result.get('status')}",
            observed_at=str(data["completed_at"] or data["started_at"]),
        )


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
    priority: int = 0


# ============================================================
# Trigger 类层次
# ============================================================


class Trigger:
    """触发条件基类"""

    def is_due(self) -> bool:
        raise NotImplementedError

    def update_last_run(self, ts: Optional[str] = None) -> None:
        """子类可覆盖以持久化 last_run 时间戳（基类默认空实现）。"""

    def describe(self) -> str:
        return self.__class__.__name__


class CronTrigger(Trigger):
    """Cron 表达式触发器"""

    def __init__(self, cron: str, last_run: Optional[str] = None):
        self.cron = cron
        self._last_run: Optional[datetime] = datetime.fromisoformat(last_run) if last_run else None
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
            # 检查自上次运行以来是否错过触发时间，避免 tick 频率低于 cron 精度导致漏触发
            check = self._last_run.replace(second=0, microsecond=0) + timedelta(minutes=1)
            while check <= now:
                if self._matches(check):
                    return True
                check += timedelta(minutes=1)
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
        # Python weekday: 0=Mon..6=Sun → cron: 0=Sun,1=Mon..6=Sat
        cron_dow = (now.weekday() + 1) % CRON_DOW_DAYS
        if not self._field_matches(
            self.day_of_week, cron_dow, 0, CRON_TRIGGER__MATCHES_CRON_DOW_DAYS
        ):
            return False
        return True

    @staticmethod
    def _field_matches(field: str, value: int, min_val: int, max_val: int) -> bool:
        if not min_val <= value <= max_val:
            return False
        if field == "*":
            return True
        # 简单步长：*/N
        if field.startswith("*/"):
            try:
                step = int(field[2:])
            except ValueError:
                return False
            if step <= 0:
                return False
            return value % step == 0
        # 枚举：1,3,5
        if "," in field:
            try:
                values = [int(x) for x in field.split(",")]
            except ValueError:
                return False
            if any(item < min_val or item > max_val for item in values):
                return False
            return value in values
        # 范围：1-5
        if "-" in field:
            try:
                lo, hi = field.split("-", 1)
                lower = int(lo)
                upper = int(hi)
            except ValueError:
                return False
            if lower > upper or lower < min_val or upper > max_val:
                return False
            return lower <= value <= upper
        # 精确值
        try:
            exact = int(field)
        except ValueError:
            return False
        return min_val <= exact <= max_val and value == exact

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
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
            sqlite3.Error,
        ):
            logger.warning(
                "ConditionTrigger '%s' predicate 执行失败", self._description, exc_info=True
            )
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
