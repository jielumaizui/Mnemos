from core.sync_framework.native_event_identity import resolve_native_event_identity


def test_generic_id_is_not_treated_as_a_native_message_identity():
    identity = resolve_native_event_identity(
        metadata={
            "id": "session-container-id",
            "support_parser": "example.Parser",
            "support_manifest_hash": "manifest-v1",
            "source_artifact_id": "context.jsonl",
        },
        raw_event_refs=[],
        turn_number=3,
    )

    assert identity.kind == "parser_artifact_offset"
    assert identity.value.endswith("artifact=context.jsonl;offset=3")


def test_explicit_native_message_identity_beats_artifact_fallback():
    identity = resolve_native_event_identity(
        metadata={
            "message_id": "native-message-7",
            "support_parser": "example.Parser",
            "support_manifest_hash": "manifest-v1",
            "source_artifact_id": "context.jsonl",
        },
        raw_event_refs=[],
        turn_number=3,
    )

    assert identity.kind == "native_event_id"
    assert identity.value == "native-message-7"
