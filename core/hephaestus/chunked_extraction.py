"""Deep module for resumable, deterministic chunked extraction orchestration.

The Engine owns the surrounding seven-layer lifecycle.  This module owns the
smaller seam inside it: convert each cold/cache-hit local extraction into an
admitted bundle, then produce one session aggregate exactly once.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from core.evidence.artifact_catalog import ArtifactCatalogRejectedError
from core.evidence.source_authority import SourceAuthorityCatalogRejectedError
from core.hephaestus.chunk_aggregate import ChunkAggregateError, ChunkEpisodeMerger
from core.hephaestus.chunk_checkpoint import (
    DISTILLATION_INPUT_CONTRACT_VERSION,
    CheckpointAdmission,
    CheckpointAdmissionRequest,
    build_chunk_fingerprint,
    build_chunk_info,
)
from core.hephaestus.distill_execution_spec import prepare_chunk_execution_spec
from core.hephaestus.distill_input_spec import (
    OUTPUT_CONTRACT_VERSION,
    DistillInputSpec,
    ExtractionRequest,
)
from core.hephaestus.distillation_contract import validate_checkpoint_extraction_output
from core.hephaestus.distillation_models import (
    ChunkExtractionResult,
    DistillationResult,
    ExtractionOutcome,
    KnowledgeFragment,
)
from core.hephaestus.distillation_prompts import PROMPT_VERSION
from core.hephaestus.distillation_text import build_session_text


class ChunkSourceSpanError(ValueError):
    """A formal chunk cannot prove the raw bytes it consumed."""


def build_chunk_source_span_map(
    chunk: List[Dict[str, Any]],
    message_positions: Mapping[int, int],
) -> tuple[Dict[str, Any], ...]:
    """Return the exact raw span for each message, or reject before extraction.

    A chunk reaches an LLM only when every visible message is bound to a
    non-empty immutable Raw revision span.  A session-wide fallback would make
    later pages falsely claim evidence from chunks they never consumed.
    """

    spans: list[Dict[str, Any]] = []
    for local_ordinal, message in enumerate(chunk):
        content = str(message.get("content") or "")
        source_span = message.get("source_span")
        message_turn = message.get("turn")
        if message_turn is None:
            message_turn = message.get("turn_number")
        if message_turn is None:
            message_turn = message_positions.get(id(message), local_ordinal + 1)
        base: Dict[str, Any] = {
            "turn": message_turn,
            "message_ordinal": message_positions.get(id(message), local_ordinal + 1),
            "part": str(message.get("part") or "1/1"),
            "role": str(message.get("role") or "unknown"),
        }
        if not isinstance(source_span, Mapping):
            raise ChunkSourceSpanError(
                f"chunk_source_span_missing: message_ordinal={base['message_ordinal']}"
            )
        try:
            revision_id = str(source_span["revision_id"] or "")
            span_start = int(source_span["span_start"])
            span_end = int(source_span["span_end"])
            source_turn = source_span.get("turn_number")
            turn_number = int(base["turn"] if source_turn is None else source_turn)
        except (KeyError, TypeError, ValueError) as exc:
            raise ChunkSourceSpanError(
                f"chunk_source_span_invalid: message_ordinal={base['message_ordinal']}"
            ) from exc
        content_hash = str(source_span.get("content_hash") or "")
        source_role = str(source_span.get("role") or base["role"])
        if (
            not revision_id
            or not content_hash
            or not content
            or span_start < 0
            or span_end <= span_start
            or span_end - span_start != len(content)
            or source_role != base["role"]
            or turn_number != int(base["turn"])
        ):
            raise ChunkSourceSpanError(
                f"chunk_source_span_invalid: message_ordinal={base['message_ordinal']}"
            )
        base.update(
            {
                "revision_id": revision_id,
                "logical_event_id": str(source_span.get("logical_event_id") or ""),
                "turn_number": turn_number,
                "content_hash": content_hash,
                "span_start": span_start,
                "span_end": span_end,
                "span_status": "exact",
            }
        )
        spans.append(base)
    return tuple(spans)


def _ordered_revision_ids(
    source_span_maps: tuple[tuple[Dict[str, Any], ...], ...],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(span["revision_id"])
            for source_span_map in source_span_maps
            for span in source_span_map
        )
    )


def _validate_source_span_catalog(
    result: DistillationResult,
    source_span_maps: tuple[tuple[Dict[str, Any], ...], ...],
) -> None:
    """Bind role-local spans to the immutable full-revision catalog when present."""

    hashes_by_revision: dict[str, str] = {}
    for source_span_map in source_span_maps:
        for span in source_span_map:
            revision_id = str(span["revision_id"])
            content_hash = str(span["content_hash"])
            previous = hashes_by_revision.setdefault(revision_id, content_hash)
            if previous != content_hash:
                raise ChunkSourceSpanError(
                    f"chunk_source_span_hash_conflict: revision_id={revision_id}"
                )
    if not result.raw_event_refs:
        return

    catalog: dict[str, Mapping[str, Any]] = {}
    for raw_ref in result.raw_event_refs:
        if not isinstance(raw_ref, Mapping):
            raise ChunkSourceSpanError("chunk_source_catalog_invalid")
        revision_id = str(raw_ref.get("revision_id") or "")
        if not revision_id or revision_id in catalog:
            raise ChunkSourceSpanError("chunk_source_catalog_invalid")
        catalog[revision_id] = raw_ref
    for source_span_map in source_span_maps:
        for span in source_span_map:
            revision_id = str(span["revision_id"])
            catalog_entry = catalog.get(revision_id)
            if catalog_entry is None:
                raise ChunkSourceSpanError(
                    f"chunk_source_revision_not_in_catalog: revision_id={revision_id}"
                )
            catalog_hash = str(catalog_entry.get("content_hash") or "")
            catalog_logical_id = str(catalog_entry.get("logical_event_id") or "")
            try:
                catalog_start = int(catalog_entry["span_start"])
                catalog_end = int(catalog_entry["span_end"])
                catalog_turn = int(catalog_entry.get("turn_number", span["turn_number"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ChunkSourceSpanError("chunk_source_catalog_invalid") from exc
            if (
                not catalog_hash
                or catalog_hash != span["content_hash"]
                or (
                    catalog_logical_id
                    and catalog_logical_id != span["logical_event_id"]
                )
                or catalog_turn != int(span["turn_number"])
                or catalog_start < 0
                or int(span["span_start"]) < catalog_start
                or int(span["span_end"]) > catalog_end
            ):
                raise ChunkSourceSpanError(
                    f"chunk_source_span_catalog_mismatch: revision_id={revision_id}"
                )


def _input_spec_from_spans(
    result: DistillationResult,
    *,
    visible_input: str,
    input_mode: str,
    source_span_maps: tuple[tuple[Dict[str, Any], ...], ...],
    source_messages: List[Dict[str, Any]] | None = None,
) -> DistillInputSpec:
    revision_ids = _ordered_revision_ids(source_span_maps)
    allowed = set(revision_ids)
    artifact_refs = []
    for ref in result.artifact_refs:
        if not isinstance(ref, Mapping):
            artifact_refs.append(ref)
            continue
        supplied = ref.get("source_event_ids")
        if isinstance(supplied, (list, tuple)):
            source_ids = {str(value) for value in supplied if str(value)}
        else:
            source_id = str(ref.get("source_event_id") or "")
            source_ids = {source_id} if source_id else set()
        if not source_ids or source_ids.intersection(allowed):
            artifact_refs.append(ref)
    return DistillInputSpec.build(
        source_agent=result.source,
        source_session_id=result.session_id,
        source_event_ids=revision_ids,
        raw_completeness=result.raw_completeness,
        visible_input=visible_input,
        input_mode=input_mode,
        artifact_refs=artifact_refs,
        source_messages=source_messages,
        source_authority_context=result.source_authority_context,
    )


class ChunkedExtractionCoordinator:
    """One-method module hiding checkpoint/cold/hit aggregate orchestration."""

    def extract(
        self,
        engine: Any,
        result: DistillationResult,
        filtered: List[Dict],
        budget: Dict[str, Any],
    ) -> Tuple[Optional[List[KnowledgeFragment]], List[Dict[str, Any]]]:
        result.analysis_type = "chunked"
        cfg = budget["cfg"]
        incremental_batch_turns = cfg.get("distill.incremental_batch_turns")
        chunks = engine._chunk_messages(
            filtered,
            max_tokens_per_chunk=budget["chunk_size"],
            max_turns_per_chunk=incremental_batch_turns,
        )
        message_positions = {id(message): index + 1 for index, message in enumerate(filtered)}
        try:
            source_span_maps = tuple(
                build_chunk_source_span_map(chunk, message_positions) for chunk in chunks
            )
            _validate_source_span_catalog(result, source_span_maps)
        except ChunkSourceSpanError as exc:
            result.judgment = "error"
            result.judgment_reason = str(exc)
            result.error = "chunk_source_span_invalid"
            result.extraction_contract_valid = False
            return None, []

        checkpoint_store = engine._chunk_checkpoint_store(cfg)
        chunk_infos: List[Dict[str, Any]] = []
        chunk_results: List[ChunkExtractionResult] = []
        aggregate_identity = ChunkEpisodeMerger().checkpoint_identity()

        for index, chunk in enumerate(chunks):
            source_span_map = source_span_maps[index]
            chunk_meta: Dict[str, Any] = {}
            chunk_text = build_session_text(
                chunk, max_tokens=budget["chunk_size"], out_meta=chunk_meta, lossless=True
            )
            input_spec = _input_spec_from_spans(
                result,
                visible_input=chunk_text,
                input_mode="chunked",
                source_span_maps=(source_span_map,),
                source_messages=chunk,
            )
            request = ExtractionRequest(
                session_text=chunk_text,
                analysis_type="chunked",
                input_spec=input_spec,
            )
            try:
                prepared, execution_spec = prepare_chunk_execution_spec(
                    extractor=engine._extractor,
                    merge_component=engine._fragment_merger,
                    cfg=cfg,
                    request=request,
                    input_contract_version=DISTILLATION_INPUT_CONTRACT_VERSION,
                    prompt_version=PROMPT_VERSION,
                )
            except (ArtifactCatalogRejectedError, SourceAuthorityCatalogRejectedError) as exc:
                result.judgment = "error"
                authority_rejected = isinstance(exc, SourceAuthorityCatalogRejectedError)
                result.judgment_reason = (
                    "distillation source authority catalog rejected"
                    if authority_rejected
                    else "distillation artifact catalog rejected"
                )
                result.error = (
                    "source_authority_catalog_rejected"
                    if authority_rejected
                    else "artifact_catalog_rejected"
                )
                result.extraction_contract_valid = False
                return None, []
            except (AttributeError, TypeError, ValueError):
                result.judgment = "error"
                result.judgment_reason = "distillation extractor protocol violation"
                result.error = "extractor_protocol_violation"
                result.extraction_contract_valid = False
                return None, []
            chunk_hash = build_chunk_fingerprint(
                chunk,
                index,
                budget["chunk_size"],
                incremental_batch_turns,
                execution_spec.execution_spec_hash,
            )
            checkpoint_miss_reason = "checkpoint_disabled"
            checkpoint_spec_diff_fields: tuple[str, ...] = ()
            if checkpoint_store is not None:
                lookup = checkpoint_store.lookup_completed(
                    result.session_id,
                    index,
                    chunk_hash,
                    execution_spec,
                    CheckpointAdmissionRequest.for_input_spec(input_spec),
                )
                if lookup.cache_hit and lookup.chunk_info is not None:
                    cached_outcome = self._cached_outcome(lookup, input_spec)
                    if engine._verify_extraction_outcome(cached_outcome, input_spec):
                        cached_info = dict(lookup.chunk_info)
                        cached_info.update(
                            {
                                "checkpoint_reused": True,
                                "cache_hit": True,
                                "miss_reason": "",
                                "spec_diff_fields": [],
                                "source_span_map": list(source_span_map),
                                "chunk_aggregate_contract_version": aggregate_identity[
                                    "contract_version"
                                ],
                                "chunk_aggregate_contract_hash": aggregate_identity["contract_hash"],
                            }
                        )
                        chunk_infos.append(cached_info)
                        chunk_results.append(
                            self._chunk_result(
                                chunk_index=index,
                                chunk_hash=chunk_hash,
                                input_spec=input_spec,
                                execution_spec_hash=execution_spec.execution_spec_hash,
                                outcome=cached_outcome,
                                source_span_map=source_span_map,
                                cache_hit=True,
                            )
                        )
                        continue
                    checkpoint_miss_reason = "checkpoint_output_contract_invalid"
                else:
                    checkpoint_miss_reason = lookup.miss_reason
                    checkpoint_spec_diff_fields = lookup.spec_diff_fields

            outcome = engine._safe_extract(request, result, prepared=prepared)
            if outcome is None:
                if checkpoint_store is not None:
                    checkpoint_store.mark_failed(
                        result.session_id,
                        index,
                        chunk_hash,
                        execution_spec,
                        result.judgment_reason or result.error or "chunk extraction failed",
                    )
                return None, []
            chunk_fragments = list(outcome.fragments)
            admission = CheckpointAdmission(
                input_spec_hash=input_spec.input_spec_hash,
                output_contract_version=OUTPUT_CONTRACT_VERSION,
                canonical_output_hash=outcome.canonical_output_hash,
                judgment=outcome.judgment,
            )
            chunk_info = build_chunk_info(
                index,
                chunk,
                chunk_meta,
                chunk_fragments,
                message_positions,
                execution_spec,
                checkpoint_miss_reason,
                checkpoint_spec_diff_fields,
                admission,
            )
            chunk_info.update(
                {
                    "source_span_map": list(source_span_map),
                    "chunk_aggregate_contract_version": aggregate_identity["contract_version"],
                    "chunk_aggregate_contract_hash": aggregate_identity["contract_hash"],
                }
            )
            if checkpoint_store is not None:
                checkpoint_store.save_completed(
                    result.session_id,
                    index,
                    chunk_hash,
                    execution_spec,
                    chunk_fragments,
                    chunk_info,
                    dict(outcome.structured_output or {}),
                    admission,
                    canonical_output=outcome.canonical_output,
                    input_spec=input_spec,
                )
            chunk_infos.append(chunk_info)
            chunk_results.append(
                self._chunk_result(
                    chunk_index=index,
                    chunk_hash=chunk_hash,
                    input_spec=input_spec,
                    execution_spec_hash=execution_spec.execution_spec_hash,
                    outcome=outcome,
                    source_span_map=source_span_map,
                    cache_hit=False,
                )
            )

        session_text = build_session_text(
            filtered,
            max_tokens=budget["chunk_size"],
            lossless=True,
        )
        session_input_spec = _input_spec_from_spans(
            result,
            visible_input=session_text,
            input_mode="chunked_aggregate_v1",
            source_span_maps=source_span_maps,
            source_messages=filtered,
        )
        try:
            aggregate = ChunkEpisodeMerger().aggregate(
                session_input_spec=session_input_spec,
                chunks=chunk_results,
            )
        except ChunkAggregateError as exc:
            result.judgment = "error"
            result.judgment_reason = str(exc)
            result.error = str(exc).split(":", 1)[0]
            result.extraction_contract_valid = False
            return None, []

        result.chunk_extraction_results = list(aggregate.ordered_chunks)
        result.chunk_aggregate = aggregate
        result.input_spec = aggregate.session_input_spec
        result.extraction_judgment = str(aggregate.aggregate_root.get("judgment") or "")
        result.extraction_contract_valid = True
        result.structured_output = dict(aggregate.aggregate_root.get("structured_output") or {})
        result.extraction_output = dict(aggregate.aggregate_root)
        result.extraction_output_hash = aggregate.aggregate_root_hash
        fragments = list(aggregate.merged_fragments)
        result.session_coverage = (
            f"分块蒸馏（共 {len(chunks)} 个 chunk，提取 {len(fragments)} 个片段）"
        )
        if chunk_infos:
            result.session_coverage += "；" + "; ".join(
                f"chunk{info['chunk_index']}: turn{info['covered_turn_range']}"
                for info in chunk_infos
            )
        result.distill_input_mode = "chunked"
        result.truncated = any(
            info.get("truncated", False)
            and (
                info.get("omitted_turns", 0) > 0
                or any(message.get("truncated") for message in info.get("message_truncations", []))
            )
            for info in chunk_infos
        )
        return fragments, chunk_infos

    @staticmethod
    def _cached_outcome(lookup: Any, input_spec: Any) -> ExtractionOutcome:
        admission = lookup.admission
        cached_validation = validate_checkpoint_extraction_output(
            canonical_output=lookup.canonical_output,
            input_spec=input_spec,
        )
        return ExtractionOutcome(
            judgment=admission.judgment if admission else "",
            fragments=lookup.fragments,
            structured_output=lookup.structured_output,
            canonical_output=dict(lookup.canonical_output or {}),
            admission=cached_validation,
            canonical_output_hash=admission.canonical_output_hash if admission else "",
        )

    @staticmethod
    def _chunk_result(
        *,
        chunk_index: int,
        chunk_hash: str,
        input_spec: Any,
        execution_spec_hash: str,
        outcome: ExtractionOutcome,
        source_span_map: tuple[Dict[str, Any], ...],
        cache_hit: bool,
    ) -> ChunkExtractionResult:
        return ChunkExtractionResult(
            chunk_index=chunk_index,
            chunk_hash=chunk_hash,
            input_spec=input_spec,
            execution_spec_hash=execution_spec_hash,
            canonical_output=dict(outcome.canonical_output),
            canonical_output_hash=outcome.canonical_output_hash,
            fragments=tuple(outcome.fragments),
            episode_fragment=dict(outcome.structured_output or {}),
            source_span_map=source_span_map,
            contract_verdict="admitted",
            cache_hit=cache_hit,
        )
