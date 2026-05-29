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


def test_empty_session_id_does_not_apply_global_cooldown(tmp_path):
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

    assert decision.should_push is True
    assert "冷却中" not in decision.reason


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


def test_orchestrator_push_handles_single_decision(tmp_path, monkeypatch):
    from core.kia.teiresias import PushDecision
    from core.orchestrator import Orchestrator

    class FakeEngine:
        def __init__(self, wiki_base=None):
            self.wiki_base = wiki_base

        def decide_push(self, context):
            return PushDecision(should_push=True, reason=f"ctx={context}")

    monkeypatch.setattr("core.kia.teiresias.PredictivePushEngine", FakeEngine)

    result = Orchestrator(wiki_base=str(tmp_path)).run_push(context="怎么处理报错")

    assert result["status"] == "ok"
    assert result["decisions"] == 1
    assert result["triggered"] == 1
    assert result["reason"] == "ctx=怎么处理报错"
