# -*- coding: utf-8 -*-
"""Shared models for structured forced retrospectives."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


RETROSPECTIVE_SCHEMA = "mnemos.retrospective.v1"
RECAP_SKIP_SCHEMA = "mnemos.recap_skip.v1"

RECAP_QUESTIONS = [
    {
        "id": "goal_actual",
        "text": "目标和实际是什么？",
    },
    {
        "id": "cause_lesson",
        "text": "最关键原因或教训是什么？",
    },
    {
        "id": "next_handling",
        "text": "下次具体怎么改？",
    },
]

RECAP_STATE_ORDER = [
    "detected",
    "started",
    "q1_goal_actual",
    "q2_cause_lesson",
    "q3_next_handling",
    "draft_generated",
    "user_confirmed",
    "proposal_pending",
    "consumption_pending",
    "retryable_failed",
    "finalized",
    "consumed",
]

VALID_SKIP_REASONS = {
    "no_time",
    "low_value",
    "false_positive",
    "already_handled",
    "no_response",
}

SKIP_POLICY_MAP = {
    "no_time": ("deferred", "reschedule", True),
    "low_value": ("dismissed", "lower_similar_trigger_weight", False),
    "false_positive": ("false_positive", "correct_trigger", False),
    "already_handled": ("already_handled", "archive", False),
    "no_response": ("cooldown", "cooldown", False),
}


def utcnow_iso() -> str:
    """Return an ISO timestamp without forcing callers to import datetime."""
    return datetime.now().isoformat()


@dataclass
class RetrospectiveActionItem:
    """Action item extracted from the third recap question."""

    action_id: str
    action: str
    owner: str = ""
    deadline: str = ""
    metric: str = ""
    follow_up_at: str = ""
    status: str = "open"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RetrospectiveDraft:
    """Structured draft generated after the three questions."""

    recap_id: str
    task_id: str
    title: str
    lesson: str
    goal: str
    actual: str
    delta: str
    root_type: List[str] = field(default_factory=list)
    root_cause: str = ""
    next_handling: str = "specific_action"
    no_action_reason: str = ""
    action_items: List[RetrospectiveActionItem] = field(default_factory=list)
    activation_rules: Dict[str, Any] = field(default_factory=dict)
    consumption_targets: List[str] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    missing_fields: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["action_items"] = [item.to_dict() for item in self.action_items]
        return data


@dataclass
class RetrospectiveRecord:
    """Final record ready to be persisted to the Wiki."""

    draft: RetrospectiveDraft
    status: str = "confirmed"
    completion_state: str = "confirmed"
    source: str = "system"
    source_agent: str = ""
    owner_agent: str = ""
    source_agents: List[str] = field(default_factory=list)
    session_id: str = ""
    project: str = ""
    task_type: str = ""
    subtype: str = ""
    severity: str = "medium"
    write_policy: str = "recap_finalize"
    trigger_reason: List[str] = field(default_factory=list)
    reviewed_at: str = field(default_factory=utcnow_iso)
    follow_up_at: str = ""
    related_pages: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["draft"] = self.draft.to_dict()
        return data


@dataclass
class RecapSession:
    """State-machine row for one active recap conversation."""

    recap_id: str
    task_id: str
    state: str
    owner_agent: str
    source_agent: str = ""
    source_agents: List[str] = field(default_factory=list)
    session_id: str = ""
    mode: str = "minimal"
    topic: str = ""
    project: str = ""
    task_type: str = ""
    subtype: str = ""
    answers: Dict[str, str] = field(default_factory=dict)
    draft: Optional[RetrospectiveDraft] = None
    finalized_page: str = ""
    completion_receipt: Dict[str, Any] = field(default_factory=dict)
    evidence_refs: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)

    def answered_questions(self) -> List[str]:
        return [question["id"] for question in RECAP_QUESTIONS if self.answers.get(question["id"])]

    def next_question(self) -> str:
        for question in RECAP_QUESTIONS:
            if not self.answers.get(question["id"]):
                return question["id"]
        return ""


@dataclass
class SkipEvent:
    """Structured feedback produced when a user skips a recap."""

    event_id: str
    recap_id: str
    task_id: str
    skip_reason: str
    skip_status: str
    next_policy: str
    source: str = "system"
    source_agent: str = ""
    owner_agent: str = ""
    source_agents: List[str] = field(default_factory=list)
    project: str = ""
    task_type: str = ""
    trigger_reason: List[str] = field(default_factory=list)
    selected_at: str = field(default_factory=utcnow_iso)
    defer_until: str = ""
    user_note: str = ""
    consumption_targets: List[str] = field(default_factory=list)
    write_to_wiki: bool = False
    consumption_plan: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["event_type"] = "recap_skipped"
        data["schema"] = RECAP_SKIP_SCHEMA
        return data


@dataclass
class ConsumptionPlan:
    """Where a finalized recap or skip event should be consumed next."""

    recap_id: str
    targets: List[str]
    activation_rules: Dict[str, Any] = field(default_factory=dict)
    consume_priority: str = "medium"
    follow_up_at: str = ""
    outcomes: List[Dict[str, Any]] = field(default_factory=list)
    plan_id: str = ""
    plan_status: str = "pending"
    target_statuses: List[Dict[str, Any]] = field(default_factory=list)
    required_receipt_count: int = 0
    terminal_receipt_count: int = 0
    consumed_at: str = ""
    retryable: bool = False
    failed_targets: List[str] = field(default_factory=list)
    effect_evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
