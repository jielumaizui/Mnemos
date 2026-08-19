from __future__ import annotations

import sqlite3

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.state_schema import initialize_cognitive_state_schema

from tests.integration.test_recap_trusted_completion import _confirmed_recap
from tests.unit.test_retrospective_workflow import _create_recap_task, _patch_config


def _search_access():
    return {
        "principal": PrincipalEnvelope(
            principal_id="mcp:codex:recap-feedback-test",
            agent="codex",
            host_kind="test",
            capability_id="recap-feedback-test",
            capabilities=frozenset({"memory_read", "memory_write"}),
            allowed_projects=frozenset({"mnemos"}),
        ),
        "narrowing": AccessNarrowing(project="mnemos"),
    }


def _recap_feedback(service, recap_id: str, feedback_type: str, **kwargs):
    """Call the authenticated recap-feedback seam used by Agora."""

    from core.config import get_config as current_get_config

    state_db = current_get_config().database_dir / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_db)
    return service.recap_feedback(
        recap_id,
        feedback_type,
        **kwargs,
        **_search_access(),
    )


def test_inaccurate_feedback_neutralizes_each_committed_domain_effect(
    monkeypatch,
    tmp_path,
):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService

    service = KiaApplicationService()
    recap_id = _confirmed_recap(service, task_id)
    finalized = service.recap_finalize(recap_id, confirmed_by_user=True)
    assert finalized["state"] == "consumed"
    from core.app.context_search import ContextAwareSearch

    search = ContextAwareSearch(wiki_base=cfg.wiki_dir)
    before = search.search(
        "verify_installation.py",
        allow_embedding=False,
        limit=10,
        **_search_access(),
    )
    assert finalized["page_path"] in {
        item.page_path for item in before
    }, search.get_last_query_trace()

    feedback = _recap_feedback(
        service,
        recap_id,
        "inaccurate",
        comment="这条复盘把原因归错了，需要停止后续影响。",
        source_agent="codex",
    )

    assert feedback["success"] is True, feedback
    assert feedback["terminal"] is True
    assert feedback["correction_status"] == "complete"
    assert feedback["feedback_event_id"]
    assert feedback["supersedes_ref"] == ""
    assert feedback["failed_targets"] == []
    assert feedback["pending_review_targets"] == []
    assert {
        item["canonical_target"] for item in feedback["correction_receipts"]
    } == {"knowledge_retrieval", "policy_patch", "follow_up", "persona"}
    assert all(item["status"] == "committed" for item in feedback["correction_receipts"])
    assert feedback["canonical_feedback"]["disposition"] == "proposal_eligible"
    assert {
        item["disposition"]
        for item in feedback["canonical_feedback"]["terminal_receipts"]
    } == {"proposal_committed"}

    with sqlite3.connect(str(cfg.database_dir / "policy_patches.db")) as conn:
        patch_status = conn.execute(
            "SELECT status FROM policy_patches WHERE source_id=?",
            (recap_id,),
        ).fetchone()[0]
    assert patch_status == "review"

    with sqlite3.connect(str(cfg.database_dir / "dialog_reminder.db")) as conn:
        reminder_status = conn.execute(
            "SELECT status FROM dialog_reminders WHERE issue_id=?",
            (f"recap-follow-up:{recap_id}",),
        ).fetchone()[0]
    assert reminder_status == "dismissed"

    with sqlite3.connect(str(cfg.database_dir / "recap_tasks.db")) as conn:
        superseded = conn.execute(
            """
            SELECT COUNT(*) FROM recap_effect_states
            WHERE recap_id=? AND status='superseded'
            """,
            (recap_id,),
        ).fetchone()[0]
    assert superseded == 4

    with sqlite3.connect(str(cfg.database_dir / "user_signals.db")) as conn:
        suppressions = conn.execute(
            """
            SELECT COUNT(*) FROM reflection_signal_suppressions AS suppressed
            JOIN reflection_signals AS signal ON signal.id=suppressed.signal_id
            WHERE signal.source LIKE ?
            """,
            (f"recap:{recap_id}%",),
        ).fetchone()[0]
    assert suppressions == 1

    after = search.search(
        "verify_installation.py",
        allow_embedding=False,
        limit=10,
        **_search_access(),
    )
    assert finalized["page_path"] not in {item.page_path for item in after}


def test_feedback_is_idempotent_and_conflicts_require_explicit_supersession(
    monkeypatch,
    tmp_path,
):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService

    service = KiaApplicationService()
    recap_id = _confirmed_recap(service, task_id)
    assert service.recap_finalize(recap_id, confirmed_by_user=True)["state"] == "consumed"

    first = _recap_feedback(
        service,
        recap_id,
        "inaccurate",
        comment="原因归类错误",
        source_agent="codex",
    )
    duplicate = _recap_feedback(
        service,
        recap_id,
        "inaccurate",
        comment="原因归类错误",
        source_agent="codex",
    )

    assert duplicate["feedback_event_id"] == first["feedback_event_id"]
    assert duplicate["correction_receipts"] == first["correction_receipts"]
    with sqlite3.connect(str(cfg.database_dir / "recap_tasks.db")) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM recap_feedback_events WHERE recap_id=?",
            (recap_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM recap_correction_receipts WHERE event_id=?",
            (first["feedback_event_id"],),
        ).fetchone()[0] == 4

    conflict = _recap_feedback(
        service,
        recap_id,
        "irrelevant",
        comment="与当前任务无关",
        source_agent="codex",
    )
    assert conflict["success"] is False
    assert conflict["error"] == "stale_reaction_supersedes"

    superseded = _recap_feedback(
        service,
        recap_id,
        "irrelevant",
        comment="与当前任务无关",
        source_agent="codex",
        supersedes_event_id=first["feedback_event_id"],
    )
    assert superseded["success"] is True
    assert superseded["terminal"] is True
    assert superseded["supersedes_ref"] == first["feedback_event_id"]

    stale_duplicate = _recap_feedback(
        service,
        recap_id,
        "inaccurate",
        comment="原因归类错误",
        source_agent="codex",
    )
    assert stale_duplicate["success"] is False
    assert stale_duplicate["error"] == "stale_reaction_supersedes"

    restored_chain = _recap_feedback(
        service,
        recap_id,
        "inaccurate",
        comment="原因归类错误",
        source_agent="codex",
        supersedes_event_id=superseded["feedback_event_id"],
    )
    assert restored_chain["success"] is True
    assert restored_chain["feedback_event_id"] not in {
        first["feedback_event_id"],
        superseded["feedback_event_id"],
    }
    assert restored_chain["supersedes_ref"] == superseded["feedback_event_id"]


def test_canonical_recap_bridge_executes_bound_policy_neutralization(
    monkeypatch,
    tmp_path,
):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService
    from core.cognitive.policy_patch import PolicyPatchStore

    service = KiaApplicationService()
    recap_id = _confirmed_recap(service, task_id)
    assert service.recap_finalize(recap_id, confirmed_by_user=True)["state"] == "consumed"
    calls = []
    original = PolicyPatchStore.record_feedback

    def tracked(self, *args, **kwargs):
        calls.append((args, kwargs))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PolicyPatchStore, "record_feedback", tracked)
    corrected = _recap_feedback(
        service,
        recap_id,
        "outdated",
        comment="这条规则已经过期",
        source_agent="codex",
    )

    assert corrected["success"] is True
    assert corrected["correction_status"] == "complete"
    assert len(calls) == 1
    assert calls[0][1]["material_action"] is not None
    policy = next(
        item
        for item in corrected["correction_receipts"]
        if item["canonical_target"] == "policy_patch"
    )
    assert policy["status"] == "committed"
    assert policy["evidence"]["patch_status"] == "review"


def test_negative_skip_feedback_restores_scheduler_with_a_durable_receipt(
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
        skip_reason="false_positive",
        owner_agent="codex",
        source_agent="codex",
    )
    assert skipped["success"] is True
    with sqlite3.connect(str(cfg.database_dir / "recap_tasks.db")) as conn:
        assert conn.execute(
            "SELECT status FROM recap_tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()[0] == "ignored"

    corrected = _recap_feedback(
        service,
        started["recap_id"],
        "inaccurate",
        comment="这次 false positive 判断错误，需要恢复任务。",
        source_agent="codex",
    )

    assert corrected["success"] is True, corrected
    scheduler_receipt = next(
        item
        for item in corrected["correction_receipts"]
        if item["canonical_target"] == "scheduler"
    )
    assert scheduler_receipt["status"] == "committed"
    assert scheduler_receipt["evidence"]["outcome"] == "restored_pending"
    with sqlite3.connect(str(cfg.database_dir / "recap_tasks.db")) as conn:
        task = conn.execute(
            "SELECT status, due_date FROM recap_tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        correction_count = conn.execute(
            "SELECT COUNT(*) FROM recap_scheduler_corrections"
        ).fetchone()[0]
    assert task[0] == "pending"
    assert correction_count == 1


def test_useful_feedback_has_a_durable_terminal_outcome_receipt(
    monkeypatch,
    tmp_path,
):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService

    service = KiaApplicationService()
    recap_id = _confirmed_recap(service, task_id)
    assert service.recap_finalize(recap_id, confirmed_by_user=True)["state"] == "consumed"

    useful = _recap_feedback(
        service,
        recap_id,
        "useful",
        comment="后续任务确实用到了这条复盘",
        source_agent="codex",
    )

    assert useful["success"] is True
    assert useful["correction_status"] == "complete"
    assert len(useful["correction_receipts"]) == 1
    assert useful["correction_receipts"][0]["canonical_target"] == "feedback_outcome"
    assert useful["correction_receipts"][0]["status"] == "committed"
    with sqlite3.connect(str(cfg.database_dir / "recap_tasks.db")) as conn:
        assert conn.execute(
            """
            SELECT outcome FROM recap_consumption_outcomes
            WHERE recap_id=? AND consumer='recap_feedback'
            """,
            (recap_id,),
        ).fetchone()[0] == "accepted"


def test_positive_feedback_effect_is_idempotent_after_receipt_gap(monkeypatch, tmp_path):
    cfg = _patch_config(monkeypatch, tmp_path)
    task_id = _create_recap_task(cfg)

    from core.application.kia import KiaApplicationService

    service = KiaApplicationService()
    recap_id = _confirmed_recap(service, task_id)
    assert service.recap_finalize(recap_id, confirmed_by_user=True)["state"] == "consumed"
    first = _recap_feedback(
        service,
        recap_id,
        "useful",
        comment="这条复盘在后续任务中生效",
        source_agent="codex",
    )
    command_id = first["correction_receipts"][0]["command_id"]

    # Simulate a crash after the target effect committed but before its receipt
    # was durably acknowledged by the outbox.
    with sqlite3.connect(str(cfg.database_dir / "recap_tasks.db")) as conn:
        conn.execute(
            """
            UPDATE recap_correction_commands
            SET status='retryable_failed', last_error='simulated receipt gap'
            WHERE command_id=?
            """,
            (command_id,),
        )
        conn.execute(
            "UPDATE recap_feedback_events SET status='retryable_failed' WHERE event_id=?",
            (first["feedback_event_id"],),
        )

    retried = _recap_feedback(
        service,
        recap_id,
        "useful",
        comment="这条复盘在后续任务中生效",
        source_agent="codex",
    )

    assert retried["success"] is True
    assert retried["correction_receipts"][0]["attempt_count"] == 2
    with sqlite3.connect(str(cfg.database_dir / "recap_tasks.db")) as conn:
        assert conn.execute(
            """
            SELECT COUNT(*) FROM recap_consumption_outcomes
            WHERE recap_id=? AND consumer='recap_feedback'
            """,
            (recap_id,),
        ).fetchone()[0] == 1
