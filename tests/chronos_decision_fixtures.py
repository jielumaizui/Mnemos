"""Canonical DecisionTrace helpers for Chronos tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from core.cognitive.decision_trace import MaterialActionAuthorization
from core.kia.chronos import (
    CHRONOS_EXECUTOR,
    CHRONOS_OWNER,
    CHRONOS_STEP_EXECUTE_ACTION,
    CHRONOS_TASK_CREATE_ACTION,
    KnowledgeScheduler,
)
from tests.cognitive_decision_fixtures import material_action_authorization
from core.trust.vault_mutation_service import (
    TRUSTED_MARKDOWN_ACTION_TYPE,
    TRUSTED_MARKDOWN_EXECUTOR,
    TRUSTED_MARKDOWN_OWNER,
)


def authorized_knowledge_scheduler(
    *,
    db_path: str | Path,
    **kwargs: Any,
) -> KnowledgeScheduler:
    resolved_path = Path(db_path)

    def resolve(binding: Mapping[str, str]) -> MaterialActionAuthorization:
        action_type = (
            CHRONOS_TASK_CREATE_ACTION
            if binding["target_ref"].startswith("scheduled-task:")
            else CHRONOS_STEP_EXECUTE_ACTION
        )
        return material_action_authorization(
            resolved_path.parent,
            action_type=action_type,
            owner=CHRONOS_OWNER,
            executor=CHRONOS_EXECUTOR,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
        )

    def resolve_markdown(binding: Mapping[str, str]) -> MaterialActionAuthorization:
        return material_action_authorization(
            resolved_path.parent,
            action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
            owner=TRUSTED_MARKDOWN_OWNER,
            executor=TRUSTED_MARKDOWN_EXECUTOR,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
        )

    return KnowledgeScheduler(
        db_path=str(resolved_path),
        material_action_resolver=resolve,
        trusted_markdown_action_resolver=resolve_markdown,
        **kwargs,
    )
