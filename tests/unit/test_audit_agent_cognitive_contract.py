from __future__ import annotations


def test_agent_cognitive_contract_static_audit_has_one_shared_owner_and_no_adapter_bypass():
    from scripts.audit_agent_cognitive_contract import audit

    report = audit()

    assert report["ok"] is True
    assert report["certifying"] is False
    assert report["release_eligible"] is False
    assert report["host_denominator"] == [
        "codex", "claude", "hermes", "opencode", "openclaw", "crush", "kiro", "kimi"
    ]
    assert report["metrics"]["agent_specific_domain_logic"] == 0
    assert report["metrics"]["delivery_decision_owner_count"] == 1
    assert report["metrics"]["direct_delivery_bypass"] == 0
    assert report["metrics"]["runtime_probe_verified"] == 0
