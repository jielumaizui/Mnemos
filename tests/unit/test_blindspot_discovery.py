# -*- coding: utf-8 -*-
"""
P1-3 单元测试 — Blindspot 修复 dataclass/dict 错配
以及 P1-1 盲区触发策略与闭环测试
"""

from types import SimpleNamespace
from unittest.mock import patch
import multiprocessing

import pytest

from core.access_policy import AccessNarrowing, PrincipalEnvelope


def _principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="mcp:codex:blindspot-test",
        agent="codex",
        host_kind="codex",
        capability_id="blindspot-test",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )


@pytest.fixture(autouse=True)
def _stub_authorized_context_search(monkeypatch):
    monkeypatch.setattr(
        "core.app.context_search.ContextAwareSearch.search",
        lambda *_args, **_kwargs: [],
    )


def _check(blindspots, query, **kwargs):
    return blindspots.check_blind_spot(
        query,
        principal=_principal(),
        narrowing=AccessNarrowing(),
        **kwargs,
    )


def _detect(blindspots, query):
    return blindspots._detect_blindspots(
        query,
        principal=_principal(),
        narrowing=AccessNarrowing(),
    )


def _multiprocess_cooldown_probe(args):
    db_path, query, session_id = args
    from core.app.blindspot_discovery import BlindspotDiscovery

    calls = 0

    def counted_search(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return []

    with patch(
        "core.app.context_search.ContextAwareSearch.search",
        side_effect=counted_search,
    ):
        result = _check(
            BlindspotDiscovery(db_path=db_path),
            query,
            session_id=session_id,
        )
    return result.reminder is not None, calls


class FakeBlindSpot:
    def __init__(self, type_, confidence=0.8, description=""):
        self.type = type_
        self.confidence = confidence
        self.description = description


class FakeProfile:
    def __init__(self, suspected=None, confirmed=None):
        self.suspected = suspected or []
        self.confirmed = confirmed or []


def test_knowledge_gap_detector_does_not_read_cognitive_profile():
    """知识覆盖检测不得把 Hamartia 画像写成知识缺口。"""
    from core.app.blindspot_discovery import BlindspotDiscovery

    bd = BlindspotDiscovery()
    fake_profile = FakeProfile(suspected=[FakeBlindSpot("framing", 0.8)])

    # [P1-FIX] Mock EmbeddingIndexManager.search and KnowledgeGraph.search to
    # avoid real SiliconFlow API calls (~350ms) and scanning 422 wiki files.
    with patch("core.embeddings.EmbeddingIndexManager.search", return_value=[]):
        with patch("core.kia.knowledge_graph.KnowledgeGraph.search", return_value=[]):
            with patch(
                "core.persona.hamartia.BlindSpotProfileManager._load_profile",
                return_value=fake_profile,
            ) as load_profile:
                results, notes = _detect(bd, "test query")

    assert results
    assert all(r.asset_type == "knowledge_coverage_gap" for r in results)
    assert not any(r.topic == "framing_rigidity" for r in results)
    load_profile.assert_not_called()
    assert notes == []


def test_cognitive_framing_signal_never_enters_knowledge_gap_store():
    """高置信 framing 假设仍由 Hamartia 独立拥有。"""
    from core.app.blindspot_discovery import BlindspotDiscovery

    bd = BlindspotDiscovery()
    fake_profile = FakeProfile(
        suspected=[
            FakeBlindSpot("framing", 0.9, "过度依赖单一视角"),
            FakeBlindSpot("temporal", 0.5),
        ]
    )

    # [P1-FIX] Mock EmbeddingIndexManager.search and KnowledgeGraph.search to
    # avoid real SiliconFlow API calls (~350ms) and scanning 422 wiki files.
    with patch("core.embeddings.EmbeddingIndexManager.search", return_value=[]):
        with patch("core.kia.knowledge_graph.KnowledgeGraph.search", return_value=[]):
            with patch(
                "core.persona.hamartia.BlindSpotProfileManager._load_profile",
                return_value=fake_profile,
            ) as load_profile:
                results, notes = _detect(bd, "test")

    framing = [r for r in results if r.topic == "framing_rigidity"]
    assert framing == []
    assert all(r.asset_type == "knowledge_coverage_gap" for r in results)
    load_profile.assert_not_called()
    assert notes == []


def test_check_blindspot_returns_degraded_info():
    """降级时返回 degraded=true 和 reasons"""
    from core.app.blindspot_discovery import BlindspotDiscovery

    bd = BlindspotDiscovery()
    with patch(
        "core.app.context_search.ContextAwareSearch.search",
        side_effect=RuntimeError("search down"),
    ):
        with patch(
            "core.persona.hamartia.BlindSpotProfileManager._load_profile",
            side_effect=RuntimeError("profile down"),
        ) as load_profile:
            result = _check(bd, "unknown concept")

    assert result.degraded is True
    assert result.degraded_reasons == ["授权知识搜索不可用: search down"]
    assert result.reminder is None
    load_profile.assert_not_called()


def test_check_blindspot_without_principal_never_enters_retrieval(monkeypatch, tmp_path):
    from core.app.blindspot_discovery import BlindspotDiscovery

    monkeypatch.setattr(
        "core.app.context_search.ContextAwareSearch.search",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not retrieve")),
    )
    result = BlindspotDiscovery(db_path=str(tmp_path / "blindspots.db")).check_blind_spot(
        "private topic"
    )

    assert result.reminder is None
    assert result.degraded_reasons == ["access principal required"]


# ---------------------------------------------------------------------------
# P1-1 新增测试：触发策略、状态机、闭环
# ---------------------------------------------------------------------------


def test_session_dedup_same_topic_same_session(tmp_path):
    """同一 topic 在同一 session 内只提醒一次"""
    from core.app.blindspot_discovery import BlindspotDiscovery

    db_path = tmp_path / "blindspots.db"
    bd = BlindspotDiscovery(db_path=str(db_path))

    # 模拟 KG 无结果，检测到盲区 "rustasync"
    with patch("core.embeddings.EmbeddingIndexManager.search", return_value=[]):
        with patch("core.kia.knowledge_graph.KnowledgeGraph.search", return_value=[]):
            with patch(
                "core.persona.hamartia.BlindSpotProfileManager._load_profile", return_value=None
            ):
                r1 = _check(bd, "rustasync", session_id="sess-1")
                r2 = _check(bd, "rustasync", session_id="sess-1")

    assert r1.reminder is not None
    assert r1.reminder.topic == "rustasync"
    # 同 session 内再次检查同一 topic，应被去重
    assert r2.reminder is None


def test_cooldown_blocks_context_search_before_billing_seam(tmp_path, monkeypatch):
    """The 100 repeated cooldown checks must never re-enter retrieval."""

    from core.app.blindspot_discovery import BlindspotDiscovery

    calls = 0

    def counted_search(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr("core.app.context_search.ContextAwareSearch.search", counted_search)
    discovery = BlindspotDiscovery(db_path=str(tmp_path / "blindspots.db"))

    assert _check(discovery, "rustasync", session_id="same-session").reminder
    for _ in range(100):
        assert _check(discovery, "rustasync", session_id="same-session").reminder is None

    assert calls == 1


def test_cooldown_survives_one_hundred_fresh_service_instances(
    tmp_path,
    monkeypatch,
):
    """The persisted reservation, not one object's memory, owns cooldown."""

    from core.app.blindspot_discovery import BlindspotDiscovery

    calls = 0

    def counted_search(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(
        "core.app.context_search.ContextAwareSearch.search",
        counted_search,
    )
    db_path = tmp_path / "blindspots.db"
    assert _check(
        BlindspotDiscovery(db_path=str(db_path)),
        "rustasync",
        session_id="cross-instance-session",
    ).reminder

    results = [
        _check(
            BlindspotDiscovery(db_path=str(db_path)),
            "rustasync",
            session_id="cross-instance-session",
        )
        for _ in range(100)
    ]

    assert all(result.reminder is None for result in results)
    assert calls == 1


def test_cooldown_survives_multiprocess_service_restarts(tmp_path):
    """Independent spawned processes must reject before the retrieval seam."""

    from core.app.blindspot_discovery import BlindspotDiscovery

    db_path = tmp_path / "blindspots.db"
    query = "rustasync"
    session_id = "cross-process-session"
    assert _check(
        BlindspotDiscovery(db_path=str(db_path)),
        query,
        session_id=session_id,
    ).reminder

    context = multiprocessing.get_context("spawn")
    with context.Pool(processes=4) as pool:
        results = pool.map(
            _multiprocess_cooldown_probe,
            [(str(db_path), query, session_id)] * 8,
        )

    assert results == [(False, 0)] * 8


def test_low_evidence_gap_never_persists(tmp_path, monkeypatch):
    """A detector result below the admission threshold must not create a row."""

    from core.app.blindspot_discovery import BlindspotDiscovery

    low_evidence_issue = SimpleNamespace(
        issue_type="knowledge_gap",
        dimension="missing_topic",
        scope_key="",
        page="weak-topic",
        description="weak evidence",
        evidence_refs=(),
    )
    discovery = BlindspotDiscovery(db_path=str(tmp_path / "blindspots.db"))
    low_evidence_issue.scope_key = discovery._asset_scope(
        principal=_principal(), narrowing=AccessNarrowing()
    ).key
    monkeypatch.setattr(
        "core.app.blindspot_discovery.KnowledgeImmuneSystem.detect_knowledge_gaps",
        lambda *_args, **_kwargs: [low_evidence_issue],
    )

    result = _check(discovery, "weak topic", session_id="session")

    assert result.reminder is None
    assert discovery.list_current() == []


def test_check_blindspot_returns_reminded_timestamp_contract(tmp_path):
    """提醒发生时，返回对象和结构化结果都应暴露 reminded_at。"""
    from datetime import datetime

    from core.app.blindspot_discovery import BlindspotDiscovery
    from core.app.blindspot_response_builder import BlindspotResponseBuilder

    db_path = tmp_path / "blindspots.db"
    bd = BlindspotDiscovery(db_path=str(db_path))

    with patch("core.embeddings.EmbeddingIndexManager.search", return_value=[]):
        with patch("core.kia.knowledge_graph.KnowledgeGraph.search", return_value=[]):
            with patch(
                "core.persona.hamartia.BlindSpotProfileManager._load_profile", return_value=None
            ):
                result = _check(bd, "rustasync", session_id="sess-1")

    assert result.reminder is not None
    assert result.reminder.status == "reminded"
    assert result.reminder.reminded_at
    datetime.fromisoformat(result.reminder.reminded_at)

    tool_result = BlindspotResponseBuilder.build_tool_result(
        result.reminder,
        suggested_query=result.suggested_query,
    )
    assert tool_result["reminded_at"] == result.reminder.reminded_at


def test_session_dedup_resets_in_new_session(tmp_path):
    """换 session 后可以再次提醒同一 topic"""
    from core.app.blindspot_discovery import BlindspotDiscovery

    db_path = tmp_path / "blindspots.db"
    bd = BlindspotDiscovery(db_path=str(db_path))

    with patch("core.embeddings.EmbeddingIndexManager.search", return_value=[]):
        with patch("core.kia.knowledge_graph.KnowledgeGraph.search", return_value=[]):
            with patch(
                "core.persona.hamartia.BlindSpotProfileManager._load_profile", return_value=None
            ):
                r1 = _check(bd, "rustasync", session_id="sess-1")
                r2 = _check(bd, "rustasync", session_id="sess-2")

    assert r1.reminder is not None
    assert r2.reminder is not None


def test_fallback_cooldown_without_session_id(tmp_path):
    """无 session_id 时使用短冷却兜底"""
    from core.app.blindspot_discovery import BlindspotDiscovery

    db_path = tmp_path / "blindspots.db"
    bd = BlindspotDiscovery(db_path=str(db_path))

    with patch("core.embeddings.EmbeddingIndexManager.search", return_value=[]):
        with patch("core.kia.knowledge_graph.KnowledgeGraph.search", return_value=[]):
            with patch(
                "core.persona.hamartia.BlindSpotProfileManager._load_profile", return_value=None
            ):
                r1 = _check(bd, "rustasync")
                r2 = _check(bd, "rustasync")

    assert r1.reminder is not None
    # 无 session_id 时，5 分钟冷却内重复提醒应被抑制
    assert r2.reminder is None


def test_suggested_query_returned(tmp_path):
    """check_blind_spot 返回 suggested_query"""
    from core.app.blindspot_discovery import BlindspotDiscovery

    db_path = tmp_path / "blindspots.db"
    bd = BlindspotDiscovery(db_path=str(db_path))

    with patch("core.embeddings.EmbeddingIndexManager.search", return_value=[]):
        with patch("core.kia.knowledge_graph.KnowledgeGraph.search", return_value=[]):
            with patch(
                "core.persona.hamartia.BlindSpotProfileManager._load_profile", return_value=None
            ):
                result = _check(bd, "rustasync best practices", session_id="sess-1")

    assert result.reminder is not None
    assert result.suggested_query
    assert "rustasync" in result.suggested_query.lower()


def test_status_transitions(tmp_path):
    """状态机：reminded → investigating → resolved"""
    from core.app.blindspot_discovery import BlindspotDiscovery

    db_path = tmp_path / "blindspots.db"
    bd = BlindspotDiscovery(db_path=str(db_path))

    with patch("core.embeddings.EmbeddingIndexManager.search", return_value=[]):
        with patch("core.kia.knowledge_graph.KnowledgeGraph.search", return_value=[]):
            with patch(
                "core.persona.hamartia.BlindSpotProfileManager._load_profile", return_value=None
            ):
                _check(bd, "rustasync", session_id="sess-1")

    # 用户确认搜索
    assert bd.mark_investigating("rustasync") is True
    # 裸字符串/页面名不是独立覆盖证据，不能关闭。
    assert (
        bd.mark_resolved(
            "rustasync",
            resolved_by_page="00-Inbox/Rust-async-runtime.md",
            resolution_evidence=("coverage-recheck:test-receipt",),
        )
        is False
    )
    assert bd.list_current()[0]["status"] == "investigating"


def test_resolve_by_wiki_page_requires_exact_coverage_receipt(tmp_path):
    """只有独立覆盖复核点名 exact asset_id 才能关闭缺口。"""
    import hashlib
    import json
    import sqlite3

    from core.app.blindspot_asset_schema import HEAD_TABLE, REVISION_TABLE
    from core.app.blindspot_discovery import BlindspotDiscovery
    from core.cognitive.user_model_assets import AssetScope

    db_path = tmp_path / "blindspots.db"
    bd = BlindspotDiscovery(db_path=str(db_path), wiki_base=str(tmp_path))

    with patch("core.embeddings.EmbeddingIndexManager.search", return_value=[]):
        with patch("core.kia.knowledge_graph.KnowledgeGraph.search", return_value=[]):
            with patch(
                "core.persona.hamartia.BlindSpotProfileManager._load_profile", return_value=None
            ):
                check = _check(bd, "rustasync", session_id="sess-1")

    assert check.reminder is not None
    asset_id = check.reminder.asset_id
    bd.mark_investigating("rustasync", asset_id=asset_id)
    current = bd.list_current()[0]

    page_path = tmp_path / "Rust-async-runtime.md"
    page_path.write_text("# Rust async runtime\n", encoding="utf-8")
    content_hash = "sha256:" + hashlib.sha256(page_path.read_bytes()).hexdigest()
    resolved_count = bd.resolve_by_wiki_page(
        str(page_path),
        canonical_revision_id="wiki-revision-1",
        projection_receipt_id="projection-receipt-1",
        content_hash=content_hash,
        coverage_evidence=(
            {
                "receipt_id": "coverage-recheck-1",
                "asset_id": asset_id,
                "gap_revision_id": current["revision_id"],
                "scope_key": AssetScope(
                    scope_type=current["scope_type"],
                    scope_id=current["scope_id"],
                    purpose=current["purpose"],
                    principal_id=current["principal_id"],
                ).key,
                "verifier_id": "knowledge-coverage-auditor-v1",
                "verification_method": "authorized-context-requery",
                "content_hash": content_hash,
                "verified_at": "2026-07-23T00:00:00+00:00",
                "outcome": "covered",
            },
        ),
    )
    assert resolved_count == 1

    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            f"""SELECT r.status, r.resolution_evidence_json, r.supersedes_revision_id
                FROM {HEAD_TABLE} h
                JOIN {REVISION_TABLE} r ON r.revision_id=h.revision_id
                WHERE h.asset_id=?""",
            (asset_id,),
        ).fetchone()
    assert row[0] == "resolved"
    evidence = json.loads(row[1])
    assert "coverage-recheck:coverage-recheck-1" in evidence
    assert row[2]


def test_resolve_by_wiki_page_no_match(tmp_path):
    """不匹配的 wiki 页面不关闭盲区"""
    from core.app.blindspot_discovery import BlindspotDiscovery

    db_path = tmp_path / "blindspots.db"
    bd = BlindspotDiscovery(db_path=str(db_path))

    with patch("core.embeddings.EmbeddingIndexManager.search", return_value=[]):
        with patch("core.kia.knowledge_graph.KnowledgeGraph.search", return_value=[]):
            with patch(
                "core.persona.hamartia.BlindSpotProfileManager._load_profile", return_value=None
            ):
                _check(bd, "rustasync", session_id="sess-1")

    resolved_count = bd.resolve_by_wiki_page("00-Inbox/Python-decorators.md")
    assert resolved_count == 0


def test_ignore_creates_cooldown(tmp_path):
    """用户忽略后，新 session 内仍会被 7 天冷却拦截"""
    from core.app.blindspot_discovery import BlindspotDiscovery

    db_path = tmp_path / "blindspots.db"
    bd = BlindspotDiscovery(db_path=str(db_path))

    with patch("core.embeddings.EmbeddingIndexManager.search", return_value=[]):
        with patch("core.kia.knowledge_graph.KnowledgeGraph.search", return_value=[]):
            with patch(
                "core.persona.hamartia.BlindSpotProfileManager._load_profile", return_value=None
            ):
                _check(bd, "rustasync", session_id="sess-1")

    bd.mark_ignored("rustasync")

    with patch("core.embeddings.EmbeddingIndexManager.search", return_value=[]):
        with patch("core.kia.knowledge_graph.KnowledgeGraph.search", return_value=[]):
            with patch(
                "core.persona.hamartia.BlindSpotProfileManager._load_profile", return_value=None
            ):
                result = _check(bd, "rustasync", session_id="sess-2")
    assert result.reminder is None


# ---------------------------------------------------------------------------
# BlindspotResponseBuilder 测试
# ---------------------------------------------------------------------------


def test_response_builder_generates_prompt():
    """ResponseBuilder 生成自然语言提示"""
    from core.app.blindspot_discovery import BlindSpotReminder
    from core.app.blindspot_response_builder import BlindspotResponseBuilder

    reminder = BlindSpotReminder(
        topic="rustasync",
        description="知识库中缺少关于「rustasync」的记录",
        confidence=0.72,
        status="reminded",
        detected_at="2026-06-20T21:00:00",
    )
    prompt = BlindspotResponseBuilder.build_prompt(reminder, suggested_query="rust async runtime")
    assert "rustasync" in prompt or "rust async" in prompt
    assert "查一下" in prompt or "搜索" in prompt
    assert "忽略" in prompt or "不用" in prompt


def test_response_builder_structured_result():
    """ResponseBuilder 生成结构化结果"""
    from core.app.blindspot_discovery import BlindSpotReminder
    from core.app.blindspot_response_builder import BlindspotResponseBuilder

    reminder = BlindSpotReminder(
        topic="rustasync",
        description="知识库中缺少关于「rustasync」的记录",
        confidence=0.72,
        status="reminded",
        detected_at="2026-06-20T21:00:00",
    )
    result = BlindspotResponseBuilder.build_tool_result(
        reminder, suggested_query="rust async runtime"
    )
    assert result["blindspot_found"] is True
    assert result["topic"] == "rustasync"
    assert result["expected_user_actions"] == ["search", "ignore"]
    assert result["prompt_for_user"]


def test_blindspot_reminder_is_actionable_status_contract():
    """BlindSpotReminder exposes the status policy used by callers."""
    from core.app.blindspot_discovery import BlindSpotReminder

    base = {
        "topic": "rustasync",
        "description": "知识库中缺少关于「rustasync」的记录",
        "confidence": 0.72,
        "detected_at": "2026-06-20T21:00:00",
    }

    assert BlindSpotReminder(status="detected", **base).is_actionable is True
    assert BlindSpotReminder(status="reminded", **base).is_actionable is True
    assert BlindSpotReminder(status="investigating", **base).is_actionable is False
    assert BlindSpotReminder(status="resolved", **base).is_actionable is False
    assert BlindSpotReminder(status="ignored", **base).is_actionable is False


def test_response_builder_intent_recognition():
    """ResponseBuilder 能识别用户确认/忽略意图"""
    from core.app.blindspot_response_builder import BlindspotResponseBuilder

    assert BlindspotResponseBuilder.is_confirm("查一下")
    assert BlindspotResponseBuilder.is_confirm("记录")
    assert BlindspotResponseBuilder.is_ignore("忽略")
    assert BlindspotResponseBuilder.is_ignore("不用")
    assert not BlindspotResponseBuilder.is_confirm("不用")
