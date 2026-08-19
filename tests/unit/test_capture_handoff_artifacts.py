from core.sync_framework.capture_handoff import build_artifact_refs


def test_capture_handoff_binds_artifacts_to_authoritative_raw_revision():
    events = [
        {
            "turn_number": 3,
            "raw_revision_id": "raw-revision-3",
            "payload": {
                "metadata": {
                    "raw_event_id": "raw-revision-3",
                    "artifact_refs": [
                        {
                            "uri": "mnemos-artifact://codex/session-1/turn/3/tool_result/0",
                            "artifact_type": "tool_result",
                            "summary": "pytest result",
                            "source_event_id": "legacy-synthetic-id",
                            "sha256": "a" * 64,
                        }
                    ],
                }
            },
        }
    ]

    refs = build_artifact_refs(events)

    assert refs == [
        {
            "uri": "mnemos-artifact://codex/session-1/turn/3/tool_result/0",
            "artifact_type": "tool_result",
            "summary": "pytest result",
            "source_event_id": "raw-revision-3",
            "source_event_ids": ["raw-revision-3"],
            "sha256": "a" * 64,
        }
    ]


def test_capture_handoff_drops_artifact_without_raw_revision_identity():
    events = [
        {
            "turn_number": 3,
            "payload": {
                "metadata": {
                    "artifact_refs": [
                        {
                            "uri": "mnemos-artifact://codex/session-1/turn/3/tool_result/0",
                            "artifact_type": "tool_result",
                            "summary": "pytest result",
                            "sha256": "a" * 64,
                        }
                    ]
                }
            },
        }
    ]

    assert build_artifact_refs(events) == []


def test_capture_handoff_preserves_malformed_refs_for_catalog_rejection():
    from core.evidence.artifact_catalog import ArtifactCatalog

    events = [
        {
            "turn_number": 1,
            "raw_revision_id": "raw-revision-1",
            "payload": {
                "metadata": {
                    "raw_event_id": "raw-revision-1",
                    "artifact_refs": {"unexpected": "mapping"},
                }
            },
        }
    ]

    catalog = ArtifactCatalog.from_refs(
        build_artifact_refs(events),
        allowed_source_event_ids=("raw-revision-1",),
    )

    assert catalog.rejection_codes == ("artifact_type_invalid",)
