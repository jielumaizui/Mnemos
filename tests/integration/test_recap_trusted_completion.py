from __future__ import annotations

import sqlite3

from tests.unit.test_retrospective_workflow import _create_recap_task, _patch_config


def _confirmed_recap(service, task_id: str) -> str:
    started = service.recap_start(
        task_id=task_id,
        source_agent="codex",
        owner_agent="codex",
        task_type="coding",
        project="mnemos",
    )
    service.recap_submit(
        started["recap_id"],
        {
            "goal_actual": "目标：完成配置修改\n实际：完成但漏跑验证",
            "cause_lesson": "流程缺口导致执行漏检",
            "next_handling": "下次配置修改必须先跑 verify_installation.py --json",
        },
        confirm_level="user_confirmed",
    )
    return started["recap_id"]


def test_enforced_recap_waits_for_committed_page_before_finalize(monkeypatch, tmp_path):
    cfg = _patch_config(monkeypatch, tmp_path)
    trusted_db = cfg.database_dir / "trusted_push.db"
    cfg.get = lambda key, default=None: {
        "trusted_push.mode": "enforce",
        "trusted_push.db_path": str(trusted_db),
    }.get(key, default)
    monkeypatch.setattr("core.trust.config.get_config", lambda: cfg)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService

    service = KiaApplicationService()
    recap_id = _confirmed_recap(service, task_id)
    pending = service.recap_finalize(recap_id, confirmed_by_user=True)

    assert pending["success"] is True
    assert pending["state"] == "proposal_pending"
    assert pending["terminal"] is False
    assert pending["indexed"] is False
    assert pending["consumption_plan"] is None
    assert not (cfg.wiki_dir / pending["page_path"]).exists()
    with sqlite3.connect(str(cfg.database_dir / "recap_tasks.db")) as conn:
        assert (
            conn.execute("SELECT status FROM recap_tasks WHERE task_id=?", (task_id,)).fetchone()[0]
            != "confirmed"
        )
        plan_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recap_consumption_plans'"
        ).fetchone()
        assert (
            not plan_table
            or conn.execute(
                "SELECT COUNT(*) FROM recap_consumption_plans WHERE recap_id=?", (recap_id,)
            ).fetchone()[0]
            == 0
        )


def test_consumer_routing_failure_keeps_recap_retryable(monkeypatch, tmp_path):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)
    from core.application.kia import KiaApplicationService

    service = KiaApplicationService()
    recap_id = _confirmed_recap(service, task_id)
    monkeypatch.setattr(
        "core.app.retrospective_consumption_router.RetrospectiveConsumptionRouter.route_after_finalize",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )

    failed = service.recap_finalize(recap_id, confirmed_by_user=True)

    assert failed["success"] is False
    assert failed["status"] == "retryable_failed"
    assert failed["terminal"] is False
    status = service.recap_status(recap_id=recap_id)
    assert status["state"] == "consumption_pending"


def test_committed_recap_proposal_resumes_to_finalized_with_plan(monkeypatch, tmp_path):
    cfg = _patch_config(monkeypatch, tmp_path)
    trusted_db = cfg.database_dir / "trusted_push.db"
    cfg.get = lambda key, default=None: {
        "trusted_push.mode": "enforce",
        "trusted_push.db_path": str(trusted_db),
    }.get(key, default)
    monkeypatch.setattr("core.trust.config.get_config", lambda: cfg)
    task_id = _create_recap_task(cfg)
    from core.application.kia import KiaApplicationService
    from core.trust.proposal_queue import ProposalQueue

    service = KiaApplicationService()
    recap_id = _confirmed_recap(service, task_id)
    pending = service.recap_finalize(recap_id, confirmed_by_user=True)
    proposal_id = pending["trusted_push"]["proposal_id"]
    page = cfg.wiki_dir / pending["page_path"]
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# committed recap", encoding="utf-8")
    ProposalQueue(trusted_db, wiki_base=cfg.wiki_dir).update_status(proposal_id, "committed")

    finalized = service.recap_finalize(recap_id, confirmed_by_user=True)

    assert finalized["status"] == "committed"
    assert finalized["state"] == "consumed"
    assert finalized["terminal"] is True
    assert finalized["indexed"] is True
    assert finalized["consumption_plan"] is not None


def test_rejected_recap_proposal_is_retryable_and_explicit_retry_gets_new_receipt(
    monkeypatch, tmp_path
):
    cfg = _patch_config(monkeypatch, tmp_path)
    trusted_db = cfg.database_dir / "trusted_push.db"
    cfg.get = lambda key, default=None: {
        "trusted_push.mode": "enforce",
        "trusted_push.db_path": str(trusted_db),
    }.get(key, default)
    monkeypatch.setattr("core.trust.config.get_config", lambda: cfg)
    task_id = _create_recap_task(cfg)
    from core.application.kia import KiaApplicationService
    from core.trust.proposal_queue import ProposalQueue

    service = KiaApplicationService()
    recap_id = _confirmed_recap(service, task_id)
    first = service.recap_finalize(recap_id, confirmed_by_user=True)
    first_id = first["trusted_push"]["proposal_id"]
    ProposalQueue(trusted_db, wiki_base=cfg.wiki_dir).update_status(first_id, "rejected")

    rejected = service.recap_finalize(recap_id, confirmed_by_user=True)
    retried = service.recap_finalize(recap_id, confirmed_by_user=True)

    assert rejected["status"] == "retryable_failed"
    assert rejected["terminal"] is False
    assert retried["state"] == "proposal_pending"
    assert retried["trusted_push"]["proposal_id"] != first_id


def test_finalized_recap_with_missing_page_reopens_as_retryable(monkeypatch, tmp_path):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)
    from core.application.kia import KiaApplicationService

    service = KiaApplicationService()
    recap_id = _confirmed_recap(service, task_id)
    finalized = service.recap_finalize(recap_id, confirmed_by_user=True)
    (cfg.wiki_dir / finalized["page_path"]).unlink()

    retried = service.recap_finalize(recap_id, confirmed_by_user=True)

    assert retried["success"] is False
    assert retried["state"] == "retryable_failed"
    assert retried["terminal"] is False
    assert retried["error"] == "finalized_retrospective_page_is_missing"
