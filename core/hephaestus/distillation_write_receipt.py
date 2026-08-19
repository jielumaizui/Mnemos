"""Typed completion contract for Hephaestus write routing."""

from __future__ import annotations

from dataclasses import replace
import sqlite3
from typing import Any

from core.cognitive.cognition_episode_persistence import commit_cognition_episode
from core.hephaestus.cognition_asset_store import (
    CognitiveDecisionAssetProposal,
    CognitionAssetStore,
    proposal_evidence_catalog,
)
from core.hephaestus.distillation_errors import DistillationAPIError
from core.hephaestus.distillation_models import PipelineLayerResult
from core.evidence.source_authority import output_allows_cognitive_derivative
from core.privacy.content_redaction import (
    REDACTION_POLICY,
    redact_fragments_in_place,
)
from core.pipeline_receipts import DistillationWriteReceipt


def persist_with_receipt(engine: Any, result: Any, cfg: Any) -> DistillationWriteReceipt:
    """Persist accepted artifacts and classify every zero/partial/proposal outcome."""
    if result.judgment not in {"knowledge", "skill"}:
        retryable = result.judgment in {"error", "paused"}
        return DistillationWriteReceipt(
            status="retryable_failed" if retryable else "intentional_skip",
            terminal_reason=result.judgment_reason or f"judgment:{result.judgment}",
        )
    if not result.fragments:
        return DistillationWriteReceipt(
            status="retryable_failed",
            terminal_reason=f"{result.judgment}_judgment_without_fragments",
        )

    if not engine._validate_structured_output_contract(result, cfg):
        return _all_failed(result.fragments, "structured_output_contract_failed")

    fragments = engine._prepare_fragments(list(result.fragments), cfg)
    accepted = engine._filter_accepted_fragments(result, fragments, cfg)
    if accepted is None:
        return _all_failed(fragments, "fragment_quality_gate_blocked_all_writes")
    if not accepted:
        return _all_failed(fragments, "fragment_quality_gate_accepted_zero_fragments")

    cognition_receipts: tuple[str, ...]
    try:
        episode_receipt = commit_cognition_episode(result, cfg)
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError, sqlite3.Error) as exc:
        result.layer_results.append(
            PipelineLayerResult(
                8,
                "cognition_episode_commit",
                False,
                {
                    "status": "retryable_failed",
                    "error_type": type(exc).__name__,
                },
            )
        )
        return _attach_cognition_receipts(
            _all_failed(accepted, "cognition_episode_commit_failed"),
            ("cognition_episode:unassigned:retryable_failed",),
        )
    cognition_receipts = (
        f"cognition_episode:{episode_receipt.revision_id}:{episode_receipt.status}",
    )
    result.layer_results.append(
        PipelineLayerResult(
            8,
            "cognition_episode_commit",
            True,
            {
                "revision_id": episode_receipt.revision_id,
                "event_id": episode_receipt.event_id,
                "status": episode_receipt.status,
                "outbox_ids": list(episode_receipt.outbox_ids),
                "consumer_ids": list(episode_receipt.consumer_ids),
                "redaction_counts": dict(episode_receipt.redaction_counts),
            },
        )
    )
    for fragment in accepted:
        fragment.frontmatter = dict(fragment.frontmatter or {})
        fragment.frontmatter["cognition_episode_revision_id"] = episode_receipt.revision_id

    # Redaction is deliberately narrow and happens only at the durable sink
    # boundary.  The admitted extraction root remains immutable evidence.
    privacy = redact_fragments_in_place(accepted)
    result.fragments = list(accepted)
    result.layer_results.append(
        PipelineLayerResult(
            9,
            "persistence_privacy_redaction",
            True,
            {
                "policy": REDACTION_POLICY,
                "counts": dict(privacy.counts),
                "total": privacy.total,
            },
        )
    )

    if result.judgment == "skill":
        asset_ok, skill_receipts = _persist_skill_cognition(
            engine,
            result,
            accepted,
            cfg,
        )
        cognition_receipts = tuple((*cognition_receipts, *skill_receipts))
        if not asset_ok:
            return _attach_cognition_receipts(
                _all_failed(accepted, "cognition_asset_commit_failed"),
                cognition_receipts,
            )

    if not result.structured_output:
        return _attach_cognition_receipts(
            _all_failed(accepted, "structured_action_root_missing"),
            cognition_receipts,
        )
    if not bool(cfg.get("distill.action_router.enabled", True)):
        return _attach_cognition_receipts(
            _all_failed(accepted, "distill_action_router_disabled"),
            cognition_receipts,
        )
    written, file_fragments = engine._route_structured_actions(result, accepted, cfg)
    # Provenance must be attached while the write boundary still knows which
    # fragment produced each path.  Deferring this to the receipt caller loses
    # that mapping and incorrectly makes every chunked page depend on the full
    # session.
    from core.hephaestus.raw_provenance import record_page_provenance

    missing_provenance_pages = record_page_provenance(
        result,
        tuple(written),
        config=cfg,
        file_fragments=file_fragments,
        page_raw_event_refs=result.page_raw_event_refs,
    )
    if missing_provenance_pages:
        return _attach_cognition_receipts(
            _provenance_failed(accepted, written, missing_provenance_pages),
            cognition_receipts,
        )

    engine._link_cross_agent(file_fragments)
    engine._write_metrics_back(file_fragments)
    engine._emit_distill_events(result, file_fragments, written)
    return _attach_cognition_receipts(
        _classify(result, accepted, written),
        cognition_receipts,
    )


def _persist_skill_cognition(
    engine: Any,
    result: Any,
    accepted: list[Any],
    cfg: Any,
) -> tuple[bool, tuple[str, ...]]:
    """Commit the canonical asset first, then derive an optional proposal."""

    result.skill_suggestion = ""
    result.cognitive_decision_proposal_receipt = None
    store = CognitionAssetStore.from_config(cfg, wiki_base=engine.wiki_base)
    asset_receipt = store.commit_asset(result, accepted)
    result.cognition_asset_receipt = asset_receipt
    asset_status = "committed" if asset_receipt.committed else "retryable_failed"
    asset_ref = asset_receipt.asset_id or "unassigned"
    receipts = [f"cognition_asset:{asset_ref}:{asset_status}"]
    result.layer_results.append(
        PipelineLayerResult(
            9,
            "cognition_asset_commit",
            asset_receipt.committed,
            {
                "asset_id": asset_receipt.asset_id,
                "status": asset_receipt.status,
                "content_hash": asset_receipt.content_hash,
                "error_code": asset_receipt.error_code,
                "redaction_counts": dict(asset_receipt.redaction_counts),
            },
        )
    )
    if not asset_receipt.committed:
        return False, tuple(receipts)

    authority_allows_proposal = output_allows_cognitive_derivative(
        result.structured_output,
        result.input_spec.source_authority_catalog,
    )
    if not authority_allows_proposal:
        proposal_receipt = store.record_proposal_failure(
            asset_receipt.asset_id,
            "source_authority_disallows_cognitive_derivative",
        )
    else:
        try:
            asset_payload = store.load_asset_payload(asset_receipt.asset_id)
            proposal_data = engine._extract_skill_suggestion(result, asset_payload)
            proposal = CognitiveDecisionAssetProposal.from_mapping(
                asset_id=asset_receipt.asset_id,
                value=proposal_data,
                allowed_evidence_refs=proposal_evidence_catalog(asset_payload),
            )
            proposal_receipt = store.commit_proposal(proposal)
            if proposal_receipt.committed:
                result.skill_suggestion = proposal.display_text
        except (
            DistillationAPIError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RuntimeError,
        ):
            proposal_receipt = store.record_proposal_failure(
                asset_receipt.asset_id,
                "cognitive_proposal_generation_failed",
            )
    result.cognitive_decision_proposal_receipt = proposal_receipt
    proposal_status = "committed" if proposal_receipt.committed else "optional_failed"
    proposal_ref = proposal_receipt.proposal_id or asset_receipt.asset_id
    receipts.append(f"cognitive_decision_proposal:{proposal_ref}:{proposal_status}")
    result.layer_results.append(
        PipelineLayerResult(
            10,
            "cognitive_decision_asset_proposal",
            proposal_receipt.committed,
            {
                "asset_id": asset_receipt.asset_id,
                "proposal_id": proposal_receipt.proposal_id,
                "status": proposal_receipt.status,
                "content_hash": proposal_receipt.content_hash,
                "error_code": proposal_receipt.error_code,
                "optional": True,
            },
        )
    )
    return True, tuple(receipts)


def _attach_cognition_receipts(
    receipt: DistillationWriteReceipt,
    cognition_receipts: tuple[str, ...],
) -> DistillationWriteReceipt:
    if not cognition_receipts:
        return receipt
    return replace(
        receipt,
        required_consumer_receipts=tuple(
            dict.fromkeys((*receipt.required_consumer_receipts, *cognition_receipts))
        ),
    )


def _all_failed(fragments: list[Any], reason: str) -> DistillationWriteReceipt:
    return DistillationWriteReceipt(
        status="retryable_failed",
        terminal_reason=reason,
        expected_count=len(fragments),
        failed_count=len(fragments),
    )


def _provenance_failed(
    accepted: list[Any],
    written: list[str],
    missing_pages: tuple[str, ...],
) -> DistillationWriteReceipt:
    """Keep a physical write nonterminal until its exact chunk proof exists."""
    return DistillationWriteReceipt(
        status="retryable_failed",
        terminal_reason="chunked_page_provenance_missing",
        written_pages=tuple(written),
        expected_count=max(len(accepted), len(written)),
        written_count=len(written),
        failed_count=len(missing_pages),
    )


def _classify(result: Any, accepted: list[Any], written: list[str]) -> DistillationWriteReceipt:
    trusted_proposal_ids = tuple(
        dict.fromkeys(
            str(layer.detail.get("proposal_id"))
            for layer in result.layer_results
            if layer.name == "trusted_push"
            and layer.detail.get("intercepted") is True
            and layer.detail.get("proposal_id")
        )
    )
    action_layers = [
        layer for layer in result.layer_results if layer.name == "distill_action_router"
    ]
    action_receipts = [
        receipt
        for layer in action_layers
        for receipt in layer.detail.get("action_receipts", [])
        if isinstance(receipt, dict)
    ]
    action_proposal_ids = tuple(
        str(receipt.get("proposal_id")) for receipt in action_receipts if receipt.get("proposal_id")
    )
    proposal_ids = tuple(dict.fromkeys((*trusted_proposal_ids, *action_proposal_ids)))
    action_errors = [
        str(error) for layer in action_layers for error in layer.detail.get("errors", [])
    ]
    action_errors.extend(
        str(receipt.get("error") or "action_failed")
        for receipt in action_receipts
        if receipt.get("status") == "error"
    )
    skipped_count = sum(receipt.get("status") == "skipped" for receipt in action_receipts)
    expected_count = max(len(accepted), len(action_receipts))
    written_count = len(written)
    resolved_count = min(expected_count, written_count) + skipped_count + len(proposal_ids)
    failed_count = max(len(action_errors), expected_count - resolved_count)
    status, reason = _status_reason(
        proposal_ids=proposal_ids,
        written_count=written_count,
        expected_count=expected_count,
        failed_count=failed_count,
        skipped_count=skipped_count,
        has_action_errors=bool(action_errors),
    )
    return DistillationWriteReceipt(
        status=status,
        terminal_reason=reason,
        written_pages=tuple(written),
        proposal_ids=proposal_ids,
        expected_count=expected_count,
        written_count=written_count,
        failed_count=failed_count,
        required_consumer_receipts=tuple(
            f"action:{receipt.get('action_id')}:{receipt.get('status')}"
            for receipt in action_receipts
            if receipt.get("action_id")
        ),
    )


def _status_reason(
    *,
    proposal_ids: tuple[str, ...],
    written_count: int,
    expected_count: int,
    failed_count: int,
    skipped_count: int,
    has_action_errors: bool,
) -> tuple[str, str]:
    if proposal_ids and failed_count:
        return "partial", "proposal_receipts_exist_but_other_artifacts_failed"
    if proposal_ids:
        reason = (
            "durable_artifacts_committed_but_action_proposals_pending"
            if written_count
            else "trusted_push_requires_decision_and_committed_page"
        )
        return "proposal_pending", reason
    if written_count == 0 and has_action_errors:
        return "retryable_failed", "wiki_write_produced_no_durable_artifact"
    if has_action_errors or (written_count and written_count < expected_count):
        return "partial", "some_distillation_artifacts_failed"
    if written_count >= expected_count:
        return "committed", "all_expected_artifacts_committed"
    if skipped_count >= expected_count:
        return "intentional_skip", "all_structured_actions_intentionally_skipped"
    return "retryable_failed", "wiki_write_produced_no_durable_artifact"
