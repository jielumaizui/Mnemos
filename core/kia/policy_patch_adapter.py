# -*- coding: utf-8 -*-
"""Project PolicyPatch matches onto KIA checklist and response contracts."""

from __future__ import annotations

import json
import re
from typing import Any, Dict

from core.kia.prophasis import ChecklistItem


def _trigger_terms(trigger: Any) -> list[str]:
    text = str(trigger or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, list):
            return [str(item) for item in loaded if str(item or "").strip()]
    return [text]


def to_checklist_items(policy_patches: list[Any]) -> list[ChecklistItem]:
    """Convert explained PolicyPatch matches into regular KIA checklist items."""

    items = []
    for patch in policy_patches:
        metadata = dict(getattr(patch, "metadata", {}) or {})
        trigger_keywords = list(metadata.get("matched_triggers") or [])
        if not trigger_keywords:
            trigger_keywords = _trigger_terms(getattr(patch, "trigger", ""))
        evidence_refs = list(getattr(patch, "evidence_refs", []) or [])
        items.append(
            ChecklistItem(
                item=f"策略补丁: {getattr(patch, 'content', '')}",
                source=f"policy_patch:{getattr(patch, 'patch_id', '')}",
                severity=getattr(patch, "severity", "medium"),
                freshness_score=1.0,
                applies_when=[getattr(patch, "task_type", "general")],
                trigger_keywords=trigger_keywords,
                risk_patterns=[re.escape(trigger) for trigger in trigger_keywords],
                detail=(
                    f"source_type={getattr(patch, 'source_type', '')}; "
                    f"scope={getattr(patch, 'scope', '')}; "
                    f"match_source={metadata.get('match_source', '')}; "
                    f"task_fit_score={metadata.get('task_fit_score', '')}; "
                    f"dedupe_key={metadata.get('dedupe_key', '')}; "
                    "delivery_mode=preflight_guard_only; "
                    f"evidence_refs={','.join(evidence_refs)}"
                ),
            )
        )
    return items


def to_response_dicts(policy_patches: list[Any]) -> list[Dict]:
    """Expose retrieval explanations without changing the stored patch schema."""

    items = []
    explanation_keys = (
        "match_source",
        "matched_triggers",
        "task_fit_score",
        "dedupe_key",
        "interruption_budget",
        "interruption_budget_ok",
    )
    for patch in policy_patches:
        item = patch.to_dict() if hasattr(patch, "to_dict") else dict(patch)
        metadata = dict(item.get("metadata") or {})
        item.update({key: metadata[key] for key in explanation_keys if key in metadata})
        items.append(item)
    return items
