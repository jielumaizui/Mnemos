from __future__ import annotations

import hashlib

from core.evidence.artifact_uri import build_artifact_uri
from core.hephaestus.distill_input_spec import DistillInputSpec


def _artifact_ref(path, source_event_id: str, turn: int) -> dict[str, str]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "uri": build_artifact_uri(
            "codex", "session-artifacts", turn, "attachment", 0
        ),
        "artifact_type": "attachment",
        "summary": "requirements attachment",
        "source_event_id": source_event_id,
        "path": str(path),
        "sha256": digest,
        "mime_type": "text/markdown",
    }


def _spec(*, refs, source_event_ids):
    return DistillInputSpec.build(
        source_agent="codex",
        source_session_id="session-artifacts",
        source_event_ids=source_event_ids,
        raw_completeness="full",
        visible_input="visible input",
        input_mode="chunked",
        artifact_refs=refs,
    )


def test_input_spec_binds_system_catalog_without_exposing_identity_fields(tmp_path):
    first = tmp_path / "first.md"
    second = tmp_path / "renamed.md"
    first.write_text("same requirements", encoding="utf-8")
    second.write_text("same requirements", encoding="utf-8")
    refs = [
        _artifact_ref(first, "raw-1", 1),
        _artifact_ref(second, "raw-2", 9),
    ]

    forward = _spec(refs=refs, source_event_ids=("raw-1", "raw-2"))
    reverse = _spec(refs=list(reversed(refs)), source_event_ids=("raw-1", "raw-2"))

    assert forward.input_spec_hash == reverse.input_spec_hash
    assert forward.artifact_catalog.catalog_hash == reverse.artifact_catalog.catalog_hash
    assert len(forward.artifact_catalog.entries) == 1
    prompt_catalog = forward.prompt_contract()["artifact_catalog"]
    assert prompt_catalog["entries"][0]["source_event_ids"] == ["raw-1", "raw-2"]
    assert "uri" not in prompt_catalog["entries"][0]
    assert "sha256" not in prompt_catalog["entries"][0]
    assert "acl" not in prompt_catalog["entries"][0]


def test_chunk_local_input_specs_keep_stable_id_but_filter_source_allowlist(tmp_path):
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("same requirements", encoding="utf-8")
    second.write_text("same requirements", encoding="utf-8")
    refs = [
        _artifact_ref(first, "raw-1", 1),
        _artifact_ref(second, "raw-2", 2),
    ]

    first_chunk = _spec(refs=refs, source_event_ids=("raw-1",))
    second_chunk = _spec(refs=refs, source_event_ids=("raw-2",))

    first_entry = first_chunk.artifact_catalog.entries[0]
    second_entry = second_chunk.artifact_catalog.entries[0]
    assert first_entry.artifact_ref_id == second_entry.artifact_ref_id
    assert first_entry.source_event_ids == ("raw-1",)
    assert second_entry.source_event_ids == ("raw-2",)
    assert first_chunk.input_spec_hash != second_chunk.input_spec_hash


def test_chunk_coordinator_input_spec_uses_only_span_revision_artifacts(tmp_path):
    from core.hephaestus.chunked_extraction import _input_spec_from_spans
    from core.hephaestus.distillation_models import DistillationResult

    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("first artifact", encoding="utf-8")
    second.write_text("second artifact", encoding="utf-8")
    result = DistillationResult(
        session_id="session-artifacts",
        source="codex",
        artifact_refs=[
            _artifact_ref(first, "raw-1", 1),
            _artifact_ref(second, "raw-2", 2),
        ],
    )

    spec = _input_spec_from_spans(
        result,
        visible_input="chunk one",
        input_mode="chunked",
        source_span_maps=(
            (
                {
                    "revision_id": "raw-1",
                    "span_start": 0,
                    "span_end": 9,
                    "content_hash": "hash-1",
                },
            ),
        ),
    )

    assert spec.source_event_ids == ("raw-1",)
    assert spec.artifact_catalog.rejected_count == 0
    spec.artifact_catalog.require_admissible()
    assert len(spec.artifact_catalog.entries) == 1
    assert spec.artifact_catalog.entries[0].sha256.endswith(
        hashlib.sha256(first.read_bytes()).hexdigest()
    )
