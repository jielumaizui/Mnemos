# -*- coding: utf-8 -*-
"""DistillationEngine core pipeline.

The full architecture is documented in the repository docs and Desktop system
map; this module keeps the executable conversation distillation flow.
"""

from __future__ import annotations

import json
import logging
import re  # noqa: F401
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from core.config import get_config
from core.evidence.artifact_catalog import ArtifactCatalogRejectedError
from core.evidence.source_authority import SourceAuthorityCatalogRejectedError
from core.frontmatter import to_chinese_frontmatter, parse_frontmatter, write_frontmatter
from core.frontmatter import fm_get  # noqa: F401 (re-export compatibility)
from core.llm_config import LLMApiChain
from core.hephaestus.distillation_prompts import PROMPT_VERSION
from core.hephaestus.fragment_merger import FragmentMerger
from core.hephaestus.embedding_refresh import trigger_embedding_index_refresh
from core.hephaestus.model_call_run import start_distillation_model_call_run
from core.hephaestus.distillation_errors import (
    DistillationAPIError,
    generate_distillation_error_report as _generate_distillation_error_report,
)
from core.hephaestus.backend_bundle import backend_from_caller, DistillBackendBundle
from core.hephaestus.distill_backend import DistillBackend
from core.hephaestus.distill_response import DistillBackendResponse
from core.hephaestus.distillation_extractor import KnowledgeExtractor as _BaseKnowledgeExtractor
from core.hephaestus.distillation_feedback import DistillFeedbackLoop
from core.hephaestus.distillation_failure import (
    publish_wiki_page_updated,
    record_distillation_failure,
)
from core.hephaestus.distillation_json import extract_json  # noqa: F401 (re-export compatibility)
from core.hephaestus.distillation_contract import (
    RECOMMENDED_ACTIONS,
    canonical_extraction_output_hash,
    canonicalize_extraction_output,
    validate_admitted_extraction_root,
    validate_checkpoint_extraction_output,
    validate_distill_output_contract,
)
from core.hephaestus.distill_action_router import (
    DistillActionRouter,
    DistillActionRouterOptions,
)
from core.hephaestus.chunk_checkpoint import (
    DISTILLATION_INPUT_CONTRACT_VERSION,  # noqa: F401 (re-export compatibility)
    ChunkCheckpointStore,
)
from core.hephaestus.distill_input_spec import (
    DistillInputSpec,
    ExtractionRequest,
    PreparedExtractionPrompt,
)
from core.hephaestus.distillation_llm import (
    HttpApiHostAgentCaller as _BaseHttpApiHostAgentCaller,
)
from core.hephaestus.distillation_models import (
    DistillationResult,
    ExtractionOutcome,
    FragmentRouteCapability,
    KnowledgeFragment,
    PipelineLayerResult,
)
from core.hephaestus.chunk_aggregate import (
    validate_session_chunk_aggregate,
)
from core.hephaestus.chunked_extraction import ChunkedExtractionCoordinator
from core.hephaestus.cognition_asset_store import resolve_cognition_judgment
from core.hephaestus.distillation_quality import (
    _apply_domain_scores,
    _apply_expression_formatting,
    _auto_remediate_fragment as _quality_auto_remediate_fragment,
    _collect_fragment_errors as _quality_collect_fragment_errors,
    _fatal_self_check_errors,
    _strict_validate_fragments as _quality_strict_validate_fragments,
    _validate_fragment as _quality_validate_fragment,
)
from core.ops.durable_io import read_native_bytes
from core.hephaestus.distillation_quality import (
    _DOMAIN_KEYWORDS,  # noqa: F401
    _first_sentence,  # noqa: F401
    _fragment_uncertainty,  # noqa: F401
    _infer_domain,  # noqa: F401
)
from core.hephaestus.distillation_text import (
    EFFECTIVE_MAX_TOKENS,
    PER_MESSAGE_TOKEN_LIMIT,
    build_session_text,
)
from core.hephaestus.distillation_text import clean_message_content  # noqa: F401 (re-export)
from core.hephaestus.distillation_pause import (
    is_distillation_paused,
    pause_distillation,
)
from core.hephaestus.distillation_prejudge import NoiseFilter, ValuePrejudgment
from core.hephaestus.distillation_self_check import (
    DistillSelfCheck,
    max_self_check_severity,
)
from core.hephaestus.distillation_self_check import (
    classify_self_check_issue,
)  # noqa: F401 (re-export)
from core.hephaestus.distillation_value_judge import LLMValueJudge as _BaseLLMValueJudge
from core.hephaestus.distillation_cross_linker import CrossAgentLinker
from core.hephaestus.distillation_wiki_page import (
    generate_wiki_page as _generate_wiki_page,
)
from core.hephaestus.distillation_wiki_page import (
    _map_form_to_type,  # noqa: F401
    _source_quality_notes,  # noqa: F401
    _usage_hints_for_fragment,  # noqa: F401
    _yaml_safe,  # noqa: F401
)
from core.hephaestus.prompt_builder import PromptBuilder, DistillTask, Session, TokenBudget
from core.hephaestus.tokenizer import get_tokenizer
from core.hephaestus.distillation_pipeline_support import (
    DistillationPipelineMixin,
    bind_engine_namespace,
)
from core.hephaestus.link_probe_worker import get_link_probe_worker
from core.hephaestus.trusted_push_bridge import submit_distillation_page_candidate
from core.hephaestus.frontmatter_mutation import update_frontmatter_field
from core.hephaestus import distillation_page_identity as page_identity, raw_provenance
from core.ops import cognitive_pipeline_receipts as pipeline_receipts
from core.pipeline_receipts import DistillationWriteReceipt
from core.trust.vault_mutation_service import commit_trusted_markdown

# 这些值仅作为配置未设置时的回退；业务代码应优先读取 distill.* 配置。
DEFAULT_TOKEN_BUDGET_TOTAL = 16000
FRAGMENT_BOUNDARY_CHARS = 8000
MIN_CORE_CONTENT_CHARS = 100
HTTP_API_HOST_AGENT_CALLER_TIMEOUT_SECONDS = 180
RESPONSE_TOKENS = 6000
DISTILLATION_ENGINE_PROCESS_FEEDBACK_LOOP = 7

logger = logging.getLogger(__name__)


def _get_wiki_dir() -> Path:
    return get_config().wiki_dir


def _get_wiki_db() -> Path:
    return get_config().database_dir / "wiki_state.db"


def _distill_prompt_budget() -> TokenBudget:
    """构造 PromptBuilder 使用的 TokenBudget，与 DistillationEngine 截断阈值对齐。

    DistillationEngine 的 P0-5 策略最大按 `token_budget_total * chunk_std_factor * 2`
    生成 session_text，PromptBuilder 预算需要覆盖该文本 + 相关上下文 + 输出余量。
    使用 (chunk_std_factor * 2 + 1)x 留足系统指令和相关上下文余量。
    """
    cfg = get_config()
    total = int(
        cfg.get("distill.token_budget_total", DEFAULT_TOKEN_BUDGET_TOTAL)
        or DEFAULT_TOKEN_BUDGET_TOTAL
    )
    std_factor = float(cfg.get("distill.chunk_std_factor", 3) or 3)
    output_reserve = int(cfg.get("distill.token_budget_output_reserve", 2000) or 2000)
    return TokenBudget(total_limit=int(total * (std_factor * 2 + 1)), output_reserve=output_reserve)


def generate_distillation_error_report(error: DistillationAPIError) -> Path:
    return _generate_distillation_error_report(error, _get_wiki_dir())


# ========== 模块级辅助函数（消除类间重复）==========


def _build_fragment(frag_data: Dict) -> Optional[KnowledgeFragment]:
    """从 JSON dict 构建 KnowledgeFragment，统一处理 keywords/relations 兼容逻辑。"""
    fm = frag_data.get("frontmatter", {})
    kw = fm.get("关键词", {})
    keywords = []
    if isinstance(kw, list):
        keywords = kw
    elif isinstance(kw, dict):
        for layer_words in kw.values():
            if isinstance(layer_words, list):
                keywords.extend(layer_words)
            elif isinstance(layer_words, str):
                keywords.append(layer_words)

    # These values have already passed the canonical extraction schema.  Do
    # not run the retired JSON cleaner here: it strips fenced code blocks and
    # makes the parsed fragment differ from the output that was admitted.
    raw_title = frag_data.get("title", "无标题")
    raw_content = frag_data.get("core_content", "")
    return KnowledgeFragment(
        form=frag_data.get("form", "未知"),
        title=raw_title,
        frontmatter=fm,
        background=frag_data.get("background", ""),
        core_content=raw_content,
        boundaries=frag_data.get("boundaries", {}),
        anti_patterns=frag_data.get("anti_patterns", []),
        related_concepts=frag_data.get("related_concepts", []),
        claim_ids=frag_data.get("claim_ids", []),
        relations=frag_data.get("relations", []),
        keywords=keywords,
    )


def _try_init(
    module_path: str,
    class_name: str,
    log_level: str = "warning",
    log_msg: str = "",
    *args,
    **kwargs,
) -> Any:
    """懒加载并实例化类，失败时返回 None 并记录日志。"""
    from core.import_guard import assert_allowed_module

    try:
        assert_allowed_module(module_path)
        module = __import__(module_path, fromlist=[class_name])
        cls = getattr(module, class_name)
        return cls(*args, **kwargs)
    except (OSError, ValueError, TypeError, ImportError, AttributeError, RuntimeError):
        if log_msg:
            getattr(logger, log_level, logger.warning)(log_msg, exc_info=True)
        return None


# ========== 硬校验门槛 — 防止非结构化输出入库 ==========


def _auto_remediate_fragment(fragment: KnowledgeFragment) -> bool:
    return _quality_auto_remediate_fragment(fragment, config_getter=get_config)


def _validate_fragment(fragment: KnowledgeFragment) -> List[str]:
    return _quality_validate_fragment(fragment, config_getter=get_config)


def _strict_validate_fragments(fragments: List[KnowledgeFragment]) -> Tuple[bool, List[str]]:
    return _quality_strict_validate_fragments(fragments, config_getter=get_config)


def _save_failed_distill(
    session_id: str,
    fragments: List[KnowledgeFragment],
    validation_errors: List[str],
    source: str = "",
    raw_response: str = "",
    exc_info: str = "",
    parse_metadata: Dict[str, Any] | None = None,
    severity: str = "high",
    database_dir: Path | None = None,
    producer: str = "conversation_distillation",
) -> Path:
    return record_distillation_failure(
        session_id=session_id,
        fragments=fragments,
        validation_errors=validation_errors,
        database_dir=database_dir or get_config().database_dir,
        source=source,
        raw_response=raw_response,
        exc_info=exc_info,
        parse_metadata=parse_metadata,
        producer=producer,
        severity=severity,
    ).artifact_path


bind_engine_namespace(lambda: globals())


class HttpApiHostAgentCaller(_BaseHttpApiHostAgentCaller):
    def __init__(
        self,
        timeout: int | None = None,
        api_chain: LLMApiChain | None = None,
        force_provider: str | None = None,
    ):
        super().__init__(
            timeout=timeout,
            api_chain=api_chain,
            force_provider=force_provider,
            config_getter=get_config,
            wiki_db_getter=_get_wiki_db,
        )


class LLMValueJudge(_BaseLLMValueJudge):
    def __init__(
        self, backend: DistillBackend | None = None, caller: HttpApiHostAgentCaller | None = None
    ):
        backend = backend or backend_from_caller(caller)
        super().__init__(backend=backend, prompt_budget_getter=_distill_prompt_budget)


class KnowledgeExtractor(_BaseKnowledgeExtractor):
    def __init__(
        self, backend: DistillBackend | None = None, caller: HttpApiHostAgentCaller | None = None
    ):
        backend = backend or backend_from_caller(caller)
        super().__init__(
            backend=backend,
            config_getter=get_config,
            prompt_budget_getter=_distill_prompt_budget,
            fragment_builder=_build_fragment,
            strict_validator=_strict_validate_fragments,
        )


# ========== 第5层：自检 ==========


# ========== 第6层：跨 Agent 关联 ==========


def generate_wiki_page(
    fragment: KnowledgeFragment,
    session_id: str,
    source: str = "",
    session_coverage: str = "",
    distill_input_mode: str = "",
    distill_prompt_version: str = "",
    covered_turn_range: str = "",
    truncated: bool = False,
    structured_output: Dict[str, Any] | None = None,
) -> str:
    """Compatibility wrapper around the extracted Wiki page renderer."""
    return _generate_wiki_page(
        fragment,
        session_id,
        source=source,
        session_coverage=session_coverage,
        distill_input_mode=distill_input_mode,
        distill_prompt_version=distill_prompt_version,
        covered_turn_range=covered_turn_range,
        truncated=truncated,
        structured_output=structured_output,
        is_stop_phrase=CrossAgentLinker._is_stop_phrase,
        wiki_dir_getter=_get_wiki_dir,
    )


# ========== 蒸馏引擎 ==========


class DistillationEngine(DistillationPipelineMixin):
    """七层蒸馏流水线引擎"""

    def __init__(
        self,
        wiki_base: str | None = None,
        caller: HttpApiHostAgentCaller | None = None,
        backend_factory: Callable[[], DistillBackend] | None = None,
        receipt_config: Any | None = None,
        event_bus: Any | None = None,
    ):
        self._runtime_receipt_config = receipt_config or get_config()
        self._runtime_receipt_config_is_explicit = receipt_config is not None
        self._event_bus = event_bus
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else _get_wiki_dir()
        self.inbox_dir = self.wiki_base / "00-Inbox"
        self._backends = DistillBackendBundle.build(
            caller=caller,
            backend_factory=backend_factory,
        )
        self._noise_filter = NoiseFilter()
        self._value_prejudgment = ValuePrejudgment()
        self._llm_judge = LLMValueJudge(backend=self._backends.judge)
        self._extractor = KnowledgeExtractor(backend=self._backends.extractor)
        self._self_check = DistillSelfCheck(link_probe_worker=get_link_probe_worker())
        self._cross_linker = CrossAgentLinker()  # 旧 Jaccard linker（L6 层保留）
        self._feedback_loop = DistillFeedbackLoop()
        self._fragment_merger = FragmentMerger()
        self._kia_linker = None  # 懒加载：core.kia.cross_agent_linker.CrossAgentLinker

    def _get_kia_linker(self):
        """懒加载新的跨 Agent 关联器（阶段三接入）。"""
        if self._kia_linker is None:
            try:
                from core.kia.cross_agent_linker import CrossAgentLinker as KiaCrossAgentLinker

                self._kia_linker = KiaCrossAgentLinker(wiki_root=self.wiki_base)
            except ImportError:
                logger.debug("KiaCrossAgentLinker not available", exc_info=True)
                self._kia_linker = False  # 标记为已尝试但失败
        return self._kia_linker if self._kia_linker is not False else None

    def _handle_api_error(self, e: DistillationAPIError, result: DistillationResult):
        """处理 LLM API 故障：暂停蒸馏、生成报告、弹窗。"""
        pause_distillation(
            reason=f"LLM API 故障: {e}",
            api_chain_desc=e.chain_desc,
            last_error=str(e),
        )
        try:
            generate_distillation_error_report(e)
        except (OSError, RuntimeError, ValueError, TypeError) as report_err:
            logger.warning("[Distillation] 生成错误报告失败: %s", report_err)
        result.judgment = "error"
        result.judgment_reason = f"API 故障: {e}"

    def _allocate_page_path(
        self,
        fragment: KnowledgeFragment,
        session_short: str,
        seen_slugs: set,
        *,
        result: Optional[DistillationResult] = None,
        fragment_hash: str = "",
    ) -> Tuple[str, Path]:
        """根据标题生成不重复的 page_id 与文件路径。"""
        title = fragment.title or fm_get(fragment.frontmatter, "name") or "untitled"
        return page_identity.allocate_revision_page_path(
            wiki_base=self.wiki_base,
            inbox_dir=self.inbox_dir,
            title=str(title),
            frontmatter=fragment.frontmatter,
            source_id=f"{session_short}:{result.input_revision if result else ''}",
            source_session=result.session_id if result else "",
            input_revision=result.input_revision if result else "",
            fragment_hash=fragment_hash,
            seen_slugs=seen_slugs,
        )

    def _write_single_page(
        self,
        fragment: KnowledgeFragment,
        result: DistillationResult,
        page_id: str,
        file_path: Path,
        fragment_hash: str = "",
    ) -> Optional[str]:
        """生成并原子写入单个 wiki 页面，返回文件路径或 None。"""
        page_content = _generate_wiki_page(
            fragment,
            result.session_id,
            source=result.source,
            session_coverage=result.session_coverage,
            distill_input_mode=getattr(result, "distill_input_mode", ""),
            distill_prompt_version=PROMPT_VERSION,
            truncated=getattr(result, "truncated", False),
            structured_output=self._redacted_persistence_payload(result.structured_output),
            input_revision=result.input_revision,
            fragment_hash=fragment_hash,
        )
        trusted, trusted_layer = submit_distillation_page_candidate(
            wiki_base=self.wiki_base,
            fragment=fragment,
            result=result,
            page_id=page_id,
            file_path=file_path,
            page_content=page_content,
        )
        if trusted_layer is not None:
            result.layer_results.append(trusted_layer)
        if trusted.intercepted:
            logger.info(
                "[trusted_push] enforce intercepted Hephaestus write: %s -> %s",
                page_id,
                trusted.proposal_id,
            )
            return None
        try:
            commit_trusted_markdown(
                trusted,
                target_path=file_path,
                content=page_content,
                material_action=trusted.material_action,
            )
            return str(file_path)
        except (OSError, ValueError, TypeError, ImportError, AttributeError, RuntimeError):
            logger.error("write_pages failed for %s", page_id, exc_info=True)
            return None

    def _persist_pages(
        self,
        result: DistillationResult,
        fragments: List[KnowledgeFragment],
    ) -> Tuple[List[str], List[Tuple[Path, KnowledgeFragment]]]:
        """把片段持久化为 wiki 页面文件。"""
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        written: List[str] = []
        file_fragments: List[Tuple[Path, KnowledgeFragment]] = []
        seen_slugs: set = set()
        session_short = self._session_short(result.session_id)

        for ordinal, fragment in enumerate(fragments):
            fragment_hash = page_identity.distillation_fragment_hash(fragment, ordinal)
            page_id, file_path = self._allocate_page_path(
                fragment,
                session_short,
                seen_slugs,
                result=result,
                fragment_hash=fragment_hash,
            )
            path = self._write_single_page(fragment, result, page_id, file_path, fragment_hash)
            if path:
                written.append(path)
                file_fragments.append((file_path, fragment))
        return written, file_fragments

    @staticmethod
    def _redacted_persistence_payload(value: Any) -> Any:
        """Return a detached PII/credential-redacted payload for durable sinks."""
        from core.privacy.content_redaction import redact_persistence_value

        return redact_persistence_value(value).value

    def _link_cross_agent(
        self,
        file_fragments: List[Tuple[Path, KnowledgeFragment]],
    ) -> None:
        """为已写入页面建立跨 Agent 关联并回写 frontmatter。"""
        linker = self._get_kia_linker()
        if not linker or not file_fragments:
            return
        for file_path, fragment in file_fragments:
            try:
                actions = linker.link_after_distill(file_path)
                if not actions:
                    continue
                refs = [
                    {
                        "page": str(a.to_page),
                        "reason": a.reason,
                        "similarity": round(a.similarity, 4),
                    }
                    for a in actions
                    if a.from_page == file_path
                ]
                fragment.frontmatter["cross_agent_refs"] = refs
                if refs:
                    self._update_frontmatter_field_trusted(
                        file_path,
                        "cross_agent_refs",
                        refs,
                    )
            except (OSError, ValueError, TypeError, ImportError, AttributeError, RuntimeError):
                logger.debug("Cross-agent linking failed for %s", file_path, exc_info=True)

    def _write_metrics_back(
        self,
        file_fragments: List[Tuple[Path, KnowledgeFragment]],
    ) -> None:
        """回写质量分、热度分、状态、知识阶段等 metrics 到 frontmatter。"""
        if not file_fragments:
            return
        try:
            from core.wiki_metrics import WikiMetrics

            metrics = WikiMetrics(wiki_dir=str(self.wiki_base))
            for file_path, fragment in file_fragments:
                rel_path = str(file_path.relative_to(self.wiki_base))
                page = metrics.get_page(rel_path)
                if page is None:
                    content = read_native_bytes(file_path).decode("utf-8")
                    metrics.assess_quality(rel_path, content)
                    metrics.upsert_page(
                        rel_path,
                        title=fragment.title,
                        source_count=1,
                        heat_score=1.0,
                        heat_level="warm",
                    )
                    page = metrics.get_page(rel_path)
                if page is None:
                    continue
                self._update_frontmatter_field_trusted(
                    file_path, "mnemos_quality_score", round(page.quality_score / 100, 2)
                )
                self._update_frontmatter_field_trusted(
                    file_path, "mnemos_heat_score", int(page.heat_score)
                )
                self._update_frontmatter_field_trusted(
                    file_path, "mnemos_usage_count", page.source_count
                )
                self._update_frontmatter_field_trusted(
                    file_path, "mnemos_last_scored", datetime.now().strftime("%Y-%m-%d")
                )
                self._update_frontmatter_field_trusted(
                    file_path, "质量分", round(page.quality_score, 1)
                )
                self._update_frontmatter_field_trusted(
                    file_path, "热度分", round(page.heat_score, 1)
                )
                self._update_frontmatter_field_trusted(file_path, "来源数量", page.source_count)
                self._update_frontmatter_field_trusted(
                    file_path, "最后评分日期", datetime.now().strftime("%Y-%m-%d")
                )
                from core.wiki_metrics import _status_to_display, _stage_to_display

                self._update_frontmatter_field_trusted(
                    file_path, "状态", _status_to_display(page.status)
                )
                self._update_frontmatter_field_trusted(
                    file_path, "知识阶段", _stage_to_display(page.knowledge_stage)
                )
            try:
                from core.wiki_metrics import write_mnemos_home

                write_mnemos_home(str(self.wiki_base))
            except ImportError:
                logger.debug("Mnemos home update failed", exc_info=True)
        except (OSError, ValueError, TypeError, ImportError, AttributeError, RuntimeError):
            logger.debug("Frontmatter metrics writeback failed", exc_info=True)

    def _emit_distill_events(
        self,
        result: DistillationResult,
        file_fragments: List[Tuple[Path, KnowledgeFragment]],
        written: List[str],
    ) -> None:
        """发射 distill_complete 与 wiki_page_updated 事件。"""
        revision_id = str(result.cognition_episode_revision_id or "")
        if not written and not file_fragments and not revision_id:
            return
        from core.mnemos_bus import Event, EventBus, get_event_bus
        from core.wiki_projection_lifecycle import (
            WikiProjectionLedger,
            resolve_wiki_projection_db_path,
        )

        event_bus = getattr(self, "_event_bus", None)
        owned_event_bus = False
        if event_bus is None:
            if getattr(self, "_runtime_receipt_config_is_explicit", False):
                event_bus = EventBus(config=self._runtime_receipt_config)
                owned_event_bus = True
            else:
                event_bus = get_event_bus()
        try:
            if written:
                for file_path, fragment in file_fragments:
                    event_bus.publish(
                        Event(
                            event_type="distill_complete",
                            source="distill",
                            payload={
                                "page_path": str(file_path),
                                "title": fragment.title,
                                "session_id": result.session_id,
                                "form": fragment.form,
                            },
                        )
                    )

            if file_fragments:
                ledger = WikiProjectionLedger(
                    resolve_wiki_projection_db_path(self._runtime_receipt_config)
                )
                for file_path, _ in file_fragments:
                    publish_wiki_page_updated(
                        file_path,
                        update_type="create",
                        ledger=ledger,
                        event_bus=event_bus,
                    )

            if revision_id:
                from core.cognitive.cognition_episode_dispatch import (
                    publish_cognition_episode_revision,
                )

                publish_cognition_episode_revision(
                    config=self._runtime_receipt_config,
                    event_bus=event_bus,
                    revision_id=revision_id,
                )
        finally:
            if owned_event_bus:
                event_bus.close()

    def _check_paused(self, result: DistillationResult) -> bool:
        """若蒸馏处于暂停状态则设置结果并返回 True。"""
        if is_distillation_paused():
            result.judgment = "paused"
            result.judgment_reason = "蒸馏处于暂停状态（API 故障恢复中）"
            return True
        return False

    def _run_noise_filter(self, result: DistillationResult, messages: List[Dict]) -> List[Dict]:
        """L1 噪音过滤；无有效消息时直接标记 skip。"""
        filtered, noise_stats = self._noise_filter.filter(messages)
        result.layer_results.append(
            PipelineLayerResult(1, "noise_filter", True, noise_stats),
        )
        if not filtered:
            result.judgment = "skip"
            result.judgment_reason = "全部消息为噪声"
        return filtered

    def _run_value_prejudgment(
        self,
        result: DistillationResult,
        filtered: List[Dict],
    ) -> Tuple[str, float]:
        """L2 价值预判；CERTAINLY_NO 时直接标记 skip。"""
        verdict, confidence = self._value_prejudgment.judge(filtered)
        result.prejudgment = verdict
        result.prejudgment_confidence = confidence
        result.layer_results.append(
            PipelineLayerResult(
                2,
                "value_prejudgment",
                True,
                {"verdict": verdict, "confidence": round(confidence, 3)},
            ),
        )
        if verdict == ValuePrejudgment.CERTAINLY_NO:
            result.judgment = "skip"
            result.judgment_reason = f"预判无价值 (confidence={confidence:.2f})"
        return verdict, confidence

    def _build_distillation_budget(
        self,
        filtered: List[Dict],
        *,
        run_context: str = "",
        subject_scope: tuple[str, str] | None = None,
        subject_scopes: List[tuple[str, str]] | None = None,
    ) -> Dict[str, Any]:
        """根据会话 token 长度与配置预计算三层蒸馏阈值。"""
        raw_tokens = sum(get_tokenizer().estimate(m.get("content", "")) for m in filtered)
        cfg = get_config()
        token_budget_total = int(
            cfg.get("distill.token_budget_total", DEFAULT_TOKEN_BUDGET_TOTAL)
            or DEFAULT_TOKEN_BUDGET_TOTAL
        )
        std_factor = float(cfg.get("distill.chunk_std_factor", 3) or 3)
        total_factor = float(cfg.get("distill.chunk_total_factor", 25) or 25)
        size_factor = float(cfg.get("distill.chunk_size_factor", 1.5) or 1.5)
        std_threshold = int(token_budget_total * std_factor)
        chunk_threshold = int(token_budget_total * total_factor)
        chunk_size = int(token_budget_total * size_factor)
        start_distillation_model_call_run(
            self._backends,
            self._fragment_merger,
            cfg,
            run_context,
            subject_scope=subject_scope,
            subject_scopes=subject_scopes,
        )
        return {
            "raw_tokens": raw_tokens,
            "cfg": cfg,
            "token_budget_total": token_budget_total,
            "std_threshold": std_threshold,
            "chunk_threshold": chunk_threshold,
            "chunk_size": chunk_size,
        }

    def _chunk_checkpoint_store(self, cfg: Any) -> Optional[ChunkCheckpointStore]:
        """Return the durable chunk checkpoint store when enabled."""
        if not bool(cfg.get("distill.chunk_checkpoint_enabled", True)):
            return None
        try:
            configured = cfg.get("distill.chunk_checkpoint_db_path")
            if configured:
                return ChunkCheckpointStore(Path(configured).expanduser())

            # Production uses the configured database dir. Tests and explicit
            # custom wiki_base runs should stay self-contained under wiki_base.
            cfg_wiki_dir = Path(getattr(cfg, "wiki_dir", "") or "").expanduser()
            if cfg_wiki_dir and self.wiki_base == cfg_wiki_dir:
                db_path = Path(cfg.database_dir) / "distillation_chunks.db"
            else:
                db_path = self.wiki_base / ".mnemos" / "distillation_chunks.db"
            return ChunkCheckpointStore(db_path)
        except (OSError, ValueError, TypeError, AttributeError):
            logger.warning("[Distillation] chunk checkpoint store unavailable", exc_info=True)
            return None

    def _run_llm_judge(
        self,
        result: DistillationResult,
        filtered: List[Dict],
        budget: Dict[str, Any],
        verdict: str,
        confidence: float,
    ) -> Tuple[str, str, float, bool]:
        """L3 LLM 语义判断；出现预算耗尽或 API 故障时返回 continue=False。"""
        if verdict == ValuePrejudgment.CERTAINLY_YES and confidence > 0.85:
            judgment, judgment_reason = "knowledge", "预判高价值，跳过LLM判断"
            judgment_confidence = confidence
        else:
            if self._backends.budget_exceeded:
                result.judgment = "budget_exceeded"
                result.judgment_reason = (
                    f"会话 LLM 成本预算已耗尽 (current={self._backends.session_cost:.6f})"
                )
                return "", "", 0.0, False
            raw_tokens = budget["raw_tokens"]
            std_threshold = budget["std_threshold"]
            chunk_threshold = budget["chunk_threshold"]
            chunk_size = budget["chunk_size"]
            if raw_tokens <= std_threshold:
                session_text = build_session_text(filtered, max_tokens=std_threshold)
            elif raw_tokens <= chunk_threshold:
                session_text = build_session_text(filtered, max_tokens=std_threshold * 2)
            else:
                session_text = build_session_text(filtered, max_tokens=chunk_size)
            try:
                judgment, judgment_reason, judgment_confidence = self._llm_judge.judge(
                    session_text,
                    result.session_id,
                )
            except DistillationAPIError as e:
                self._handle_api_error(e, result)
                return "", "", 0.0, False

        result.judgment = judgment
        result.judgment_reason = judgment_reason
        result.layer_results.append(
            PipelineLayerResult(
                3,
                "llm_value_judge",
                True,
                {"judgment": judgment, "confidence": round(judgment_confidence, 3)},
            ),
        )
        return judgment, judgment_reason, judgment_confidence, True

    def _safe_extract(
        self,
        request: ExtractionRequest,
        result: DistillationResult,
        prepared: PreparedExtractionPrompt | None = None,
    ) -> Optional[ExtractionOutcome]:
        """Run the typed extractor port and fail closed on protocol drift."""
        if self._backends.budget_exceeded:
            result.judgment = "budget_exceeded"
            result.judgment_reason = (
                f"会话 LLM 成本预算已耗尽 (current={self._backends.session_cost:.6f})"
            )
            return None
        active_prepared = prepared
        try:
            active_prepared = active_prepared or self._extractor.prepare_prompt(request)
            result.extraction_prompt_hash = active_prepared.prompt_hash
            outcome = self._extractor.extract(request, prepared=active_prepared)
            if not isinstance(outcome, ExtractionOutcome):
                raise TypeError("extractor_protocol_violation")
            self._record_extraction_response_evidence(result, list(outcome.backend_responses))
            if not self._verify_extraction_outcome(outcome, request.input_spec):
                result.judgment = "error"
                result.judgment_reason = "distillation extraction output contract failed"
                result.error = "extraction_contract_rejected"
                result.extraction_contract_valid = False
                _save_failed_distill(
                    result.session_id,
                    list(outcome.fragments),
                    self._contract_failure_errors(outcome),
                    source=result.source,
                    raw_response=result.raw_response,
                    parse_metadata=self._extraction_failure_metadata(
                        result,
                        request=request,
                        prepared=active_prepared,
                        correction_attempts=outcome.correction_count,
                        failure_path="contract_rejected",
                    ),
                    database_dir=Path(self._runtime_receipt_config.database_dir),
                )
                return None
            return outcome
        except (ArtifactCatalogRejectedError, SourceAuthorityCatalogRejectedError) as error:
            result.judgment = "error"
            authority_rejected = isinstance(error, SourceAuthorityCatalogRejectedError)
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
            _save_failed_distill(
                result.session_id,
                [],
                [
                    f"{'source authority' if authority_rejected else 'artifact'} "
                    f"catalog rejected: {code}"
                    for code in error.rejection_codes
                ],
                source=result.source,
                raw_response="",
                parse_metadata=self._extraction_failure_metadata(
                    result,
                    request=request,
                    prepared=active_prepared,
                    failure_path=result.error,
                ),
                database_dir=Path(self._runtime_receipt_config.database_dir),
            )
            return None
        except DistillationAPIError as e:
            if isinstance(e.response_evidence, DistillBackendResponse):
                self._record_extraction_response_evidence(result, [e.response_evidence])
            self._handle_api_error(e, result)
            result.error = "distillation_api_error"
            result.extraction_contract_valid = False
            _save_failed_distill(
                result.session_id,
                [],
                [f"distillation model response unavailable: {e}"],
                source=result.source,
                raw_response=result.raw_response,
                parse_metadata=self._extraction_failure_metadata(
                    result,
                    request=request,
                    prepared=active_prepared,
                    failure_path="provider_or_parse_failure",
                ),
                database_dir=Path(self._runtime_receipt_config.database_dir),
            )
            return None
        except (AttributeError, TypeError, ValueError):
            result.judgment = "error"
            result.judgment_reason = "distillation extractor protocol violation"
            result.error = "extractor_protocol_violation"
            result.extraction_contract_valid = False
            _save_failed_distill(
                result.session_id,
                [],
                ["distillation extractor protocol violation"],
                source=result.source,
                raw_response=result.raw_response,
                parse_metadata=self._extraction_failure_metadata(
                    result,
                    request=request,
                    prepared=active_prepared,
                    failure_path="extractor_protocol_violation",
                ),
                database_dir=Path(self._runtime_receipt_config.database_dir),
            )
            return None

    def _extract_standard(
        self,
        result: DistillationResult,
        filtered: List[Dict],
        budget: Dict[str, Any],
    ) -> Tuple[Optional[List[KnowledgeFragment]], Dict[str, Any]]:
        """标准路径：单条会话文本一次性提取片段。"""
        coverage_meta: Dict[str, Any] = {}
        session_text = build_session_text(
            filtered, max_tokens=budget["std_threshold"], out_meta=coverage_meta, lossless=True
        )
        result.analysis_type = "standard"
        input_spec = self._input_spec_for_extraction(
            result, session_text, result.analysis_type, filtered
        )
        outcome = self._safe_extract(
            ExtractionRequest(
                session_text=session_text,
                analysis_type=result.analysis_type,
                input_spec=input_spec,
            ),
            result,
        )
        if outcome is None:
            return None, {}
        self._apply_admitted_outcome(result, input_spec, outcome)
        return list(outcome.fragments), coverage_meta

    def _extract_chunked(
        self,
        result: DistillationResult,
        filtered: List[Dict],
        budget: Dict[str, Any],
    ) -> Tuple[Optional[List[KnowledgeFragment]], List[Dict[str, Any]]]:
        """Delegate resumable extraction and session aggregation to its deep module."""
        return ChunkedExtractionCoordinator().extract(self, result, filtered, budget)

    def _assemble_coverage(
        self,
        result: DistillationResult,
        filtered: List[Dict],
        all_fragments: List[KnowledgeFragment],
        budget: Dict[str, Any],
        coverage_meta: Dict[str, Any],
        chunk_infos: List[Dict[str, Any]],
    ) -> None:
        """根据标准/分块路径组装 session_coverage 与 truncated 标记。"""
        raw_tokens = budget["raw_tokens"]
        std_threshold = budget["std_threshold"]

        if raw_tokens > std_threshold and chunk_infos:
            result.session_coverage = (
                f"分块蒸馏（共 {len(chunk_infos)} 个 chunk，"
                f"合并后 {len(all_fragments)} 个知识）"
            )
            result.session_coverage += "；" + "; ".join(
                f"chunk{c['chunk_index']}: turn{c['covered_turn_range']}" for c in chunk_infos
            )
            return

        if raw_tokens <= std_threshold:
            if coverage_meta.get("truncated"):
                result.session_coverage = (
                    f"部分覆盖（共 {coverage_meta['total_turns']} 轮，"
                    f"保留开头 {coverage_meta['head_turns']} 轮 + 结尾 {coverage_meta['tail_turns']} 轮，"
                    f"中间 {coverage_meta['omitted_turns']} 轮省略）"
                )
                result.truncated = True
            else:
                result.session_coverage = (
                    f"完整覆盖（共 {coverage_meta.get('total_turns', len(filtered))} 轮）"
                )
                result.truncated = False
            result.distill_input_mode = coverage_meta.get("distill_input_mode", "unknown")

        msg_trunc = coverage_meta.get("message_truncations", [])
        truncated_msgs = [m for m in msg_trunc if m["truncated"]]
        if truncated_msgs and raw_tokens <= std_threshold:
            trunc_summary = ", ".join(
                f"turn{m['turn']}({m['role']}): {m['original_tokens']}→{m['kept_tokens']} tokens"
                for m in truncated_msgs[:5]
            )
            if len(truncated_msgs) > 5:
                trunc_summary += f" 等共 {len(truncated_msgs)} 条消息被截断"
            result.session_coverage += f"；消息截断: {trunc_summary}"

    def _run_knowledge_extraction(
        self,
        result: DistillationResult,
        filtered: List[Dict],
        budget: Dict[str, Any],
    ) -> Optional[List[KnowledgeFragment]]:
        """L4 知识提取（标准/分块）与片段合并。"""
        if budget["raw_tokens"] <= budget["std_threshold"]:
            all_fragments, coverage_meta = self._extract_standard(result, filtered, budget)
            chunk_infos: List[Dict[str, Any]] = []
        else:
            all_fragments, chunk_infos = self._extract_chunked(result, filtered, budget)
            coverage_meta = {}

        if all_fragments is None:
            return None

        if all_fragments:
            all_fragments = self._fragment_merger.merge(all_fragments)

        self._assemble_coverage(result, filtered, all_fragments, budget, coverage_meta, chunk_infos)

        result.fragments = all_fragments
        if not all_fragments:
            legal_skip = bool(
                result.extraction_contract_valid and result.extraction_judgment == "skip"
            )
            result.layer_results.append(
                PipelineLayerResult(
                    4,
                    "knowledge_extraction",
                    legal_skip,
                    {
                        "fragment_count": 0,
                        "chunks": chunk_infos,
                        "legal_skip": legal_skip,
                    },
                ),
            )
            if legal_skip:
                result.judgment = "skip"
                structured = result.structured_output or {}
                result.judgment_reason = str(
                    structured.get("skip_reason") or "提取无长期可复用知识"
                )
                return None
            result.judgment = "error"
            result.judgment_reason = "non-skip extraction produced no admitted fragments"
            result.error = "non_skip_empty_extraction"
            return None
        result.layer_results.append(
            PipelineLayerResult(
                4,
                "knowledge_extraction",
                True,
                {"fragment_count": len(all_fragments), "chunks": chunk_infos},
            ),
        )
        return all_fragments

    def _run_self_check(
        self,
        result: DistillationResult,
        all_fragments: List[KnowledgeFragment],
        filtered: List[Dict],
    ) -> bool:
        """L5 自检；fatal 时返回 False 阻断后续链路。"""
        check_passed, issues = self._self_check.check(all_fragments, filtered)
        result.self_check_passed = check_passed
        result.self_check_issues = issues
        result.self_check_severity = max_self_check_severity(issues)
        result.layer_results.append(
            PipelineLayerResult(
                5,
                "self_check",
                check_passed,
                {"issues": issues[:5], "severity": result.self_check_severity},
            ),
        )
        if result.self_check_severity == "fatal":
            result.judgment_reason = (
                result.judgment_reason or "自检发现 fatal 问题，阻断后续链路和正式入库"
            )
            return False
        return True

    def _run_cross_linking(
        self,
        result: DistillationResult,
        all_fragments: List[KnowledgeFragment],
    ) -> List[KnowledgeFragment]:
        """L6 跨 Agent 关联。"""
        linked_fragments = self._cross_linker.link(all_fragments)
        result.fragments = linked_fragments
        # This is the sole production issuance point for the post-admission
        # fragment capability.  Formatting, quality filtering and linking may
        # mutate/reorder the same live objects, but an external router caller
        # cannot replace them after this point.
        aggregate = result.chunk_aggregate
        result.fragment_route_capability = FragmentRouteCapability(
            extraction_output_hash=result.extraction_output_hash,
            input_spec_hash=(
                result.input_spec.input_spec_hash
                if isinstance(result.input_spec, DistillInputSpec)
                else ""
            ),
            fragments=tuple(linked_fragments),
            chunk_root_hashes=(
                tuple(chunk.canonical_output_hash for chunk in aggregate.ordered_chunks)
                if aggregate is not None
                else ()
            ),
            chunk_aggregate_contract_hash=(
                aggregate.aggregate_contract_hash if aggregate is not None else ""
            ),
        )
        result.cross_agent_links = [link for f in linked_fragments for link in f.cross_agent_links]
        result.layer_results.append(
            PipelineLayerResult(
                6, "cross_agent_linking", True, {"links": len(result.cross_agent_links)}
            ),
        )
        return linked_fragments

    def _run_feedback_loop(self, result: DistillationResult) -> None:
        """L7 反馈循环。"""
        feedback_signals = self._feedback_loop.evaluate(result)
        result.layer_results.append(
            PipelineLayerResult(
                DISTILLATION_ENGINE_PROCESS_FEEDBACK_LOOP,
                "feedback_loop",
                True,
                {"signals": len(feedback_signals)},
            ),
        )

    def process(
        self, session_id: str, messages: List[Dict], meta: Dict | None = None
    ) -> DistillationResult:
        """运行七层蒸馏流水线。

        Args:
            session_id: 会话 ID
            messages: 消息列表 [{role, content}, ...]
            meta: 元数据 {source, model, cwd, ...}

        Returns:
            DistillationResult 包含所有层的执行结果
        """
        result = DistillationResult(session_id=session_id)
        meta = meta or {}
        result.source = meta.get("source", "")
        result.raw_completeness = self._raw_completeness_from_meta(meta)
        result.input_revision = str(meta.get("input_revision") or "")
        try:
            result.raw_event_refs = raw_provenance.normalize_raw_event_refs(
                meta.get("raw_event_refs")
            )
        except (TypeError, ValueError):
            result.judgment = "error"
            result.judgment_reason = "invalid immutable raw provenance metadata"
            return result
        artifact_refs = meta.get("artifact_refs")
        result.artifact_refs = list(artifact_refs) if isinstance(artifact_refs, list) else []
        result.source_authority_context = {
            key: str(meta.get(key) or "")
            for key in ("source_authority", "source_authority_purpose")
            if str(meta.get(key) or "")
        }

        if self._check_paused(result):
            return result

        filtered = self._run_noise_filter(result, messages)
        if not filtered:
            return result

        verdict, confidence = self._run_value_prejudgment(result, filtered)
        pipeline_receipts.record_distillation_prejudgment(
            self._runtime_receipt_config, session_id=session_id, meta=meta, verdict=verdict
        )
        if verdict == ValuePrejudgment.CERTAINLY_NO:
            return result
        root_subject_scope = (
            ("session", str(session_id))
            if str(session_id or "").strip()
            else ("source", "distillation_engine")
        )
        entry_subject_scopes = [root_subject_scope]
        # Every immutable raw revision whose visible bytes enter this
        # distillation request is attached to the individual provider entry.
        # A session run is only the shared budget owner; it is not sufficient
        # for deleting a raw asset that shares a batch/run with another one.
        entry_subject_scopes.extend(
            ("raw_event_id", str(ref["revision_id"]))
            for ref in result.raw_event_refs
            if str(ref.get("revision_id") or "").strip()
        )
        budget = self._build_distillation_budget(
            filtered,
            run_context=f"{session_id}:{result.input_revision}",
            subject_scope=root_subject_scope,
            subject_scopes=entry_subject_scopes,
        )
        judgment, _, _, ok = self._run_llm_judge(result, filtered, budget, verdict, confidence)
        if not ok:
            return result

        if judgment not in {"knowledge", "skill"}:
            return result

        all_fragments = self._run_knowledge_extraction(result, filtered, budget)
        if all_fragments is None:
            return result
        if not resolve_cognition_judgment(result, judgment):
            return result
        raw_provenance.attach_raw_provenance(result, all_fragments)

        if not self._run_self_check(result, all_fragments, filtered):
            return result

        self._run_cross_linking(result, all_fragments)
        self._run_feedback_loop(result)

        # NOTE: knowledge_distilled 事件在 write_pages() 后由 distill_and_write() /
        # HephaestusWorker 统一发射，以确保 payload 包含完整的 wiki_pages 和
        # kg_input。process() 本身不直接发射该事件。
        return result

    def write_pages_with_receipt(self, result: DistillationResult) -> DistillationWriteReceipt:
        return pipeline_receipts.persist_distillation_with_receipt(
            self,
            result,
            self._runtime_receipt_config,
        )

    def write_pages(self, result: DistillationResult) -> List[str]:
        """Compatibility surface returning only committed paths; prefer the typed receipt."""
        return list(self.write_pages_with_receipt(result).written_pages)

    def _update_frontmatter_field_trusted(self, file_path: Path, key: str, value) -> None:
        self._update_frontmatter_field(file_path, key, value, wiki_base=self.wiki_base)

    @staticmethod
    def _update_frontmatter_field(
        file_path: Path,
        key: str,
        value,
        *,
        wiki_base: Path | None = None,
    ) -> None:
        """Compatibility surface for the trusted frontmatter mutation helper."""
        update_frontmatter_field(file_path, key, value, wiki_base=wiki_base or file_path.parent)

    def _extract_skill_suggestion(
        self,
        result: DistillationResult,
        asset_payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Derive an optional proposal from an already committed full asset.

        The compatibility name remains while callers migrate, but this method
        no longer owns cognition and never rebuilds an anonymous truncated
        session from raw messages.
        """
        if self._backends.budget_exceeded:
            raise ValueError("cognitive_proposal_budget_exceeded")
        session_text = json.dumps(
            asset_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        session = Session(
            id=result.session_id,
            messages=[{"role": "user", "content": session_text}],
            agent_name=result.source or result.input_spec.source_agent,
        )
        task = DistillTask(
            task_type="skill_suggestion",
            session=session,
            session_type="general",
            budget_config=_distill_prompt_budget(),
            preformatted=True,
        )
        prompt = PromptBuilder().build(task)
        proposal = self._backends.skill.call(prompt, expect_json=True).require_mapping()
        return dict(proposal)

    def _parse_fragments(self, data: Dict) -> List[KnowledgeFragment]:
        """从解析后的 JSON 数据中提取知识片段（供外部复用）。

        委托模块级 _build_fragment，保持与 KnowledgeExtractor 的解析逻辑一致。
        """
        fragments = []
        for frag_data in data.get("fragments", []):
            try:
                fragment = _build_fragment(frag_data)
                if fragment is not None:
                    fragments.append(fragment)
            except (OSError, ValueError, TypeError, ImportError, AttributeError, RuntimeError):
                logging.getLogger(__name__).warning(
                    "Caught unexpected error at distillation_engine.py", exc_info=True
                )
                continue
        return fragments


def distill_session(
    session_id: str, messages: List[Dict], wiki_base: str | None = None, meta: Dict | None = None
) -> DistillationResult:
    """便捷函数：蒸馏单个 session"""
    engine = DistillationEngine(wiki_base=wiki_base)
    return engine.process(session_id, messages, meta=meta or {})


def _build_knowledge_distilled_payload(
    session_id: str, result: DistillationResult, written: List[str]
) -> Dict:
    """构建 knowledge_distilled 事件的 payload。"""
    entities = []
    relations = []
    for frag in result.fragments:
        entities.extend(frag.keywords or [])
        entities.extend(frag.related_concepts or [])
        # cross_agent_links（传统反向链接）
        for link in frag.cross_agent_links or []:
            relations.append(
                {
                    "source": frag.title,
                    "target": link,
                    "type": "related_to",
                    "confidence": 0.5,
                }
            )
        # 结构化关联上下文（ADR-019）
        for rel in frag.relations or []:
            relations.append(
                {
                    "source": frag.title,
                    "target": rel.get("target", "").strip("[]"),
                    "type": rel.get("type", "related_to"),
                    "context": rel.get("context", ""),
                    "confidence": 0.7,
                }
            )
    # 保序去重，兼容不可哈希项
    try:
        entities = list(dict.fromkeys(entities))
    except TypeError:
        logger.warning("[distillation_engine] TypeError suppressed", exc_info=True)
    return {
        "session_id": session_id,
        "wiki_pages": written,
        "kg_input": {
            "entities": entities,
            "relations": relations,
        },
    }


def _emit_knowledge_distilled(
    session_id: str, result: DistillationResult, written: List[str]
) -> None:
    """发射 knowledge_distilled 事件（公共函数，供所有写页入口复用）"""
    if not written or not result.fragments:
        return
    try:
        payload = _build_knowledge_distilled_payload(session_id, result, written)

        from core.mnemos_bus import publish_event

        publish_event("knowledge_distilled", "distill", payload)

    except (OSError, ValueError, TypeError, ImportError, AttributeError, RuntimeError):
        logging.getLogger(__name__).warning("knowledge_distilled event emit failed", exc_info=True)


def distill_and_write(
    session_id: str, messages: List[Dict], wiki_base: str | None = None, meta: Dict | None = None
) -> Tuple[DistillationResult, List[str]]:
    """便捷函数：蒸馏并写入 Wiki"""
    engine = DistillationEngine(wiki_base=wiki_base)
    result = engine.process(session_id, messages, meta=meta or {})
    written = engine.write_pages(result)

    if written:
        trigger_embedding_index_refresh(wiki_base)

    return result, written
