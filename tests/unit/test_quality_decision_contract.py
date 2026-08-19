from core.hephaestus.quality_gate import QualityGate
from core.system_contracts import (
    QUALITY_DECISIONS,
    audit_quality_decision_contract,
    make_quality_decision,
)


def test_quality_decision_registry_is_strictly_valid():
    assert audit_quality_decision_contract(strict=True) == []
    assert QUALITY_DECISIONS == {"accept", "skip", "degrade", "needs_review", "auto_fix", "reject"}


def test_quality_decision_validates_required_fields():
    decision = make_quality_decision(
        subject="wiki:page",
        decision="accept",
        reason_codes=("schema_valid",),
        evidence_refs=("core/system_contracts.py",),
        confidence=0.8,
    )

    assert decision.validate() == []


def test_legacy_quality_gate_maps_to_unified_decision():
    gate_decision = QualityGate().evaluate("## Decision\n\n原因：有验证和测试。")
    unified = gate_decision.as_unified_decision("distill:claim")

    assert unified.decision in {"accept", "needs_review", "reject"}
    assert unified.subject == "distill:claim"
    assert unified.evidence_refs == ("core/hephaestus/quality_gate.py",)
