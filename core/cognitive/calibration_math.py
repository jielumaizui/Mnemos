"""Pure, replayable math and identity helpers for Observation calibration."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def recompute_posterior(
    prior: float,
    validations: Iterable[Mapping[str, Any]],
    *,
    prior_weight: float,
    precision: int = 2,
) -> float:
    """Replay weighted evidence shrinkage from persisted validator results."""

    conclusive = [
        validation
        for validation in validations
        if str(validation.get("verdict") or "") != "inconclusive"
    ]
    evidence_weight = sum(float(value["weight"]) for value in conclusive)
    weighted_score = sum(
        float(value["score"]) * float(value["weight"])
        for value in conclusive
    )
    posterior = (
        float(prior)
        if evidence_weight == 0
        else (float(prior) * float(prior_weight) + weighted_score)
        / (float(prior_weight) + evidence_weight)
    )
    return round(max(0.1, min(1.0, posterior)), precision)


def validator_input_hash(
    *,
    calculation_input_hash: str,
    validator_name: str,
    validator_code_hash: str,
) -> str:
    """Bind one validator result to the shared input and exact validator code."""

    return canonical_hash(
        {
            "calculation_input_hash": calculation_input_hash,
            "validator_name": validator_name,
            "validator_code_hash": validator_code_hash,
        }
    )


__all__ = ["canonical_hash", "recompute_posterior", "validator_input_hash"]
