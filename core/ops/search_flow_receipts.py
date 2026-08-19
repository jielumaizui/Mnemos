"""Runtime receipt adapters for search, heat, feedback, and persona weighting."""

from __future__ import annotations

from typing import Any, Iterable

from core.ops.runtime_flow_telemetry import (
    record_runtime_consumed,
    record_runtime_produced,
    runtime_item_id,
)


def start_heat_application(database_dir: Any, page_path: str) -> str:
    item_id = runtime_item_id("wiki-page", page_path)
    record_runtime_produced(
        "heat_to_search_and_quality_gate",
        source="core/wiki_metrics.py",
        item_id=item_id,
        intended_consumers=["core/app/context_search.py"],
        metadata={"transition": "heat_metric_updated"},
        config_or_path=database_dir,
    )
    return item_id


def finish_heat_application(database_dir: Any, item_id: str) -> None:
    record_runtime_consumed(
        "heat_to_search_and_quality_gate",
        source="core/app/context_search.py",
        item_id=item_id,
        metadata={"transition": "search_result_heat_applied"},
        config_or_path=database_dir,
    )


def start_search_feedback(database_dir: Any, session_id: str, transition: str) -> None:
    record_runtime_produced(
        "search_feedback_to_scoring",
        source="core/app/context_search.py",
        item_id=session_id,
        intended_consumers=["core/scoring/adaptive_scorer_v2.py"],
        metadata={"transition": transition},
        config_or_path=database_dir,
    )


def finish_search_feedback(database_dir: Any, session_id: str, label: int) -> None:
    record_runtime_consumed(
        "search_feedback_to_scoring",
        source="core/scoring/adaptive_scorer_v2.py",
        item_id=session_id,
        metadata={"transition": "ground_truth_inserted", "label": label},
        config_or_path=database_dir,
    )


def start_persona_search(
    database_dir: Any,
    assertion_revision_refs: Iterable[str],
) -> str:
    item_id = runtime_item_id(
        "persona-profile",
        *sorted(assertion_revision_refs),
    )
    record_runtime_produced(
        "persona_to_behavior_and_search",
        source="core/persona/psyche.py",
        item_id=item_id,
        intended_consumers=["core/app/context_search.py"],
        metadata={"transition": "profile_weights_loaded"},
        config_or_path=database_dir,
    )
    return item_id


def finish_persona_search(database_dir: Any, item_id: str, result_count: int) -> None:
    record_runtime_consumed(
        "persona_to_behavior_and_search",
        source="core/app/context_search.py",
        item_id=item_id,
        metadata={
            "transition": "persona_weighted_search_completed",
            "result_count": result_count,
        },
        config_or_path=database_dir,
    )
