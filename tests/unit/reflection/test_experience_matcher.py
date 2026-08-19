"""Tests for core.reflection.experience_matcher."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.reflection.experience_matcher import ExperienceMatcher, ExperienceMatch
from core.reflection.models import (
    CognitiveShift,
    InsightSnapshot,
    ReflectionRecord,
    ReflectionTrigger,
)


def _make_record(record_id, user_query, trigger_event, dims=None, insight_summary=""):
    dims = dims or ["attention", "decisions"]
    return ReflectionRecord(
        id=record_id,
        created_at=datetime(2026, 6, 10, 10, 0, 0),
        trigger=ReflectionTrigger.NEW_PROJECT,
        trigger_event=trigger_event,
        user_query=user_query,
        mirror_dimensions=dims,
        insight=(
            InsightSnapshot(
                summary=insight_summary,
                key_points=["kp"],
                dimensions_involved=dims,
            )
            if insight_summary
            else None
        ),
    )


def _principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="mcp:codex:experience-test",
        agent="codex",
        host_kind="test",
        capability_id="experience-test",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def _narrowing() -> AccessNarrowing:
    return AccessNarrowing(session_id="session-1", project="mnemos")


def _secure_store(records=None, shifts=None):
    store = MagicMock()
    store.authorized_get_latest.return_value = (records or [], {"authorized_count": len(records or [])})
    store.authorized_get_shifts.return_value = (shifts or [], {"authorized_count": len(shifts or [])})
    return store


def _find(matcher, query, **kwargs):
    return matcher.find_similar(
        query,
        principal=_principal(),
        narrowing=_narrowing(),
        **kwargs,
    )


def test_find_similar_returns_empty_when_no_candidates():
    store = _secure_store()

    matcher = ExperienceMatcher(reflection_store=store)
    matches = _find(matcher, "启动新项目")
    assert matches == []


def test_find_similar_ranks_by_relevance():
    records = [
        _make_record(
            "r1",
            user_query="我要重构这个系统",
            trigger_event="用户说要重构系统",
            insight_summary="重构需要大量测试覆盖",
        ),
        _make_record(
            "r2",
            user_query="今天天气不错",
            trigger_event="闲聊",
            insight_summary="",
        ),
    ]
    store = _secure_store(records)

    matcher = ExperienceMatcher(reflection_store=store)
    matches = _find(matcher, "重构老项目", top_k=5)

    assert len(matches) == 2
    # r1 should score higher because it contains 重构
    assert matches[0].source_id == "r1"
    assert matches[0].score > matches[1].score


def test_find_similar_applies_scene_role_dimension_boosts():
    records = [
        _make_record(
            "r1",
            user_query="builder 模式重构项目",
            trigger_event="用户以 builder 角色重构项目",
            dims=["attention", "growth"],
            insight_summary="builder 角色偏好重构",
        ),
        _make_record(
            "r2",
            user_query="普通查询",
            trigger_event="普通触发",
            dims=["decisions"],
            insight_summary="",
        ),
    ]
    store = _secure_store(records)

    matcher = ExperienceMatcher(reflection_store=store)
    matches = _find(
        matcher,
        "builder 重构项目",
        scene="new_project",
        role="builder",
        dimensions=["growth"],
        top_k=5,
    )

    assert matches[0].source_id == "r1"
    # Scene/role/dimension boost should make r1 significantly higher
    assert matches[0].score > matches[1].score


def test_find_similar_returns_top_k():
    records = [
        _make_record(
            f"r{i}",
            user_query=f"项目{i} 描述",
            trigger_event=f"事件{i}",
            insight_summary=f"洞察{i}",
        )
        for i in range(10)
    ]
    store = _secure_store(records)

    matcher = ExperienceMatcher(reflection_store=store)
    matches = _find(matcher, "项目描述", top_k=3)
    assert len(matches) == 3


def test_find_similar_includes_cognitive_shifts():
    store = _secure_store(
        shifts=[
            CognitiveShift(
                dimension="growth",
                shift_type="role_change_to_manager",
                from_state="开发者",
                to_state="管理者",
                confidence=0.8,
                evidence=["带团队", "做决策"],
                first_seen_at=datetime(2026, 1, 1, 0, 0, 0),
                shift_detected_at=datetime(2026, 6, 10, 0, 0, 0),
            )
        ]
    )

    matcher = ExperienceMatcher(reflection_store=store)
    matches = _find(matcher, "管理者 角色变化", top_k=5)

    assert len(matches) == 1
    assert matches[0].source_type == "cognitive_shift"
    assert "管理者" in matches[0].summary


def test_experience_match_to_dict():
    match = ExperienceMatch(
        source_type="reflection",
        source_id="r1",
        title="旧 Reflection",
        summary="summary",
        score=0.8577,
        metadata={"trigger": "new_project"},
    )
    d = match.to_dict()
    assert d["source_type"] == "reflection"
    assert d["source_id"] == "r1"
    assert d["score"] == 0.8577
    assert d["metadata"] == {"trigger": "new_project"}


def test_find_similar_with_embedding_client_uses_semantic_scores():
    records = [
        _make_record(
            "r1",
            user_query=" completely unrelated text ",
            trigger_event="unrelated",
            insight_summary="unrelated",
        ),
    ]
    store = _secure_store(records)

    embedding_client = MagicMock()
    # Same embedding for query and candidate -> cosine similarity 1.0
    embedding_client.embed.return_value = [[1.0, 0.0], [1.0, 0.0]]

    matcher = ExperienceMatcher(
        reflection_store=store,
        embedding_client=embedding_client,
    )
    matches = _find(matcher, "query", top_k=5)

    assert len(matches) == 1
    embedding_client.embed.assert_called_once()
    # base_score = 1.0*0.6 + 0*0.4 = 0.6 (no keyword overlap)
    assert matches[0].score == pytest.approx(0.6)


def test_find_similar_embedding_failure_falls_back_to_keyword():
    records = [
        _make_record(
            "r1",
            user_query="重构系统",
            trigger_event="重构",
            insight_summary="",
        ),
    ]
    store = _secure_store(records)

    embedding_client = MagicMock()
    embedding_client.embed.side_effect = RuntimeError("embedding failed")

    matcher = ExperienceMatcher(
        reflection_store=store,
        embedding_client=embedding_client,
    )
    matches = _find(matcher, "重构项目", top_k=5)

    assert len(matches) == 1
    # keyword score should still be positive
    assert matches[0].score > 0


def test_find_similar_does_not_read_retrospectives_outside_the_wiki_acl_seam(tmp_path):
    retro_dir = tmp_path / "06-Retrospectives"
    retro_dir.mkdir()
    (retro_dir / "2026-06-01.md").write_text("本次重构收获很大", encoding="utf-8")

    store = _secure_store()

    matcher = ExperienceMatcher(reflection_store=store, wiki_dir=str(tmp_path))
    matches = _find(matcher, "重构收获", top_k=5)

    assert matches == []


class TestExperienceMatcherEdgePaths:
    def test_semantic_scores_with_empty_candidates(self):
        matcher = ExperienceMatcher(reflection_store=MagicMock())
        client = MagicMock()
        scores = matcher._semantic_scores("query", [])
        assert scores == []
        client.embed.assert_not_called()

    def test_semantic_scores_zero_norm_embeddings(self):
        matcher = ExperienceMatcher(reflection_store=MagicMock())
        client = MagicMock()
        client.embed.return_value = [[0.0, 0.0], [0.0, 0.0]]
        matcher.embedding_client = client
        scores = matcher._semantic_scores("query", ["candidate"])
        assert scores == [0.0]

    def test_keyword_scores_empty_query_tokens(self):
        matcher = ExperienceMatcher(reflection_store=MagicMock())
        scores = matcher._keyword_scores("!@#", ["some text"])
        assert scores == [0.0]

    def test_keyword_scores_empty_candidate_tokens(self):
        matcher = ExperienceMatcher(reflection_store=MagicMock())
        scores = matcher._keyword_scores("hello world", ["!@#"])
        assert scores[0] == 0.0

    def test_tokenize_empty_string(self):
        assert ExperienceMatcher._tokenize("") == []

    def test_gather_candidates_survives_store_exception(self):
        store = MagicMock()
        store.authorized_get_latest.side_effect = RuntimeError("store down")
        store.authorized_get_shifts.return_value = ([], {})

        matcher = ExperienceMatcher(reflection_store=store)
        candidates = matcher._gather_candidates(principal=_principal(), narrowing=_narrowing())
        assert candidates == []

    def test_gather_candidates_does_not_open_retrospective_files(self, tmp_path):
        retro_dir = tmp_path / "06-Retrospectives"
        retro_dir.mkdir()
        retro_file = retro_dir / "bad.md"
        retro_file.write_text("content", encoding="utf-8")

        store = _secure_store()

        matcher = ExperienceMatcher(reflection_store=store, wiki_dir=str(tmp_path))

        with patch("pathlib.Path.read_text", side_effect=PermissionError("cannot read")):
            candidates = matcher._gather_candidates(principal=_principal(), narrowing=_narrowing())

        assert candidates == []

    def test_candidate_text_summary_id_metadata_for_unknown_candidate(self):
        matcher = ExperienceMatcher(reflection_store=MagicMock())
        unknown = object()
        assert matcher._candidate_text(unknown) == str(unknown)
        title, summary = matcher._candidate_summary(unknown)
        assert title == "Unknown"
        assert summary == str(unknown)[:300]
        assert matcher._candidate_id(unknown) == str(id(unknown))
        assert matcher._candidate_metadata(unknown) == {}

    def test_compute_boost_with_none_scene_role_dimensions(self):
        record = _make_record("r1", "query", "event", dims=["attention"])
        boost = ExperienceMatcher._compute_boost(record, None, None, None)
        assert boost == 0.0

    def test_compute_boost_with_unknown_candidate(self):
        boost = ExperienceMatcher._compute_boost(object(), "scene", "role", ["dim"])
        assert boost == 0.0

    def test_find_similar_with_empty_text_candidates(self):
        records = [
            _make_record("r1", "", "", dims=[], insight_summary=""),
        ]
        store = _secure_store(records)

        matcher = ExperienceMatcher(reflection_store=store)
        matches = _find(matcher, "alsoempty", top_k=5)
        # keyword score should be 0 and all-zero semantic -> still returns one match with 0 score
        assert len(matches) == 1
        assert matches[0].score == pytest.approx(0.0)

    def test_find_similar_records_and_shifts_both_fail(self):
        store = MagicMock()
        store.authorized_get_latest.side_effect = RuntimeError("latest failed")
        store.authorized_get_shifts.side_effect = RuntimeError("shifts failed")

        matcher = ExperienceMatcher(reflection_store=store)
        candidates = matcher._gather_candidates(principal=_principal(), narrowing=_narrowing())
        assert candidates == []
