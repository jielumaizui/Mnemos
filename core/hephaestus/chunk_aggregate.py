"""Deterministic, lossless aggregation for admitted chunk extraction roots.

Chunk checkpoints prove only a local visible-input slice.  This module is the
single owner of turning an ordered bundle of those proofs into a new,
session-level v4 root.  It intentionally does not persist a second checkpoint:
the aggregate is a pure function of admitted chunk results, so cold runs,
cache hits and restarts all take exactly the same path.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

from core.cognition_episode_contract import (
    COGNITION_EPISODE_FIELDS,
    iter_cognition_episode_evidence,
)
from core.evidence.artifact_catalog import (
    model_artifact_projection,
    resolve_model_artifact_selections,
)
from core.evidence.source_authority import (
    model_source_authority_projection,
    resolve_model_source_authority_selections,
)
from core.hephaestus.distill_input_spec import DistillInputSpec
from core.hephaestus.distill_output_version import DISTILL_OUTPUT_CONTRACT_VERSION
from core.hephaestus.distillation_contract import (
    canonical_extraction_output_hash,
    canonicalize_extraction_output,
    validate_checkpoint_extraction_output,
    validate_extraction_output,
)
from core.hephaestus.distillation_models import (
    ChunkExtractionResult,
    KnowledgeFragment,
    SessionChunkAggregate,
)


CHUNK_AGGREGATE_CONTRACT_VERSION = "mnemos.chunk_aggregate.v2"
CHUNK_AGGREGATE_CONTRACT_DESCRIPTOR = {
    "input": "ordered-admitted-chunk-roots-with-exact-spans",
    "claims": "semantic-stable-id-conservative-confidence",
    "events": "preserve-every-occurrence-with-temporal-origin",
    "relations": "stable-id-with-conflict-manifest",
    "fragments": "rebuild-from-canonical-root-lossless-block-order",
}


class ChunkAggregateError(ValueError):
    """A verified chunk bundle cannot safely form one session root."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}-{_sha256(_canonical_json(value)).removeprefix('sha256:')[:24]}"


def _stable_union(values: Iterable[Any]) -> list[Any]:
    """Keep first-seen temporal order while removing exact stable duplicates."""

    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = _canonical_json(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(deepcopy(value))
    return result


def _with_aggregate_origin(value: Any, **origin: int) -> dict[str, Any]:
    """Preserve one temporal occurrence instead of collapsing equal events."""

    item = _as_mapping(value, name="temporal_event")
    item["aggregate_origin"] = dict(origin)
    return item


def _as_mapping(value: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ChunkAggregateError(f"chunk_aggregate_invalid_{name}")
    return {str(key): deepcopy(child) for key, child in value.items()}


def aggregate_contract_hash() -> str:
    """Hash the versioned semantic descriptor without runtime source-file I/O."""

    return _sha256(
        _canonical_json(
            {
                "version": CHUNK_AGGREGATE_CONTRACT_VERSION,
                "semantics": CHUNK_AGGREGATE_CONTRACT_DESCRIPTOR,
            }
        )
    )


def _fragment_from_root(data: Mapping[str, Any]) -> KnowledgeFragment:
    """Restore only fields bound by the admitted canonical fragment root."""

    return KnowledgeFragment(
        form=str(data.get("form") or "未知"),
        title=str(data.get("title") or "无标题"),
        frontmatter=deepcopy(data.get("frontmatter") or {}),
        background=str(data.get("background") or ""),
        core_content=str(data.get("core_content") or ""),
        boundaries=deepcopy(data.get("boundaries") or {}),
        anti_patterns=deepcopy(data.get("anti_patterns") or []),
        related_concepts=deepcopy(data.get("related_concepts") or []),
        claim_ids=deepcopy(data.get("claim_ids") or []),
        relations=deepcopy(data.get("relations") or []),
    )


class ChunkEpisodeMerger:
    """Build one session-level root from ordered, independently admitted chunks."""

    contract_version = CHUNK_AGGREGATE_CONTRACT_VERSION

    def checkpoint_identity(self) -> dict[str, str]:
        """Expose aggregate semantics to the existing chunk execution spec."""

        return {
            "contract_version": self.contract_version,
            "contract_hash": aggregate_contract_hash(),
        }

    def aggregate(
        self,
        *,
        session_input_spec: DistillInputSpec,
        chunks: Sequence[ChunkExtractionResult],
    ) -> SessionChunkAggregate:
        ordered = tuple(sorted(chunks, key=lambda item: item.chunk_index))
        self._validate_chunk_bundle(ordered)

        non_skips = tuple(item for item in ordered if self._judgment(item) != "skip")
        if non_skips:
            root, fragments = self._aggregate_non_skip_root(
                session_input_spec,
                ordered,
                non_skips,
            )
        else:
            root = self._aggregate_skip_root(session_input_spec, ordered)
            fragments = []

        root = self._rebind_session_evidence(root, session_input_spec)
        validation = validate_extraction_output(root, session_input_spec)
        if not validation.valid:
            raise ChunkAggregateError(
                "chunk_aggregate_root_contract_rejected: " + validation.error_text
            )
        root_hash = canonical_extraction_output_hash(canonical_output=root)
        episode = _as_mapping(root["structured_output"].get("chunk_aggregation"), name="episode")
        return SessionChunkAggregate(
            session_input_spec=session_input_spec,
            ordered_chunks=ordered,
            aggregate_root=deepcopy(root),
            aggregate_root_hash=root_hash,
            merged_fragments=tuple(fragments),
            episode=episode,
            aggregate_contract_version=self.contract_version,
            aggregate_contract_hash=aggregate_contract_hash(),
        )

    @staticmethod
    def _judgment(chunk: ChunkExtractionResult) -> str:
        return str(chunk.canonical_output.get("judgment") or "")

    def _validate_chunk_bundle(self, chunks: tuple[ChunkExtractionResult, ...]) -> None:
        if not chunks:
            raise ChunkAggregateError("chunk_aggregate_empty_bundle")
        expected_indexes = tuple(range(len(chunks)))
        actual_indexes = tuple(item.chunk_index for item in chunks)
        if actual_indexes != expected_indexes:
            raise ChunkAggregateError("chunk_aggregate_non_contiguous_chunk_indexes")
        for chunk in chunks:
            if chunk.contract_verdict != "admitted":
                raise ChunkAggregateError("chunk_aggregate_unadmitted_chunk")
            if not chunk.execution_spec_hash or not chunk.chunk_hash:
                raise ChunkAggregateError("chunk_aggregate_missing_chunk_identity")
            self._validate_chunk_source_spans(chunk)
            validation = validate_checkpoint_extraction_output(
                canonical_output=chunk.canonical_output,
                input_spec=chunk.input_spec,
            )
            if not validation.valid:
                raise ChunkAggregateError(
                    "chunk_aggregate_invalid_chunk_root: " + validation.error_text
                )
            if chunk.canonical_output_hash != canonical_extraction_output_hash(
                canonical_output=chunk.canonical_output,
            ):
                raise ChunkAggregateError("chunk_aggregate_chunk_root_hash_mismatch")
            structured = chunk.canonical_output.get("structured_output")
            if structured != chunk.episode_fragment:
                raise ChunkAggregateError("chunk_aggregate_episode_fragment_mismatch")

    @staticmethod
    def _validate_chunk_source_spans(chunk: ChunkExtractionResult) -> None:
        """Require a durable exact Raw span for every visible chunk message."""

        if not chunk.source_span_map:
            raise ChunkAggregateError("chunk_aggregate_missing_source_span")
        for span in chunk.source_span_map:
            if not isinstance(span, Mapping):
                raise ChunkAggregateError("chunk_aggregate_invalid_source_span")
            try:
                revision_id = str(span["revision_id"] or "")
                span_start = int(span["span_start"])
                span_end = int(span["span_end"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ChunkAggregateError("chunk_aggregate_invalid_source_span") from exc
            if (
                span.get("span_status") != "exact"
                or not revision_id
                or span_start < 0
                or span_end <= span_start
            ):
                raise ChunkAggregateError("chunk_aggregate_invalid_source_span")

    def _ordered_fragments(
        self,
        chunks: Sequence[ChunkExtractionResult],
        claim_id_map: Mapping[tuple[int, str], str],
    ) -> list[KnowledgeFragment]:
        fragments: list[KnowledgeFragment] = []
        for chunk in chunks:
            root_fragments = chunk.canonical_output.get("fragments")
            if not isinstance(root_fragments, list):
                raise ChunkAggregateError("chunk_aggregate_invalid_root_fragments")
            for raw_fragment in root_fragments:
                if not isinstance(raw_fragment, Mapping):
                    raise ChunkAggregateError("chunk_aggregate_invalid_root_fragment")
                # Rebuild from the immutable admitted root.  The working
                # fragment instances pass through later formatting and
                # quality layers, so reusing them here would make a write-time
                # recomputation depend on permitted presentation mutations.
                fragment = _fragment_from_root(raw_fragment)
                mapped_claim_ids: list[str] = []
                for local_claim_id in fragment.claim_ids:
                    mapped = claim_id_map.get((chunk.chunk_index, local_claim_id))
                    if not mapped:
                        raise ChunkAggregateError(
                            "chunk_aggregate_fragment_claim_mapping_missing"
                        )
                    if mapped not in mapped_claim_ids:
                        mapped_claim_ids.append(mapped)
                fragment.claim_ids = mapped_claim_ids
                # This metadata binds the later readable Wiki projection to
                # the precise chunk spans rather than silently claiming that
                # every page used every raw event in the session.
                fragment.frontmatter = dict(fragment.frontmatter or {})
                fragment.frontmatter["chunk_source_spans"] = [
                    deepcopy(span) for span in chunk.source_span_map
                ]
                precise_refs = [
                    {
                        key: deepcopy(value)
                        for key, value in span.items()
                        if key
                        in {
                            "revision_id",
                            "logical_event_id",
                            "turn_number",
                            "content_hash",
                            "span_start",
                            "span_end",
                            "role",
                        }
                    }
                    for span in chunk.source_span_map
                    if span.get("revision_id")
                    and isinstance(span.get("span_start"), int)
                    and isinstance(span.get("span_end"), int)
                    and int(span["span_end"]) > int(span["span_start"])
                ]
                if precise_refs:
                    fragment.frontmatter["raw_event_refs"] = _stable_union(precise_refs)
                fragments.append(fragment)
        return fragments

    def _aggregate_non_skip_root(
        self,
        session_input_spec: DistillInputSpec,
        ordered: tuple[ChunkExtractionResult, ...],
        non_skips: tuple[ChunkExtractionResult, ...],
    ) -> tuple[dict[str, Any], list[KnowledgeFragment]]:
        judgments = tuple(dict.fromkeys(self._judgment(chunk) for chunk in non_skips))
        if len(judgments) != 1:
            # ``skill`` is still a different typed terminal under COG-013.
            # Coercing it into knowledge here would silently lose the asset.
            raise ChunkAggregateError("chunk_aggregate_mixed_judgment")
        payloads = [
            _as_mapping(chunk.episode_fragment, name="episode_fragment")
            for chunk in non_skips
        ]
        claims, claim_manifest, claim_id_map = self._aggregate_claims(non_skips, payloads)
        fragments = self._ordered_fragments(non_skips, claim_id_map)
        if not fragments:
            raise ChunkAggregateError("chunk_aggregate_non_skip_without_fragments")
        behavior, competing_hypotheses = self._aggregate_behavior(non_skips, payloads)
        input_ids = _stable_union(
            event_id
            for chunk in ordered
            for event_id in chunk.input_spec.source_event_ids
        )
        output_ids = list(session_input_spec.source_event_ids)
        if input_ids != output_ids:
            raise ChunkAggregateError("chunk_aggregate_input_ids_output_ids_not_conserved")

        intents = [str(payload.get("distill_intent") or "") for payload in payloads]
        selected_intent = intents[0]
        candidate_summary = self._join_labeled(
            (
                chunk.chunk_index,
                str(payload.get("candidate_summary") or ""),
            )
            for chunk, payload in zip(non_skips, payloads)
        )
        episode = self._episode_manifest(
            ordered=ordered,
            input_ids=input_ids,
            output_ids=output_ids,
            claim_manifest=claim_manifest,
            competing_hypotheses=competing_hypotheses,
            competing_intents=_stable_union(intents),
        )
        structured: dict[str, Any] = {
            "schema_version": DISTILL_OUTPUT_CONTRACT_VERSION,
            **session_input_spec.prompt_contract(),
            "distill_intent": selected_intent,
            "candidate_summary": candidate_summary,
            "user_behavior_intent": behavior,
            "claims": claims,
            "chunk_aggregation": episode,
        }
        structured.update(
            self._aggregate_episode_collections(
                non_skips,
                payloads,
                claim_id_map,
            )
        )
        # A skill asset is preserved only when every admitted non-skip chunk is
        # skill.  More sophisticated skill persistence remains COG-013 work.
        if judgments[0] == "skill":
            assets = [
                deepcopy(chunk.canonical_output.get("cognitive_decision_asset"))
                for chunk in non_skips
                if isinstance(chunk.canonical_output.get("cognitive_decision_asset"), Mapping)
            ]
            if assets:
                episode["cognitive_decision_assets"] = _stable_union(assets)
        root_payload: dict[str, Any] = {
            "judgment": judgments[0],
            "judgment_reason": (
                f"Session aggregate preserved {len(non_skips)} admitted non-skip chunks "
                f"and {len(ordered) - len(non_skips)} local legal skips."
            ),
            "structured_output": structured,
        }
        return canonicalize_extraction_output(root_payload, fragments), fragments

    def _aggregate_skip_root(
        self,
        session_input_spec: DistillInputSpec,
        ordered: tuple[ChunkExtractionResult, ...],
    ) -> dict[str, Any]:
        payloads = [
            _as_mapping(chunk.episode_fragment, name="episode_fragment") for chunk in ordered
        ]
        input_ids = _stable_union(
            event_id
            for chunk in ordered
            for event_id in chunk.input_spec.source_event_ids
        )
        output_ids = list(session_input_spec.source_event_ids)
        if input_ids != output_ids:
            raise ChunkAggregateError("chunk_aggregate_input_ids_output_ids_not_conserved")
        evidence = [
            _with_aggregate_origin(
                item,
                chunk_index=chunk.chunk_index,
                local_ordinal=ordinal,
            )
            for chunk, payload in zip(ordered, payloads)
            for ordinal, item in enumerate(payload.get("no_value_evidence") or [])
        ]
        if not evidence:
            raise ChunkAggregateError("chunk_aggregate_skip_without_evidence")
        episode = self._episode_manifest(
            ordered=ordered,
            input_ids=input_ids,
            output_ids=output_ids,
            claim_manifest={"input_count": 0, "output_count": 0, "lost_claims": 0, "duplicate_claims": 0},
            competing_hypotheses=[],
            competing_intents=["skip"],
        )
        structured: dict[str, Any] = {
            "schema_version": DISTILL_OUTPUT_CONTRACT_VERSION,
            **session_input_spec.prompt_contract(),
            "distill_intent": "skip",
            "candidate_summary": self._join_labeled(
                (
                    chunk.chunk_index,
                    str(payload.get("candidate_summary") or ""),
                )
                for chunk, payload in zip(ordered, payloads)
            ),
            "skip_reason": self._join_labeled(
                (
                    chunk.chunk_index,
                    str(payload.get("skip_reason") or ""),
                )
                for chunk, payload in zip(ordered, payloads)
            ),
            "no_value_evidence": evidence,
            "claims": [],
            "chunk_aggregation": episode,
        }
        root_payload = {
            "judgment": "skip",
            "judgment_reason": "All admitted chunks are legal local skips.",
            "structured_output": structured,
        }
        return canonicalize_extraction_output(root_payload, ())

    @staticmethod
    def _join_labeled(parts: Iterable[tuple[int, str]]) -> str:
        rendered = [f"[chunk {index}] {text}" for index, text in parts if text]
        return "\n\n".join(rendered) or "No additional summary was supplied by admitted chunks."

    def _aggregate_claims(
        self,
        chunks: Sequence[ChunkExtractionResult],
        payloads: Sequence[Mapping[str, Any]],
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, Any],
        dict[tuple[int, str], str],
    ]:
        groups: dict[str, dict[str, Any]] = {}
        original_to_output: dict[str, list[str]] = {}
        input_count = 0
        for chunk, payload in zip(chunks, payloads):
            raw_claims = payload.get("claims")
            if not isinstance(raw_claims, list):
                raise ChunkAggregateError("chunk_aggregate_invalid_claims")
            for ordinal, raw_claim in enumerate(raw_claims):
                claim = _as_mapping(raw_claim, name="claim")
                original_id = str(claim.get("claim_id") or "")
                if not original_id:
                    raise ChunkAggregateError("chunk_aggregate_missing_claim_id")
                input_count += 1
                semantic = {
                    key: deepcopy(value)
                    for key, value in claim.items()
                    if key
                    not in {
                        "claim_id",
                        "evidence",
                        "confidence",
                        "cognitive_actions",
                        "aggregate_origin",
                        "aggregate_original_claim_ids",
                    }
                }
                semantic_key = _canonical_json(semantic)
                output_id = _stable_id("claim", semantic)
                original_to_output.setdefault(original_id, [])
                if output_id not in original_to_output[original_id]:
                    original_to_output[original_id].append(output_id)
                group = groups.get(semantic_key)
                if group is None:
                    group = {
                        "claim": claim,
                        "output_id": output_id,
                        "evidence": [],
                        "actions": [],
                        "confidences": [],
                        "origins": [],
                        "original_ids": [],
                    }
                    groups[semantic_key] = group
                group["evidence"].extend(
                    _with_aggregate_origin(
                        evidence,
                        chunk_index=chunk.chunk_index,
                        claim_ordinal=ordinal,
                        local_ordinal=evidence_ordinal,
                    )
                    for evidence_ordinal, evidence in enumerate(claim.get("evidence") or [])
                )
                group["actions"].extend(claim.get("cognitive_actions") or [])
                confidence = claim.get("confidence")
                if isinstance(confidence, (int, float)):
                    group["confidences"].append(float(confidence))
                group["origins"].append(
                    {
                        "chunk_index": chunk.chunk_index,
                        "local_ordinal": ordinal,
                        "original_claim_id": original_id,
                    }
                )
                if original_id not in group["original_ids"]:
                    group["original_ids"].append(original_id)

        claims: list[dict[str, Any]] = []
        claim_id_map: dict[tuple[int, str], str] = {}
        for group in groups.values():
            merged = deepcopy(group["claim"])
            merged["claim_id"] = group["output_id"]
            merged["evidence"] = deepcopy(group["evidence"])
            if group["actions"]:
                merged["cognitive_actions"] = _stable_union(group["actions"])
            # Evidence/claim confidence is not an independently observed fact
            # after aggregation.  A conservative minimum is reproducible and
            # never manufactures confidence by arithmetic averaging.
            if group["confidences"]:
                merged["confidence"] = min(group["confidences"])
            merged["aggregate_origin"] = deepcopy(group["origins"])
            merged["aggregate_original_claim_ids"] = list(group["original_ids"])
            claims.append(merged)
            for origin in group["origins"]:
                claim_id_map[
                    (int(origin["chunk_index"]), str(origin["original_claim_id"]))
                ] = str(group["output_id"])

        output_ids = [str(item["claim_id"]) for item in claims]
        if len(output_ids) != len(set(output_ids)):
            raise ChunkAggregateError("chunk_aggregate_duplicate_output_claim_id")
        collisions = [
            {"original_claim_id": original, "aggregate_claim_ids": derived}
            for original, derived in original_to_output.items()
            if len(derived) > 1
        ]
        manifest = {
            "input_count": input_count,
            "output_count": len(claims),
            "lost_claims": 0,
            "duplicate_claims": 0,
            "claim_id_collisions": collisions,
        }
        return claims, manifest, claim_id_map

    def _aggregate_behavior(
        self,
        chunks: Sequence[ChunkExtractionResult],
        payloads: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        behaviors = [
            _as_mapping(payload.get("user_behavior_intent"), name="user_behavior_intent")
            for payload in payloads
        ]
        if not behaviors:
            raise ChunkAggregateError("chunk_aggregate_missing_user_behavior_intent")
        # Evidence naturally differs between chunks, so it cannot by itself
        # make two otherwise identical hypotheses compete.  Preserve complete
        # records below, but decide conflict on the actual hypothesis fields.
        hypothesis_keys = _stable_union(
            {
                "content_source": behavior.get("content_source"),
                "user_intent_signal": behavior.get("user_intent_signal"),
                "intent_hypothesis": behavior.get("intent_hypothesis"),
                "intent_status": behavior.get("intent_status"),
            }
            for behavior in behaviors
        )
        hypotheses = _stable_union(behaviors)
        evidence = [
            _with_aggregate_origin(
                item,
                chunk_index=chunk.chunk_index,
                local_ordinal=ordinal,
            )
            for chunk, behavior in zip(chunks, behaviors)
            for ordinal, item in enumerate(behavior.get("intent_evidence") or [])
        ]
        verification_events = [
            _with_aggregate_origin(
                item,
                chunk_index=chunk.chunk_index,
                local_ordinal=ordinal,
            )
            for chunk, behavior in zip(chunks, behaviors)
            for ordinal, item in enumerate(
                behavior.get("intent_verification_events") or []
            )
        ]
        if len(hypothesis_keys) > 1:
            # Do not select one temporal chunk and quietly erase a competing
            # hypothesis.  The root stays valid and intentionally unknown;
            # the complete competing set remains in the aggregate manifest.
            return (
                {
                    "content_source": "unknown",
                    "user_intent_signal": "unknown",
                    "intent_hypothesis": "unknown",
                    "intent_evidence": evidence,
                    "intent_verification_events": verification_events,
                    "intent_confidence": min(
                        0.3,
                        *[
                            float(item.get("intent_confidence", 0.0))
                            for item in behaviors
                            if isinstance(item.get("intent_confidence"), (int, float))
                        ],
                    ),
                    "intent_status": "unknown",
                    "behavior_summary": (
                        "Admitted chunks contain competing user-intent hypotheses; "
                        "the aggregation manifest preserves each hypothesis."
                    ),
                },
                hypotheses,
            )
        primary = deepcopy(behaviors[0])
        primary["intent_evidence"] = evidence
        primary["intent_verification_events"] = verification_events
        confidences: list[float] = []
        for item in behaviors:
            confidence = item.get("intent_confidence")
            if isinstance(confidence, (int, float)):
                confidences.append(float(confidence))
        if confidences:
            primary["intent_confidence"] = min(confidences)
        return primary, []

    @staticmethod
    def _aggregate_episode_collections(
        chunks: Sequence[ChunkExtractionResult],
        payloads: Sequence[Mapping[str, Any]],
        claim_id_map: Mapping[tuple[int, str], str],
    ) -> dict[str, dict[str, list[dict[str, Any]]]]:
        """Conserve evidence-distinct entries and remap local claim IDs.

        Independent chunks frequently emit the exact same non-assertive
        ``unknown``/``not_applicable`` entry.  Those entries describe one
        absence of session evidence, not multiple temporal occurrences, so
        the session contract keeps the first canonical copy.  Known entries
        with different evidence references remain distinct.
        """

        aggregate_episode: dict[str, list[dict[str, Any]]] = {}
        for field_name in COGNITION_EPISODE_FIELDS:
            occurrences: list[dict[str, Any]] = []
            for chunk, payload in zip(chunks, payloads):
                episode = payload.get("cognition_episode")
                if not isinstance(episode, Mapping):
                    raise ChunkAggregateError("chunk_aggregate_missing_cognition_episode")
                entries = episode.get(field_name)
                if not isinstance(entries, list) or not entries:
                    raise ChunkAggregateError(
                        f"chunk_aggregate_missing_cognition_episode_{field_name}"
                    )
                for raw_entry in entries:
                    entry = _as_mapping(raw_entry, name="cognition_episode_entry")
                    raw_claim_ids = entry.get("claim_ids")
                    if not isinstance(raw_claim_ids, list):
                        raise ChunkAggregateError(
                            "chunk_aggregate_invalid_cognition_episode_claim_ids"
                        )
                    mapped_claim_ids: list[str] = []
                    for local_claim_id in raw_claim_ids:
                        mapped = claim_id_map.get(
                            (chunk.chunk_index, str(local_claim_id or ""))
                        )
                        if not mapped:
                            raise ChunkAggregateError(
                                "chunk_aggregate_cognition_episode_claim_mapping_missing"
                            )
                        if mapped not in mapped_claim_ids:
                            mapped_claim_ids.append(mapped)
                    entry["claim_ids"] = mapped_claim_ids
                    occurrences.append(entry)
            aggregate_episode[field_name] = _stable_union(occurrences)
        return {"cognition_episode": aggregate_episode}

    @staticmethod
    def _rebind_session_evidence(
        root: Mapping[str, Any],
        session_input_spec: DistillInputSpec,
    ) -> dict[str, Any]:
        """Replace chunk-local authority identities with session-catalog refs."""

        projected = model_source_authority_projection(model_artifact_projection(root))
        structured = projected.get("structured_output")
        if not isinstance(structured, dict):
            raise ChunkAggregateError("chunk_aggregate_missing_structured_output")
        behavior = structured.get("user_behavior_intent")
        if isinstance(behavior, dict):
            for field_name in ("intent_evidence", "intent_verification_events"):
                for item in behavior.get(field_name) or []:
                    if isinstance(item, dict):
                        item.pop("source_authority_id", None)
        for claim in structured.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            for evidence in claim.get("evidence") or []:
                if isinstance(evidence, dict):
                    evidence.pop("source_authority_id", None)
        for _, evidence in iter_cognition_episode_evidence(projected):
            evidence.pop("source_authority_id", None)

        artifact_resolution = resolve_model_artifact_selections(
            projected,
            session_input_spec.artifact_catalog,
        )
        if artifact_resolution.issues:
            raise ChunkAggregateError("chunk_aggregate_artifact_rebind_failed")
        authority_resolution = resolve_model_source_authority_selections(
            artifact_resolution.payload,
            session_input_spec.source_authority_catalog,
        )
        if authority_resolution.issues:
            raise ChunkAggregateError("chunk_aggregate_authority_rebind_failed")
        if not isinstance(authority_resolution.payload, dict):
            raise ChunkAggregateError("chunk_aggregate_rebind_payload_invalid")
        rebound = authority_resolution.payload
        rebound_structured = rebound.get("structured_output")
        rebound_episode = (
            rebound_structured.get("cognition_episode")
            if isinstance(rebound_structured, dict)
            else None
        )
        if (
            isinstance(rebound_structured, dict)
            and rebound_structured.get("distill_intent") == "skip"
        ):
            if rebound_episode is not None:
                raise ChunkAggregateError("chunk_aggregate_skip_has_cognition_episode")
            return rebound
        if not isinstance(rebound_episode, dict):
            raise ChunkAggregateError("chunk_aggregate_missing_cognition_episode")
        # Two chunk-local selections can resolve to the same session-level
        # authority identity.  Deduplicate only after that canonical rebind so
        # the final root cannot acquire duplicate entries during enrichment.
        for field_name in COGNITION_EPISODE_FIELDS:
            entries = rebound_episode.get(field_name)
            if not isinstance(entries, list) or not entries:
                raise ChunkAggregateError(
                    f"chunk_aggregate_missing_cognition_episode_{field_name}"
                )
            rebound_episode[field_name] = _stable_union(entries)
        return rebound

    def _episode_manifest(
        self,
        *,
        ordered: Sequence[ChunkExtractionResult],
        input_ids: list[Any],
        output_ids: list[Any],
        claim_manifest: Mapping[str, Any],
        competing_hypotheses: Sequence[Mapping[str, Any]],
        competing_intents: Sequence[Any],
    ) -> dict[str, Any]:
        relation_groups: dict[str, dict[str, Any]] = {}
        for chunk in ordered:
            for fragment_ordinal, fragment in enumerate(chunk.fragments):
                for relation in fragment.relations or []:
                    if not isinstance(relation, Mapping):
                        continue
                    relation_payload = _as_mapping(relation, name="fragment_relation")
                    key = _canonical_json(relation_payload)
                    group = relation_groups.get(key)
                    if group is None:
                        group = {
                            "relation_id": _stable_id("relation", relation_payload),
                            "relation": relation_payload,
                            "origins": [],
                        }
                        relation_groups[key] = group
                    group["origins"].append(
                        {
                            "chunk_index": chunk.chunk_index,
                            "fragment_ordinal": fragment_ordinal,
                        }
                    )
        relations = list(relation_groups.values())
        relation_by_target: dict[str, list[dict[str, Any]]] = {}
        for item in relations:
            relation_value = item.get("relation") if isinstance(item, Mapping) else None
            if not isinstance(relation_value, Mapping):
                continue
            target = str(relation_value.get("target") or "")
            relation_by_target.setdefault(target, []).append(dict(item))
        relation_conflicts = [
            {"target": target, "relations": items}
            for target, items in relation_by_target.items()
            if len({str(item["relation"].get("type") or "") for item in items}) > 1
        ]
        return {
            "contract_version": self.contract_version,
            "contract_hash": aggregate_contract_hash(),
            "chunk_root_hashes": [chunk.canonical_output_hash for chunk in ordered],
            "execution_spec_hashes": [chunk.execution_spec_hash for chunk in ordered],
            "input_ids": list(input_ids),
            "output_ids": list(output_ids),
            "input_ids_output_ids_conserved": list(input_ids) == list(output_ids),
            "chunks": [
                {
                    "chunk_index": chunk.chunk_index,
                    "chunk_hash": chunk.chunk_hash,
                    "input_spec_hash": chunk.input_spec.input_spec_hash,
                    "canonical_output_hash": chunk.canonical_output_hash,
                    "execution_spec_hash": chunk.execution_spec_hash,
                    "contract_verdict": chunk.contract_verdict,
                    # Reuse is an operational receipt, not cognition.  It
                    # must never enter the canonical aggregate root or make a
                    # cold run hash differ from a checkpoint hit/restart.
                    "checkpoint_admission": "verified",
                    "source_span_map": [deepcopy(span) for span in chunk.source_span_map],
                }
                for chunk in ordered
            ],
            # The final root keeps the ordered semantic contribution of every
            # chunk, including legal local skips.  A hash-only manifest would
            # make those goals/hypotheses/evidence unavailable after the
            # transient Engine object is gone.
            "chunk_episode_fragments": [
                {
                    "chunk_index": chunk.chunk_index,
                    "judgment": self._judgment(chunk),
                    "canonical_output_hash": chunk.canonical_output_hash,
                    "structured_output": deepcopy(chunk.episode_fragment),
                    "source_span_map": [deepcopy(span) for span in chunk.source_span_map],
                }
                for chunk in ordered
            ],
            "claim_conservation": dict(claim_manifest),
            "relations": relations,
            "relation_conflicts": relation_conflicts,
            "lost_relations": 0,
            "competing_hypotheses": [deepcopy(item) for item in competing_hypotheses],
            "competing_intents": list(competing_intents),
            "confidence_policy": "conservative_min_with_component_provenance",
            "time_order_policy": "ordered_chunk_then_local_ordinal",
        }


def validate_session_chunk_aggregate(
    result: Any,
) -> list[str]:
    """Recheck the complete chunk bundle before a formal router write."""

    chunks = getattr(result, "chunk_extraction_results", None) or []
    if not chunks:
        return []
    aggregate = getattr(result, "chunk_aggregate", None)
    if not isinstance(aggregate, SessionChunkAggregate):
        return ["chunked result is missing a typed session aggregate"]
    errors: list[str] = []
    ordered = tuple(chunks)
    if ordered != aggregate.ordered_chunks:
        errors.append("session aggregate chunk bundle differs from result bundle")
    merger = ChunkEpisodeMerger()
    try:
        merger._validate_chunk_bundle(aggregate.ordered_chunks)
    except ChunkAggregateError as exc:
        errors.append(str(exc))
    if aggregate.aggregate_contract_version != CHUNK_AGGREGATE_CONTRACT_VERSION:
        errors.append("session aggregate contract version is unsupported")
    if aggregate.aggregate_contract_hash != aggregate_contract_hash():
        errors.append("session aggregate contract hash mismatch")
    if aggregate.aggregate_root_hash != canonical_extraction_output_hash(
        canonical_output=aggregate.aggregate_root,
    ):
        errors.append("session aggregate root hash mismatch")
    validation = validate_extraction_output(
        aggregate.aggregate_root,
        aggregate.session_input_spec,
    )
    if not validation.valid:
        errors.append("session aggregate root contract rejected: " + validation.error_text)
    try:
        recomputed = merger.aggregate(
            session_input_spec=aggregate.session_input_spec,
            chunks=aggregate.ordered_chunks,
        )
    except ChunkAggregateError as exc:
        errors.append("session aggregate recomputation failed: " + str(exc))
    else:
        if recomputed.aggregate_root != aggregate.aggregate_root:
            errors.append("session aggregate root differs from deterministic recomputation")
        if recomputed.aggregate_root_hash != aggregate.aggregate_root_hash:
            errors.append("session aggregate hash differs from deterministic recomputation")
        if recomputed.episode != aggregate.episode:
            errors.append("session aggregate episode differs from deterministic recomputation")
    if getattr(result, "input_spec", None) != aggregate.session_input_spec:
        errors.append("result input spec is not the session aggregate input spec")
    if getattr(result, "extraction_output", None) != aggregate.aggregate_root:
        errors.append("result extraction root is not the session aggregate root")
    if getattr(result, "extraction_output_hash", "") != aggregate.aggregate_root_hash:
        errors.append("result extraction root hash is not the session aggregate hash")
    capability = getattr(result, "fragment_route_capability", None)
    if capability is not None:
        expected_hashes = tuple(chunk.canonical_output_hash for chunk in aggregate.ordered_chunks)
        if tuple(capability.chunk_root_hashes) != expected_hashes:
            errors.append("fragment route capability is not bound to all chunk roots")
        if capability.chunk_aggregate_contract_hash != aggregate.aggregate_contract_hash:
            errors.append("fragment route capability is not bound to aggregate contract")
    return errors
