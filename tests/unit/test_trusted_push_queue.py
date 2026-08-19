from pathlib import Path

from core.trust import CandidateBundle, ProposalQueue


def test_queue_persists_validated_proposal(tmp_path: Path):
    candidate = CandidateBundle.from_payload(
        source="hephaestus_distillation",
        target_kind="markdown",
        target_path=str(tmp_path / "page.md"),
        payload={"content": "# Page\n\nBody"},
        evidence_refs=["session:abc"],
    )

    queue = ProposalQueue(tmp_path / "trusted.db", wiki_base=tmp_path)
    proposal = queue.submit_candidate(candidate)

    assert proposal.status == "validated"
    assert queue.get(proposal.proposal_id).candidate.payload["content"].startswith("# Page")
    assert queue.list(statuses=["validated"])[0].proposal_id == proposal.proposal_id


def test_queue_reuses_exact_active_retry_but_not_rejected_receipt(tmp_path: Path):
    def candidate():
        return CandidateBundle.from_payload(
            source="hephaestus_distillation",
            source_agent="codex",
            source_session_id="session-1",
            target_kind="markdown",
            target_path=str(tmp_path / "page.md"),
            payload={"content": "# Page\n\nBody", "input_revision": "revision-1"},
            evidence_refs=["session:session-1"],
        )

    queue = ProposalQueue(tmp_path / "trusted.db", wiki_base=tmp_path)
    first = queue.submit_candidate(candidate())
    exact_retry = queue.submit_candidate(candidate())
    queue.update_status(first.proposal_id, "rejected")
    after_rejection = queue.submit_candidate(candidate())

    assert exact_retry.proposal_id == first.proposal_id
    assert after_rejection.proposal_id != first.proposal_id
