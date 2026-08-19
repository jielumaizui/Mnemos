import sqlite3
from types import SimpleNamespace
from unittest.mock import patch


def _patch_config(monkeypatch, tmp_path):
    cfg = SimpleNamespace()
    cfg.wiki_dir = tmp_path / "wiki"
    cfg.database_dir = tmp_path / "db"
    cfg.data_dir = tmp_path / "data"
    cfg.wiki_dir.mkdir(parents=True, exist_ok=True)
    cfg.database_dir.mkdir(parents=True, exist_ok=True)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.get = lambda key, default=None: default
    for target in [
        "core.config.get_config",
        "core.app.forced_retrospective.get_config",
        "core.app.retrospective_store.get_config",
        "core.app.retrospective_session_manager.get_config",
        "core.app.retrospective_skip_event_store.get_config",
        "core.app.retrospective_consumption_router.get_config",
        "core.cognitive.policy_patch.get_config",
        "core.trust.config.get_config",
        "core.app.context_search.get_config",
    ]:
        monkeypatch.setattr(target, lambda cfg=cfg: cfg)
    from core.persona.psyche import SignalStore

    SignalStore(
        db_path=cfg.database_dir / "user_signals.db",
        config=cfg,
        initialize_schema=True,
    ).close()
    return cfg


def _create_recap_task(cfg, topic="配置验证复盘"):
    from core.app.forced_retrospective import ForcedRetrospective

    forced = ForcedRetrospective(db_path=str(cfg.database_dir / "recap_tasks.db"))
    with patch.object(
        ForcedRetrospective,
        "_generate_reminder_page",
        return_value="08-Reminders/复盘提醒-test",
    ):
        return forced.create_system_recap(
            topic=topic,
            severity="high",
            context="改配置后漏跑验证。",
            suggested_points="- 固化验证 gate",
        )


def test_structured_recap_start_submit_finalize_writes_wiki_and_plan(
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

    assert started["success"] is True
    assert started["state"] == "q1_goal_actual"
    assert started["interaction_contract"]["must_ask_exactly_three_questions"] is True

    submitted = service.recap_submit(
        started["recap_id"],
        {
            "goal_actual": "目标：改配置后稳定运行\n实际：改完后没有跑验证，风险留到收尾才发现",
            "cause_lesson": "执行漏了，配置类修改必须有验证 gate",
            "next_handling": "以后改配置相关代码后必须运行 verify_installation.py --json 并确认通过",
        },
    )

    assert submitted["success"] is True
    assert submitted["state"] == "draft_generated"
    assert submitted["missing_fields"] == []
    assert "preflight" in submitted["draft"]["consumption_targets"]
    assert submitted["draft"]["action_items"][0]["owner"] == "codex"

    status = service.recap_status(recap_id=started["recap_id"])
    assert status["can_finalize"] is True
    assert status["answered_questions"] == [
        "goal_actual",
        "cause_lesson",
        "next_handling",
    ]

    finalized = service.recap_finalize(
        started["recap_id"],
        follow_up_at="2026-07-10T09:00:00",
        confirmed_by_user=True,
    )

    assert finalized["success"] is True, finalized
    assert finalized["indexed"] is True
    assert finalized["consumption_plan"]["targets"]
    assert any(
        item["canonical_target"] == "policy_patch"
        for item in finalized["consumption_plan"]["target_statuses"]
    )
    assert finalized["consumption_plan"]["outcomes"][0]["consumer"] == "policy_patch"
    assert finalized["consumption_plan"]["outcomes"][0]["outcome"] == "proposed"
    written = cfg.wiki_dir / finalized["page_path"]
    assert written.exists()
    content = written.read_text(encoding="utf-8")
    assert "schema: mnemos.retrospective.v1" in content
    assert "activation_rules:" in content
    assert "verify_installation.py --json" in content

    with sqlite3.connect(str(cfg.database_dir / "recap_tasks.db")) as conn:
        status_row = conn.execute(
            "SELECT status FROM recap_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        plan_row = conn.execute(
            "SELECT targets FROM recap_consumption_plans WHERE recap_id = ?",
            (started["recap_id"],),
        ).fetchone()
        receipt_row = conn.execute(
            """
            SELECT status, evidence_json FROM recap_consumption_commands
            WHERE recap_id = ? AND canonical_target = 'policy_patch'
            """,
            (started["recap_id"],),
        ).fetchone()
    with sqlite3.connect(str(cfg.database_dir / "policy_patches.db")) as conn:
        patch_row = conn.execute(
            "SELECT source_type, source_id, content, trigger FROM policy_patches"
        ).fetchone()
    assert status_row[0] == "confirmed"
    assert "guard" in plan_row[0]
    assert receipt_row[0] == "committed"
    assert '"outcome":"proposed"' in receipt_row[1]
    assert patch_row[0] == "retrospective"
    assert patch_row[1] == started["recap_id"]
    assert "verify_installation.py --json" in patch_row[2]
    assert "verify_installation.py" in patch_row[3]


def test_recap_finalize_enforce_submits_trusted_proposal_without_direct_write(
    monkeypatch,
    tmp_path,
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

    finalized = service.recap_finalize(
        started["recap_id"],
        follow_up_at="2026-07-10T09:00:00",
        confirmed_by_user=True,
    )

    page_path = cfg.wiki_dir / finalized["page_path"]
    assert finalized["success"] is True
    assert finalized["indexed"] is False
    assert finalized["trusted_push"]["action"] == "intercept"
    assert finalized["trusted_push"]["proposal_id"]
    assert not page_path.exists()
    proposals = ProposalQueue(trusted_db, wiki_base=cfg.wiki_dir).list()
    assert len(proposals) == 1
    assert proposals[0].candidate.source == "retrospective_store"
    assert proposals[0].candidate.target_path == str(page_path)


def test_recap_skip_records_reason_and_updates_task_policy(monkeypatch, tmp_path):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg, topic="误判提醒复盘")

    from core.application.kia import KiaApplicationService

    service = KiaApplicationService()
    started = service.recap_start(task_id=task_id, source_agent="codex", owner_agent="codex")
    skipped = service.recap_skip(
        recap_id=started["recap_id"],
        skip_reason="false_positive",
        user_note="这是我故意保留的提醒，不是问题",
        owner_agent="codex",
        source_agent="codex",
    )

    assert skipped["success"] is True
    assert skipped["skip_status"] == "false_positive"
    assert skipped["next_policy"] == "correct_trigger"
    assert skipped["consumption_targets"] == ["scheduler"]

    with sqlite3.connect(str(cfg.database_dir / "recap_tasks.db")) as conn:
        event_row = conn.execute(
            "SELECT skip_reason, next_policy FROM recap_skip_events WHERE event_id = ?",
            (skipped["event_id"],),
        ).fetchone()
        task_row = conn.execute(
            "SELECT status, context FROM recap_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    assert event_row == ("false_positive", "correct_trigger")
    assert task_row[0] == "ignored"
    assert "recap_skip" in task_row[1]
    assert not (cfg.database_dir / "mnemos.db").exists()


def test_recap_queue_close_records_status_event(
    monkeypatch,
    tmp_path,
):
    from core.app.forced_retrospective import ForcedRetrospective

    cfg = _patch_config(monkeypatch, tmp_path)
    forced = ForcedRetrospective(db_path=str(cfg.database_dir / "recap_tasks.db"))
    high_task = forced.create_system_recap(topic="高优先级复盘", severity="high")
    forced.create_system_recap(topic="中优先级复盘", severity="medium")

    pending = forced.list_recap_tasks(status="pending")
    assert [task.severity for task in pending[:2]] == ["high", "medium"]

    changed = forced.close_pending_recaps(
        "dismissed",
        severity="high",
        reason="historical distill failure already audited",
        actor="test",
    )

    assert changed == 1
    with sqlite3.connect(str(cfg.database_dir / "recap_tasks.db")) as conn:
        task_row = conn.execute(
            "SELECT status, context FROM recap_tasks WHERE task_id = ?",
            (high_task,),
        ).fetchone()
        event_row = conn.execute(
            "SELECT status, reason, actor FROM recap_task_events WHERE task_id = ?",
            (high_task,),
        ).fetchone()
    assert task_row[0] == "dismissed"
    assert "historical distill failure already audited" in task_row[1]
    assert event_row == ("dismissed", "historical distill failure already audited", "test")


def test_recap_claim_owner_rejects_conflicting_owner(monkeypatch, tmp_path):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService

    service = KiaApplicationService()
    started = service.recap_start(task_id=task_id, source_agent="codex", owner_agent="codex")

    same_owner = service.recap_claim_owner(started["recap_id"], "codex", "session-1")
    other_owner = service.recap_claim_owner(started["recap_id"], "claude", "session-2")

    assert same_owner["success"] is True
    assert other_owner["success"] is False
    assert other_owner["owner_agent"] == "codex"


def test_recap_start_and_submit_respect_owner_lock(monkeypatch, tmp_path):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService

    service = KiaApplicationService()
    started = service.recap_start(task_id=task_id, source_agent="codex", owner_agent="codex")

    conflict_start = service.recap_start(task_id=task_id, source_agent="claude", owner_agent="claude")
    conflict_submit = service.recap_submit(
        started["recap_id"],
        {
            "goal_actual": "目标：完成配置修改\n实际：完成但漏跑验证",
            "cause_lesson": "流程缺口导致执行漏检",
            "next_handling": "下次配置修改必须先跑验证 gate",
        },
        source_agent="claude",
    )

    assert conflict_start["success"] is False
    assert conflict_start["error"] == "owner_conflict"
    assert conflict_start["owner_agent"] == "codex"
    assert conflict_start["questions"] == []
    assert conflict_submit["success"] is False
    assert conflict_submit["error"] == "owner_conflict"
    status = service.recap_status(recap_id=started["recap_id"])
    assert status["answered_questions"] == []


def test_freeform_recap_answer_generates_complete_draft(monkeypatch, tmp_path):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService

    service = KiaApplicationService()
    started = service.recap_start(task_id=task_id, source_agent="codex", owner_agent="codex")

    submitted = service.recap_submit(
        started["recap_id"],
        {
            "freeform": "目标判断错了。下次先做小样本验证，不要直接全量改。",
        },
        source_agent="codex",
    )

    assert submitted["success"] is True
    assert submitted["state"] == "draft_generated"
    assert submitted["missing_fields"] == []
    assert "wrong_assumption" in submitted["draft"]["root_type"]
    assert "小样本验证" in submitted["draft"]["action_items"][0]["action"]


def test_finalize_rejects_incomplete_three_question_contract(monkeypatch, tmp_path):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService

    service = KiaApplicationService()
    started = service.recap_start(task_id=task_id, source_agent="codex", owner_agent="codex")
    service.recap_submit(
        started["recap_id"],
        {
            "goal_actual": "目标：完成部署\n实际：未完成",
            "cause_lesson": "执行漏了",
        },
    )

    finalized = service.recap_finalize(started["recap_id"], confirmed_by_user=True)

    assert finalized["success"] is False
    assert finalized["error"] == "contract_violation"
    assert "draft" in finalized["missing_fields"]


def test_confirmed_recap_rejects_late_answer_edits(monkeypatch, tmp_path):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService

    service = KiaApplicationService()
    started = service.recap_start(task_id=task_id, source_agent="codex", owner_agent="codex")
    confirmed = service.recap_submit(
        started["recap_id"],
        {
            "goal_actual": "目标：完成配置修改\n实际：完成但漏跑验证",
            "cause_lesson": "流程缺口导致执行漏检",
            "next_handling": "下次配置修改必须先跑验证 gate",
        },
        confirm_level="user_confirmed",
    )
    assert confirmed["state"] == "user_confirmed"

    rejected = service.recap_submit(
        started["recap_id"],
        {"next_handling": "覆盖掉已经确认的行动项"},
    )

    assert rejected["success"] is False
    assert rejected["error"] == "invalid_state"
    status = service.recap_status(recap_id=started["recap_id"])
    assert status["state"] == "user_confirmed"
    assert status["can_finalize"] is True


def test_finalized_recap_is_idempotent_and_cannot_be_skipped(
    monkeypatch,
    tmp_path,
):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService

    service = KiaApplicationService()
    started = service.recap_start(task_id=task_id, source_agent="codex", owner_agent="codex")
    service.recap_submit(
        started["recap_id"],
        {
            "goal_actual": "目标：完成配置修改\n实际：完成但漏跑验证",
            "cause_lesson": "流程缺口导致执行漏检",
            "next_handling": "下次配置修改必须先跑验证 gate",
        },
        confirm_level="user_confirmed",
    )
    finalized = service.recap_finalize(started["recap_id"], confirmed_by_user=True)
    assert finalized["success"] is True

    second_finalize = service.recap_finalize(started["recap_id"], confirmed_by_user=True)
    skipped = service.recap_skip(recap_id=started["recap_id"], skip_reason="false_positive")

    assert second_finalize["success"] is True
    assert second_finalize["already_finalized"] is True
    assert second_finalize["page_path"] == finalized["page_path"]
    assert skipped["success"] is False
    assert skipped["error"] == "recap_already_closed"
    status = service.recap_status(recap_id=started["recap_id"])
    assert status["state"] == "consumed"
    assert status["can_finalize"] is False
    with sqlite3.connect(str(cfg.database_dir / "recap_tasks.db")) as conn:
        task_status = conn.execute(
            "SELECT status FROM recap_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        has_skip_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'recap_skip_events'"
        ).fetchone()
        skip_count = (
            conn.execute(
                "SELECT COUNT(*) FROM recap_skip_events WHERE recap_id = ?",
                (started["recap_id"],),
            ).fetchone()[0]
            if has_skip_table
            else 0
        )
    assert task_status == "confirmed"
    assert skip_count == 0


def test_retrospective_store_uses_unique_paths_for_same_day_same_title(
    monkeypatch,
    tmp_path,
):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id_1 = _create_recap_task(cfg, topic="重复标题复盘")
    task_id_2 = _create_recap_task(cfg, topic="重复标题复盘")

    from core.app.retrospective_builder import RetrospectiveBuilder
    from core.app.retrospective_models import RetrospectiveRecord
    from core.app.retrospective_store import RetrospectiveStore
    from core.app.forced_retrospective import ForcedRetrospective

    forced = ForcedRetrospective(db_path=str(cfg.database_dir / "recap_tasks.db"))
    builder = RetrospectiveBuilder()
    answers = {
        "goal_actual": "目标：完成配置修改\n实际：完成但漏跑验证",
        "cause_lesson": "流程缺口导致执行漏检",
        "next_handling": "下次配置修改必须先跑验证 gate",
    }
    draft_1 = builder.build_draft(
        forced.get_recap_task(task_id_1),
        answers,
        recap_id="retro-same-title-1",
        owner_agent="codex",
    )
    draft_2 = builder.build_draft(
        forced.get_recap_task(task_id_2),
        answers,
        recap_id="retro-same-title-2",
        owner_agent="codex",
    )
    store = RetrospectiveStore(wiki_base=cfg.wiki_dir, db_path=cfg.database_dir / "recap_tasks.db")

    path_1 = store.save(RetrospectiveRecord(draft=draft_1, reviewed_at="2026-07-03T10:00:00"))
    path_2 = store.save(RetrospectiveRecord(draft=draft_2, reviewed_at="2026-07-03T11:00:00"))

    assert path_1 != path_2
    assert (cfg.wiki_dir / path_1).exists()
    assert (cfg.wiki_dir / path_2).exists()
    assert "retro-same-title-1" in (cfg.wiki_dir / path_1).read_text(encoding="utf-8")
    assert "retro-same-title-2" in (cfg.wiki_dir / path_2).read_text(encoding="utf-8")


def test_lower_level_retrospective_public_interfaces_are_live(monkeypatch, tmp_path):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.app.retrospective_builder import RetrospectiveBuilder
    from core.app.retrospective_consumption_router import RetrospectiveConsumptionRouter
    from core.app.retrospective_models import RetrospectiveRecord
    from core.app.retrospective_session_manager import RetrospectiveSessionManager
    from core.app.retrospective_skip_event_store import RetrospectiveSkipEventStore
    from core.app.retrospective_store import RetrospectiveStore

    manager = RetrospectiveSessionManager(db_path=cfg.database_dir / "recap_tasks.db")
    session = manager.start(task_id=task_id, owner_agent="codex", source_agent="codex")
    session = manager.submit_answer(
        session.recap_id,
        "goal_actual",
        "目标：完成配置修改\n实际：修改完成但未验证",
    )
    assert session.state == "q2_cause_lesson"
    session = manager.submit_answer(session.recap_id, "cause_lesson", "执行漏了验证")
    assert session.state == "q3_next_handling"
    session = manager.submit_answer(
        session.recap_id,
        "next_handling",
        "下次改配置后必须运行 verify_installation.py --json",
    )
    assert session.state == "draft_generated"
    assert session.draft is not None

    revised = RetrospectiveBuilder().revise_draft(session.draft, "根因是流程缺口，不是个人注意力问题")
    assert "流程缺口" in revised.lesson

    store = RetrospectiveStore(
        wiki_base=cfg.wiki_dir,
        db_path=cfg.database_dir / "recap_tasks.db",
    )
    store.mark_action_status(revised.recap_id, "action-1", "verified", "验证命令已加入收尾检查")

    router = RetrospectiveConsumptionRouter(db_path=cfg.database_dir / "recap_tasks.db")
    router.route_after_finalize(
        RetrospectiveRecord(
            draft=revised,
            source_agent="codex",
            owner_agent="codex",
            source_agents=["codex"],
            task_type="coding",
            severity="high",
        )
    )
    router.mark_consumed(revised.recap_id, "guard", "accepted", "命中配置修改场景")
    plan_matches = router.match_for_task("coding", "准备修改配置并部署", "core/config.py")
    assert plan_matches[0]["recap_id"] == revised.recap_id

    skip_store = RetrospectiveSkipEventStore(db_path=cfg.database_dir / "recap_tasks.db")
    event = skip_store.record_skip(
        recap_id=revised.recap_id,
        task_id=task_id,
        skip_reason="low_value",
        owner_agent="codex",
    )
    assert skip_store.derive_next_policy(event) == "lower_similar_trigger_weight"
    routed = skip_store.route_skip_event(event)
    assert routed.targets == ["scheduler"]

    with sqlite3.connect(str(cfg.database_dir / "recap_tasks.db")) as conn:
        action_row = conn.execute(
            "SELECT status FROM recap_action_status WHERE recap_id = ? AND action_id = ?",
            (revised.recap_id, "action-1"),
        ).fetchone()
        outcome_row = conn.execute(
            "SELECT outcome FROM recap_consumption_outcomes "
            "WHERE recap_id = ? AND consumer = 'guard'",
            (revised.recap_id,),
        ).fetchone()
    assert action_row[0] == "verified"
    assert outcome_row[0] == "accepted"


def test_recap_policy_patch_requires_explicit_trigger(monkeypatch, tmp_path):
    cfg = _patch_config(monkeypatch, tmp_path)

    from core.app.retrospective_consumption_router import RetrospectiveConsumptionRouter
    from core.app.retrospective_models import RetrospectiveDraft, RetrospectiveRecord

    draft = RetrospectiveDraft(
        recap_id="retro-no-trigger",
        task_id="task_no_trigger",
        title="复盘：泛化策略",
        lesson="下次先确认再继续",
        goal="完成任务",
        actual="需要确认",
        delta="确认不足",
        root_type=["process_gap"],
        root_cause="确认不足",
        next_handling="judgement_memory",
        activation_rules={"trigger_when": ["task_start"]},
        consumption_targets=["preflight", "guard"],
        evidence_refs=["recap_task://task_no_trigger/context"],
    )
    plan = RetrospectiveConsumptionRouter(db_path=cfg.database_dir / "recap_tasks.db").route_after_finalize(
        RetrospectiveRecord(draft=draft, task_type="coding", severity="high")
    )

    assert plan.targets == ["preflight", "guard"]
    assert plan.outcomes == [
        {
            "consumer": "policy_patch",
            "outcome": "skipped",
            "evidence": "missing_trigger",
        }
    ]
    assert not (cfg.database_dir / "policy_patches.db").exists()
