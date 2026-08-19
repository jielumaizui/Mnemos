"""Canonical identity for the public Mnemos health snapshot."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

CANONICAL_HEALTH_CHECK_IDS = (
    "storage",
    "wiki",
    "agent",
    "daemon",
    "event_bus",
    "schema",
    "amphora",
    "queues",
    "disk",
    "api",
    "multimodal",
    "heartbeat",
    "wiki_route",
    "wiki_projection",
    "system_contracts",
    "module_toggles",
    "runtime_producer_consumer",
    "migrations",
    "backup",
    "data_ownership",
    "model_call_ledger",
    "golden_benchmark",
    "distill_json_quality",
    "distill_cognitive_actions",
    "install_lifecycle",
    "sqlite_disk_budget",
    "adaptive_policy",
    "cognitive_readiness",
    "cognitive_learning",
    "security",
    "auto_healing",
)


def health_check_ids_hash(check_ids: Iterable[str]) -> str:
    """Return an order-sensitive digest for one canonical check sequence."""
    normalized = [str(check_id) for check_id in check_ids]
    encoded = json.dumps(normalized, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


CANONICAL_HEALTH_CHECK_IDS_HASH = health_check_ids_hash(CANONICAL_HEALTH_CHECK_IDS)
