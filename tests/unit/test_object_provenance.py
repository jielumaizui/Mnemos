from __future__ import annotations

import pytest

from core.cognitive.access_control import make_cognitive_access_envelope


def _access_envelope(*, session_id: str = "session-1", project: str = "mnemos"):
    return make_cognitive_access_envelope(
        owner_principal_id="mcp:codex:provenance-test",
        owner_agent="Codex",
        scope_type="session",
        scope_id=session_id,
        session_id=session_id,
        project=project,
        purposes=("provenance_test",),
        consent_provenance_refs=("raw:provenance-test",),
        sensitivity="sensitive",
        retention_policy="test_retention",
        source_acl_lineage=("sha256:" + "a" * 64,),
        visibility="private",
    )


def test_typed_object_provenance_exposes_exact_hash_only_selectors():
    from core.privacy.object_provenance import (
        ObjectProvenance,
        scope_selector_hash,
    )

    provenance = ObjectProvenance.from_access_control(_access_envelope())

    assert provenance.state == "tracked"
    assert provenance.access_hash.startswith("sha256:")
    assert set(provenance.selector_hashes) == {
        ("agent", scope_selector_hash("agent", "codex")),
        ("project", scope_selector_hash("project", "mnemos")),
        ("session", scope_selector_hash("session", "session-1")),
    }
    assert "session-1" not in provenance.access_hash
    assert provenance.access_json.startswith("{")


def test_object_provenance_rejects_untyped_or_malformed_scope_selectors():
    from core.privacy.object_provenance import (
        ObjectProvenanceError,
        normalize_scope_selector,
    )

    with pytest.raises(ObjectProvenanceError, match="unsupported"):
        normalize_scope_selector("unknown", "value")
    with pytest.raises(ObjectProvenanceError, match="all scope"):
        normalize_scope_selector("all", "not-all")
    with pytest.raises(ObjectProvenanceError, match="required"):
        normalize_scope_selector("session", "")
