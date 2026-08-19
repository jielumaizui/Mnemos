from __future__ import annotations


def _turn(*, user: str, assistant: str, authority_context=None):
    from core.sync_framework.raw_event_store import CanonicalRawTurn

    return CanonicalRawTurn(
        logical_event_id="event-1",
        revision_id="revision-1",
        source_agent="codex",
        session_id="session-1",
        conversation_at="2026-07-15T00:00:00+00:00",
        captured_at="2026-07-15T00:00:00+00:00",
        updated_at="2026-07-15T00:00:00+00:00",
        content_hash="sha256:raw",
        user_content=user,
        assistant_content=assistant,
        reasoning="",
        tool_calls=[],
        tool_results=[],
        attachments=[],
        raw_event_refs=[],
        source_files=[],
        authority_context=dict(authority_context or {}),
    )


def test_observation_projection_keeps_raw_assistant_but_exposes_only_user_cognition():
    from core.cognitive.sources import ContentTier, SourceReader

    item = SourceReader()._source_item_from_canonical_turn(
        _turn(
            user="用户说只使用本地缓存。",
            assistant="助手猜测用户也许偏好云端缓存。",
        )
    )

    assert item.content == "用户说只使用本地缓存。"
    assert item.assistant_content == "助手猜测用户也许偏好云端缓存。"
    assert item.content_tier == ContentTier.USER_GENERATED
    assert "云端缓存" not in item.content


def test_external_raw_projection_is_attention_only():
    from core.cognitive.sources import ContentSource, ContentTier, SourceReader

    item = SourceReader()._source_item_from_canonical_turn(
        _turn(
            user="外部材料中的方法论正文。",
            assistant="",
            authority_context={
                "asset_kind": "trusted_user_document",
                "content_source": "external_file",
                "source_authority": "external_content",
            },
        )
    )

    assert item.content == "外部材料中的方法论正文。"
    assert item.content_source == ContentSource.EXTERNAL_FILE
    assert item.content_tier == ContentTier.EXTERNAL_QUOTED
