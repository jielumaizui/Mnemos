"""Immutable raw revision provenance for distillation inputs and outputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core.hephaestus.distillation_models import DistillationResult, KnowledgeFragment


def normalize_raw_event_refs(value: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for ref in value or []:
        if not isinstance(ref, dict) or not ref.get("revision_id"):
            raise ValueError("raw provenance requires revision_id")
        normalized = dict(ref)
        normalized["span_start"] = int(normalized.get("span_start") or 0)
        normalized["span_end"] = int(normalized.get("span_end") or 0)
        if (
            normalized["span_start"] < 0
            or normalized["span_end"] <= normalized["span_start"]
        ):
            raise ValueError("raw provenance requires a non-empty ordered span")
        refs.append(normalized)
    return refs


def _strict_chunk_refs(value: Any) -> list[dict[str, Any]]:
    refs = normalize_raw_event_refs(value)
    if not refs or any(not str(ref.get("content_hash") or "") for ref in refs):
        raise ValueError("chunked raw provenance requires content_hash")
    return refs


def preflight_chunked_write_provenance(
    result: DistillationResult,
    claims: Sequence[Mapping[str, Any]],
    fragments: Sequence[KnowledgeFragment],
) -> dict[int, tuple[dict[str, Any], ...]]:
    """Prove exact page sources before any chunked router write is attempted."""

    if not _is_chunked_result(result):
        return {}
    chunks = tuple(getattr(result, "chunk_extraction_results", ()) or ())
    if not chunks:
        raise ValueError("chunked write provenance requires admitted chunk results")

    if any(claim.get("recommended_action") == "create_page" for claim in claims):
        for fragment in fragments:
            frontmatter = getattr(fragment, "frontmatter", None)
            fragment_refs = (
                frontmatter.get("raw_event_refs") if isinstance(frontmatter, dict) else None
            )
            _strict_chunk_refs(fragment_refs)

    chunks_by_index = {int(chunk.chunk_index): chunk for chunk in chunks}
    claim_refs: dict[int, tuple[dict[str, Any], ...]] = {}
    for claim in claims:
        action = str(claim.get("recommended_action") or "")
        if action in {"", "create_page", "skip"}:
            continue
        origins = claim.get("aggregate_origin")
        if not isinstance(origins, list) or not origins:
            raise ValueError(
                f"chunked write claim lacks aggregate origin: {claim.get('claim_id') or ''}"
            )
        claim_source_refs: list[dict[str, Any]] = []
        for origin in origins:
            if not isinstance(origin, Mapping):
                raise ValueError("chunked write claim has invalid aggregate origin")
            try:
                chunk_index = int(origin["chunk_index"])
                local_ordinal = int(origin["local_ordinal"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("chunked write claim has invalid aggregate origin") from exc
            chunk = chunks_by_index.get(chunk_index)
            if chunk is None or not isinstance(chunk.episode_fragment, Mapping):
                raise ValueError("chunked write claim origin does not bind a local claim")
            local_claims = chunk.episode_fragment.get("claims")
            if (
                not isinstance(local_claims, list)
                or local_ordinal < 0
                or local_ordinal >= len(local_claims)
                or not isinstance(local_claims[local_ordinal], Mapping)
                or str(local_claims[local_ordinal].get("claim_id") or "")
                != str(origin.get("original_claim_id") or "")
            ):
                raise ValueError("chunked write claim origin does not bind a local claim")
            claim_source_refs.extend(_strict_chunk_refs(chunk.source_span_map))
        deduplicated: list[dict[str, Any]] = []
        seen: set[tuple[str, int, int]] = set()
        for ref in claim_source_refs:
            identity = (
                str(ref["revision_id"]),
                int(ref["span_start"]),
                int(ref["span_end"]),
            )
            if identity in seen:
                continue
            seen.add(identity)
            deduplicated.append(ref)
        claim_refs[id(claim)] = tuple(deduplicated)
    return claim_refs


def attach_raw_provenance(
    result: DistillationResult, fragments: Iterable[KnowledgeFragment]
) -> None:
    if not result.raw_event_refs:
        return
    for fragment in fragments:
        frontmatter = fragment.frontmatter
        exact_refs = frontmatter.get("raw_event_refs")
        exact_refs = (
            [dict(ref) for ref in exact_refs if isinstance(ref, dict)]
            if isinstance(exact_refs, list)
            else []
        )
        if exact_refs:
            # COG-012 aggregation may already have attached the exact raw
            # spans consumed by this fragment.  Do not overwrite that proof
            # with the whole session's provenance, which would falsely imply
            # every chunk used every event.
            frontmatter["raw_event_refs"] = exact_refs
            continue
        if _is_chunked_result(result):
            # A defensive caller may hand us an incomplete chunk result.  A
            # full-session ref would be fabricated evidence in that case, so
            # keep the fragment explicitly unprovenanced instead.
            frontmatter.pop("raw_event_refs", None)
            continue
        frontmatter["raw_event_refs"] = [dict(ref) for ref in result.raw_event_refs]


def _page_id(page: str | Path) -> str:
    return str(Path(page).expanduser().resolve())


def _fragment_page_refs(
    file_fragments: Iterable[tuple[Path, KnowledgeFragment]],
) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    """Index the exact provenance already attached to each written fragment.

    The result-level refs describe the complete session, not the individual
    page.  They are therefore not a valid substitute once a concrete fragment
    was responsible for the write.  The second return value marks pages for
    which any producing fragment lacks a valid exact span.
    """
    page_refs: dict[str, list[dict[str, Any]]] = {}
    incomplete_page_ids: set[str] = set()
    for file_path, fragment in file_fragments:
        page_id = _page_id(file_path)
        refs = fragment.frontmatter.get("raw_event_refs")
        if not isinstance(refs, list):
            # A chunked mapped fragment without valid evidence must not
            # acquire a broader session proof as a fallback.  The ordinary
            # single-root path is handled separately below.
            page_refs.setdefault(page_id, [])
            incomplete_page_ids.add(page_id)
            continue
        try:
            exact_refs = normalize_raw_event_refs(refs)
        except (TypeError, ValueError):
            page_refs.setdefault(page_id, [])
            incomplete_page_ids.add(page_id)
            continue
        if not exact_refs:
            page_refs.setdefault(page_id, [])
            incomplete_page_ids.add(page_id)
            continue
        existing = page_refs.setdefault(page_id, [])
        existing_identities = {
            (str(ref["revision_id"]), int(ref["span_start"]), int(ref["span_end"]))
            for ref in existing
        }
        for ref in exact_refs:
            identity = (
                str(ref["revision_id"]),
                int(ref["span_start"]),
                int(ref["span_end"]),
            )
            if identity not in existing_identities:
                existing.append(ref)
                existing_identities.add(identity)
    return page_refs, incomplete_page_ids


def _explicit_page_refs(
    page_raw_event_refs: Iterable[tuple[Path, Sequence[Mapping[str, Any]]]],
) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    page_refs: dict[str, list[dict[str, Any]]] = {}
    incomplete_page_ids: set[str] = set()
    for file_path, raw_refs in page_raw_event_refs:
        page_id = _page_id(file_path)
        try:
            exact_refs = _strict_chunk_refs(raw_refs)
        except (TypeError, ValueError):
            page_refs.setdefault(page_id, [])
            incomplete_page_ids.add(page_id)
            continue
        existing = page_refs.setdefault(page_id, [])
        seen = {
            (str(ref["revision_id"]), int(ref["span_start"]), int(ref["span_end"]))
            for ref in existing
        }
        for ref in exact_refs:
            identity = (
                str(ref["revision_id"]),
                int(ref["span_start"]),
                int(ref["span_end"]),
            )
            if identity not in seen:
                existing.append(ref)
                seen.add(identity)
    return page_refs, incomplete_page_ids


def _is_chunked_result(result: DistillationResult) -> bool:
    return bool(
        getattr(result, "chunk_extraction_results", ())
        or getattr(result, "chunk_aggregate", None)
        or getattr(result, "analysis_type", "") == "chunked"
        or str(getattr(result, "distill_input_mode", "")).startswith("chunked")
    )


def record_page_provenance(
    result: DistillationResult,
    written_pages: tuple[str, ...],
    *,
    config: Any | None = None,
    file_fragments: Iterable[tuple[Path, KnowledgeFragment]] = (),
    page_raw_event_refs: Iterable[
        tuple[Path, Sequence[Mapping[str, Any]]]
    ] = (),
) -> tuple[str, ...]:
    """Record page edges from the fragment that actually produced each page.

    Non-chunked callers that only have paths retain their session-level
    fallback.  Chunked outputs never use that fallback: absent or malformed
    per-fragment refs produce no broad provenance edge.
    """
    if not written_pages:
        return ()
    page_refs, incomplete_page_ids = _fragment_page_refs(file_fragments)
    routed_refs, routed_incomplete = _explicit_page_refs(page_raw_event_refs)
    incomplete_page_ids.update(routed_incomplete)
    for page_id, routed_page_refs in routed_refs.items():
        existing = page_refs.setdefault(page_id, [])
        seen = {
            (str(ref["revision_id"]), int(ref["span_start"]), int(ref["span_end"]))
            for ref in existing
        }
        for ref in routed_page_refs:
            identity = (
                str(ref["revision_id"]),
                int(ref["span_start"]),
                int(ref["span_end"]),
            )
            if identity not in seen:
                existing.append(ref)
                seen.add(identity)
    chunked = _is_chunked_result(result)
    fallback_refs: list[dict[str, Any]] = []
    if not chunked:
        try:
            fallback_refs = normalize_raw_event_refs(result.raw_event_refs)
        except (TypeError, ValueError):
            fallback_refs = []
    if not page_refs and not fallback_refs and not chunked:
        return ()
    from core.sync_framework.raw_event_store import RawEventStore

    store = RawEventStore(config=config)
    missing_pages: list[str] = []
    try:
        for page in written_pages:
            consumer_id = _page_id(page)
            # When the persistence layer gives us the source fragment, its
            # frontmatter is the only admissible proof for this page.  The
            # broad fallback exists solely for single-root full-session calls
            # that have no page-to-fragment mapping at all.
            page_source_refs = page_refs.get(consumer_id)
            if chunked and consumer_id in incomplete_page_ids:
                page_source_refs = []
            if page_source_refs is None or (not page_source_refs and not chunked):
                page_source_refs = fallback_refs
            if not page_source_refs:
                # A chunked write with no exact fragment proof is not an
                # implicit full-session edge.  Keep an auditable pending gap
                # instead, and do not resolve any earlier missing-proof row.
                if chunked:
                    store.record_provenance_gap(
                        consumer_type="wiki_page",
                        consumer_id=consumer_id,
                        reason="chunked_fragment_raw_provenance_missing",
                        source_agent=str(getattr(result, "source", "") or ""),
                        session_id=str(getattr(result, "session_id", "") or ""),
                    )
                    missing_pages.append(consumer_id)
                continue
            for ref in page_source_refs:
                store.record_provenance_edge(
                    source_revision_id=str(ref["revision_id"]),
                    span_start=int(ref.get("span_start") or 0),
                    span_end=int(ref.get("span_end") or 0),
                    consumer_type="wiki_page",
                    consumer_id=consumer_id,
                )
            store.resolve_provenance_gaps(
                consumer_type="wiki_page", consumer_id=consumer_id
            )
    finally:
        store.close()
    return tuple(dict.fromkeys(missing_pages))
