import pytest


def test_page_index_scans_entire_vault(tmp_path):
    from core.kia.teiresias import PredictivePushEngine

    page = tmp_path / "03-Tech" / "python-debug.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        """---
类型: 问题-解决
关键词:
  核心概念: [调试]
  场景标签: [报错]
  工具实体: [Python]
---
# Python Debug

## 核心内容
排查 Python 报错时先定位堆栈和复现步骤。
""",
        encoding="utf-8",
    )

    engine = PredictivePushEngine(
        wiki_base=str(tmp_path),
        db_path=str(tmp_path / ".kg" / "push.db"),
    )

    decision = engine.decide_push("Python 报错怎么处理", session_id="s1")

    assert decision.should_push is True
    assert decision.matches[0].page_title == "Python Debug"


def test_refresh_index_clears_shared_page_index_cache(tmp_path):
    from core.kia.teiresias import PredictivePushEngine

    PredictivePushEngine._page_index_cache.clear()
    first = tmp_path / "first.md"
    first.write_text("# First\n\n## 核心内容\nfirst page", encoding="utf-8")

    engine = PredictivePushEngine(
        wiki_base=str(tmp_path),
        db_path=str(tmp_path / ".kg" / "push.db"),
    )
    assert [page["title"] for page in engine._get_page_index()] == ["First"]

    second = tmp_path / "second.md"
    second.write_text("# Second\n\n## 核心内容\nsecond page", encoding="utf-8")

    engine.refresh_index()

    assert [page["title"] for page in engine._get_page_index()] == ["First", "Second"]


def test_empty_session_id_applies_global_cooldown(tmp_path):
    from core.kia.teiresias import KnowledgeMatch, PredictivePushEngine, PushDecision

    engine = PredictivePushEngine(
        wiki_base=str(tmp_path),
        db_path=str(tmp_path / ".kg" / "push.db"),
    )
    engine.record_push(
        PushDecision(
            should_push=True,
            reason="existing",
            matches=[KnowledgeMatch(page_path="old.md", page_title="Old", match_score=0.9)],
        ),
        session_id="",
    )
    engine.match_knowledge = lambda signal: [
        KnowledgeMatch(page_path="new.md", page_title="New", match_score=0.9)
    ]

    decision = engine.decide_push("怎么调试 Python 报错", session_id="")

    assert decision.should_push is False
    assert "冷却中" in decision.reason


def test_emotional_state_lowers_dynamic_threshold(tmp_path):
    from core.kia.teiresias import PredictivePushEngine

    engine = PredictivePushEngine(
        wiki_base=str(tmp_path),
        db_path=str(tmp_path / ".kg" / "push.db"),
    )

    frustrated = engine.analyze_context("Python 报错卡住了")
    urgent = engine.analyze_context("线上环境马上处理")

    assert frustrated.emotional_state == "frustrated"
    assert urgent.emotional_state == "urgent"
    assert engine._get_dynamic_threshold(frustrated) == pytest.approx(0.35)
    assert engine._get_dynamic_threshold(urgent) == pytest.approx(0.55)


def test_emotional_state_is_recorded_in_push_reason(tmp_path):
    from core.kia.teiresias import KnowledgeMatch, PredictivePushEngine

    engine = PredictivePushEngine(
        wiki_base=str(tmp_path),
        db_path=str(tmp_path / ".kg" / "push.db"),
    )
    engine.match_knowledge = lambda signal: [
        KnowledgeMatch(page_path="incident.md", page_title="Incident", match_score=0.56)
    ]

    decision = engine.decide_push("线上环境马上处理", session_id="emotion")

    assert decision.should_push is True
    assert "情绪信号: urgent" in decision.reason


def test_named_session_id_keeps_per_session_cooldown(tmp_path):
    from core.kia.teiresias import KnowledgeMatch, PredictivePushEngine, PushDecision

    engine = PredictivePushEngine(
        wiki_base=str(tmp_path),
        db_path=str(tmp_path / ".kg" / "push.db"),
    )
    engine.record_push(
        PushDecision(
            should_push=True,
            reason="existing",
            matches=[KnowledgeMatch(page_path="old.md", page_title="Old", match_score=0.9)],
        ),
        session_id="s1",
    )
    engine.match_knowledge = lambda signal: [
        KnowledgeMatch(page_path="new.md", page_title="New", match_score=0.9)
    ]

    decision = engine.decide_push("怎么调试 Python 报错", session_id="s1")

    assert decision.should_push is False
    assert "冷却中" in decision.reason


def test_get_push_stats_counts_recent_push_responses(tmp_path):
    from core.kia.teiresias import KnowledgeMatch, PredictivePushEngine, PushDecision

    engine = PredictivePushEngine(
        wiki_base=str(tmp_path),
        db_path=str(tmp_path / ".kg" / "push.db"),
    )
    decision = PushDecision(
        should_push=True,
        reason="matched",
        matches=[KnowledgeMatch(page_path="one.md", page_title="One", match_score=0.9)],
    )

    engine.record_push(decision, session_id="s1", user_response="accept")
    engine.record_push(decision, session_id="s2", user_response="ignore")

    stats = engine.get_push_stats(days=7)

    assert stats["total_pushes"] == 2
    assert stats["response_distribution"] == {"accept": 1, "ignore": 1}
    assert stats["accept_rate"] == pytest.approx(0.5)


def test_denied_candidates_do_not_create_push_state_or_read_body(tmp_path, monkeypatch):
    from pathlib import Path

    from core.kia.teiresias import PredictivePushEngine

    page = tmp_path / "03-Tech" / "private.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        """---
类型: 问题-解决
关键词:
  场景标签: [报错]
---
# Forbidden body

PRIVATE-BODY-MUST-NOT-BE-READ
""",
        encoding="utf-8",
    )
    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if Path(path).resolve() == page.resolve():
            raise AssertionError("denied page body must not be read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    db_path = tmp_path / ".kg" / "push.db"
    engine = PredictivePushEngine(
        wiki_base=str(tmp_path),
        db_path=str(db_path),
    )

    decision = engine.decide_push(
        "怎么处理报错",
        candidate_path_filter=lambda _path: False,
    )

    assert decision.should_push is False
    assert not db_path.exists()
    assert not db_path.with_name("application_hub.db").exists()
