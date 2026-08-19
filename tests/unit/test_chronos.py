"""
Chronos — KnowledgeScheduler 单元测试

覆盖公共 API：
- 初始化与数据库创建
- 任务调度（schedule）
- 到期提醒获取（get_pending_reminders）
- 任务完成标记（mark_completed）
- 任务取消（cancel）
- 任务列表（list_all）
- 启动补偿（startup_compensation）
- 周期性任务（periodic tasks）
- 旧任务清理（cleanup_old_tasks）
- 提醒日期计算（_reminder_date_for）
- 任务 ID 构建（_build_task_id）
- 步骤注册与状态查询（register / get_step_status）
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.cognitive.decision_trace import MaterialActionAuthorization
from core.kia.chronos import (
    CHRONOS_EXECUTOR,
    CHRONOS_OWNER,
    CHRONOS_STEP_EXECUTE_ACTION,
    CHRONOS_TASK_CREATE_ACTION,
    CronTrigger,
    EventTrigger,
    KnowledgeScheduler,
    ScheduledStep,
    scheduled_step_material_action_binding,
    scheduled_task_material_action_binding,
)
from tests.chronos_decision_fixtures import authorized_knowledge_scheduler
from tests.cognitive_decision_fixtures import material_action_authorization

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """提供独立的临时数据库路径。"""
    return tmp_path / "test_chronos.db"


@pytest.fixture
def scheduler(tmp_db_path: Path) -> KnowledgeScheduler:
    """提供已初始化的 KnowledgeScheduler 实例。"""
    return authorized_knowledge_scheduler(db_path=tmp_db_path)


@pytest.mark.no_canonical_material_actions
def test_schedule_seals_canonical_decision(tmp_db_path: Path, fixed_now: datetime) -> None:
    scheduler = KnowledgeScheduler(db_path=str(tmp_db_path))

    task_id = scheduler.schedule(
        task_type="review",
        subtype="requested",
        due_date=fixed_now,
    )

    assert task_id
    with sqlite3.connect(tmp_db_path.parent / "producer_consumer_ledger.db") as conn:
        assert conn.execute(
            "SELECT status FROM cognitive_state_effect_receipts"
        ).fetchone() == ("committed",)


@pytest.mark.no_canonical_material_actions
def test_registered_step_seals_canonical_decision(tmp_db_path: Path) -> None:
    scheduler = KnowledgeScheduler(db_path=str(tmp_db_path))
    step = ScheduledStep(
        name="project_contract_step",
        func=lambda: {"status": "ok"},
        trigger=CronTrigger("0 9 * * *"),
    )
    scheduler.register(step)

    result = scheduler._run_step(step)

    assert result["status"] == "ok"
    with sqlite3.connect(tmp_db_path.parent / "producer_consumer_ledger.db") as conn:
        assert conn.execute(
            "SELECT status FROM cognitive_state_effect_receipts"
        ).fetchone() == ("committed",)


@pytest.mark.no_canonical_material_actions
def test_registered_step_propagates_programming_errors(tmp_db_path: Path) -> None:
    scheduler = KnowledgeScheduler(db_path=str(tmp_db_path))
    step = ScheduledStep(
        name="buggy_project_contract_step",
        func=lambda: (_ for _ in ()).throw(AssertionError("step contract bug")),
        trigger=CronTrigger("0 9 * * *"),
    )
    scheduler.register(step)

    with pytest.raises(AssertionError, match="step contract bug"):
        scheduler._run_step(step)


@pytest.mark.no_canonical_material_actions
def test_registered_step_can_run_in_two_distinct_terminal_generations(
    tmp_db_path: Path,
) -> None:
    invocations = 0

    def run_step():
        nonlocal invocations
        invocations += 1
        return {"status": "ok", "invocation": invocations}

    scheduler = KnowledgeScheduler(db_path=str(tmp_db_path))
    step = ScheduledStep(
        name="repeatable_project_contract_step",
        func=run_step,
        trigger=CronTrigger("0 9 * * *"),
    )
    scheduler.register(step)

    assert scheduler._run_step(step)["invocation"] == 1
    assert scheduler._run_step(step)["invocation"] == 2

    with sqlite3.connect(
        tmp_db_path.parent / "producer_consumer_ledger.db"
    ) as conn:
        statuses = conn.execute(
            "SELECT status FROM cognitive_state_effect_receipts ORDER BY created_at"
        ).fetchall()
    assert statuses == [("committed",), ("committed",)]


@pytest.mark.no_canonical_material_actions
def test_unregistered_step_is_rejected_before_invocation(tmp_db_path: Path) -> None:
    invoked = False

    def run():
        nonlocal invoked
        invoked = True
        return {"status": "ok"}

    scheduler = KnowledgeScheduler(db_path=str(tmp_db_path))
    step = ScheduledStep(
        name="unregistered_step",
        func=run,
        trigger=CronTrigger("0 9 * * *"),
    )

    with pytest.raises(PermissionError, match="enabled registered step"):
        scheduler._run_step(step)

    assert invoked is False


def test_schedule_recovers_crash_after_task_insert_without_duplicate(
    tmp_db_path: Path,
    fixed_now: datetime,
    monkeypatch,
) -> None:
    due_date = fixed_now + timedelta(days=2)
    binding = scheduled_task_material_action_binding(
        task_type="review",
        subtype="crash-window",
        due_date=due_date,
        context="exact-context",
    )
    authorization = material_action_authorization(
        tmp_db_path.parent,
        action_type=CHRONOS_TASK_CREATE_ACTION,
        owner=CHRONOS_OWNER,
        executor=CHRONOS_EXECUTOR,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
        nonce="chronos-task-crash-recovery",
    )
    scheduler = KnowledgeScheduler(
        db_path=str(tmp_db_path),
        material_action_resolver=lambda _: authorization,
    )
    original = MaterialActionAuthorization.record_terminal
    calls = 0

    def crash_once(self, terminal):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected crash after scheduled task insert")
        return original(self, terminal)

    monkeypatch.setattr(
        MaterialActionAuthorization,
        "record_terminal",
        crash_once,
    )
    kwargs = {
        "task_type": "review",
        "subtype": "crash-window",
        "due_date": due_date,
        "context": "exact-context",
    }
    with pytest.raises(OSError, match="after scheduled task insert"):
        scheduler.schedule(**kwargs)
    task_id = scheduler.schedule(**kwargs)

    with sqlite3.connect(str(tmp_db_path)) as conn:
        rows = conn.execute(
            "SELECT task_id FROM knowledge_scheduled_tasks"
        ).fetchall()
    assert rows == [(task_id,)]
    with sqlite3.connect(
        str(tmp_db_path.parent / "producer_consumer_ledger.db")
    ) as conn:
        receipt = conn.execute(
            "SELECT status, target_effect_id FROM cognitive_state_effect_receipts "
            "WHERE command_id=?",
            (authorization.permit.command_id,),
        ).fetchone()
    assert receipt == ("committed", authorization.permit.effect_id)


@pytest.fixture
def fixed_now() -> datetime:
    """固定当前时间，避免测试因时间流逝而 flaky。"""
    return datetime(2026, 6, 7, 12, 0, 0)


def test_register_event_handlers_uses_global_event_bus(
    scheduler: KnowledgeScheduler, monkeypatch
) -> None:
    """Chronos 事件订阅必须复用全局 EventBus，避免孤立 handler 表。"""
    subscriptions = []

    class FakeBus:
        def subscribe(self, event_type, handler, *, consumer_id):
            subscriptions.append((event_type, handler, consumer_id))

    fake_bus = FakeBus()
    monkeypatch.setattr("core.mnemos_bus.get_event_bus", lambda: fake_bus)

    scheduler._register_event_steps()
    scheduler._register_event_handlers()

    assert [event_type for event_type, _, _ in subscriptions] == [
        "page.created",
        "page.modified",
        "session.start",
        "message.exchanged",
    ]
    assert [consumer_id for _, _, consumer_id in subscriptions] == [
        "chronos:page_created",
        "chronos:page_modified",
        "chronos:session_start",
        "chronos:message_exchanged",
    ]
    assert all(
        isinstance(scheduler.steps[name].trigger, EventTrigger)
        for name in (
            "page_created",
            "page_modified",
            "session_start",
            "message_exchanged",
        )
    )


def test_disabled_event_trigger_step_skips_event_bus_handler(
    scheduler: KnowledgeScheduler, monkeypatch
) -> None:
    """事件步骤禁用后，EventBus handler 应跳过执行对应事件路由。"""
    handlers = {}

    class FakeBus:
        def subscribe(self, event_type, handler, *, consumer_id):
            assert consumer_id.startswith("chronos:")
            handlers[event_type] = handler

    monkeypatch.setattr("core.mnemos_bus.get_event_bus", lambda: FakeBus())
    scheduler._register_event_steps()
    scheduler._register_event_handlers()
    scheduler.disable_step("page_created")

    event = type("Event", (), {"payload": {"dry_run": True}})()
    result = handlers["page.created"](event)

    assert result == {
        "status": "skipped",
        "reason": "disabled",
        "event_type": "page.created",
    }


# ============================================================
# 初始化
# ============================================================


def test_init_creates_database_tables(tmp_db_path: Path) -> None:
    """初始化时应自动创建所需的数据表和索引。"""
    assert not tmp_db_path.exists()
    _ = KnowledgeScheduler(db_path=str(tmp_db_path))
    assert tmp_db_path.exists()

    with sqlite3.connect(str(tmp_db_path)) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "knowledge_scheduled_tasks" in tables
        assert "scheduler_step_log" in tables

        indexes = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "idx_kst_status" in indexes
        assert "idx_kst_reminder" in indexes
        assert "idx_kst_priority_reminder" in indexes


def test_init_ensure_priority_column(tmp_db_path: Path) -> None:
    """若数据库已存在但缺少 priority 列，应自动补齐。"""
    with sqlite3.connect(str(tmp_db_path)) as conn:
        conn.execute("""
            CREATE TABLE knowledge_scheduled_tasks (
                task_id TEXT PRIMARY KEY,
                task_type TEXT,
                subtype TEXT,
                due_date TEXT,
                reminder_date TEXT,
                is_periodic INTEGER,
                period TEXT,
                status TEXT,
                context TEXT,
                created_at TIMESTAMP
            )
        """)

    KnowledgeScheduler(db_path=str(tmp_db_path))

    with sqlite3.connect(str(tmp_db_path)) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_scheduled_tasks)")}
        assert "priority" in cols


# ============================================================
# 任务调度
# ============================================================


def test_schedule_creates_pending_task(scheduler: KnowledgeScheduler, fixed_now: datetime) -> None:
    """schedule() 应创建一条 status='pending' 的记录并返回 task_id。"""
    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat

        task_id = scheduler.schedule(
            task_type="review",
            subtype="wiki",
            due_date=fixed_now + timedelta(days=3),
            context="test context",
            priority=5,
        )

    assert isinstance(task_id, str)
    assert task_id.startswith("review-wiki-20260610")

    tasks = scheduler.list_all()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.task_type == "review"
    assert task.subtype == "wiki"
    assert task.status == "pending"
    assert task.context == "test context"
    assert task.priority == 5
    assert task.is_periodic is False


def test_schedule_periodic_task(scheduler: KnowledgeScheduler, fixed_now: datetime) -> None:
    """调度周期性任务时 is_periodic 和 period 应正确写入。"""
    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat

        _ = scheduler.schedule(
            task_type="backup",
            subtype="daily",
            due_date=fixed_now + timedelta(days=1),
            is_periodic=True,
            period="daily",
        )

    task = scheduler.list_all()[0]
    assert task.is_periodic is True
    assert task.period == "daily"


# ============================================================
# 到期提醒
# ============================================================


def test_get_pending_reminders_only_returns_due_tasks(
    scheduler: KnowledgeScheduler, fixed_now: datetime
) -> None:
    """只返回 reminder_date <= 当前时间的 pending 任务。"""
    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat

        # 已到期（reminder_date = now - 1天）
        scheduler.schedule(
            task_type="review",
            subtype="due",
            due_date=fixed_now + timedelta(days=1),
        )
        # 未到期（reminder_date 远大于 now）
        scheduler.schedule(
            task_type="review",
            subtype="future",
            due_date=fixed_now + timedelta(days=90),
        )

    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        reminders = scheduler.get_pending_reminders()

    assert len(reminders) == 1
    assert reminders[0].subtype == "due"


def test_get_pending_reminders_respects_priority(
    scheduler: KnowledgeScheduler, fixed_now: datetime
) -> None:
    """结果应按 priority DESC, reminder_date ASC 排序。"""
    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat

        scheduler.schedule(
            task_type="t",
            subtype="low",
            due_date=fixed_now + timedelta(days=2),
            priority=1,
        )
        scheduler.schedule(
            task_type="t",
            subtype="high",
            due_date=fixed_now + timedelta(days=3),
            priority=10,
        )

    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now + timedelta(days=4)
        reminders = scheduler.get_pending_reminders()

    assert [r.subtype for r in reminders] == ["high", "low"]


# ============================================================
# 任务完成
# ============================================================


def test_mark_completed_sets_status_and_creates_next_periodic(
    scheduler: KnowledgeScheduler, fixed_now: datetime
) -> None:
    """完成周期性任务后应自动生成下一周期的任务。"""
    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat

        task_id = scheduler.schedule(
            task_type="backup",
            subtype="daily",
            due_date=fixed_now,
            is_periodic=True,
            period="daily",
        )

    # mark_completed 内部调用 _insert_task，后者又调用 _reminder_date_for
    # _reminder_date_for 内部使用 datetime.now()，因此需要 patch
    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat
        scheduler.mark_completed(task_id)

    all_tasks = scheduler.list_all()
    assert len(all_tasks) == 2

    completed = [t for t in all_tasks if t.status == "completed"]
    pending = [t for t in all_tasks if t.status == "pending"]
    assert len(completed) == 1
    assert len(pending) == 1
    assert pending[0].due_date == (fixed_now + timedelta(days=1)).isoformat()


def test_mark_completed_non_periodic_no_new_task(
    scheduler: KnowledgeScheduler, fixed_now: datetime
) -> None:
    """非周期性任务完成后不应生成新任务。"""
    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat

        task_id = scheduler.schedule(
            task_type="review",
            subtype="once",
            due_date=fixed_now,
        )

    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        scheduler.mark_completed(task_id)

    assert len(scheduler.list_all()) == 1
    assert scheduler.list_all()[0].status == "completed"


# ============================================================
# 任务取消
# ============================================================


def test_cancel_sets_status_cancelled(scheduler: KnowledgeScheduler, fixed_now: datetime) -> None:
    """cancel() 应将任务状态设为 cancelled。"""
    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat

        task_id = scheduler.schedule(task_type="review", subtype="cancel_me", due_date=fixed_now)

    scheduler.cancel(task_id)
    task = scheduler.list_all()[0]
    assert task.status == "cancelled"


# ============================================================
# 启动补偿
# ============================================================


def test_startup_compensation_returns_missed_and_stale_reminded(
    scheduler: KnowledgeScheduler, fixed_now: datetime
) -> None:
    """startup_compensation 应返回：
    1. 所有 pending 且 reminder_date 已过的任务；
    2. 所有 reminded 且超过 3 天未处理的任务。
    """
    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat

        # pending + 已过期
        scheduler.schedule(task_type="review", subtype="missed", due_date=fixed_now)
        # pending + 未过期（不应返回）
        scheduler.schedule(
            task_type="review",
            subtype="future",
            due_date=fixed_now + timedelta(days=30),
        )

    # 手动插入一条 reminded 且超过 3 天的记录
    with sqlite3.connect(str(scheduler.DB_PATH)) as conn:
        conn.execute(
            """
            INSERT INTO knowledge_scheduled_tasks
            (task_id, task_type, subtype, due_date, reminder_date,
             is_periodic, period, status, context, priority, created_at, reminded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                "old-reminded-1",
                "review",
                "stale",
                fixed_now.isoformat(),
                fixed_now.isoformat(),
                0,
                None,
                "reminded",
                "",
                0,
                fixed_now.isoformat(),
                (fixed_now - timedelta(days=5)).isoformat(),
            ),
        )

    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat
        missed = scheduler.startup_compensation()

    subtypes = {t.subtype for t in missed}
    assert "missed" in subtypes
    assert "stale" in subtypes
    assert "future" not in subtypes


def test_startup_compensation_respects_max_tasks(
    scheduler: KnowledgeScheduler, fixed_now: datetime
) -> None:
    """startup_compensation 应受 max_tasks 限制，防止 backlog 拖垮调用方。"""
    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat
        for i in range(5):
            scheduler.schedule(
                task_type="review", subtype=f"missed-{i}", due_date=fixed_now
            )

    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat
        missed = scheduler.startup_compensation(max_tasks=2)

    assert len(missed) == 2


# ============================================================
# 旧任务清理
# ============================================================


def test_cleanup_old_tasks_deletes_only_old_completed_or_cancelled(
    scheduler: KnowledgeScheduler, fixed_now: datetime
) -> None:
    """cleanup_old_tasks 只删除 completed/cancelled 且超过 N 天的记录。"""
    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat

        # 新建 pending 任务
        scheduler.schedule(task_type="review", subtype="keep", due_date=fixed_now)

    # 手动插入旧 completed 和旧 cancelled
    with sqlite3.connect(str(scheduler.DB_PATH)) as conn:
        for status in ("completed", "cancelled"):
            conn.execute(
                """
                INSERT INTO knowledge_scheduled_tasks
                (task_id, task_type, subtype, due_date, reminder_date,
                 is_periodic, period, status, context, priority, created_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    f"old-{status}",
                    "review",
                    status,
                    fixed_now.isoformat(),
                    fixed_now.isoformat(),
                    0,
                    None,
                    status,
                    "",
                    0,
                    fixed_now.isoformat(),
                    (fixed_now - timedelta(days=40)).isoformat(),
                ),
            )

    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        scheduler.cleanup_old_tasks(days=30)

    tasks = scheduler.list_all()
    subtypes = {t.subtype for t in tasks}
    assert "keep" in subtypes
    assert "completed" not in subtypes
    assert "cancelled" not in subtypes


# ============================================================
# 工具方法
# ============================================================


def test_reminder_date_for_close_due(fixed_now: datetime) -> None:
    """7 天内到期的任务提前 1 天提醒。"""
    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        due = fixed_now + timedelta(days=5)
        reminder = KnowledgeScheduler._reminder_date_for(due)
        assert reminder == due - timedelta(days=1)


def test_reminder_date_for_medium_due(fixed_now: datetime) -> None:
    """8-30 天内到期的任务提前 3 天提醒。"""
    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        due = fixed_now + timedelta(days=15)
        reminder = KnowledgeScheduler._reminder_date_for(due)
        assert reminder == due - timedelta(days=3)


def test_reminder_date_for_far_due(fixed_now: datetime) -> None:
    """超过 30 天到期的任务提前 7 天提醒。"""
    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        due = fixed_now + timedelta(days=60)
        reminder = KnowledgeScheduler._reminder_date_for(due)
        assert reminder == due - timedelta(days=7)


def test_build_task_id_is_deterministic_for_same_inputs(fixed_now: datetime) -> None:
    """相同输入在同一秒内应生成相同 task_id（基于时间戳后缀）。"""
    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat

        id1 = KnowledgeScheduler._build_task_id("a", "b", fixed_now, "ctx")
        id2 = KnowledgeScheduler._build_task_id("a", "b", fixed_now, "ctx")
        assert id1 == id2
        assert id1.startswith("a-b-20260607-")


def test_next_periodic_due_calculations(fixed_now: datetime) -> None:
    """_next_periodic_due 应正确计算各周期下次到期时间。"""
    assert KnowledgeScheduler._next_periodic_due(fixed_now, "daily") == fixed_now + timedelta(
        days=1
    )
    assert KnowledgeScheduler._next_periodic_due(fixed_now, "weekly") == fixed_now + timedelta(
        days=7
    )
    assert KnowledgeScheduler._next_periodic_due(fixed_now, "biweekly") == fixed_now + timedelta(
        days=14
    )
    assert KnowledgeScheduler._next_periodic_due(fixed_now, "monthly") == fixed_now + timedelta(
        days=30
    )
    assert KnowledgeScheduler._next_periodic_due(fixed_now, "quarterly") == fixed_now + timedelta(
        days=90
    )
    assert KnowledgeScheduler._next_periodic_due(fixed_now, "yearly") == fixed_now + timedelta(
        days=365
    )
    assert KnowledgeScheduler._next_periodic_due(fixed_now, "unknown") is None


# ============================================================
# 步骤注册与状态
# ============================================================


def test_register_and_get_step_status(scheduler: KnowledgeScheduler) -> None:
    """注册步骤后 get_step_status 应返回正确状态。"""
    step = ScheduledStep(
        name="test_step",
        func=lambda: {"status": "ok"},
        trigger=CronTrigger("0 9 * * *"),
        deps=["dep1"],
        timeout=120,
    )
    scheduler.register(step)

    status = scheduler.get_step_status()
    assert "test_step" in status
    info = status["test_step"]
    assert info["trigger"] == "cron:0 9 * * *"
    assert info["enabled"] is True
    assert info["consecutive_failures"] == 0
    assert info["timeout"] == 120
    assert info["deps"] == ["dep1"]


def test_enable_disable_step(scheduler: KnowledgeScheduler) -> None:
    """enable_step / disable_step 应正确切换 enabled 状态。"""
    step = ScheduledStep(
        name="toggle_me",
        func=lambda: {"status": "ok"},
        trigger=CronTrigger("0 9 * * *"),
    )
    scheduler.register(step)

    assert scheduler.disable_step("toggle_me") is True
    assert scheduler.get_step_status()["toggle_me"]["enabled"] is False

    assert scheduler.enable_step("toggle_me") is True
    assert scheduler.get_step_status()["toggle_me"]["enabled"] is True

    # 不存在的步骤返回 False
    assert scheduler.disable_step("nonexistent") is False
    assert scheduler.enable_step("nonexistent") is False


def test_deferred_step_does_not_persist_last_run(scheduler: KnowledgeScheduler) -> None:
    """deferred 步骤不应更新 last_run，确保下次 tick 会继续重试。"""
    step = ScheduledStep(
        name="deferred_step",
        func=lambda: {"status": "deferred", "reason": "startup_delay"},
        trigger=CronTrigger("0 9 * * *"),
    )
    scheduler.register(step)

    result = scheduler._run_step(step)

    assert result["status"] == "deferred"
    assert step.trigger._last_run is None  # noqa: SLF001
    with sqlite3.connect(str(scheduler.DB_PATH)) as conn:
        row = conn.execute(
            "SELECT last_run FROM scheduler_step_state WHERE step_name = ?",
            ("deferred_step",),
        ).fetchone()
    assert row is None
    with sqlite3.connect(
        str(scheduler.DB_PATH.parent / "producer_consumer_ledger.db")
    ) as conn:
        status = conn.execute(
            """
            SELECT r.status
            FROM cognitive_state_outbox o
            LEFT JOIN cognitive_state_effect_receipts r
              ON r.command_id=o.command_id
            WHERE o.command_type='execute_material_action'
              AND json_extract(o.payload_json, '$.target_ref')=?
            """,
            ("scheduled-step:deferred_step",),
        ).fetchone()[0]
    assert status == "failed_terminal"


def test_error_step_does_not_forge_a_committed_terminal(
    scheduler: KnowledgeScheduler,
) -> None:
    step = ScheduledStep(
        name="error_step",
        func=lambda: {"status": "error", "error": "target failed"},
        trigger=CronTrigger("0 9 * * *"),
    )

    result = scheduler._run_step(step)

    assert result["status"] == "error"
    assert step.trigger._last_run is None  # noqa: SLF001
    with sqlite3.connect(
        str(scheduler.DB_PATH.parent / "producer_consumer_ledger.db")
    ) as conn:
        row = conn.execute(
            """
            SELECT r.status
            FROM cognitive_state_outbox o
            LEFT JOIN cognitive_state_effect_receipts r
              ON r.command_id=o.command_id
            WHERE o.command_type='execute_material_action'
              AND json_extract(o.payload_json, '$.target_ref')=?
            """,
            ("scheduled-step:error_step",),
        ).fetchone()
    assert row == ("failed_terminal",)


def test_step_crash_window_is_dead_lettered_without_duplicate_invocation(
    tmp_db_path: Path,
    monkeypatch,
) -> None:
    invocations = 0

    def execute_once():
        nonlocal invocations
        invocations += 1
        return {"status": "ok", "count": invocations}

    step = ScheduledStep(
        name="crash_window_step",
        func=execute_once,
        trigger=CronTrigger("0 9 * * *"),
    )
    binding = scheduled_step_material_action_binding(step)
    authorization = material_action_authorization(
        tmp_db_path.parent,
        action_type=CHRONOS_STEP_EXECUTE_ACTION,
        owner=CHRONOS_OWNER,
        executor=CHRONOS_EXECUTOR,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
        nonce="chronos-step-crash-window",
    )
    scheduler = KnowledgeScheduler(
        db_path=str(tmp_db_path),
        material_action_resolver=lambda _: authorization,
    )
    original = scheduler._finalize_step_attempt  # noqa: SLF001
    calls = 0

    def crash_before_finalize(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected crash after scheduled step invocation")
        return original(**kwargs)

    monkeypatch.setattr(scheduler, "_finalize_step_attempt", crash_before_finalize)

    with pytest.raises(OSError, match="after scheduled step invocation"):
        scheduler._run_step(step)
    recovered = scheduler._run_step(step)

    assert invocations == 1
    assert recovered["reason"] == "crash_window_dead_letter"
    with sqlite3.connect(
        str(tmp_db_path.parent / "producer_consumer_ledger.db")
    ) as conn:
        receipt = conn.execute(
            """
            SELECT status, evidence_refs
            FROM cognitive_state_effect_receipts WHERE command_id=?
            """,
            (authorization.permit.command_id,),
        ).fetchone()
    assert receipt[0] == "dead_letter"
    assert f"retry-budget-exhausted:{authorization.permit.command_id}" in json.loads(
        receipt[1]
    )


@pytest.mark.no_canonical_material_actions
def test_project_contract_step_retry_recovers_its_pending_generation(
    tmp_db_path: Path,
    monkeypatch,
) -> None:
    invocations = 0

    def execute_once():
        nonlocal invocations
        invocations += 1
        return {"status": "ok", "count": invocations}

    scheduler = KnowledgeScheduler(db_path=str(tmp_db_path))
    step = ScheduledStep(
        name="project_contract_crash_window_step",
        func=execute_once,
        trigger=CronTrigger("0 9 * * *"),
    )
    scheduler.register(step)
    original = scheduler._finalize_step_attempt  # noqa: SLF001
    calls = 0

    def crash_before_finalize(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected project-contract crash after invocation")
        return original(**kwargs)

    monkeypatch.setattr(scheduler, "_finalize_step_attempt", crash_before_finalize)

    with pytest.raises(OSError, match="after invocation"):
        scheduler._run_step(step)
    recovered = scheduler._run_step(step)

    assert invocations == 1
    assert recovered["reason"] == "crash_window_dead_letter"
    with sqlite3.connect(
        tmp_db_path.parent / "producer_consumer_ledger.db"
    ) as conn:
        statuses = conn.execute(
            "SELECT status FROM cognitive_state_effect_receipts"
        ).fetchall()
    assert statuses == [("dead_letter",)]


def test_tick_defers_due_steps_when_resource_budget_blocks(
    scheduler: KnowledgeScheduler, monkeypatch
) -> None:
    """KIA 调度预算不足时，tick 应延后后台步骤且不推进 last_run。"""
    calls = []
    step = ScheduledStep(
        name="resource_limited_step",
        func=lambda: calls.append("ran") or {"status": "ok"},
        trigger=CronTrigger("* * * * *"),
    )
    scheduler.register(step)

    class FakeBudget:
        def can_run(self, service: str) -> bool:
            assert service == "kia_sched"
            return False

        def throttle_delay(self, service: str) -> float:
            assert service == "kia_sched"
            return 15.0

        def status(self) -> dict:
            return {
                "state": "throttled",
                "cpu": "95.0%",
                "memory": "70.0%",
                "thermal": "normal",
                "power": "ac",
            }

    monkeypatch.setattr("core.resource_budget.get_budget", lambda: FakeBudget())

    result = scheduler.tick()

    assert calls == []
    assert result["resource_limited_step"]["status"] == "deferred"
    assert result["resource_limited_step"]["reason"] == "resource_budget"
    assert result["resource_limited_step"]["resource_state"] == "throttled"
    assert result["resource_limited_step"]["retry_after_seconds"] == 15
    assert scheduler.get_last_results()["resource_limited_step"]["status"] == "deferred"
    assert step.trigger._last_run is None  # noqa: SLF001
    with sqlite3.connect(str(scheduler.DB_PATH)) as conn:
        row = conn.execute(
            "SELECT last_run FROM scheduler_step_state WHERE step_name = ?",
            ("resource_limited_step",),
        ).fetchone()
    assert row is None


# ============================================================
# 格式化提醒
# ============================================================


def test_format_reminder_contains_task_info(
    scheduler: KnowledgeScheduler, fixed_now: datetime
) -> None:
    """format_reminder 输出应包含任务类型、到期日等关键信息。"""
    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat

        scheduler.schedule(
            task_type="review",
            subtype="wiki",
            due_date=fixed_now + timedelta(days=5),
            context="page_42",
        )

    task = scheduler.list_all()[0]
    text = scheduler.format_reminder(task)
    assert "review/wiki" in text
    assert "2026-06-12" in text
    assert "page_42" in text


# ============================================================
# 持久化 — 重启后数据不丢失
# ============================================================


def test_persistence_across_restarts(tmp_db_path: Path, fixed_now: datetime) -> None:
    """数据库写入后，重新初始化 KnowledgeScheduler 应能读取到已有任务。"""
    with patch("core.kia.chronos.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat

        ks1 = authorized_knowledge_scheduler(db_path=tmp_db_path)
        task_id = ks1.schedule(task_type="review", subtype="persist", due_date=fixed_now)

    # 模拟重启：新建实例指向同一数据库
    ks2 = KnowledgeScheduler(db_path=str(tmp_db_path))
    tasks = ks2.list_all()
    assert len(tasks) == 1
    assert tasks[0].task_id == task_id
    assert tasks[0].task_type == "review"


def test_default_steps_include_shadow_stress_and_raw_survival_refresh():
    """默认步骤中应包含关键定时任务。"""
    from core.kia.chronos import KnowledgeScheduler

    scheduler = KnowledgeScheduler()
    scheduler.register_all_default_steps()
    status = scheduler.get_step_status()

    assert "shadow_page" in status
    assert "stress_test" in status
    assert "raw_survival_refresh" in status
    assert "verification_queue" in status
    # 验证为 cron 触发即可，具体时间可能根据使用场景调整
    assert status["shadow_page"]["trigger"].startswith("cron:")
    assert status["stress_test"]["trigger"].startswith("cron:")
    assert status["raw_survival_refresh"]["trigger"] == "cron:10 10 * * 1"
    assert status["verification_queue"]["trigger"].startswith("cron:")


def test_default_steps_can_disable_heavy_stress_test():
    """Daemon 默认可跳过重型全量任务，避免启动后长时间占用 CPU。"""
    from core.kia.chronos import KnowledgeScheduler

    scheduler = KnowledgeScheduler()
    scheduler.register_all_default_steps(include_heavy_steps=False)
    status = scheduler.get_step_status()

    assert status["graph_build"]["enabled"] is False
    assert status["connect_worker"]["enabled"] is False
    assert status["knowledge_evolution"]["enabled"] is False
    assert status["stress_test"]["enabled"] is False
    assert status["shadow_page"]["enabled"] is True


def test_default_steps_expose_event_trigger_routes(tmp_path: Path, monkeypatch) -> None:
    """默认步骤应把事件触发路由纳入 scheduler step registry。"""
    subscriptions = []

    class FakeBus:
        def subscribe(self, event_type, handler, *, consumer_id):
            subscriptions.append((event_type, handler, consumer_id))

    monkeypatch.setattr("core.mnemos_bus.get_event_bus", lambda: FakeBus())

    scheduler = KnowledgeScheduler(db_path=str(tmp_path / "chronos.db"))
    scheduler.register_all_default_steps(wiki_base=str(tmp_path))

    status = scheduler.get_step_status()
    assert status["page_created"]["trigger"] == "event:page.created"
    assert status["page_modified"]["trigger"] == "event:page.modified"
    assert status["session_start"]["trigger"] == "event:session.start"
    assert status["message_exchanged"]["trigger"] == "event:message.exchanged"
    assert all(not scheduler.steps[name].trigger.is_due() for name in (
        "page_created",
        "page_modified",
        "session_start",
        "message_exchanged",
    ))
    assert [event_type for event_type, _, _ in subscriptions] == [
        "page.created",
        "page.modified",
        "session.start",
        "message.exchanged",
    ]


def test_knowledge_profile_default_step_calls_generate_profile_function(
    tmp_path: Path, monkeypatch
) -> None:
    """knowledge_profile 默认步骤应调用 Metis 模块级画像报告入口。"""
    from core.kia import metis

    calls = []

    def fake_generate_profile(wiki_base=None):
        calls.append(("generate_profile", wiki_base))
        return "# profile"

    monkeypatch.setattr(metis, "generate_profile", fake_generate_profile)

    scheduler = KnowledgeScheduler(db_path=str(tmp_path / "chronos.db"))
    scheduler.register_all_default_steps(wiki_base=str(tmp_path))
    result = scheduler.steps["knowledge_profile"].func()

    assert result == {"status": "ok", "result": "# profile"}
    assert calls == [("generate_profile", str(tmp_path))]


def test_raw_survival_refresh_runs_physical_purge(scheduler: KnowledgeScheduler, monkeypatch):
    """raw 生存值刷新应在配置允许时执行物理清理并返回清理统计。"""

    class FakeConfig:
        def get(self, key, default=None):
            values = {
                "raw_event_store.startup_delay_seconds": 0,
                "raw_event_store.physical_delete_enabled": True,
                "raw_event_store.physical_delete_batch_limit": 7,
            }
            return values.get(key, default)

    class FakeStore:
        def refresh_survival_scores(self):
            return {"updated": 2, "eligible_delete": 1, "active": 1}

        def purge_eligible_delete(self, *, limit=None):
            return {
                "purged": 1,
                "raw_turns_deleted": 1,
                "raw_metrics_deleted": 1,
                "raw_access_logs_deleted": 1,
                "limit": limit,
            }

        def close(self):
            pass

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr("core.sync_framework.raw_event_store.RawEventStore", FakeStore)

    result = scheduler._run_raw_survival_refresh()

    assert result["status"] == "ok"
    assert result["updated"] == 2
    assert result["physical_purge"]["purged"] == 1
    assert result["physical_purge"]["limit"] == 7


def test_issue_pipeline_step_uses_auto_fix_executor_factory(
    scheduler: KnowledgeScheduler, monkeypatch
) -> None:
    """issue_pipeline 默认步骤应复用 IssuePipeline 的 executor 工厂入口。"""
    from core.kia import issue_pipeline

    issue = SimpleNamespace(issue_id="issue-1", issue_type="orphan")
    calls = []

    class FakeRegistry:
        def list_issues(self, status, limit):
            calls.append(("list", status, limit))
            return [issue]

        def count_by_severity(self):
            return {"low": 1}

    class DirectExecutor:
        def __init__(self, registry=None):
            raise AssertionError("_run_issue_pipeline should call get_auto_fix_executor()")

    class FactoryExecutor:
        def can_auto_fix(self, observed_issue):
            calls.append(("can_auto_fix", observed_issue.issue_id))
            return True

        def execute(self, observed_issue):
            calls.append(("execute", observed_issue.issue_id))
            return SimpleNamespace(success=True, skipped=False, action="fixed")

    registry = FakeRegistry()

    def fake_get_auto_fix_executor(registry=None):
        calls.append(("factory", registry))
        return FactoryExecutor()

    monkeypatch.setattr(issue_pipeline, "AutoFixExecutor", DirectExecutor)
    monkeypatch.setattr(issue_pipeline, "get_auto_fix_executor", fake_get_auto_fix_executor)

    result = scheduler._run_issue_pipeline(registry=registry)

    assert result["status"] == "ok"
    assert result["auto_fixable"] == 1
    assert result["results"][0]["action"] == "fixed"
    assert ("factory", registry) in calls


def test_issue_pipeline_step_uses_dispute_generator_factory_for_manual_review(
    scheduler: KnowledgeScheduler, monkeypatch
) -> None:
    """issue_pipeline 高风险人工确认路径应复用争议页生成器工厂。"""
    from core.kia import issue_pipeline

    issue = SimpleNamespace(
        issue_id="issue-critical",
        issue_type="conflict",
        severity="critical",
        page_path="conflict.md",
    )
    calls = []

    class FakeRegistry:
        def list_issues(self, status, limit):
            calls.append(("list", status, limit))
            return [issue]

        def count_by_severity(self):
            return {"critical": 1}

        def update_status(
            self,
            issue_id,
            status,
            resolved_by="",
            resolution_action="",
            resolution_notes="",
        ):
            calls.append(
                (
                    "update_status",
                    issue_id,
                    status,
                    resolved_by,
                    resolution_action,
                    resolution_notes,
                )
            )
            return True

    class DirectGenerator:
        def __init__(self, wiki_base=None):
            raise AssertionError("_run_issue_pipeline should call get_dispute_generator()")

    class FactoryExecutor:
        def can_auto_fix(self, observed_issue):
            calls.append(("can_auto_fix", observed_issue.issue_id))
            return False

    class FactoryGenerator:
        def generate(self, observed_issue):
            calls.append(("generate_dispute", observed_issue.issue_id))
            return Path("99-Reports/争议-conflict.md")

    registry = FakeRegistry()

    def fake_get_auto_fix_executor(registry=None):
        calls.append(("executor_factory", registry))
        return FactoryExecutor()

    def fake_get_dispute_generator(wiki_base=None):
        calls.append(("dispute_factory", wiki_base))
        return FactoryGenerator()

    monkeypatch.setattr(issue_pipeline, "DisputePageGenerator", DirectGenerator)
    monkeypatch.setattr(issue_pipeline, "get_auto_fix_executor", fake_get_auto_fix_executor)
    monkeypatch.setattr(issue_pipeline, "get_dispute_generator", fake_get_dispute_generator)

    result = scheduler._run_issue_pipeline(registry=registry)

    assert result["status"] == "ok"
    assert result["auto_fixable"] == 0
    assert result["disputes_created"] == 1
    assert result["disputes"][0]["page_path"] == "99-Reports/争议-conflict.md"
    assert ("dispute_factory", None) in calls
    assert (
        "update_status",
        "issue-critical",
        "dispute",
        "issue_pipeline",
        "created_dispute_page",
        "99-Reports/争议-conflict.md",
    ) in calls


def test_banner_task_scanner_uses_page_banner_injector_factory(
    scheduler: KnowledgeScheduler, monkeypatch
) -> None:
    """banner_task_scanner 应复用 dialog_reminder 的横幅注入工厂入口。"""
    from core.kia import dialog_reminder

    calls = []

    class DirectInjector:
        def __init__(self):
            raise AssertionError(
                "_run_banner_task_scanner should call get_page_banner_injector()"
            )

    class DirectQueue:
        def __init__(self):
            raise AssertionError(
                "_run_banner_task_scanner should call get_dialog_reminder_queue()"
            )

    class FactoryQueue:
        pass

    class FactoryInjector:
        def process_banners(self, queue=None):
            calls.append(("process_banners", isinstance(queue, FactoryQueue)))
            return {"checked": 1, "removed": 0}

    def fake_get_page_banner_injector():
        calls.append(("injector_factory",))
        return FactoryInjector()

    def fake_get_dialog_reminder_queue(db_path=None):
        calls.append(("queue_factory", db_path))
        return FactoryQueue()

    monkeypatch.setattr(dialog_reminder, "PageBannerInjector", DirectInjector)
    monkeypatch.setattr(dialog_reminder, "DialogReminderQueue", DirectQueue)
    monkeypatch.setattr(
        dialog_reminder, "get_page_banner_injector", fake_get_page_banner_injector
    )
    monkeypatch.setattr(
        dialog_reminder, "get_dialog_reminder_queue", fake_get_dialog_reminder_queue
    )

    result = scheduler._run_banner_task_scanner()

    assert result == {"status": "ok", "stats": {"checked": 1, "removed": 0}}
    assert ("injector_factory",) in calls
    assert ("queue_factory", None) in calls
    assert ("process_banners", True) in calls


def test_cron_trigger_detects_missed_execution():
    """CronTrigger 应能检测自上次运行以来错过的触发时间，避免低频 tick 漏触发。"""
    trigger = CronTrigger("*/30 * * * *")
    # 上次运行是 01:05，当前 01:32（错过 01:30）
    trigger.update_last_run("2026-06-20T01:05:00")
    with patch("core.kia.chronos_contracts.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 6, 20, 1, 32, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert trigger.is_due() is True


def test_cron_trigger_same_minute_not_due():
    """同一分钟内不应重复触发。"""
    trigger = CronTrigger("*/30 * * * *")
    trigger.update_last_run("2026-06-20T01:30:00")
    with patch("core.kia.chronos_contracts.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 6, 20, 1, 30, 15)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert trigger.is_due() is False


def test_cron_trigger_rejects_out_of_bounds_ranges():
    """Cron field ranges outside their allowed bounds should not match."""
    now = datetime(2026, 6, 20, 9, 30, 0)

    assert CronTrigger("0-99 9 * * *")._matches(now) is False
    assert CronTrigger("30 9 0-31 * *")._matches(now) is False


def test_trigger_page_modified_detects_stale_and_refreshes(scheduler, tmp_path, monkeypatch):
    """_trigger_page_modified 应正确检测 stale 并触发自动刷新。"""
    from datetime import datetime, timedelta

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    old = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
    page = wiki / "03-Tech" / "stale.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"---\nupdated_at: {old}\n---\n\nbody", encoding="utf-8")

    class Cfg:
        wiki_dir = wiki
        database_dir = tmp_path / ".mnemos"

        def get(self, key, default=None):
            if key == "daemon.services.freshness_refresh":
                return True
            return default

    monkeypatch.setattr("core.config.get_config", lambda: Cfg())
    monkeypatch.setattr("core.trust.config.get_config", lambda: Cfg())
    scheduler.shutdown()
    Cfg.database_dir.mkdir(parents=True, exist_ok=True)
    scheduler = authorized_knowledge_scheduler(
        db_path=Cfg.database_dir / "test_chronos.db"
    )

    result = scheduler._trigger_page_modified(
        {"page_path": "03-Tech/stale.md", "wiki_base": str(wiki)}
    )
    assert result["status"] == "ok"
    assert len(result["freshness_alerts"]) == 1
    assert result["freshness_refresh"]["status"] == "refreshed"
    content = page.read_text(encoding="utf-8")
    today = datetime.now().strftime("%Y-%m-%d")
    assert "updated_at:" in content and today in content
