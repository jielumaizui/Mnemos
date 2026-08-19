from __future__ import annotations

import sqlite3

import pytest

from tests.integration.test_recap_trusted_completion import _confirmed_recap
from tests.unit.test_retrospective_workflow import _create_recap_task, _patch_config


def test_finalize_reports_consumed_only_after_every_required_target_receipt(
    monkeypatch,
    tmp_path,
):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService

    service = KiaApplicationService()
    recap_id = _confirmed_recap(service, task_id)

    finalized = service.recap_finalize(recap_id, confirmed_by_user=True)

    assert finalized["success"] is True, finalized
    assert finalized["state"] == "consumed"
    assert finalized["terminal"] is True
    plan = finalized["consumption_plan"]
    assert plan["plan_id"]
    assert plan["plan_status"] == "consumed"
    assert plan["required_receipt_count"] > 0
    assert plan["required_receipt_count"] == plan["terminal_receipt_count"]
    assert plan["failed_targets"] == []
    assert all(
        target["status"] in {"committed", "intentional_skip"}
        for target in plan["target_statuses"]
        if target["required"]
    )


def test_duplicate_finalize_returns_the_existing_terminal_plan(monkeypatch, tmp_path):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService

    service = KiaApplicationService()
    recap_id = _confirmed_recap(service, task_id)
    first = service.recap_finalize(recap_id, confirmed_by_user=True)
    duplicate = service.recap_finalize(recap_id, confirmed_by_user=True)

    assert duplicate["success"] is True
    assert duplicate["already_finalized"] is True
    assert duplicate["state"] == "consumed"
    assert duplicate["terminal"] is True
    assert duplicate["consumption_plan"]["plan_id"] == first["consumption_plan"]["plan_id"]
    assert duplicate["consumption_plan"]["plan_status"] == "consumed"


def test_retry_after_consumer_failure_only_replays_the_missing_receipt(
    monkeypatch,
    tmp_path,
):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService
    from core.persona import psyche

    service = KiaApplicationService()
    recap_id = _confirmed_recap(service, task_id)
    actual_get_signal_store = psyche.get_signal_store

    class LockedPersonaStore:
        def add_signal(self, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(psyche, "get_signal_store", lambda: LockedPersonaStore())
    failed = service.recap_finalize(recap_id, confirmed_by_user=True)

    assert failed["success"] is False
    assert failed["state"] == "consumption_pending"
    assert failed["consumption_plan"]["plan_status"] == "partial"
    assert failed["failed_targets"] == ["persona"]

    monkeypatch.setattr(psyche, "get_signal_store", actual_get_signal_store)
    retried = service.recap_finalize(recap_id, confirmed_by_user=True)

    assert retried["success"] is True, retried
    assert retried["state"] == "consumed"
    attempts = {
        item["canonical_target"]: item["attempt_count"]
        for item in retried["consumption_plan"]["target_statuses"]
    }
    assert attempts["persona"] == 2
    assert attempts["knowledge_retrieval"] == 1
    assert attempts["policy_patch"] == 1
    assert attempts["follow_up"] == 1


def test_retry_reuses_the_committed_plan_even_if_request_arguments_change(
    monkeypatch,
    tmp_path,
):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService
    from core.persona import psyche

    service = KiaApplicationService()
    recap_id = _confirmed_recap(service, task_id)
    actual_get_signal_store = psyche.get_signal_store

    class LockedPersonaStore:
        def add_signal(self, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(psyche, "get_signal_store", lambda: LockedPersonaStore())
    first = service.recap_finalize(
        recap_id,
        confirmed_by_user=True,
        follow_up_at="2030-01-01T00:00:00+00:00",
    )
    assert first["consumption_plan"]["plan_status"] == "partial"

    monkeypatch.setattr(psyche, "get_signal_store", actual_get_signal_store)
    retried = service.recap_finalize(
        recap_id,
        confirmed_by_user=True,
        follow_up_at="2031-01-01T00:00:00+00:00",
    )

    assert retried["success"] is True, retried
    assert retried["consumption_plan"]["plan_id"] == first["consumption_plan"]["plan_id"]
    assert retried["consumption_plan"]["follow_up_at"] == "2030-01-01T00:00:00+00:00"
    with sqlite3.connect(str(cfg.database_dir / "recap_tasks.db")) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM recap_consumption_plans WHERE recap_id=?",
            (recap_id,),
        ).fetchone()[0] == 1


def test_daemon_style_restart_drain_finishes_session_without_replaying_committed_effects(
    monkeypatch,
    tmp_path,
):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService
    from core.app.retrospective_consumption_router import RetrospectiveConsumptionRouter
    from core.persona import psyche

    service = KiaApplicationService()
    recap_id = _confirmed_recap(service, task_id)
    actual_get_signal_store = psyche.get_signal_store

    class CrashedPersonaStore:
        def add_signal(self, **_kwargs):
            raise sqlite3.OperationalError("simulated crash before persona receipt")

    monkeypatch.setattr(psyche, "get_signal_store", lambda: CrashedPersonaStore())
    partial = service.recap_finalize(recap_id, confirmed_by_user=True)
    assert partial["consumption_plan"]["plan_status"] == "partial"

    monkeypatch.setattr(psyche, "get_signal_store", actual_get_signal_store)
    drained = RetrospectiveConsumptionRouter(
        db_path=cfg.database_dir / "recap_tasks.db"
    ).drain_pending()

    assert drained["errors"] == 0
    assert drained["plans_processed"] == 1
    assert drained["plans"][0]["plan_status"] == "consumed"
    assert KiaApplicationService().recap_status(recap_id=recap_id)["state"] == "consumed"
    attempts = {
        item["canonical_target"]: item["attempt_count"]
        for item in drained["plans"][0]["target_statuses"]
    }
    assert attempts["persona"] == 2
    assert attempts["knowledge_retrieval"] == 1


def test_restart_recovers_crash_between_page_commit_and_plan_creation(
    monkeypatch,
    tmp_path,
):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService
    from core.app.retrospective_consumption_router import RetrospectiveConsumptionRouter

    service = KiaApplicationService()
    recap_id = _confirmed_recap(service, task_id)
    original = RetrospectiveConsumptionRouter.route_after_finalize

    def crash_before_plan(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated crash before plan commit")

    monkeypatch.setattr(
        RetrospectiveConsumptionRouter,
        "route_after_finalize",
        crash_before_plan,
    )
    crashed = service.recap_finalize(recap_id, confirmed_by_user=True)
    assert crashed["success"] is False
    assert crashed["state"] == "consumption_pending"
    assert service.recap_status(recap_id=recap_id)["consumption_plan"] is None

    monkeypatch.setattr(
        RetrospectiveConsumptionRouter,
        "route_after_finalize",
        original,
    )
    drained = RetrospectiveConsumptionRouter(
        db_path=cfg.database_dir / "recap_tasks.db"
    ).drain_pending()

    assert drained["plans_recovered"] == 1
    assert drained["plans"][0]["plan_status"] == "consumed"
    assert KiaApplicationService().recap_status(recap_id=recap_id)["state"] == "consumed"


def test_restart_recovers_skip_event_committed_before_plan_creation(
    monkeypatch,
    tmp_path,
):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.app.retrospective_consumption_router import RetrospectiveConsumptionRouter
    from core.app.retrospective_skip_event_store import RetrospectiveSkipEventStore

    store = RetrospectiveSkipEventStore(db_path=cfg.database_dir / "recap_tasks.db")
    original = RetrospectiveConsumptionRouter.route_skip_event

    def crash_before_plan(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated crash before skip plan commit")

    monkeypatch.setattr(
        RetrospectiveConsumptionRouter,
        "route_skip_event",
        crash_before_plan,
    )
    with pytest.raises(sqlite3.OperationalError, match="before skip plan commit"):
        store.record_skip(
            recap_id="recap-orphan-skip",
            task_id=task_id,
            skip_reason="false_positive",
            owner_agent="codex",
            source_agent="codex",
            project="mnemos",
            task_type="coding",
        )

    monkeypatch.setattr(
        RetrospectiveConsumptionRouter,
        "route_skip_event",
        original,
    )
    drained = RetrospectiveConsumptionRouter(
        db_path=cfg.database_dir / "recap_tasks.db"
    ).drain_pending()

    assert drained["errors"] == 0
    assert drained["plans_recovered"] == 1
    recovered = drained["plans"][0]
    assert recovered["recap_id"] == "recap-orphan-skip"
    assert recovered["plan_status"] == "consumed"
    assert recovered["recovered_missing_plan"] is True
    with sqlite3.connect(str(cfg.database_dir / "recap_tasks.db")) as conn:
        assert conn.execute(
            "SELECT status FROM recap_tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()[0] == "ignored"


def test_recap_skip_remains_an_operational_scheduler_transition(
    monkeypatch,
    tmp_path,
):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService

    service = KiaApplicationService()
    started = service.recap_start(
        task_id=task_id,
        source_agent="codex",
        owner_agent="codex",
        task_type="coding",
        project="mnemos",
    )

    skipped = service.recap_skip(
        recap_id=started["recap_id"],
        skip_reason="low_value",
        owner_agent="codex",
        source_agent="codex",
    )

    assert skipped["success"] is True
    plan = skipped["consumption_plan"]
    assert plan["plan_status"] == "consumed"
    assert plan["required_receipt_count"] == 1
    assert {
        item["canonical_target"] for item in plan["target_statuses"]
    } == {"scheduler"}
    assert all(item["status"] == "committed" for item in plan["target_statuses"])


@pytest.mark.parametrize(
    "skip_reason",
    ["no_time", "low_value", "false_positive", "already_handled", "no_response"],
)
def test_every_skip_policy_uses_only_registered_production_targets(
    monkeypatch,
    tmp_path,
    skip_reason,
):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg, topic=f"skip-{skip_reason}")

    from core.application.kia import KiaApplicationService

    service = KiaApplicationService()
    started = service.recap_start(
        task_id=task_id,
        source_agent="codex",
        owner_agent="codex",
        task_type="coding",
        project="mnemos",
    )
    skipped = service.recap_skip(
        recap_id=started["recap_id"],
        skip_reason=skip_reason,
        owner_agent="codex",
        source_agent="codex",
    )

    assert skipped["terminal"] is True
    assert skipped["consumption_plan"]["plan_status"] == "consumed"
    assert skipped["consumption_plan"]["failed_targets"] == []


def test_unregistered_target_is_rejected_before_plan_acceptance(monkeypatch, tmp_path):
    cfg = _patch_config(monkeypatch, tmp_path)
    from core.app.retrospective_consumption_router import RetrospectiveConsumptionRouter
    from core.app.retrospective_models import RetrospectiveDraft, RetrospectiveRecord

    draft = RetrospectiveDraft(
        recap_id="recap-unknown-target",
        task_id="task-unknown-target",
        title="复盘：未知消费者",
        lesson="必须拒绝没有 handler 的 target",
        goal="完成消费",
        actual="target 未注册",
        delta="缺少生产消费者",
        consumption_targets=["imaginary_consumer"],
    )

    with pytest.raises(ValueError, match="unregistered recap consumption targets"):
        RetrospectiveConsumptionRouter(
            db_path=cfg.database_dir / "recap_tasks.db"
        ).route_after_finalize(RetrospectiveRecord(draft=draft))

    with sqlite3.connect(str(cfg.database_dir / "recap_tasks.db")) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM recap_consumption_plans WHERE recap_id=?",
            (draft.recap_id,),
        ).fetchone()[0] == 0
