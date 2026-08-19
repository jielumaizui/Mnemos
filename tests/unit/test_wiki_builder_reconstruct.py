import hashlib
from types import SimpleNamespace

import core.hephaestus.wiki_builder as wiki_builder_module
from core.evidence.artifact_capture import write_managed_capture_artifact
from core.sync_framework.agent_source import Turn
from core.sync_framework.sync_engine import build_turn_markdown
from core.hephaestus.wiki_builder import fetch_l1_sessions, reconstruct_session
from core.hephaestus.distillation_engine import build_session_text


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

    messages, meta = reconstruct_session(
        [
            {
                "content": content,
                "tags": ["layer=L1", "session=sess-tools", "source=claude", "turn=1"],
                "createTime": "2026-06-04T12:00:00Z",
            }
        ]
    )

    assistant_messages = [m["content"] for m in messages if m["role"] == "assistant"]
    assert assistant_messages
    assistant = assistant_messages[0]
    assert "Tool Results" in assistant
    assert "test_ok PASSED" in assistant
    assert meta["source"] == "claude"


def test_reconstruct_session_uses_full_capture_artifact(tmp_path, monkeypatch):
    artifact_content = """# Capture Artifact

- session_id: sess-big
- turn_number: 0
- captured_at: 2026-06-04T12:00:00

---

## User

完整用户原文-不要丢

---

## Assistant

完整助手原文-不要丢

---

## Structured Capture

````json
{
  "tool_calls": [{"name": "ReadFile", "input": {"path": "core/kia/aegis.py"}}],
  "tool_results": [{"stdout": "完整工具结果-不要丢"}],
  "completeness": {"visible_text": "artifact_summary", "truncated": true}
}
````
"""
    artifact = write_managed_capture_artifact(
        database_dir=tmp_path,
        source_agent="claude",
        session_id="sess-big",
        turn_number=0,
        artifact_type="capture",
        content=artifact_content,
    )
    monkeypatch.setattr(
        wiki_builder_module,
        "get_config",
        lambda: SimpleNamespace(database_dir=tmp_path),
    )

    l1_content = build_turn_markdown(
        Turn(
            turn_number=0,
            user_content="摘要用户",
            assistant_content="摘要助手",
            metadata={"artifact_path": str(artifact)},
        ),
        session_id="sess-big",
        model_tag="claude",
    )

    messages, meta = reconstruct_session(
        [
            {
                "content": l1_content,
                "tags": [
                    "layer=L1",
                    "session=sess-big",
                    "source=claude",
                    "turn=1",
                    "capture_truncated=true",
                    (
                        "capture_artifact_sha256="
                        + hashlib.sha256(artifact_content.encode("utf-8")).hexdigest()
                    ),
                ],
                "createTime": "2026-06-04T12:00:00Z",
            }
        ]
    )

    joined = "\n".join(m["content"] for m in messages)
    assert "完整用户原文-不要丢" in joined
    assert "完整助手原文-不要丢" in joined
    assert "完整工具结果-不要丢" in joined
    assert "摘要用户" not in joined
    assert meta["used_capture_artifacts"] is True
    assert meta["artifact_paths"] == [str(artifact)]


def test_reconstruct_session_never_reads_unmanaged_or_symlinked_artifact(
    tmp_path,
    monkeypatch,
):
    database_dir = tmp_path / "database"
    monkeypatch.setattr(
        wiki_builder_module,
        "get_config",
        lambda: SimpleNamespace(database_dir=database_dir),
    )
    sentinel = tmp_path / "foreign-secret.md"
    sentinel.write_text("FOREIGN-SECRET-MUST-NOT-BE-READ", encoding="utf-8")
    managed_content = "# Capture Artifact\n\n## User\n\nsafe\n\n## Assistant\n\nsafe\n"
    managed = write_managed_capture_artifact(
        database_dir=database_dir,
        source_agent="claude",
        session_id="sess-safe",
        turn_number=0,
        artifact_type="capture",
        content=managed_content,
    )
    wrong_digest_content = (
        "# Capture Artifact\n\n## User\n\nWRONG-DIGEST-SECRET\n\n"
        "## Assistant\n\nunsafe\n"
    )
    wrong_digest_artifact = write_managed_capture_artifact(
        database_dir=database_dir,
        source_agent="claude",
        session_id="sess-safe",
        turn_number=0,
        artifact_type="capture",
        content=wrong_digest_content,
    )
    managed.unlink()
    managed.symlink_to(sentinel)

    for artifact_path, claimed_digest in (
        (sentinel, hashlib.sha256(sentinel.read_bytes()).hexdigest()),
        (managed, hashlib.sha256(managed_content.encode("utf-8")).hexdigest()),
        (wrong_digest_artifact, "0" * 64),
    ):
        content = build_turn_markdown(
            Turn(
                turn_number=0,
                user_content="summary-user",
                assistant_content="summary-assistant",
                metadata={"artifact_path": str(artifact_path)},
            ),
            session_id="sess-safe",
            model_tag="claude",
        )
        messages, meta = reconstruct_session(
            [
                {
                    "content": content,
                    "tags": [
                        "layer=L1",
                        "session=sess-safe",
                        "source=claude",
                        "turn=1",
                        "capture_truncated=true",
                        f"capture_artifact_sha256={claimed_digest}",
                    ],
                }
            ]
        )

        joined = "\n".join(message["content"] for message in messages)
        assert "FOREIGN-SECRET-MUST-NOT-BE-READ" not in joined
        assert "WRONG-DIGEST-SECRET" not in joined
        assert "summary-user" in joined
        assert meta["artifact_rejections"] == [
            "capture_artifact_reference_untrusted"
        ]


def test_build_session_text_does_not_truncate_single_message_when_under_total_limit():
    long_text = "单条长消息-" + ("丁" * 9000)
    meta = {}

    text = build_session_text(
        [{"role": "user", "content": long_text}],
        max_tokens=16000,
        out_meta=meta,
    )

    assert long_text in text
    assert "(truncated)" not in text
    assert meta["truncated"] is False
    assert meta["message_truncations"][0]["truncated"] is False


def test_fetch_l1_sessions_passes_cycle_budget_to_backend():
    from core.sync_framework.storage_backend import StorageResult

    class _Client:
        def __init__(self):
            self.tags = None
            self.limit = None

        def list_by_tags(self, tags, limit=None):
            self.tags = tags
            self.limit = limit
            results = [
                StorageResult(
                    uid="m1",
                    content="a",
                    tags=["layer=L1", "session=s1"],
                    metadata={},
                    created_at="",
                    updated_at="",
                ),
                StorageResult(
                    uid="m2",
                    content="c",
                    tags=["layer=L1", "session=s1"],
                    metadata={},
                    created_at="",
                    updated_at="",
                ),
            ]
            return results[:limit]

    client = _Client()

    sessions = fetch_l1_sessions(client, max_records=2)

    assert client.tags == ["layer=L1"]
    assert client.limit == 2
    assert set(sessions) == {"s1"}
    assert len(sessions["s1"]) == 2
