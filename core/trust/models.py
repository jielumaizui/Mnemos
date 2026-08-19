"""Shared models for trusted push proposals and journal events."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(data: Any) -> str:
    return sha256_text(stable_json(data))


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class CandidateBundle:
    candidate_id: str
    source: str
    source_agent: str | None
    source_session_id: str | None
    target_kind: str
    target_path: str | None
    payload: Dict[str, Any]
    payload_hash: str
    evidence_refs: List[str]
    confidence_score: float
    risk_level: str
    missing_info: List[str]
    proposed_actions: List[str]
    created_at: str = field(default_factory=utc_now_iso)

    @classmethod
    def from_payload(
        cls,
        *,
        source: str,
        target_kind: str,
        payload: Dict[str, Any],
        target_path: str | None = None,
        evidence_refs: List[str] | None = None,
        source_agent: str | None = None,
        source_session_id: str | None = None,
        confidence_score: float = 0.7,
        risk_level: str = "medium",
        missing_info: List[str] | None = None,
        proposed_actions: List[str] | None = None,
    ) -> "CandidateBundle":
        return cls(
            candidate_id=new_id("cand"),
            source=source,
            source_agent=source_agent,
            source_session_id=source_session_id,
            target_kind=target_kind,
            target_path=target_path,
            payload=payload,
            payload_hash=sha256_json(payload),
            evidence_refs=list(evidence_refs or []),
            confidence_score=confidence_score,
            risk_level=risk_level,
            missing_info=list(missing_info or []),
            proposed_actions=list(proposed_actions or ["create_or_update"]),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CandidateBundle":
        return cls(**data)


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    candidate: CandidateBundle
    status: str
    gate_decision: str
    gate_reasons: List[str]
    risk_level: str
    created_at: str
    updated_at: str
    revision: int = 0
    error_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["candidate"] = self.candidate.to_dict()
        return data


@dataclass(frozen=True)
class UserDecision:
    proposal_id: str
    decision: str
    actor: str = "user"
    reason: str = ""
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class JournalEventInput:
    proposal_id: str
    event_type: str
    target_uri: str
    content_hash: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    actor: str = "mnemos"

    def canonical_payload(self, previous_hash: str) -> Dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "event_type": self.event_type,
            "target_uri": self.target_uri,
            "content_hash": self.content_hash,
            "metadata": self.metadata,
            "actor": self.actor,
            "previous_hash": previous_hash,
        }
