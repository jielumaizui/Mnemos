from __future__ import annotations


def test_cognitive_source_authority_audit_closes_all_denominators():
    from scripts.audit_cognitive_source_authority import audit

    report = audit()

    assert report["ok"] is True
    assert report["errors"] == []
    assert report["source_authority"] == {
        "catalog_denominator": 7,
        "resolved_count": 7,
        "unauthorized_cognitive_update_count": 0,
        "high_authority_trace_gap": 0,
        "role_confusion_rejected": True,
        "model_upgrade_rejected": True,
    }
    assert report["lossless_external_ingestion"]["external_knowledge_preserved"] is True
    assert report["embedded_quote_boundary"] == {
        "corpus_denominator": 4,
        "searchable_resolved_count": 4,
        "unauthorized_update_count": 0,
        "external_override_rejected": True,
        "duplicate_ref_ambiguity_rejected": True,
        "apostrophe_preserves_explicit_user": True,
    }
    assert report["raw_cognitive_projection"][
        "assistant_excluded_from_user_cognition"
    ] is True
    assert report["static_contract"]["raw_blocking_site_count"] == 0
    assert report["static_contract"]["detached_input_low_authority"] is True
    assert report["static_contract"]["external_intent_cautious"] is True
