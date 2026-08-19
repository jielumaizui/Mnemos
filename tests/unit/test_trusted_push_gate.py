from pathlib import Path

from core.trust import CandidateBundle, PushDecisionGate


def test_gate_rejects_candidate_without_evidence(tmp_path: Path):
    candidate = CandidateBundle.from_payload(
        source="test",
        target_kind="markdown",
        target_path=str(tmp_path / "page.md"),
        payload={"content": "# Page\n"},
        evidence_refs=[],
    )

    result = PushDecisionGate(wiki_base=tmp_path).evaluate(candidate)

    assert result.decision == "reject"
    assert "missing evidence_refs" in result.reasons


def test_gate_marks_high_entropy_payload_for_manual_review(tmp_path: Path):
    secret_like = (
        "Az9+/Bc8_De7-Fg6+Hi5/Jk4_Lm3-No2+Pq1/Rs0_Tu9-Vw8+Xy7/"
        "Za6_Bc5-De4+Fg3/Hi2_Jk1-Lm0+No9/Pq8_Rs7-Tu6+Vw5/Xy4"
    )
    candidate = CandidateBundle.from_payload(
        source="test",
        target_kind="markdown",
        target_path=str(tmp_path / "page.md"),
        payload={"content": f"# Page\n\n{secret_like}"},
        evidence_refs=["session:1"],
    )

    result = PushDecisionGate(wiki_base=tmp_path).evaluate(candidate)

    assert result.decision == "needs_manual_review"
    assert result.risk_level == "high"
    assert any("high-entropy" in reason for reason in result.reasons)


def test_gate_rejects_target_path_outside_wiki_base(tmp_path: Path):
    outside = tmp_path.parent / "outside.md"
    candidate = CandidateBundle.from_payload(
        source="test",
        target_kind="markdown",
        target_path=str(outside),
        payload={"content": "# Outside\n"},
        evidence_refs=["session:1"],
    )

    result = PushDecisionGate(wiki_base=tmp_path).evaluate(candidate)

    assert result.decision == "reject"
    assert "target_path outside wiki_base" in result.reasons


def test_gate_recalculates_risk_on_edit(tmp_path: Path):
    from core.trust.proposal_queue import ProposalQueue

    db_path = tmp_path / "trusted.db"
    candidate = CandidateBundle.from_payload(
        source="test",
        target_kind="markdown",
        target_path=str(tmp_path / "page.md"),
        payload={"content": "# Safe\n\nNo sensitive text."},
        evidence_refs=["session:1"],
    )
    queue = ProposalQueue(db_path, wiki_base=tmp_path)
    proposal = queue.submit_candidate(candidate)

    updated = queue.revise_payload(
        proposal.proposal_id,
        {"content": "# Unsafe\n\napi_key = 'abc'"},
    )

    assert proposal.status == "validated"
    assert updated.status == "needs_manual_review"
    assert updated.risk_level == "high"
