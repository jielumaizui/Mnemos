"""Authoritative contract validation for durable Wiki mutation events."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.wiki_projection_lifecycle import WikiProjectionLedger
from core.event_outcome import HandlerOutcome


class InvalidWikiMutationEvent(ValueError):
    """Raised before dispatch when an event cannot be tied to its ledger row."""


def canonicalize_wiki_mutation_event(event: Any, ledger_path: Path) -> Any:
    """Return an event whose lifecycle fields come from the durable ledger."""

    mutation_id = str(event.payload.get("mutation_id") or "")
    canonical = (
        WikiProjectionLedger(ledger_path).get_mutation(mutation_id)
        if mutation_id
        else None
    )
    if canonical is None:
        raise InvalidWikiMutationEvent("unknown Wiki mutation_id")
    if event.trace_id != mutation_id:
        raise InvalidWikiMutationEvent("Wiki mutation trace_id must equal mutation_id")
    claimed_trace = str(canonical.get("event_trace_id") or "")
    if claimed_trace and claimed_trace != event.trace_id:
        raise InvalidWikiMutationEvent("Wiki mutation trace_id conflicts with ledger claim")

    fields = (
        "mutation_id",
        "page_id",
        "page_revision",
        "page_path",
        "previous_path",
        "mutation_type",
        "tombstone",
    )
    mismatches = [
        name
        for name in fields
        if name in event.payload and event.payload[name] != canonical[name]
    ]
    if mismatches:
        raise InvalidWikiMutationEvent(
            "Wiki mutation payload mismatch: " + ", ".join(mismatches)
        )

    payload = dict(event.payload)
    payload.update({name: canonical[name] for name in fields})
    payload["update_type"] = canonical["mutation_type"]
    return type(event)(
        event_type=event.event_type,
        source=event.source,
        payload=payload,
        timestamp=event.timestamp,
        trace_id=event.trace_id,
        chain_depth=event.chain_depth,
    )


def predecessor_retry(
    event: Any, consumer: str, ledger_path: Path
) -> HandlerOutcome | None:
    """Block a projection side effect until this consumer completed prior revisions."""

    if event.event_type != "wiki_page_updated":
        return None
    mutation_id = str(event.payload.get("mutation_id") or "")
    predecessor = WikiProjectionLedger(ledger_path).first_unacknowledged_predecessor(
        mutation_id, consumer
    )
    if predecessor is None:
        return None
    return HandlerOutcome.defer(
        consumer,
        f"waiting for predecessor revision: {predecessor}",
        predecessor_mutation_id=predecessor,
        deferred_keys=[f"projection:{predecessor}:{consumer}"],
    )


def projection_already_complete(event: Any, consumer: str, ledger_path: Path) -> bool:
    """Return whether this exact mutation already crossed the consumer watermark."""

    if event.event_type != "wiki_page_updated":
        return False
    mutation_id = str(event.payload.get("mutation_id") or "")
    return (
        WikiProjectionLedger(ledger_path).terminal_projection_receipt(
            mutation_id, consumer
        )
        is not None
    )
