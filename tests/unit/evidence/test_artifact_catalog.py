from __future__ import annotations

import hashlib

import pytest

from core.evidence.artifact_capture import (
    build_capture_artifact_refs,
    managed_capture_artifact_relative_path,
)
from core.evidence.artifact_catalog import (
    ArtifactCatalog,
    ArtifactCatalogRejectedError,
    resolve_model_artifact_selections,
)
from core.evidence.artifact_uri import build_artifact_uri
from core.ops.durable_io import DurableIOError


def _ref(
    *,
    source_event_id: str,
    sha256: str,
    path: str,
    turn: int,
    artifact_type: str = "attachment",
) -> dict[str, str]:
    return {
        "uri": build_artifact_uri(
            "codex",
            "session-1",
            turn,
            artifact_type,
            0,
        ),
        "artifact_type": artifact_type,
        "summary": "same attachment",
        "source_event_id": source_event_id,
        "sha256": sha256,
        "path": path,
        "mime_type": "text/plain",
    }


def test_catalog_converges_same_bytes_across_paths_and_rounds(tmp_path):
    content = b"stable attachment bytes\n"
    digest = hashlib.sha256(content).hexdigest()
    first = tmp_path / "first" / "evidence.txt"
    second = tmp_path / "second" / "renamed.txt"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(content)
    second.write_bytes(content)

    catalog = ArtifactCatalog.from_refs(
        [
            _ref(
                source_event_id="raw-1",
                sha256=digest,
                path=str(first),
                turn=1,
            ),
            _ref(
                source_event_id="raw-2",
                sha256=digest,
                path=str(second),
                turn=8,
            ),
        ],
        allowed_source_event_ids=("raw-1", "raw-2"),
    )

    assert catalog.rejected_count == 0
    assert len(catalog.entries) == 1
    entry = catalog.entries[0]
    expected_ref_token = hashlib.sha256(
        f"artifact-ref-v1\0attachment\0{digest}".encode("utf-8")
    ).hexdigest()[:32]
    assert entry.artifact_ref_id == f"artifact-ref:{expected_ref_token}"
    assert entry.uri == f"mnemos-artifact://content/sha256/{digest}/attachment"
    assert entry.source_event_ids == ("raw-1", "raw-2")
    assert entry.sha256 == f"sha256:{digest}"

    # Local paths, canonical URI, hash and ACL stay system-owned.
    prompt_payload = catalog.prompt_payload()
    rendered = str(prompt_payload)
    assert str(first) not in rendered
    assert str(second) not in rendered
    assert "mnemos-artifact://" not in rendered
    assert digest not in rendered
    assert "local_user" not in rendered


def test_catalog_rejects_claimed_type_that_conflicts_with_source_uri(tmp_path):
    path = tmp_path / "shot.png"
    path.write_bytes(b"png bytes")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    ref = _ref(
        source_event_id="raw-1",
        sha256=digest,
        path=str(path),
        turn=1,
        artifact_type="attachment",
    )
    ref["uri"] = build_artifact_uri(
        "codex", "session-1", 1, "screenshot", 0
    )

    catalog = ArtifactCatalog.from_refs(
        [ref],
        allowed_source_event_ids=("raw-1",),
    )

    assert catalog.entries == ()
    assert catalog.rejected_count == 1
    assert catalog.rejection_codes == ("artifact_type_mismatch",)


def test_catalog_rejects_unverifiable_hash_and_non_local_acl(tmp_path):
    path = tmp_path / "evidence.txt"
    path.write_bytes(b"authoritative bytes")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    wrong_hash = _ref(
        source_event_id="raw-1",
        sha256="0" * 64,
        path=str(path),
        turn=1,
    )
    unauthorized = _ref(
        source_event_id="raw-1",
        sha256=digest,
        path=str(path),
        turn=1,
    )
    unauthorized["acl"] = "shared"

    catalog = ArtifactCatalog.from_refs(
        [wrong_hash, unauthorized],
        allowed_source_event_ids=("raw-1",),
    )

    assert catalog.entries == ()
    assert catalog.rejected_count == 2
    assert catalog.rejection_codes == (
        "artifact_hash_mismatch",
        "artifact_ref_unauthorized",
    )


def test_catalog_rejects_missing_file_even_with_plausible_digest(tmp_path):
    missing = tmp_path / "deleted-evidence.txt"
    ref = _ref(
        source_event_id="raw-1",
        sha256="a" * 64,
        path=str(missing),
        turn=1,
    )

    catalog = ArtifactCatalog.from_refs(
        [ref],
        allowed_source_event_ids=("raw-1",),
    )

    assert catalog.entries == ()
    assert catalog.rejection_codes == ("artifact_content_missing",)
    with pytest.raises(
        ArtifactCatalogRejectedError,
        match="artifact_content_missing",
    ):
        catalog.require_admissible()


def test_capture_preserves_missing_attachment_as_rejected_input(tmp_path):
    missing = tmp_path / "missing-capture-attachment.txt"
    refs = build_capture_artifact_refs(
        source_agent="codex",
        session_id="session-1",
        turn_number=1,
        source_event_id="raw-1",
        attachments=({"path": str(missing)},),
    )

    assert len(refs) == 1
    catalog = ArtifactCatalog.from_refs(
        refs,
        allowed_source_event_ids=("raw-1",),
    )
    assert catalog.rejection_codes == ("artifact_content_missing",)


def test_capture_and_reasoning_paths_require_managed_owner_root(tmp_path):
    foreign = tmp_path / "foreign.md"
    foreign.write_text("must not be admitted as managed", encoding="utf-8")

    with pytest.raises(DurableIOError, match="capture_artifact_owner_root_required"):
        build_capture_artifact_refs(
            source_agent="codex",
            session_id="session-1",
            turn_number=1,
            capture_artifact_path=foreign,
        )
    with pytest.raises(DurableIOError, match="reasoning_artifact_owner_root_required"):
        build_capture_artifact_refs(
            source_agent="codex",
            session_id="session-1",
            turn_number=1,
            reasoning_artifact_path=foreign,
        )


def test_managed_artifact_relative_path_binds_source_agent():
    common = {
        "session_id": "shared-session",
        "turn_number": 1,
        "artifact_type": "reasoning",
        "content": "same bytes",
    }

    assert managed_capture_artifact_relative_path(
        source_agent="codex",
        **common,
    ) != managed_capture_artifact_relative_path(
        source_agent="kimi",
        **common,
    )


def test_pathless_tool_result_requires_recomputable_inline_payload():
    official = build_capture_artifact_refs(
        source_agent="codex",
        session_id="session-1",
        turn_number=1,
        source_event_id="raw-1",
        tool_results=({"tool_name": "pytest", "result": "3 passed"},),
    )[0]
    forged_without_payload = dict(official)
    forged_without_payload["metadata"] = {
        "hash_verification": "inline_payload_sha256_v1"
    }
    forged_digest = dict(official)
    forged_digest["sha256"] = "d" * 64

    catalog = ArtifactCatalog.from_refs(
        [official, forged_without_payload, forged_digest],
        allowed_source_event_ids=("raw-1",),
    )

    assert len(catalog.entries) == 1
    assert catalog.rejection_codes == (
        "artifact_hash_unverifiable",
        "artifact_hash_mismatch",
    )


def test_catalog_applies_only_narrow_pii_and_credential_redaction(tmp_path):
    path = tmp_path / "evidence.txt"
    path.write_bytes(b"ordinary technical evidence")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    ref = _ref(
        source_event_id="raw-1",
        sha256=digest,
        path=str(path),
        turn=1,
    )
    fake_key = "sk-" + "abcdefghijklmnop"
    ref["summary"] = f"owner=a@example.com api_key={fake_key} keep pytest"

    catalog = ArtifactCatalog.from_refs(
        [ref],
        allowed_source_event_ids=("raw-1",),
    )

    summary = catalog.entries[0].summary
    assert "a@example.com" not in summary
    assert fake_key not in summary
    assert "pytest" in summary


def test_model_selection_resolves_only_catalogued_local_reference(tmp_path):
    path = tmp_path / "report.txt"
    path.write_bytes(b"3 tests failed")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    catalog = ArtifactCatalog.from_refs(
        [
            _ref(
                source_event_id="raw-1",
                sha256=digest,
                path=str(path),
                turn=2,
                artifact_type="test_report",
            )
        ],
        allowed_source_event_ids=("raw-1",),
    )
    ref_id = catalog.entries[0].artifact_ref_id
    model_output = {
        "structured_output": {
            "claims": [
                {
                    "evidence": [
                        {
                            "source_event_id": "raw-1",
                            "quote": "3 tests failed",
                            "artifact_ref_id": ref_id,
                        }
                    ]
                }
            ]
        }
    }

    resolved = resolve_model_artifact_selections(model_output, catalog)

    assert resolved.valid is True
    evidence = resolved.payload["structured_output"]["claims"][0]["evidence"][0]
    assert evidence == {
        "source_event_id": "raw-1",
        "quote": "3 tests failed",
        "artifact_ref_id": ref_id,
        "artifact_uri": catalog.entries[0].uri,
        "artifact_type": "test_report",
        "artifact_summary": "same attachment",
        "artifact_sha256": f"sha256:{digest}",
        "artifact_mime_type": "text/plain",
        "artifact_acl": "local_user",
    }


def test_model_selection_rejects_forged_cross_chunk_and_model_owned_identity(tmp_path):
    path = tmp_path / "report.txt"
    path.write_bytes(b"report")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    catalog = ArtifactCatalog.from_refs(
        [
            _ref(
                source_event_id="raw-1",
                sha256=digest,
                path=str(path),
                turn=2,
                artifact_type="test_report",
            )
        ],
        allowed_source_event_ids=("raw-1",),
    )
    ref_id = catalog.entries[0].artifact_ref_id

    forged = {
        "structured_output": {
            "claims": [
                {
                    "evidence": [
                        {
                            "source_event_id": "raw-2",
                            "quote": "report",
                            "artifact_ref_id": ref_id,
                        }
                    ]
                }
            ]
        }
    }
    cross_chunk = resolve_model_artifact_selections(forged, catalog)
    assert cross_chunk.valid is False
    assert cross_chunk.issues[0].code == "artifact_ref_source_mismatch"

    forged["structured_output"]["claims"][0]["evidence"][0].update(
        {
            "source_event_id": "raw-1",
            "artifact_uri": "mnemos-artifact://content/sha256/"
            + digest
            + "/screenshot",
            "artifact_type": "screenshot",
        }
    )
    model_owned = resolve_model_artifact_selections(forged, catalog)
    assert model_owned.valid is False
    assert model_owned.issues[0].code == "model_owned_artifact_identity"
