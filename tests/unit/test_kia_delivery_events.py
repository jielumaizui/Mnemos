# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.application.kia import KiaApplicationService


class _FakeConfig:
    def __init__(self, tmp_path: Path):
        self.data_dir = tmp_path
        self.database_dir = tmp_path
        self.wiki_dir = tmp_path / "wiki"
        self.wiki_dir.mkdir(parents=True, exist_ok=True)

    def get(self, key, default=None):
        values = {
            "delivery.db_path": str(self.database_dir / "delivery_events.db"),
            "delivery.preference": "active",
            "delivery.profiles.active": {},
            "delivery.profiles.active.daily_total": 20,
            "delivery.profiles.active.per_task_total": 8,
            "delivery.profiles.active.per_task_hint": 8,
            "delivery.profiles.active.per_task_warn": 4,
            "delivery.profiles.active.force_open_daily": 2,
            "delivery.profiles.active.same_topic_cooldown_hours": 0,
            "delivery.profiles.active.dismiss_cooldown_days": 1,
            "trust.db_path": str(self.database_dir / "trust_decisions.db"),
        }
        return values.get(key, default)


def _patch_config(monkeypatch, tmp_path):
    # Import before replacing core.config.get_config so this module never
    # captures a test-local lambda as its teardown "original".
    import core.embeddings.index_manager as embedding_index_manager

    cfg = _FakeConfig(tmp_path)
    monkeypatch.setattr("core.config.get_config", lambda: cfg)
    monkeypatch.setattr("core.cognitive.delivery_router.get_config", lambda: cfg)
    monkeypatch.setattr("core.cognitive.trust_scorer.get_config", lambda: cfg)
    monkeypatch.setattr(embedding_index_manager, "get_config", lambda: cfg)
    return cfg


def _delivery_rows(tmp_path):
    with sqlite3.connect(tmp_path / "delivery_events.db") as conn:
        return conn.execute(
            """
            SELECT source, channel, decision, delivered_level
            FROM delivery_events
            ORDER BY created_at
            """
        ).fetchall()


def _principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="mcp:codex:preflight-test",
        agent="codex",
        host_kind="codex",
        capability_id="preflight-test",
        capabilities=frozenset({"memory_read"}),
    )


def test_preflight_inject_records_silent_delivery_event(
    monkeypatch,
    tmp_path,
):
    _patch_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "core.application.intelligence.IntelligenceApplicationService.context_aware_search",
        lambda *_args, **_kwargs: {
            "results": [
                {
                    "page_path": "06-Retrospectives/coding/test.md",
                    "title": "先跑测试",
                    "snippet": "先跑测试再提交",
                    "score": 0.9,
                }
            ]
        },
    )

    result = KiaApplicationService().preflight_inject(
        "coding",
        subtype="fix",
        principal=_principal(),
        narrowing=AccessNarrowing(),
    )

    assert result["loaded"] is True
    assert result["delivery_event_id"]
    assert result["delivery_decision"]["delivered_level"] == "silent"
    assert _delivery_rows(tmp_path) == [
        ("preflight_inject", "preflight_inject", "deliver", "silent")
    ]


def test_guard_check_alert_records_delivery_event(
    monkeypatch,
    tmp_path,
):
    _patch_config(monkeypatch, tmp_path)

    class _EmptyPreFlightInjector:
        def inject(self, task_type, subtype, time_window, context_text):
            return None

    monkeypatch.setattr("core.kia.prophasis.PreFlightInjector", _EmptyPreFlightInjector)

    result = KiaApplicationService().guard_check(
        user_message="请删除生产数据库",
        task_type="ops",
    )

    assert result["alert"] is True
    assert result["delivery_event_id"]
    assert result["delivery_decision"]["channel"] == "guard_check"
    rows = _delivery_rows(tmp_path)
    assert rows[0][0:3] == ("guard_check", "guard_check", "deliver")


def test_preflight_inject_without_principal_never_constructs_injector(monkeypatch, tmp_path):
    _patch_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        KiaApplicationService,
        "_active_policy_patches",
        lambda *args, **kwargs: [],
    )

    def forbidden_injector(*_args, **_kwargs):
        raise AssertionError("unauthenticated preflight must not construct injector")

    monkeypatch.setattr("core.kia.prophasis.PreFlightInjector", forbidden_injector)

    result = KiaApplicationService().preflight_inject("coding", subtype="fix")

    assert result["success"] is True
    assert result["loaded"] is False
    assert result["source"] == "access_denied"
    assert result["access_filter"] == {"principal_required": 1}


def test_preflight_fallback_uses_authorized_intelligence_search(monkeypatch, tmp_path):
    _patch_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        KiaApplicationService,
        "_active_policy_patches",
        lambda *args, **kwargs: [],
    )

    captured = {}

    def fake_search(_service, query, limit, *, principal, narrowing):
        captured.update(
            {
                "query": query,
                "limit": limit,
                "principal": principal,
                "narrowing": narrowing,
            }
        )
        return {
            "results": [
                {
                    "page_path": "03-Tech/preflight.md",
                    "title": "Preflight Fallback",
                    "snippet": "轻量启动期召回",
                    "score": 0.9,
                }
            ]
        }

    monkeypatch.setattr(
        "core.application.intelligence.IntelligenceApplicationService.context_aware_search",
        fake_search,
    )

    result = KiaApplicationService().preflight_inject(
        "coding",
        subtype="fix",
        principal=_principal(),
        narrowing=AccessNarrowing(),
    )

    assert result["success"] is True
    assert result["source"] == "general_wiki_fallback"
    assert captured["limit"] == 3
    assert captured["principal"] == _principal()


def test_guard_check_degrades_to_default_rules_when_injector_lock_times_out(
    monkeypatch,
    tmp_path,
):
    _patch_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        KiaApplicationService,
        "_active_policy_patches",
        lambda *args, **kwargs: [],
    )

    class _LockedPreFlightInjector:
        def __init__(self):
            raise sqlite3.OperationalError(
                "sqlite lock timeout for user_signals.db"
            )

    monkeypatch.setattr("core.kia.prophasis.PreFlightInjector", _LockedPreFlightInjector)

    result = KiaApplicationService().guard_check(
        user_message="请删除生产数据库",
        task_type="ops",
    )

    assert result["success"] is True
    assert result["alert"] is True
    assert result["level"] == "interrupt"
    assert result["severity"] == "critical"
    assert result["trigger_text"] == "删除生产"


def test_check_pending_recaps_records_delivery_decision_per_item(
    monkeypatch,
    tmp_path,
):
    _patch_config(monkeypatch, tmp_path)

    recap = SimpleNamespace(
        task_id="recap-1",
        topic="蒸馏失败复盘",
        source="system",
        severity="high",
        status="pending",
        target_page="08-Reminders/recap-1.md",
        age_days=4,
        same_type_count=1,
    )
    decision = SimpleNamespace(
        should_force_open=False,
        score=3,
        reason="severity=high(+2); age=4d(>=3,+1)",
        channel="dialog_reminder",
    )

    class _FakeForcedRetrospective:
        def get_pending_system_recaps(self):
            return [recap]

        def list_user_reminders(self):
            return []

        def should_force_open(self, _recap_item, _user_context):
            return decision

    monkeypatch.setattr(
        "core.app.forced_retrospective.ForcedRetrospective",
        _FakeForcedRetrospective,
    )

    result = KiaApplicationService().check_pending_recaps(limit=1)

    assert result["pending_count"] == 1
    item = result["items"][0]
    assert item["delivery_event_id"]
    assert item["delivery_decision"]["channel"] == "dialog_reminder"
    assert _delivery_rows(tmp_path) == [
        ("check_pending_recaps", "dialog_reminder", "deliver", "warn")
    ]
