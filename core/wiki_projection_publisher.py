"""Publish durable Wiki mutations without coupling the ledger to EventBus."""

from __future__ import annotations

from pathlib import Path
import hashlib
from typing import Any

from core.wiki_projection_lifecycle import WikiMutationReceipt, WikiProjectionLedger


def _system_projection_event_provenance(
    receipt: WikiMutationReceipt, source: str
) -> dict[str, Any]:
    """Attribute lifecycle control metadata without widening page access.

    This envelope covers the mutation event only; it does not authorize Wiki
    body retrieval.  Callers with an exact source-object ACL may pass that
    stricter provenance explicitly.
    """

    from core.cognitive.access_control import make_cognitive_access_envelope

    material = "\x1f".join(
        (
            str(receipt.mutation_id),
            str(receipt.page_id),
            str(receipt.page_revision),
            str(source),
        )
    )
    lineage = "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
    return make_cognitive_access_envelope(
        owner_principal_id="system:wiki_projection_lifecycle",
        owner_agent="mnemos",
        scope_type="project",
        scope_id="mnemos",
        project="mnemos",
        purposes=("wiki_projection_lifecycle",),
        consent_provenance_refs=(lineage,),
        sensitivity="sensitive",
        retention_policy="wiki_projection_lifecycle",
        source_acl_lineage=(lineage,),
        visibility="system",
    )


def publish_wiki_mutation(
    receipt: WikiMutationReceipt,
    *,
    ledger: WikiProjectionLedger,
    source: str = "wiki_mutation",
    event_bus: Any | None = None,
    subject_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish one already-recorded mutation through its exact ledger owner.

    Subject deletion must not fall back to the process-global projection path:
    the event and its immutable receipt have to bind the same ledger row that
    was tombstoned before the Markdown body is removed.
    """

    if receipt.event_trace_id:
        return receipt.to_dict()
    payload = {
        "page_path": receipt.page_path,
        "previous_path": receipt.previous_path,
        "page_id": receipt.page_id,
        "page_revision": receipt.page_revision,
        "mutation_id": receipt.mutation_id,
        "mutation_type": receipt.mutation_type,
        "tombstone": receipt.tombstone,
        "update_type": receipt.mutation_type,
    }
    event_provenance = subject_provenance or _system_projection_event_provenance(receipt, source)
    if event_bus is None:
        from core.mnemos_bus import publish_event

        trace_id = publish_event(
            "wiki_page_updated",
            source,
            payload,
            trace_id=receipt.mutation_id,
            subject_provenance=event_provenance,
        )
    else:
        from core.mnemos_bus import Event

        trace_id = event_bus.publish(
            Event(
                event_type="wiki_page_updated",
                source=source,
                payload=payload,
                trace_id=receipt.mutation_id,
                subject_provenance=event_provenance,
            )
        )
    if not trace_id:
        raise RuntimeError(
            f"EventBus did not return a trace id for Wiki mutation {receipt.mutation_id}"
        )
    ledger.attach_event(receipt.mutation_id, trace_id)
    return {**receipt.to_dict(), "event_trace_id": trace_id}


def publish_wiki_page_updated(
    page_path: Path,
    update_type: str = "update",
    *,
    previous_path: Path | None = None,
    page_id: str = "",
    source: str = "wiki_mutation",
    ledger: WikiProjectionLedger | None = None,
    event_bus: Any | None = None,
) -> dict[str, Any]:
    """Durably record and publish one authoritative Wiki lifecycle mutation."""

    normalized = str(update_type or "update").strip().lower()
    mutation_type = "update" if normalized in {"append", "replace", "merge"} else normalized
    target_ledger = ledger or WikiProjectionLedger()
    receipt = target_ledger.record_mutation(
        page_path,
        mutation_type=mutation_type,
        previous_path=previous_path,
        page_id=page_id,
    )
    published = publish_wiki_mutation(
        receipt,
        ledger=target_ledger,
        source=source,
        event_bus=event_bus,
    )
    published["update_type"] = update_type
    return published


def publish_unpublished_mutations(
    ledger: WikiProjectionLedger,
    *,
    limit: int = 100,
    source: str = "wiki_projection_reconcile",
) -> dict[str, Any]:
    """Publish durable, not-yet-linked mutations to the shared EventBus."""

    from core.mnemos_bus import publish_event

    published: list[dict[str, str]] = []
    for receipt in ledger.unpublished_mutations(limit=limit):
        payload = {
            "page_path": receipt.page_path,
            "previous_path": receipt.previous_path,
            "page_id": receipt.page_id,
            "page_revision": receipt.page_revision,
            "mutation_id": receipt.mutation_id,
            "mutation_type": receipt.mutation_type,
            "tombstone": receipt.tombstone,
            "update_type": receipt.mutation_type,
        }
        trace_id = publish_event(
            "wiki_page_updated",
            source,
            payload,
            trace_id=receipt.mutation_id,
            subject_provenance=_system_projection_event_provenance(receipt, source),
        )
        if not trace_id:
            raise RuntimeError(f"EventBus did not return a trace id for {receipt.mutation_id}")
        ledger.attach_event(receipt.mutation_id, trace_id)
        published.append({"mutation_id": receipt.mutation_id, "event_trace_id": trace_id})
    return {"published": len(published), "events": published}
