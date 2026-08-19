"""P0 hard-boundary gate for trusted push proposals."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List

from core.trust.config import TrustedPushConfig, load_trusted_push_config
from core.trust.models import CandidateBundle, sha256_json


class GateDecision:
    ALLOW_PENDING_USER_DECISION = "allow_pending_user_decision"
    NEEDS_MANUAL_REVIEW = "needs_manual_review"
    REJECT = "reject"


_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password|private[_-]?key)"),
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


@dataclass(frozen=True)
class GateResult:
    decision: str
    risk_level: str
    reasons: List[str] = field(default_factory=list)
    missing_info: List[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.decision in {
            GateDecision.ALLOW_PENDING_USER_DECISION,
            GateDecision.NEEDS_MANUAL_REVIEW,
        }


class PushDecisionGate:
    """Validate candidate bundles before they enter a writable queue."""

    def __init__(
        self,
        *,
        wiki_base: Path | None = None,
        config: TrustedPushConfig | None = None,
    ):
        self._wiki_base = wiki_base.resolve() if wiki_base else None
        self._config = config or load_trusted_push_config(wiki_base=wiki_base)

    def evaluate(self, candidate: CandidateBundle) -> GateResult:
        reasons: List[str] = []
        missing: List[str] = []
        high_risk = False

        if not candidate.evidence_refs:
            reasons.append("missing evidence_refs")
            missing.append("evidence_refs")
        if not isinstance(candidate.payload, dict) or not candidate.payload:
            reasons.append("empty payload")
        if candidate.payload_hash != sha256_json(candidate.payload):
            reasons.append("payload_hash mismatch")
        if candidate.target_kind not in {"markdown", "native_store", "wiki_page"}:
            reasons.append(f"unsupported target_kind: {candidate.target_kind}")

        target_path = candidate.target_path
        if target_path:
            path_result = self._validate_target_path(target_path)
            reasons.extend(path_result[0])
            high_risk = high_risk or path_result[1]
        elif candidate.target_kind in {"markdown", "wiki_page"}:
            reasons.append("missing target_path")
            missing.append("target_path")

        payload_text = _payload_text(candidate.payload)
        if _has_sensitive_marker(payload_text):
            reasons.append("privacy-sensitive marker detected")
            high_risk = True
        if _has_high_entropy_token(
            payload_text,
            min_length=self._config.high_entropy_min_length,
            threshold=self._config.high_entropy_threshold,
        ):
            reasons.append("high-entropy token detected")
            high_risk = True

        if any(
            r.startswith("missing ")
            or r.endswith("mismatch")
            or r.startswith("unsupported")
            or r == "target_path outside wiki_base"
            for r in reasons
        ):
            return GateResult(
                decision=GateDecision.REJECT,
                risk_level="high" if high_risk else candidate.risk_level,
                reasons=reasons,
                missing_info=missing,
            )
        if high_risk or candidate.risk_level == "high":
            return GateResult(
                decision=GateDecision.NEEDS_MANUAL_REVIEW,
                risk_level="high",
                reasons=reasons or ["high risk requires manual review"],
                missing_info=missing,
            )
        return GateResult(
            decision=GateDecision.ALLOW_PENDING_USER_DECISION,
            risk_level=candidate.risk_level or "medium",
            reasons=reasons,
            missing_info=missing,
        )

    def _validate_target_path(self, target_path: str) -> tuple[List[str], bool]:
        reasons: List[str] = []
        high_risk = False
        path = Path(target_path).expanduser()
        if path.is_absolute() and self._wiki_base is not None:
            try:
                path.resolve().relative_to(self._wiki_base)
            except ValueError:
                reasons.append("target_path outside wiki_base")
                high_risk = True
        if path.exists():
            high_risk = True
            reasons.append("target_path already exists")
        return reasons, high_risk


def _payload_text(payload: object) -> str:
    if isinstance(payload, dict):
        filtered = {
            key: value
            for key, value in payload.items()
            if key not in {"target_path", "page_id", "expected_existing_hash"}
        }
        payload = filtered
    try:
        from core.trust.models import stable_json

        return stable_json(payload)
    except (TypeError, ValueError):
        return str(payload)


def _has_sensitive_marker(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SENSITIVE_PATTERNS)


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = {char: value.count(char) for char in set(value)}
    total = len(value)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _candidate_tokens(text: str, min_length: int) -> Iterable[str]:
    for token in re.findall(r"[A-Za-z0-9+/=_-]+", text):
        if len(token) >= min_length:
            yield token


def _has_high_entropy_token(text: str, *, min_length: int, threshold: float) -> bool:
    return any(_entropy(token) >= threshold for token in _candidate_tokens(text, min_length))
