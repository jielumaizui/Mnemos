# -*- coding: utf-8 -*-
"""Chunking and structured-output helpers for DistillationEngine."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from core.hephaestus.chunk_aggregate import validate_session_chunk_aggregate
from core.hephaestus.distill_action_router import (
    DistillActionRouter,
    DistillActionRouterOptions,
)
from core.hephaestus.distill_input_spec import (
    DistillInputSpec,
    ExtractionRequest,
    PreparedExtractionPrompt,
)
from core.hephaestus.distill_response import DistillBackendResponse
from core.hephaestus.distillation_contract import (
    RECOMMENDED_ACTIONS,
    canonical_extraction_output_hash,
    canonicalize_extraction_output,
    validate_admitted_extraction_root,
    validate_checkpoint_extraction_output,
    validate_distill_output_contract,
)
from core.hephaestus.distillation_models import (
    DistillationResult,
    ExtractionOutcome,
    KnowledgeFragment,
    PipelineLayerResult,
)
from core.hephaestus.distillation_quality import (
    _apply_domain_scores,
    _apply_expression_formatting,
    _auto_remediate_fragment as _quality_auto_remediate_fragment,
    _collect_fragment_errors as _quality_collect_fragment_errors,
    _fatal_self_check_errors,
)
from core.hephaestus.distillation_text import (
    EFFECTIVE_MAX_TOKENS,
    PER_MESSAGE_TOKEN_LIMIT,
)
from core.ops import cognitive_pipeline_receipts as pipeline_receipts

logger = logging.getLogger(__name__)


_ENGINE_NAMESPACE_PROVIDER: Callable[[], Mapping[str, Any]] | None = None


def bind_engine_namespace(provider: Callable[[], Mapping[str, Any]]) -> None:
    """Bind the patchable engine facade without creating a reverse import."""
    global _ENGINE_NAMESPACE_PROVIDER
    _ENGINE_NAMESPACE_PROVIDER = provider


class _EngineNamespaceProxy:
    def __getattr__(self, name: str) -> Any:
        if _ENGINE_NAMESPACE_PROVIDER is None:
            raise RuntimeError("distillation engine namespace is not bound")
        return _ENGINE_NAMESPACE_PROVIDER()[name]


_ENGINE_NAMESPACE = _EngineNamespaceProxy()


def _engine_module() -> _EngineNamespaceProxy:
    return _ENGINE_NAMESPACE


class DistillationPipelineMixin:
    """Lossless chunking, filtering, and structured-output contracts."""

    # Structural contract supplied by ``DistillationEngine``.
    _runtime_receipt_config: Any
    wiki_base: Path
    _persist_pages: Callable[
        [DistillationResult, List[KnowledgeFragment]],
        Tuple[List[str], List[Tuple[Path, KnowledgeFragment]]],
    ]

    @staticmethod
    def _jaccard_similarity(a: str, b: str) -> float:
        """计算两个字符串的 Jaccard 相似度（基于字符二元组）"""
        if not a or not b:
            return 0.0
        sa = set(a[i : i + 2] for i in range(len(a) - 1))
        sb = set(b[i : i + 2] for i in range(len(b) - 1))
        inter = len(sa & sb)
        union = len(sa | sb)
        return inter / union if union > 0 else 0.0

    @staticmethod
    def _dedup_fragments_semantic(
        fragments: List[KnowledgeFragment], threshold: float = 0.75
    ) -> List[KnowledgeFragment]:
        """按 title 精确去重 + Jaccard 语义去重（保持原始顺序）。"""
        if not fragments:
            return []
        # 先精确去重，防御 None title
        seen_titles = set()
        unique = []
        for frag in fragments:
            title = frag.title or ""
            if title not in seen_titles:
                seen_titles.add(title)
                unique.append(frag)
        # 再语义去重：相似度 > threshold 的只保留第一个
        deduped = []  # type: ignore[var-annotated]
        for frag in unique:
            is_dup = False
            frag_title = frag.title or ""
            for kept in deduped:
                kept_title = kept.title or ""
                sim = DistillationPipelineMixin._jaccard_similarity(frag_title, kept_title)
                if sim >= threshold:
                    is_dup = True
                    break
            if not is_dup:
                deduped.append(frag)
        return deduped

    @staticmethod
    def _resolve_chunk_limits(
        max_tokens_per_chunk: int | None,
        per_message_token_limit: int | None,
    ) -> Tuple[int, int, int]:
        """解析 chunk 与单消息 token 限制，返回 (effective_max, per_msg_limit, split_limit)。"""
        cfg = _engine_module().get_config()

        effective_max_tokens = max_tokens_per_chunk
        if effective_max_tokens is None:
            effective_max_tokens = int(
                cfg.get("distill.effective_max_tokens", EFFECTIVE_MAX_TOKENS)
                or EFFECTIVE_MAX_TOKENS
            )

        if per_message_token_limit is None:
            per_message_token_limit = int(
                cfg.get("distill.per_message_token_limit", PER_MESSAGE_TOKEN_LIMIT)
                or PER_MESSAGE_TOKEN_LIMIT
            )

        # 保留 200 token 余量给 role 前缀、分隔符和 head-tail marker
        split_limit = min(per_message_token_limit, max(1, effective_max_tokens - 200))
        return effective_max_tokens, per_message_token_limit, split_limit

    @staticmethod
    def _expand_long_messages(
        messages: List[Dict],
        tokenizer: Any,
        split_limit: int,
        need_turns: bool,
    ) -> List[Dict]:
        """把超长消息拆成多个 part，保留原始 turn 编号。"""

        def _message_with_part_span(message: Dict, part: str, offset: int) -> Dict:
            """Copy a message and narrow its raw span to one lossless part."""
            expanded_message = dict(message)
            source_span = message.get("source_span")
            if not isinstance(source_span, dict):
                return expanded_message
            try:
                span_start = int(source_span["span_start"])
                span_end = int(source_span["span_end"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("message source_span requires ordered integer bounds") from exc
            part_end = offset + len(part)
            if span_start < 0 or span_end <= span_start or part_end > span_end - span_start:
                raise ValueError("message source_span does not cover split content")
            part_span = dict(source_span)
            part_span["span_start"] = span_start + offset
            part_span["span_end"] = span_start + part_end
            expanded_message["source_span"] = part_span
            return expanded_message

        expanded: List[Dict] = []
        for idx, msg in enumerate(messages):
            content = msg.get("content", "")
            original_turn = msg.get("turn")
            if original_turn is None:
                original_turn = msg.get("turn_number")
            turn = original_turn if original_turn is not None else idx + 1
            msg_tokens = tokenizer.estimate(content)
            if msg_tokens > split_limit:
                parts = tokenizer.split_to_tokens(content, split_limit)
                if isinstance(msg.get("source_span"), dict) and "".join(parts) != content:
                    raise ValueError("tokenizer split must preserve source_span content exactly")
                total_parts = len(parts)
                offset = 0
                for i, part in enumerate(parts):
                    new_msg = _message_with_part_span(msg, part, offset)
                    new_msg["content"] = part
                    new_msg["turn"] = turn
                    new_msg["part"] = f"{i + 1}/{total_parts}"
                    expanded.append(new_msg)
                    offset += len(part)
            elif need_turns:
                new_msg = dict(msg)
                new_msg["turn"] = turn
                expanded.append(new_msg)
            else:
                expanded.append(msg)
        return expanded

    @staticmethod
    def _group_messages_into_chunks(
        expanded: List[Dict],
        effective_max_tokens: int,
        max_turns_per_chunk: int | None,
        tokenizer: Any,
    ) -> List[List[Dict]]:
        """按 token 预算与 turn 预算把展开后的消息分组。"""
        chunks: List[List[Dict]] = []
        current_chunk: List[Dict] = []
        current_tokens = 0
        current_turns: set = set()

        for msg in expanded:
            msg_tokens = tokenizer.estimate(msg.get("content", ""))
            msg_turn = msg.get("turn")

            token_overflow = current_tokens + msg_tokens > effective_max_tokens and current_chunk
            turn_overflow = False
            if max_turns_per_chunk and msg_turn is not None and current_chunk:
                would_turns = set(current_turns)
                would_turns.add(msg_turn)
                if len(would_turns) > max_turns_per_chunk:
                    turn_overflow = True

            if token_overflow or turn_overflow:
                chunks.append(current_chunk)
                current_chunk = []
                current_tokens = 0
                current_turns = set()

            current_chunk.append(msg)
            current_tokens += msg_tokens
            if msg_turn is not None:
                current_turns.add(msg_turn)

        if current_chunk:
            chunks.append(current_chunk)
        return chunks

    @staticmethod
    def _chunk_messages(
        messages: List[Dict],
        max_tokens_per_chunk: int | None = None,
        per_message_token_limit: int | None = None,
        max_turns_per_chunk: int | None = None,
    ) -> List[List[Dict]]:
        """将消息列表按 token 数切片，用于分块蒸馏（P0-5）。

        超长单条消息会被拆成多个 part（而不是截断），保证所有内容都能被后续 LLM 看到。
        当提供 max_turns_per_chunk 时，累计 turn 数达到上限也会强制切分。

        Args:
            max_tokens_per_chunk: 每 chunk 的 token 上限（默认 24000）
            per_message_token_limit: 单条消息 token 上限，超限时拆分为多个 part
            max_turns_per_chunk: 每 chunk 最多包含的原始 turn 数（None 表示不限制）
        """
        if not messages:
            return []

        tokenizer = _engine_module().get_tokenizer()
        effective_max_tokens, _, split_limit = DistillationPipelineMixin._resolve_chunk_limits(
            max_tokens_per_chunk, per_message_token_limit
        )

        need_turns = max_turns_per_chunk is not None
        expanded = DistillationPipelineMixin._expand_long_messages(
            messages, tokenizer, split_limit, need_turns
        )
        return DistillationPipelineMixin._group_messages_into_chunks(
            expanded, effective_max_tokens, max_turns_per_chunk, tokenizer
        )

    @staticmethod
    def _slugify(name: str) -> str:
        """将名称转为 URL/文件安全的 slug"""
        slug = name.lower().strip()
        # 保留中英文、数字、横线；其他字符替换为横线
        slug = re.sub(r"[^\w\u4e00-\u9fa5-]", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-")
        return slug[:64] if slug else "untitled"

    @staticmethod
    def _session_short(session_id: str | None) -> str:
        """从 session_id 提取简短前缀，避免文件名出现 ``session__`` 双下划线。"""
        raw = session_id or "unknown"
        # 去掉常见的 "session_" 前缀，否则 (session_id)[:8] 会得到 "session_"，
        # 再与 slug 组合就变成 "session__<title>"。
        short = re.sub(r"^session[_-]?", "", raw)
        return (short[:8] or "unknown").strip("-_")

    @staticmethod
    def _prepare_fragments(fragments: List[KnowledgeFragment], cfg: Any) -> List[KnowledgeFragment]:
        """对每个片段做根因级自动修复与格式化增强。"""
        for frag in fragments:
            _quality_auto_remediate_fragment(frag)
            _apply_expression_formatting(frag, cfg)
            _apply_domain_scores(frag, cfg)
        return fragments

    def _filter_accepted_fragments(
        self,
        result: DistillationResult,
        fragments: List[KnowledgeFragment],
        cfg: Any,
    ) -> Optional[List[KnowledgeFragment]]:
        """根据硬校验、质量门禁和失败比例过滤可入库片段。

        Returns:
            当整体被阻断时返回 None；否则返回允许写入的片段列表（可能已过滤）。
        """
        failed_indices, all_errors = _quality_collect_fragment_errors(
            fragments, cfg, result.session_id
        )
        pipeline_receipts.record_quality_gate_decisions(
            self._runtime_receipt_config, fragments, failed_indices
        )
        fatal_errors = _fatal_self_check_errors(fragments)

        if fatal_errors:
            _engine_module()._save_failed_distill(
                result.session_id,
                fragments,
                fatal_errors,
                source=result.source,
                raw_response=result.raw_response,
                parse_metadata=self._post_extraction_failure_metadata(
                    result,
                    failure_path="fatal_fragment_self_check",
                ),
                database_dir=Path(self._runtime_receipt_config.database_dir),
            )
            logger.warning(
                "[Distillation] Session %s 自检存在 fatal 问题，已阻断正式入库",
                result.session_id,
            )
            return None

        if not failed_indices:
            return fragments

        total = len(fragments)
        passed_count = total - len(failed_indices)
        ratio = passed_count / total
        from core.kia.policy import get_shadowed_value

        min_ratio = float(
            get_shadowed_value(
                "distill.min_session_fragment_pass_ratio",
                cfg.get("distill.min_session_fragment_pass_ratio", 0.5),
            )
        )  # noqa: E501
        if ratio < min_ratio:
            _engine_module()._save_failed_distill(
                result.session_id,
                fragments,
                all_errors,
                source=result.source,
                raw_response=result.raw_response,
                parse_metadata=self._post_extraction_failure_metadata(
                    result,
                    failure_path="fragment_pass_ratio_rejected",
                ),
                database_dir=Path(self._runtime_receipt_config.database_dir),
            )
            logger.warning(
                "[Distillation] Session %s 未通过硬校验 (%s 项错误)，已保存事故证据并安排诊断",
                result.session_id,
                len(all_errors),
            )
            return None

        failed_fragments = [frag for i, frag in enumerate(fragments) if i in failed_indices]
        _engine_module()._save_failed_distill(
            result.session_id,
            failed_fragments,
            all_errors,
            source=result.source,
            raw_response=result.raw_response,
            parse_metadata=self._post_extraction_failure_metadata(
                result,
                failure_path="partial_fragment_rejection",
            ),
            severity="medium",
            database_dir=Path(self._runtime_receipt_config.database_dir),
        )
        logger.warning(
            "[Distillation] Session %s 部分片段未通过硬校验（%d/%d 通过），"
            "已保存失败片段；合法片段仍须通过结构化动作路由",
            result.session_id,
            passed_count,
            total,
        )
        accepted = [frag for i, frag in enumerate(fragments) if i not in failed_indices]
        result.fragments = accepted
        return accepted

    def _validate_structured_output_contract(self, result: DistillationResult, cfg: Any) -> bool:
        """Strict distill_output_v4 gate before normal Inbox writes."""
        if not bool(cfg.get("distill.structured_output_contract.enforce", True)):
            return True

        if not isinstance(result.input_spec, DistillInputSpec):
            validation_errors = [
                "structured distillation output contract failed: input_spec: "
                "live vault writes require an immutable DistillInputSpec"
            ]
            _engine_module()._save_failed_distill(
                result.session_id,
                result.fragments,
                validation_errors,
                source=result.source,
                raw_response=result.raw_response,
                parse_metadata=self._post_extraction_failure_metadata(
                    result,
                    failure_path="missing_distill_input_spec",
                ),
                database_dir=Path(self._runtime_receipt_config.database_dir),
            )
            result.layer_results.append(
                PipelineLayerResult(
                    8,
                    "structured_output_contract",
                    False,
                    {"errors": validation_errors},
                )
            )
            return False

        aggregate_errors = validate_session_chunk_aggregate(result)
        root_validation = validate_admitted_extraction_root(
            input_spec=result.input_spec,
            structured_output=result.structured_output,
            extraction_contract_valid=result.extraction_contract_valid,
            extraction_output=result.extraction_output,
            extraction_output_hash=result.extraction_output_hash,
            extraction_judgment=result.extraction_judgment,
        )
        root_errors = [
            "structured distillation output contract failed: session chunk aggregate: " + error
            for error in aggregate_errors
        ]
        root_errors.extend(
            "structured distillation output contract failed: " f"{issue.path}: {issue.message}"
            for issue in root_validation.issues
        )
        if root_errors:
            _engine_module()._save_failed_distill(
                result.session_id,
                result.fragments,
                root_errors,
                source=result.source,
                raw_response=result.raw_response,
                parse_metadata=self._post_extraction_failure_metadata(
                    result,
                    failure_path="session_aggregate_contract_rejected",
                ),
                database_dir=Path(self._runtime_receipt_config.database_dir),
            )
            result.layer_results.append(
                PipelineLayerResult(
                    8,
                    "structured_output_contract",
                    False,
                    {"errors": root_errors},
                )
            )
            return False

        validation = validate_distill_output_contract(
            result.structured_output,
            input_spec=result.input_spec,
        )
        router_enabled = bool(cfg.get("distill.action_router.enabled", True))
        allowed_actions = set(RECOMMENDED_ACTIONS) if router_enabled else set()
        unsupported_actions = sorted(set(validation.actions) - allowed_actions)
        dispute_supported = router_enabled
        if validation.valid and dispute_supported and not unsupported_actions:
            result.layer_results.append(
                PipelineLayerResult(
                    8,
                    "structured_output_contract",
                    True,
                    {
                        "schema_version": "distill_output_v4",
                        "actions": validation.actions,
                        "action_router_enabled": router_enabled,
                    },
                )
            )
            return True

        errors: List[str] = []
        if validation.issues:
            errors.extend(
                f"structured distillation output contract failed: {issue.path}: {issue.message}"
                for issue in validation.issues
            )
        if not router_enabled:
            errors.append(
                "structured distillation output requires the action router; direct Wiki writes are blocked"
            )
        if unsupported_actions:
            errors.append(
                "structured distillation output requires unsupported write action(s): "
                + ", ".join(unsupported_actions)
            )
        if not errors:
            errors.append("structured distillation output contract failed")

        _engine_module()._save_failed_distill(
            result.session_id,
            result.fragments,
            errors,
            source=result.source,
            raw_response=result.raw_response,
            parse_metadata=self._post_extraction_failure_metadata(
                result,
                failure_path="structured_output_contract_rejected",
            ),
            database_dir=Path(self._runtime_receipt_config.database_dir),
        )
        result.layer_results.append(
            PipelineLayerResult(
                8,
                "structured_output_contract",
                False,
                {"errors": errors},
            )
        )
        logger.warning(
            "[Distillation] Session %s failed strict structured output contract",
            result.session_id,
        )
        return False

    def _route_structured_actions(
        self,
        result: DistillationResult,
        fragments: List[KnowledgeFragment],
        cfg: Any,
    ) -> Tuple[List[str], List[Tuple[Path, KnowledgeFragment]]]:
        """Route distill_output_v4 actions and return created pages for post-write hooks."""
        router = DistillActionRouter(
            DistillActionRouterOptions.from_config(cfg, wiki_base=self.wiki_base)
        )
        routed = router.route(
            result,
            fragments,
            create_pages=lambda accepted: self._persist_pages(result, list(accepted)),
        )
        result.page_raw_event_refs = list(routed.page_raw_event_refs)
        result.layer_results.append(
            PipelineLayerResult(
                9,
                "distill_action_router",
                not routed.errors,
                routed.to_layer_detail(),
            )
        )
        if routed.errors:
            logger.warning(
                "[Distillation] Session %s action router had errors: %s",
                result.session_id,
                routed.errors,
            )
        return routed.written, routed.file_fragments

    @staticmethod
    def _raw_completeness_from_meta(meta: Dict[str, Any]) -> str:
        completeness = meta.get("completeness")
        if not isinstance(completeness, dict):
            return "full"
        if completeness.get("truncated"):
            return "truncated"
        if completeness.get("loss_reasons"):
            return "partial"
        if completeness.get("source_fidelity") == "unknown":
            return "unknown"
        visible_text = str(completeness.get("visible_text") or "")
        if visible_text in {"artifact", "compressed", "host_provided"}:
            return "compressed"
        return "full"

    @staticmethod
    def _input_spec_for_extraction(
        result: DistillationResult,
        text: str,
        mode: str,
        messages: List[Dict[str, Any]],
    ) -> DistillInputSpec:
        return DistillInputSpec.build(
            source_agent=result.source,
            source_session_id=result.session_id,
            source_event_ids=(str(ref.get("revision_id") or "") for ref in result.raw_event_refs),
            raw_completeness=result.raw_completeness,
            visible_input=text,
            input_mode=mode,
            artifact_refs=result.artifact_refs,
            source_messages=messages,
            source_authority_context=result.source_authority_context,
        )

    @staticmethod
    def _verify_extraction_outcome(
        outcome: ExtractionOutcome,
        input_spec: DistillInputSpec,
    ) -> bool:
        """Use the same union validator for fresh outcomes and checkpoint hits."""
        if not isinstance(outcome, ExtractionOutcome) or not outcome.admitted:
            return False
        if not isinstance(outcome.canonical_output, Mapping):
            return False
        canonical_output = dict(outcome.canonical_output)
        expected_output = canonicalize_extraction_output(canonical_output, outcome.fragments)
        if canonical_output != expected_output:
            return False
        if canonical_output.get("judgment") != outcome.judgment:
            return False
        if canonical_output.get("structured_output") != dict(outcome.structured_output or {}):
            return False
        validation = validate_checkpoint_extraction_output(
            canonical_output=canonical_output,
            input_spec=input_spec,
        )
        if not validation.valid:
            return False
        if not validation.is_skip:
            passed, _ = _engine_module()._strict_validate_fragments(list(outcome.fragments))
            if not passed:
                return False
        return outcome.canonical_output_hash == canonical_extraction_output_hash(
            canonical_output=canonical_output,
        )

    @staticmethod
    def _apply_admitted_outcome(
        result: DistillationResult,
        input_spec: DistillInputSpec,
        outcome: ExtractionOutcome,
    ) -> None:
        """Make one admitted root the result root (standard path only).

        Chunked extraction intentionally does not call this helper per chunk:
        every local root has a different immutable visible-input hash and must
        first become a ``ChunkExtractionResult`` for session aggregation.
        """
        result.input_spec = input_spec
        result.extraction_judgment = outcome.judgment
        result.extraction_contract_valid = True
        result.structured_output = dict(outcome.structured_output or {})
        result.extraction_output = dict(outcome.canonical_output)
        result.extraction_output_hash = outcome.canonical_output_hash

    @staticmethod
    def _record_extraction_response_evidence(
        result: DistillationResult,
        responses: Tuple[DistillBackendResponse, ...] | List[DistillBackendResponse],
    ) -> None:
        """Attach raw-free metadata and the latest available raw response."""

        result.response_evidence = [response.to_failure_metadata() for response in responses]
        result.raw_response = next(
            (response.raw_text for response in reversed(responses) if response.raw_text),
            "",
        )

    @staticmethod
    def _extraction_failure_metadata(
        result: DistillationResult,
        *,
        request: ExtractionRequest,
        prepared: PreparedExtractionPrompt | None,
        correction_attempts: int = 0,
        failure_path: str = "contract_rejected",
    ) -> Dict[str, Any]:
        latest = result.response_evidence[-1] if result.response_evidence else {}
        return {
            "path": str(latest.get("parse_path") or failure_path),
            "failure_path": failure_path,
            "correction_attempts": int(correction_attempts),
            "responses": list(result.response_evidence),
            "transport_empty": bool(latest.get("transport_empty", not result.raw_response)),
            "prompt_hash": prepared.prompt_hash if prepared is not None else "",
            "input_spec_hash": request.input_spec.input_spec_hash,
            "visible_input_sha256": request.input_spec.visible_input_sha256,
            "artifact_catalog_hash": request.input_spec.artifact_catalog.catalog_hash,
            "artifact_catalog_entry_count": len(request.input_spec.artifact_catalog.entries),
            "artifact_catalog_rejected_count": (request.input_spec.artifact_catalog.rejected_count),
            "source_authority_catalog_hash": (
                request.input_spec.source_authority_catalog.catalog_hash
            ),
            "source_authority_entry_count": len(
                request.input_spec.source_authority_catalog.entries
            ),
            "source_authority_rejected_count": (
                request.input_spec.source_authority_catalog.rejected_count
            ),
            "source_event_refs": list(request.input_spec.source_event_ids),
            "response_hash": str(latest.get("response_hash") or "")
            or DistillBackendResponse.hash_raw_text(result.raw_response),
        }

    @staticmethod
    def _post_extraction_failure_metadata(
        result: DistillationResult,
        *,
        failure_path: str,
    ) -> Dict[str, Any]:
        """Bind downstream quality failures to the admitted extraction input."""

        latest = result.response_evidence[-1] if result.response_evidence else {}
        input_spec = result.input_spec
        source_event_refs = (
            list(input_spec.source_event_ids)
            if isinstance(input_spec, DistillInputSpec)
            else [
                str(item.get("revision_id") or "")
                for item in result.raw_event_refs
                if isinstance(item, dict) and str(item.get("revision_id") or "")
            ]
        )
        return {
            "path": str(latest.get("parse_path") or failure_path),
            "failure_path": failure_path,
            "responses": list(result.response_evidence),
            "transport_empty": bool(latest.get("transport_empty", not result.raw_response)),
            "prompt_hash": str(result.extraction_prompt_hash or ""),
            "input_spec_hash": (
                input_spec.input_spec_hash if isinstance(input_spec, DistillInputSpec) else ""
            ),
            "visible_input_sha256": (
                input_spec.visible_input_sha256 if isinstance(input_spec, DistillInputSpec) else ""
            ),
            "source_event_refs": source_event_refs,
            "response_hash": str(latest.get("response_hash") or "")
            or DistillBackendResponse.hash_raw_text(result.raw_response),
        }

    @staticmethod
    def _contract_failure_errors(outcome: ExtractionOutcome) -> List[str]:
        issues = getattr(outcome.admission, "issues", ())
        errors = [
            f"{getattr(issue, 'path', '$')}: {getattr(issue, 'message', str(issue))}"
            for issue in issues
        ]
        return errors or ["distillation extraction output contract failed"]
