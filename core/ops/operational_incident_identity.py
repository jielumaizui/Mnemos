"""Canonical identity helpers shared by incident storage and replay."""

from __future__ import annotations

import hashlib
import json
from typing import Iterable


def canonical_replay_input_binding_hash(
    *,
    session_id: str,
    prompt_hash: str,
    visible_input_sha256: str,
    response_hash: str,
    source_event_refs: Iterable[str],
    artifact_hash: str,
) -> str:
    """Seal the immutable occurrence inputs that a formal replay must reuse."""

    payload = {
        "session_id": session_id,
        "prompt_hash": prompt_hash,
        "visible_input_sha256": visible_input_sha256,
        "response_hash": response_hash,
        "source_event_refs": list(source_event_refs),
        "artifact_hash": artifact_hash,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


__all__ = ["canonical_replay_input_binding_hash"]
