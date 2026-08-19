from __future__ import annotations

import json
from pathlib import Path

from core.sync_framework.agent_source import (
    SessionInfo,
    native_session_artifact_evidence_hash,
    parse_discovered_session_result,
)
from integrations.sources.kiro_source import KiroSource


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )


def test_kiro_source_identity() -> None:
    source = KiroSource()

    assert source.name == "kiro"
    assert source.model_tag == "kiro"
    assert source.trigger_strategy["type"] == "watchdog"
    caps = source.completeness_capabilities()
    assert caps["reasoning"] is True
    assert caps["attachments"] == "available"


def test_kiro_discover_sessions(tmp_path: Path) -> None:
    root = tmp_path / ".kiro"
    sessions = root / "sessions" / "cli"
    sessions.mkdir(parents=True)
    (sessions / "sess-a.jsonl").write_text("{}", encoding="utf-8")
    (sessions / "sess-a.json").write_text("{}", encoding="utf-8")
    (sessions / "sess-b.jsonl").write_text("{}", encoding="utf-8")

    source = KiroSource()
    source._override_data_dir = root  # type: ignore[attr-defined]

    infos = source.discover_sessions()

    assert {info.session_id for info in infos} == {"sess-a", "sess-b"}
    assert all(info.source_path.suffix == ".jsonl" for info in infos)


def test_kiro_discovers_and_preserves_orphan_declared_sidecar(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".kiro"
    sessions = root / "sessions" / "cli"
    sessions.mkdir(parents=True)
    orphan = sessions / "orphan.history"
    orphan.write_text("orphan-visible-history", encoding="utf-8")
    source = KiroSource()
    source._override_data_dir = root  # type: ignore[attr-defined]

    info = source.discover_sessions()[0]
    result = parse_discovered_session_result(source, info)

    assert info.session_id == "orphan"
    assert result.disposition == "parsed"
    assert "orphan-visible-history" in json.dumps(
        [
            ref
            for turn in result.turns
            for ref in turn.raw_event_refs
        ],
        ensure_ascii=False,
    )


def test_kiro_parse_jsonl_turns_with_tools(tmp_path: Path) -> None:
    path = tmp_path / "sess.jsonl"
    _write_jsonl(
        path,
        [
            {
                "version": "v1",
                "kind": "Prompt",
                "data": {
                    "message_id": "user-1",
                    "content": [{"kind": "text", "data": "hello"}],
                    "meta": {"timestamp": 1_760_000_000},
                },
            },
            {
                "version": "v1",
                "kind": "AssistantMessage",
                "data": {
                    "message_id": "assistant-1",
                    "content": [
                        {"kind": "text", "data": "hi"},
                        {
                            "kind": "toolUse",
                            "data": {
                                "toolUseId": "tool-1",
                                "name": "read",
                                "input": {"path": "README.md"},
                            },
                        },
                    ],
                },
            },
            {
                "version": "v1",
                "kind": "ToolResults",
                "data": {
                    "message_id": "tool-results-1",
                    "content": [
                        {
                            "kind": "toolResult",
                            "data": {
                                "toolUseId": "tool-1",
                                "status": "success",
                                "content": [{"kind": "text", "data": "ok"}],
                            },
                        }
                    ],
                },
            },
            {
                "version": "v1",
                "kind": "AssistantMessage",
                "data": {
                    "message_id": "assistant-2",
                    "content": [{"kind": "text", "data": "done"}],
                },
            },
        ],
    )

    turns = KiroSource().parse_turns(path)

    assert len(turns) == 1
    turn = turns[0]
    assert turn.user_content == "hello"
    assert turn.assistant_content == "hi\n\ndone"
    assert turn.timestamp == "2025-10-09T08:53:20+00:00"
    assert turn.metadata["session_id"] == "sess"
    assert turn.metadata["prompt_message_id"] == "user-1"
    assert turn.metadata["assistant_message_ids"] == ["assistant-1", "assistant-2"]
    assert turn.tool_calls[0]["name"] == "read"
    assert turn.tool_results[0]["status"] == "success"
    assert turn.completeness["tool_calls"] == "full"
    assert turn.completeness["tool_results"] == "full"


def test_kiro_known_event_residuals_preserve_unprojected_fields_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "residual.jsonl"
    sentinel = {"nested": ["must-survive", 7]}
    _write_jsonl(
        path,
        [
            {
                "version": "v-residual",
                "kind": "Prompt",
                "data": {
                    "message_id": "user-1",
                    "content": [
                        {
                            "kind": "text",
                            "data": "q",
                            "block_extra": sentinel,
                        }
                    ],
                    "meta": {"timestamp": 1, "data_extra": sentinel},
                },
            },
            {
                "version": "v-results",
                "kind": "ToolResults",
                "data": {
                    "content": [],
                    "message_id": "tool-1",
                    "results": sentinel,
                },
            },
        ],
    )

    refs = KiroSource().parse_turns(path)[0].raw_event_refs
    serialized = json.dumps(refs, ensure_ascii=False, sort_keys=True)

    assert serialized.count('"must-survive"') == 3
    assert '"version": "v-residual"' in serialized
    assert '"version": "v-results"' in serialized
    assert '"results"' in serialized


def test_kiro_valid_json_sidecar_preserves_exact_source_text(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"kind": "Prompt", "data": {"content": "q"}}) + "\n",
        encoding="utf-8",
    )
    sidecar = tmp_path / "session.json"
    exact = '{  "duplicate": 1, "duplicate": 2, "spaced" : true }\n'
    sidecar.write_text(exact, encoding="utf-8")

    turn = KiroSource().parse_turns(transcript)[0]
    refs = [ref for ref in turn.raw_event_refs if ref["event_type"] == "native_sidecar"]

    assert refs[0]["raw_text"] == exact
    assert "raw" not in refs[0]


def test_kiro_known_events_preserve_non_object_data_and_content_blocks(
    tmp_path: Path,
) -> None:
    path = tmp_path / "non-object.jsonl"
    _write_jsonl(
        path,
        [
            {"kind": "Prompt", "data": "scalar-data-must-survive"},
            {
                "kind": "AssistantMessage",
                "data": {
                    "content": [
                        {"kind": "text", "data": "a"},
                        "scalar-block-must-survive",
                    ]
                },
            },
        ],
    )

    turns = KiroSource().parse_turns(path)
    serialized = json.dumps(
        [ref for turn in turns for ref in turn.raw_event_refs],
        ensure_ascii=False,
    )

    assert serialized.count("scalar-data-must-survive") == 1
    assert serialized.count("scalar-block-must-survive") == 1


def test_kiro_parse_jsonl_reasoning_and_attachments(tmp_path: Path) -> None:
    path = tmp_path / "sess.jsonl"
    _write_jsonl(
        path,
        [
            {
                "kind": "Prompt",
                "data": {
                    "content": [
                        {"kind": "text", "data": "inspect this"},
                        {
                            "kind": "file",
                            "data": {"path": "README.md", "mime_type": "text/markdown"},
                        },
                    ]
                },
            },
            {
                "kind": "AssistantMessage",
                "data": {
                    "content": [
                        {"kind": "thinking", "data": "need to read the file"},
                        {"kind": "text", "data": "ok"},
                    ]
                },
            },
        ],
    )

    turns = KiroSource().parse_turns(path)

    assert len(turns) == 1
    turn = turns[0]
    assert turn.reasoning == "need to read the file"
    assert turn.attachments[0]["path"] == "README.md"
    assert turn.completeness["reasoning"] == "full"
    assert turn.completeness["attachments"] == "full"


def test_kiro_parse_jsonl_flushes_on_next_prompt(tmp_path: Path) -> None:
    path = tmp_path / "multi.jsonl"
    _write_jsonl(
        path,
        [
            {
                "kind": "Prompt",
                "data": {"content": [{"kind": "text", "data": "first"}]},
            },
            {
                "kind": "AssistantMessage",
                "data": {"content": [{"kind": "text", "data": "one"}]},
            },
            {
                "kind": "Prompt",
                "data": {"content": [{"kind": "text", "data": "second"}]},
            },
            {
                "kind": "AssistantMessage",
                "data": {"content": [{"kind": "text", "data": "two"}]},
            },
        ],
    )

    turns = KiroSource().parse_turns(path)

    assert [turn.user_content for turn in turns] == ["first", "second"]
    assert [turn.assistant_content for turn in turns] == ["one", "two"]


def test_kiro_parse_jsonl_records_unknown_blocks(tmp_path: Path) -> None:
    path = tmp_path / "unknown.jsonl"
    _write_jsonl(
        path,
        [
            {"kind": "Prompt", "data": {"content": [{"kind": "text", "data": "q"}]}},
            {
                "kind": "AssistantMessage",
                "data": {
                    "content": [
                        {"kind": "text", "data": "a"},
                        {"kind": "custom", "data": {"x": 1}},
                    ]
                },
            },
        ],
    )

    turns = KiroSource().parse_turns(path)

    assert turns[0].assistant_content == "a"
    assert "unknown_block:custom" in turns[0].completeness["loss_reasons"]
    assert any(ref["event_type"] == "custom" for ref in turns[0].raw_event_refs)


def test_kiro_does_not_carry_previous_raw_refs_into_next_turn(tmp_path: Path) -> None:
    path = tmp_path / "refs.jsonl"
    _write_jsonl(
        path,
        [
            {"kind": "SessionStarted", "data": {"id": "session-event"}},
            {"kind": "Prompt", "data": {"content": [{"kind": "text", "data": "first"}]}},
            {
                "kind": "AssistantMessage",
                "data": {
                    "content": [
                        {"kind": "text", "data": "one"},
                        {"kind": "custom", "data": {"x": 1}},
                    ]
                },
            },
            {"kind": "Prompt", "data": {"content": [{"kind": "text", "data": "second"}]}},
            {
                "kind": "AssistantMessage",
                "data": {"content": [{"kind": "text", "data": "two"}]},
            },
        ],
    )

    turns = KiroSource().parse_turns(path)

    assert len(turns) == 2
    assert any(ref["event_type"] == "SessionStarted" for ref in turns[0].raw_event_refs)
    assert any(ref["event_type"] == "custom" for ref in turns[0].raw_event_refs)
    assert turns[1].raw_event_refs == []


def test_kiro_session_state_includes_sibling_files(tmp_path: Path) -> None:
    path = tmp_path / "sess.jsonl"
    path.write_text("{}", encoding="utf-8")
    path.with_suffix(".json").write_text("{}", encoding="utf-8")
    path.with_suffix(".history").write_text("/agent\n", encoding="utf-8")
    source = KiroSource()
    state = source.get_session_state(
        SessionInfo(session_id="sess", source_path=path)
    )

    assert state is not None
    assert state["file_count"] == 3


def test_kiro_formal_parse_and_evidence_include_declared_sidecars(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sess.jsonl"
    path.write_text("", encoding="utf-8")
    json_sidecar = path.with_suffix(".json")
    history_sidecar = path.with_suffix(".history")
    json_sidecar.write_text(
        json.dumps({"future_sidecar": "json-visible"}),
        encoding="utf-8",
    )
    history_sidecar.write_text("history-visible", encoding="utf-8")
    source = KiroSource()
    session = SessionInfo(
        session_id="sess",
        source_path=path,
        source_kind="cli_jsonl",
    )

    before = native_session_artifact_evidence_hash(source, session)
    result = parse_discovered_session_result(source, session)
    history_sidecar.write_text("history-changed", encoding="utf-8")
    after = native_session_artifact_evidence_hash(source, session)

    assert result.disposition == "parsed"
    encoded_refs = json.dumps(
        [
            ref
            for turn in result.turns
            for ref in turn.raw_event_refs
        ],
        ensure_ascii=False,
    )
    assert "json-visible" in encoded_refs
    assert "history-visible" in encoded_refs
    assert before != after
