from core.sync_framework.agent_source import Turn
from core.sync_framework.sync_engine import build_turn_markdown
from core.hephaestus.wiki_builder import reconstruct_session


def test_reconstruct_session_preserves_tool_results_from_markdown():
    content = build_turn_markdown(
        Turn(
            turn_number=0,
            user_content="帮我跑测试",
            assistant_content="我跑完了，下面是结果。",
            tool_calls=[{"name": "pytest", "input": {"path": "tests/unit"}}],
            tool_results=[{"stdout": "tests/unit/test_demo.py::test_ok PASSED", "stderr": ""}],
        ),
        session_id="sess-tools",
        model_tag="claude",
    )

    messages, meta = reconstruct_session([
        {
            "content": content,
            "tags": ["layer=L1", "session=sess-tools", "source=claude", "turn=1"],
            "createTime": "2026-06-04T12:00:00Z",
        }
    ])

    assistant_messages = [m["content"] for m in messages if m["role"] == "assistant"]
    assert assistant_messages
    assistant = assistant_messages[0]
    assert "Tool Results" in assistant
    assert "test_ok PASSED" in assistant
    assert meta["source"] == "claude"
