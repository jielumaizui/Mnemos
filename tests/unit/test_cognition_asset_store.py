import json
import sqlite3
from types import SimpleNamespace

import pytest

from core.hephaestus.cognition_asset_store import (
    CognitiveDecisionAssetProposal,
    CognitionAssetStore,
)
from core.hephaestus.distill_input_spec import DistillInputSpec
from core.hephaestus.distillation_models import KnowledgeFragment


def _skill_result(fragment: KnowledgeFragment):
    input_spec = DistillInputSpec.build(
        source_agent="codex",
        source_session_id="asset-idempotency",
        source_event_ids=("raw-asset-1",),
        raw_completeness="full",
        visible_input="complete cognition source",
        input_mode="standard",
    )
    return SimpleNamespace(
        judgment="skill",
        extraction_judgment="skill",
        input_spec=input_spec,
        extraction_output_hash="sha256:admitted-root",
        extraction_output={
            "judgment": "skill",
            "fragments": [{"core_content": fragment.core_content}],
        },
        structured_output={"candidate_summary": "complete cognition"},
        source="codex",
        session_id="asset-idempotency",
        input_revision="revision-1",
        raw_event_refs=[
            {
                "revision_id": "raw-asset-1",
                "span_start": 0,
                "span_end": len(fragment.core_content),
                "span_status": "exact",
            }
        ],
        judgment_reason="reusable methodology",
        session_coverage="full",
        chunk_extraction_results=[],
        chunk_aggregate=None,
    )


def test_asset_and_proposal_commits_are_idempotent_and_parent_bound(tmp_path):
    fragment = KnowledgeFragment(
        form="方法论",
        title="幂等认知资产持久化方案",
        frontmatter={"摘要": "重试不产生重复资产。", "领域": "认知"},
        background="队列可能重试。",
        core_content="# 幂等持久化\n" + "使用稳定输入与输出根哈希。" * 12,
        boundaries={"applies": "retry"},
        anti_patterns=["INSERT OR REPLACE"],
        related_concepts=["receipt"],
    )
    result = _skill_result(fragment)
    store = CognitionAssetStore(tmp_path / "distill_actions.db")

    first = store.commit_asset(result, [fragment])
    second = store.commit_asset(result, [fragment])

    assert first.status == "committed"
    assert second.status == "existing"
    assert first.asset_id == second.asset_id
    assert first.content_hash == second.content_hash

    proposal = CognitiveDecisionAssetProposal.from_mapping(
        asset_id=first.asset_id,
        value={
            "skill_name": "幂等认知资产",
            "skill_purpose": "确保重试只复用已提交代际。",
            "asset_schema": "cognitive_decision_asset.v1",
            "asset_type": "verification_recipe",
            "evidence_refs": ["raw-asset-1"],
            "applicability": ["queue retry"],
            "failure_modes": ["duplicate asset"],
            "verification_recipe": ["assert one asset row"],
            "automation_derivative_allowed": False,
        },
        allowed_evidence_refs=("raw-asset-1",),
    )
    proposal_first = store.commit_proposal(proposal)
    proposal_second = store.commit_proposal(proposal)

    assert proposal_first.status == "committed"
    assert proposal_second.status == "existing"
    assert store.integrity_report() == {"skill_asset_without_cognition": 0}
    with sqlite3.connect(store.db_path) as conn:
        asset_count, asset_payload = conn.execute(
            "SELECT COUNT(*), asset_payload FROM cognition_asset_commits"
        ).fetchone()
        proposal_count = conn.execute(
            "SELECT COUNT(*) FROM cognitive_decision_asset_proposals"
        ).fetchone()[0]
    assert asset_count == 1
    assert proposal_count == 1
    payload = json.loads(asset_payload)
    assert payload["cognition"]["source_span_contract"] == {
        "status": "exact",
        "count": 1,
    }


def test_proposal_normalization_cannot_reintroduce_credentials():
    sensitive_value = "private-password-from-model"

    proposal = CognitiveDecisionAssetProposal.from_mapping(
        asset_id="cogasset-safe-parent",
        value={
            "skill_name": "安全派生建议",
            "skill_purpose": f"不得重新写入 password={sensitive_value}",
            "asset_schema": "cognitive_decision_asset.v1",
            "asset_type": "pitfall_pattern",
            "evidence_refs": [],
            "applicability": [],
            "failure_modes": [],
            "verification_recipe": [],
            "automation_derivative_allowed": False,
        },
        allowed_evidence_refs=(),
    )

    assert sensitive_value not in proposal.decision_context
    assert "[REDACTED:CREDENTIAL]" in proposal.decision_context


def test_proposal_rejects_unbound_evidence_and_non_boolean_automation_flag():
    base = {
        "skill_name": "证据绑定认知资产",
        "skill_purpose": "拒绝模型虚构的来源引用。",
        "asset_schema": "cognitive_decision_asset.v1",
        "asset_type": "methodology",
        "evidence_refs": ["invented-raw-event"],
        "applicability": [],
        "failure_modes": [],
        "verification_recipe": [],
        "automation_derivative_allowed": False,
    }

    with pytest.raises(ValueError, match="evidence_ref_unbound"):
        CognitiveDecisionAssetProposal.from_mapping(
            asset_id="cogasset-parent",
            value=base,
            allowed_evidence_refs=("raw-asset-1",),
        )

    base["evidence_refs"] = ["raw-asset-1"]
    base["automation_derivative_allowed"] = "false"
    with pytest.raises(ValueError, match="automation_flag_invalid"):
        CognitiveDecisionAssetProposal.from_mapping(
            asset_id="cogasset-parent",
            value=base,
            allowed_evidence_refs=("raw-asset-1",),
        )
