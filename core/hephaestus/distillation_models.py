# -*- coding: utf-8 -*-
"""Shared data models for the distillation pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from core.hephaestus.distill_response import DistillBackendResponse


class KnowledgeFragment:
    """知识片段"""

    form: str
    title: str
    frontmatter: Dict[str, Any]
    background: str
    core_content: str
    boundaries: Dict[str, str]
    anti_patterns: List[str]
    related_concepts: List[str]
    # Exact normalized claim membership.  A merged fragment may support more
    # than one claim, but every id must exist in the admitted episode root.
    claim_ids: List[str]
    # 结构化关联（ADR-019：关联上下文用于语义桥接）
    relations: List[Dict[str, str]]
    # 七层流水线扩展字段
    self_check_passed: bool = True
    self_check_issues: List[str] = field(default_factory=list)
    self_check_severity: str = "ok"
    cross_agent_links: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    # AI 关联扩充（与原始内容严格区分）
    ai_expansion: str = ""

    def __init__(
        self,
        form: str,
        title: str,
        frontmatter: Dict[str, Any],
        background: str,
        core_content: str,
        boundaries: Dict[str, str],
        anti_patterns: List[str],
        related_concepts: List[str],
        claim_ids: List[str] | None = None,
        relations: List[Dict[str, str]] | None = None,
        self_check_passed: bool = True,
        self_check_issues: List[str] | None = None,
        self_check_severity: str = "ok",
        cross_agent_links: List[str] | None = None,
        keywords: List[str] | None = None,
        ai_expansion: str = "",
    ):
        self.form = form
        self.title = title
        self.frontmatter = frontmatter
        self.background = background
        self.core_content = core_content
        self.boundaries = boundaries
        self.anti_patterns = anti_patterns
        self.related_concepts = related_concepts
        self.claim_ids = list(dict.fromkeys(str(value) for value in (claim_ids or []) if value))
        self.relations = relations or []
        self.self_check_passed = self_check_passed
        self.self_check_issues = self_check_issues or []
        self.self_check_severity = self_check_severity
        self.cross_agent_links = cross_agent_links or []
        self.keywords = keywords or []
        self.ai_expansion = ai_expansion


@dataclass(frozen=True)
class ExtractionOutcome:
    """One fully admitted (or rejected) extractor response.

    This replaces the mutable ``last_structured_output`` side channel at the
    engine boundary.  ``admission`` is intentionally opaque here to avoid a
    model-layer import cycle; it is a ContractValidationResult in production.
    """

    judgment: str
    fragments: tuple[KnowledgeFragment, ...]
    structured_output: Mapping[str, Any] | None
    canonical_output: Mapping[str, Any]
    admission: Any
    canonical_output_hash: str
    correction_count: int = 0
    backend_responses: tuple[DistillBackendResponse, ...] = ()

    @property
    def admitted(self) -> bool:
        return bool(getattr(self.admission, "valid", False))


@dataclass
class PipelineLayerResult:
    """流水线单层执行结果"""

    layer: int
    name: str
    passed: bool
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FragmentRouteCapability:
    """Bind a post-admission fragment sequence to one admitted root hash.

    The pipeline may quality-format or cross-link the same fragment objects
    after extraction.  A router must therefore preserve object identity rather
    than re-hash mutable display fields, while still refusing a direct caller
    that swaps in an unrelated fragment sequence after admission.
    """

    extraction_output_hash: str
    input_spec_hash: str
    fragments: tuple[KnowledgeFragment, ...]
    # Chunked extraction has more than one independently admitted root.  The
    # final session root is allowed to route pages only when the capability is
    # also bound to that complete ordered bundle and to the deterministic
    # aggregate contract that produced it.  Empty values keep the standard
    # (single-extraction) path backward compatible.
    chunk_root_hashes: tuple[str, ...] = ()
    chunk_aggregate_contract_hash: str = ""


@dataclass(frozen=True)
class ChunkExtractionResult:
    """One fully admitted chunk contribution to a session-level aggregate.

    A chunk root is immutable evidence for one visible-input hash, so it must
    never be overwritten into a mutable session result.  ``episode_fragment``
    is the exact admitted structured v4 payload for that chunk; it is kept
    alongside parsed fragments for deterministic, lossless aggregation.
    """

    chunk_index: int
    chunk_hash: str
    input_spec: Any
    execution_spec_hash: str
    canonical_output: Mapping[str, Any]
    canonical_output_hash: str
    fragments: tuple[KnowledgeFragment, ...]
    episode_fragment: Mapping[str, Any]
    source_span_map: tuple[Mapping[str, Any], ...]
    contract_verdict: str
    cache_hit: bool = False


@dataclass(frozen=True)
class SessionChunkAggregate:
    """Deterministic session root derived from admitted chunk contributions."""

    session_input_spec: Any
    ordered_chunks: tuple[ChunkExtractionResult, ...]
    aggregate_root: Mapping[str, Any]
    aggregate_root_hash: str
    merged_fragments: tuple[KnowledgeFragment, ...]
    episode: Mapping[str, Any]
    aggregate_contract_version: str
    aggregate_contract_hash: str


@dataclass(frozen=True)
class CognitionAssetCommitReceipt:
    """Durable receipt for one complete, privacy-redacted cognition asset."""

    status: str
    asset_id: str = ""
    content_hash: str = ""
    error_code: str = ""
    redaction_counts: Tuple[Tuple[str, int], ...] = ()
    schema_version: str = "mnemos.cognition_asset_commit_receipt.v1"

    def __post_init__(self) -> None:
        if self.status not in {"committed", "existing", "retryable_failed"}:
            raise ValueError(f"invalid cognition asset receipt status: {self.status}")
        if self.status in {"committed", "existing"} and (
            not self.asset_id or not self.content_hash
        ):
            raise ValueError("committed cognition asset receipt requires identity")
        if self.status == "retryable_failed" and not self.error_code:
            raise ValueError("failed cognition asset receipt requires error_code")

    @property
    def committed(self) -> bool:
        return self.status in {"committed", "existing"}


@dataclass(frozen=True)
class CognitiveDecisionAssetProposalReceipt:
    """Independent receipt for the optional proposal derived from an asset."""

    status: str
    asset_id: str
    proposal_id: str = ""
    content_hash: str = ""
    error_code: str = ""
    schema_version: str = "mnemos.cognitive_decision_asset_proposal_receipt.v1"

    def __post_init__(self) -> None:
        if self.status not in {"committed", "existing", "optional_failed"}:
            raise ValueError(f"invalid cognitive proposal receipt status: {self.status}")
        if not self.asset_id:
            raise ValueError("cognitive proposal receipt requires asset_id")
        if self.status in {"committed", "existing"} and (
            not self.proposal_id or not self.content_hash
        ):
            raise ValueError("committed cognitive proposal receipt requires identity")
        if self.status == "optional_failed" and not self.error_code:
            raise ValueError("failed cognitive proposal receipt requires error_code")

    @property
    def committed(self) -> bool:
        return self.status in {"committed", "existing"}


@dataclass
class DistillationResult:
    """蒸馏结果"""

    session_id: str
    judgment: str = "skip"  # knowledge / skill / skip
    judgment_reason: str = ""
    skill_suggestion: str = ""
    # ``skill`` is an asset-bearing judgment.  These independent typed
    # receipts prove that its full canonical cognition and its optional
    # derivative proposal were handled separately at the write boundary.
    cognition_asset_receipt: CognitionAssetCommitReceipt | None = None
    cognitive_decision_proposal_receipt: CognitiveDecisionAssetProposalReceipt | None = None
    # Canonical cognition episode is committed before any Wiki/action sink.
    cognition_episode_revision_id: str = ""
    cognition_episode_receipt: Any = None
    analysis_type: str = "standard"  # standard / data_distillation
    data_profile: Optional[Dict] = None
    anomalies: List[Dict] = field(default_factory=list)
    fragments: List[KnowledgeFragment] = field(default_factory=list)
    raw_response: str = ""
    response_evidence: List[Dict[str, Any]] = field(default_factory=list)
    extraction_prompt_hash: str = ""
    # distill_output_v4 payload validated before strict vault writes.
    structured_output: Optional[Dict[str, Any]] = None
    # Immutable identity supplied to the extractor; never inferred from model output.
    input_spec: Any = None
    raw_completeness: str = "full"
    # Typed extraction verdict used to distinguish a legal skip from a malformed
    # empty response without relying on the fragment count alone.
    extraction_judgment: str = ""
    extraction_contract_valid: Optional[bool] = None
    # Root union proof admitted before correction/checkpoint.  Strict writes
    # verify this proof rather than reconstructing a caller-provided payload.
    extraction_output: Optional[Dict[str, Any]] = None
    extraction_output_hash: str = ""
    # Capability issued by the engine after its permitted post-admission
    # transformations.  Direct action-router callers cannot replace
    # ``fragments`` without also failing this immutable identity binding.
    fragment_route_capability: FragmentRouteCapability | None = None
    # Populated only for the chunked path.  Keeping the bundle in-memory lets
    # the action router revalidate every per-chunk admission before a session
    # aggregate is allowed to trigger a formal write.
    chunk_extraction_results: List[ChunkExtractionResult] = field(default_factory=list)
    chunk_aggregate: SessionChunkAggregate | None = None
    error: str = ""
    needs_reconfirm: bool = False
    reconfirm_question: str = ""
    # 七层流水线追踪
    layer_results: List[PipelineLayerResult] = field(default_factory=list)
    prejudgment: str = ""  # CERTAINLY_YES / CERTAINLY_NO / MAYBE
    prejudgment_confidence: float = 0.0
    self_check_passed: bool = True
    self_check_issues: List[str] = field(default_factory=list)
    self_check_severity: str = "ok"
    cross_agent_links: List[str] = field(default_factory=list)
    # 会话覆盖范围（用于 Wiki 来源追踪）
    session_coverage: str = ""
    # 来源 Agent
    source: str = ""
    # Revision-aware workflow identity (distinct from mutable session_id).
    input_revision: str = ""
    # Immutable raw revision spans that substantiate this distillation result.
    raw_event_refs: List[Dict[str, Any]] = field(default_factory=list)
    # Capture-owned artifact references. DistillInputSpec converts these into
    # a path-free, chunk-local system catalog before any model call.
    artifact_refs: List[Dict[str, Any]] = field(default_factory=list)
    # System-owned source classification propagated from capture/document
    # boundaries.  It contains no visible content and is never model-owned.
    source_authority_context: Dict[str, Any] = field(default_factory=dict)
    # Exact page-level evidence for routed update/merge/dispute/reinforcement
    # writes that do not have a newly created KnowledgeFragment carrier.
    page_raw_event_refs: List[Tuple[Path, Sequence[Mapping[str, Any]]]] = field(
        default_factory=list
    )
    # 蒸馏输入模式（P0-5 覆盖率追踪）
    distill_input_mode: str = ""
    truncated: bool = False
    # 文档分类（文件蒸馏场景）
    doc_category: str = ""
