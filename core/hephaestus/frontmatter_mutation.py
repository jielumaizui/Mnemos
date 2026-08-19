"""Trusted mutation helper for Hephaestus page frontmatter."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.cognitive.state_contract import sha256_json
from core.frontmatter import parse_frontmatter, to_chinese_frontmatter, write_frontmatter
from core.trust.formal_markdown import (
    TrustedMarkdownDecisionPolicy,
    submit_or_write_markdown_with_decision,
)
from core.trust.models import sha256_text

logger = logging.getLogger(__name__)

DISTILL_FRONTMATTER_MARKDOWN_POLICY = TrustedMarkdownDecisionPolicy(
    contract_id="project-contract:distillation-frontmatter-update",
    contract_revision_id="mnemos.distillation_frontmatter_update.v1",
    contract_text=(
        "Hephaestus may update one exact frontmatter field on the exact distilled "
        "page preimage selected by the active distillation workflow."
    ),
    source_namespace="distillation-frontmatter-update",
    producer="hephaestus-frontmatter-mutation",
    producer_code_hash=sha256_json(
        {
            "module": "core.hephaestus.frontmatter_mutation",
            "producer": "update_frontmatter_field",
            "version": "mnemos.distillation_frontmatter_update.v1",
        }
    ),
    evaluator_id="distillation-frontmatter-evaluator",
    constraints=(
        "The target, preimage, field name, field value, and rendered bytes remain exact.",
        "The mutation may not alter a page that lacks formal frontmatter.",
    ),
    approved_candidate_key="apply_exact_frontmatter_field",
    approved_candidate_summary="Apply the exact distilled-page frontmatter field.",
    rejected_candidate_key="retain_distilled_page_preimage",
    rejected_candidate_summary="Retain the page when its preimage or field binding drifts.",
    approved_reason_code="distillation_frontmatter_binding_verified",
    rejected_reason_code="distillation_frontmatter_binding_rejected",
    committed_metric="distillation_frontmatter_committed",
    rejected_metric="unbound_distillation_frontmatter_count",
)


def update_frontmatter_field(
    file_path: Path,
    key: str,
    value: Any,
    *,
    wiki_base: Path,
) -> None:
    """Update one formal-page field through the typed trusted mutation path."""

    try:
        text = file_path.read_text(encoding="utf-8")
        frontmatter, body = parse_frontmatter(text)
        if frontmatter is None:
            return
        frontmatter[key] = value
        new_text = write_frontmatter(to_chinese_frontmatter(frontmatter), body)
        evidence_refs = [f"page:{file_path.name}", f"frontmatter:{key}"]
        submit_or_write_markdown_with_decision(
            decision_policy=DISTILL_FRONTMATTER_MARKDOWN_POLICY,
            decision_facts={
                "schema_version": "mnemos.distillation_frontmatter_facts.v1",
                "field": key,
                "value": value,
            },
            decision_task=f"Update distilled-page frontmatter field {key}",
            decision_goal="Persist the exact metadata produced by the distillation workflow.",
            decision_created_at=datetime.now(timezone.utc).isoformat(),
            wiki_base=wiki_base,
            target_path=file_path,
            content=new_text,
            source="hephaestus_frontmatter_update",
            actor="distillation_engine",
            evidence_refs=evidence_refs,
            proposed_action="update_distillation_frontmatter",
            expected_existing_hash=sha256_text(text),
        )
    except (OSError, ValueError, TypeError, ImportError, AttributeError, RuntimeError):
        logger.debug("Frontmatter update failed for %s", file_path, exc_info=True)
