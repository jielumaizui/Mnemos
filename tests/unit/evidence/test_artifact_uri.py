from core.evidence.artifact_uri import (
    artifact_uri_error,
    build_artifact_ref,
    build_artifact_uri,
    build_content_artifact_uri,
    is_valid_artifact_uri,
    parse_artifact_uri,
)


def test_build_artifact_uri_uses_stable_non_local_scheme():
    uri = build_artifact_uri("codex", "sess/1", 3, "tool_result", 0)

    assert uri == "mnemos-artifact://codex/sess%2F1/turn/3/tool_result/0"
    assert is_valid_artifact_uri(uri) is True


def test_artifact_ref_is_serializable_and_keeps_path_out_of_uri():
    local_path = "/" + "Users/example/Desktop/screenshot.png"
    ref = build_artifact_ref(
        source_agent="codex",
        session_id="sess-1",
        turn_number=2,
        artifact_type="attachment",
        index=0,
        summary="screenshot.png",
        source_event_id="raw-1",
        path=local_path,
        mime_type="image/png",
    ).to_dict()

    assert ref["uri"] == "mnemos-artifact://codex/sess-1/turn/2/attachment/0"
    assert ref["path"] == local_path
    assert local_path not in ref["uri"]


def test_parse_artifact_uri_recovers_the_exact_encoded_identity():
    identity = parse_artifact_uri(
        "mnemos-artifact://codex/sess%2F1/turn/3/tool_result/0"
    )

    assert identity.source_agent == "codex"
    assert identity.session_id == "sess/1"
    assert identity.turn_number == "3"
    assert identity.artifact_type == "tool_result"
    assert identity.index == "0"


def test_invalid_artifact_uri_reports_contract_error():
    assert "scheme" in artifact_uri_error("file:///tmp/screenshot.png")
    assert "whitespace" in artifact_uri_error("mnemos-artifact://codex/sess 1/turn/0/file")
    assert "extra segments" in artifact_uri_error(
        "mnemos-artifact://codex/sess-1/turn/0/file/0/extra"
    )


def test_build_artifact_uri_rejects_empty_identity_segments():
    import pytest

    with pytest.raises(ValueError, match="source_agent"):
        build_artifact_uri("", "sess-1", 0, "file")
    with pytest.raises(ValueError, match="session_id"):
        build_artifact_uri("codex", "", 0, "file")
    with pytest.raises(ValueError, match="turn_number"):
        build_artifact_uri("codex", "sess-1", "", "file")


def test_content_artifact_uri_is_path_and_round_independent():
    digest = "a" * 64

    uri = build_content_artifact_uri("attachment", digest)
    identity = parse_artifact_uri(uri)

    assert uri == f"mnemos-artifact://content/sha256/{digest}/attachment"
    assert identity.identity_kind == "content"
    assert identity.sha256 == digest
    assert identity.artifact_type == "attachment"
    assert is_valid_artifact_uri(uri) is True


def test_content_artifact_uri_rejects_short_hash_and_unknown_type():
    assert "64 lowercase hex" in artifact_uri_error(
        "mnemos-artifact://content/sha256/abc/attachment"
    )
    assert "artifact_type" in artifact_uri_error(
        f"mnemos-artifact://content/sha256/{'a' * 64}/unknown"
    )
