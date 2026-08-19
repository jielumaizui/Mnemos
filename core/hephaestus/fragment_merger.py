# -*- coding: utf-8 -*-
"""
FragmentMerger — 跨 chunk 知识片段合成器

把分块蒸馏产生的多个局部片段，按主题聚类后通过 LLM 合成（或规则回退）为
完整、连贯的知识页面，避免同一话题被拆成多个 Wiki 页面。

设计约束：
- 使用与蒸馏相同的 LLM API chain（模型 / 地址 / key / 回退一致）。
- 拥有独立的 HttpApiHostAgentCaller 实例，可单独开关、调整超时/重试。
- LLM 失败时自动回退到规则合并，保证不丢失片段。
"""

from __future__ import annotations

import json
import logging
import inspect
import time
import uuid
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Optional, Set

from core.hephaestus.response_budget import resolve_response_token_limits
from core.telemetry.prompt_call_log import (
    ModelCallBudgetExceeded,
    ModelCallLedger,
    ModelCallSubjectFrozen,
    metered_provider_usage,
)
from core.telemetry.provider_request import (
    canonical_chat_input,
    non_redirecting_openai_client,
    safe_provider_error_category,
    utf8_token_upper_bound,
)

logger = logging.getLogger(__name__)


# 延迟导入 distillation_engine 中的符号，避免循环依赖
_distillation_engine = None


def _get_distillation_engine():
    global _distillation_engine
    if _distillation_engine is None:
        from core.hephaestus import distillation_engine as _distillation_engine
    return _distillation_engine


def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    return _get_distillation_engine().extract_json(raw)  # type: ignore[no-any-return]


def _strict_validate_fragments(fragments: List[Any]) -> tuple:
    # type: ignore[no-any-return]
    return _get_distillation_engine()._strict_validate_fragments(fragments)  # type: ignore[no-any-return]  # noqa: E501


class FragmentMerger:
    """跨 chunk 片段合成器。"""

    DEFAULT_THRESHOLD = 0.4
    DEFAULT_TIMEOUT = 120
    DEFAULT_MAX_RETRIES = 1

    def __init__(
        self,
        api_chain=None,
        threshold: Optional[float] = None,
        enable_llm: Optional[bool] = None,
        timeout: int | None = None,
        max_retries: int | None = None,
    ):
        from core.config import get_config
        from core.llm_config import resolve_llm_api_chain

        self._cfg = get_config()
        self._api_chain = api_chain or resolve_llm_api_chain(self._cfg)
        self._threshold = (
            threshold
            if threshold is not None
            else self._cfg.get("distill.fragment_merge_threshold", self.DEFAULT_THRESHOLD)
        )
        self._enable_llm = (
            enable_llm
            if enable_llm is not None
            else self._cfg.get("distill.enable_llm_fragment_merge", True)
        )
        self._timeout = timeout or self.DEFAULT_TIMEOUT
        self._max_retries = max_retries if max_retries is not None else self.DEFAULT_MAX_RETRIES
        self._model_call_ledger: ModelCallLedger | None = None
        self._model_call_run_id = ""
        self._model_call_subject_scopes: tuple[tuple[str, str], ...] = ()

    def bind_model_call_run(
        self,
        ledger: ModelCallLedger,
        run_id: str,
        *,
        subject_scopes: Iterable[tuple[str, str]] | None = None,
    ) -> None:
        """Use the same durable budget/run as the surrounding distillation."""
        self._model_call_ledger = ledger
        self._model_call_run_id = ledger.start_run(run_id)
        if subject_scopes is not None:
            self._model_call_subject_scopes = tuple(subject_scopes)

    def _ledger_for_call(self) -> tuple[ModelCallLedger, str]:
        ledger = self._model_call_ledger
        if ledger is None:
            ledger = ModelCallLedger.for_config(self._cfg)
            self._model_call_ledger = ledger
        if not self._model_call_run_id:
            self._model_call_run_id = ledger.start_run(
                f"standalone-merge:{uuid.uuid4().hex}",
                subject_scope=("source", "standalone_fragment_merger"),
            )
        return ledger, self._model_call_run_id

    def checkpoint_identity(self) -> Dict[str, Any]:
        """Return the effective merge contract without API credentials."""
        return {
            "component": f"{type(self).__module__}.{type(self).__qualname__}",
            "threshold": self._threshold,
            "enable_llm": self._enable_llm,
            "timeout": self._timeout,
            "max_retries": self._max_retries,
            "api_chain": [
                {
                    "provider": str(cfg.provider or ""),
                    "model": str(cfg.model or ""),
                    "base_url": str(cfg.base_url or ""),
                    "timeout": cfg.timeout,
                    "cost_level": str(cfg.cost_level or ""),
                }
                for cfg in self._api_chain.all_configs
            ],
        }

    # ==================== 公共入口 ====================

    def merge(self, fragments: List[Any]) -> List[Any]:
        """对片段列表进行聚类，并合并同类片段为完整知识。"""
        if not fragments:
            return []

        clusters = self.cluster_fragments(fragments, self._threshold)
        merged: List[Any] = []
        for cluster in clusters:
            if len(cluster) == 1:
                merged.append(cluster[0])
                continue
            try:
                if self._enable_llm:
                    result = self._llm_merge_cluster(cluster)
                    if result is not None:
                        merged.append(result)
                        continue
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError) as e:
                logger.warning(
                    "[FragmentMerger] LLM synthesis failed; using rule fallback: category=%s",
                    safe_provider_error_category(e),
                )
            # 回退：规则合并
            merged.append(self._rule_merge_cluster(cluster))
        return merged

    def cluster_fragments(
        self, fragments: List[Any], threshold: float | None = None
    ) -> List[List[Any]]:
        """基于标题 + keywords 的 Jaccard 相似度做贪婪聚类。"""
        threshold = threshold if threshold is not None else self._threshold
        clusters: List[List[Any]] = []
        for frag in fragments:
            text = self._cluster_text(frag)
            placed = False
            for cluster in clusters:
                rep_text = self._cluster_text(cluster[0])
                if self._jaccard_similarity(text, rep_text) >= threshold:
                    cluster.append(frag)
                    placed = True
                    break
            if not placed:
                clusters.append([frag])
        return clusters

    # ==================== 合并实现 ====================

    def _llm_merge_cluster(self, cluster: List[Any]) -> Optional[Any]:
        """调用 LLM 把同一聚类的多个片段合成一条完整知识。"""
        raw = self._call_llm_for_cluster(self._build_merge_prompt(cluster), len(cluster))
        if not raw:
            return None
        parsed = _extract_json(raw)
        if not isinstance(parsed, dict):
            return None
        frag = self._dict_to_fragment(parsed)
        if frag is None:
            return None
        if not self._llm_preserves_cluster_metadata(frag, cluster):
            logger.debug(
                "[FragmentMerger] LLM 合成结果丢失输入元数据: "
                "category=lossy_fragment_metadata"
            )
            return None
        passed, errors = _strict_validate_fragments([frag])
        if not passed:
            # Validation diagnostics can quote LLM output.  That output is
            # provider-visible and must not enter general logs.
            del errors
            logger.debug(
                "[FragmentMerger] LLM 合成结果未通过硬校验: category=invalid_fragment_output"
            )
            return None
        return frag

    def _call_llm_for_cluster(self, prompt: str, fragment_count: int) -> Optional[str]:
        """Call LLM with merge context while keeping monkeypatched tests compatible."""
        try:
            params = inspect.signature(self._call_llm).parameters
            accepts_fragment_count = "fragment_count" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
        except (TypeError, ValueError):
            accepts_fragment_count = True

        if accepts_fragment_count:
            return self._call_llm(prompt, fragment_count=fragment_count)
        return self._call_llm(prompt)

    @staticmethod
    def _select_representative(cluster: List[Any]) -> Any:
        """选标题：优先带问题词或最长的标题。"""

        def _title_score(frag):
            title = frag.title or ""
            score = len(title)
            if any(
                w in title
                for w in (
                    "为什么",
                    "如何",
                    "怎么",
                    "什么",
                    "方案",
                    "方法",
                    "原则",
                    "决策",
                    "解决",
                    "原因",
                    "本质",
                )
            ):
                score += 50
            return score

        return max(cluster, key=_title_score)

    @staticmethod
    def _combine_texts(texts: List[str]) -> str:
        """Append complete source blocks without normalising their visible bytes.

        A line-level set looks attractive for prose, but it corrupts repeated
        code lines, numbered procedures, causal timelines, and intentional
        blank lines.  The merger may add a separator *between* source blocks;
        it must never split, trim, or de-duplicate the blocks themselves.
        """
        return "\n\n".join("" if text is None else str(text) for text in texts)

    def _union_list(self, cluster: List[Any], field_name: str) -> List[str]:
        """列表字段取并集。"""
        result: List[str] = []
        seen: Set[str] = set()
        for f in cluster:
            for item in getattr(f, field_name, None) or []:
                s = str(item)
                if s and s not in seen:
                    seen.add(s)
                    result.append(s)
        return result

    @staticmethod
    def _canonical_collection_key(item: Any) -> str:
        """Return a stable equality key without flattening structured values."""
        try:
            return json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            return f"{type(item).__module__}.{type(item).__qualname__}:{item!r}"

    def _union_structured_list(self, cluster: List[Any], field_name: str) -> List[Any]:
        """Take an ordered union while retaining structured collection items."""
        result: List[Any] = []
        seen: Set[str] = set()
        for fragment in cluster:
            values = getattr(fragment, field_name, None) or []
            if not isinstance(values, (list, tuple)):
                continue
            for item in values:
                key = self._canonical_collection_key(item)
                if key in seen:
                    continue
                seen.add(key)
                result.append(item)
        return result

    def _merged_self_check_metadata(self, cluster: List[Any]) -> Dict[str, Any]:
        """Conservatively retain every input self-check signal."""
        issues = self._union_list(cluster, "self_check_issues")
        severity_rank = {"ok": 0, "warning": 1, "fatal": 2}
        severities = []
        for fragment in cluster:
            severity = str(getattr(fragment, "self_check_severity", "ok") or "ok").lower()
            severities.append(severity if severity in severity_rank else "warning")
        severity = max(severities or ["ok"], key=lambda value: severity_rank[value])
        passed = (
            all(bool(getattr(fragment, "self_check_passed", True)) for fragment in cluster)
            and not issues
            and severity == "ok"
        )
        return {
            "self_check_passed": passed,
            "self_check_issues": issues,
            "self_check_severity": severity,
        }

    def _preserved_cluster_metadata(self, cluster: List[Any]) -> Dict[str, Any]:
        """Build metadata that a lossy LLM synthesis is not allowed to replace."""
        ai_expansions = []
        for fragment in cluster:
            expansion = getattr(fragment, "ai_expansion", "")
            if expansion is not None and expansion != "":
                ai_expansions.append(expansion)
        metadata = {
            "boundaries": self._merge_boundaries(cluster),
            "anti_patterns": self._union_list(cluster, "anti_patterns"),
            "related_concepts": self._union_list(cluster, "related_concepts"),
            "claim_ids": self._union_list(cluster, "claim_ids"),
            "keywords": self._union_list(cluster, "keywords"),
            "relations": self._union_structured_list(cluster, "relations"),
            "cross_agent_links": self._union_list(cluster, "cross_agent_links"),
            "ai_expansion": self._combine_texts(ai_expansions),
        }
        metadata.update(self._merged_self_check_metadata(cluster))
        return metadata

    def _llm_preserves_cluster_metadata(self, fragment: Any, cluster: List[Any]) -> bool:
        """Require the synthesis result to retain every lossless input field."""
        expected = self._preserved_cluster_metadata(cluster)
        for field, value in expected.items():
            if getattr(fragment, field, None) != value:
                return False

        expected_frontmatter = self._merge_frontmatter(cluster)
        for key in (
            "置信度",
            "confidence",
            "置信度审计值",
            "raw_event_refs",
            "chunk_source_spans",
        ):
            if (
                key in expected_frontmatter
                and fragment.frontmatter.get(key) != expected_frontmatter[key]
            ):
                return False
        if not self._contains_ordered_blocks(
            str(getattr(fragment, "core_content", "") or ""),
            [str(getattr(item, "core_content", "") or "") for item in cluster],
        ):
            return False
        if not self._contains_ordered_blocks(
            str(getattr(fragment, "background", "") or ""),
            [str(getattr(item, "background", "") or "") for item in cluster],
        ):
            return False
        return True

    @staticmethod
    def _contains_ordered_blocks(combined: str, blocks: List[str]) -> bool:
        """Prove that every complete visible block survives, including repeats."""

        cursor = 0
        for block in blocks:
            if not block:
                continue
            index = combined.find(block, cursor)
            if index < 0:
                return False
            cursor = index + len(block)
        return True

    @staticmethod
    def _merge_boundaries(cluster: List[Any]) -> Dict[str, str]:
        """合并 boundaries。"""
        boundaries_list = [f.boundaries for f in cluster if f.boundaries]
        merged_boundaries: Dict[str, str] = {}
        for b in boundaries_list:
            for key, value in b.items():
                if not value:
                    continue
                if key not in merged_boundaries:
                    merged_boundaries[key] = str(value)
                else:
                    merged_boundaries[key] += f"\n{value}"
        return merged_boundaries

    @staticmethod
    def _merge_frontmatter(cluster: List[Any]) -> Dict[str, Any]:
        """Merge frontmatter with conservative, auditable confidence values."""
        merged_frontmatter: Dict[str, Any] = {}
        confidences: List[float] = []
        confidence_keys: List[str] = []
        summaries: List[str] = []
        domains: Set[str] = set()
        provenance_keys = ("raw_event_refs", "chunk_source_spans")
        for f in cluster:
            fm = f.frontmatter or {}
            for k, v in fm.items():
                if k in provenance_keys:
                    continue
                if (
                    k in ("置信度", "confidence")
                    and isinstance(v, (int, float))
                    and not isinstance(v, bool)
                ):
                    confidences.append(float(v))
                    if k not in confidence_keys:
                        confidence_keys.append(k)
                    continue
                if k in ("摘要", "summary") and isinstance(v, str):
                    summaries.append(v)
                    continue
                if k in ("领域", "domain") and isinstance(v, str):
                    domains.add(v)
                    continue
                if k not in merged_frontmatter:
                    merged_frontmatter[k] = v

        if confidences:
            # Independent evidence and incompatible hypotheses cannot be made
            # more trustworthy by arithmetic averaging.  Keep each source
            # value for audit and expose the least-confident value as the
            # merged presentation value.
            conservative_confidence = min(confidences)
            for key in confidence_keys:
                merged_frontmatter[key] = conservative_confidence
            merged_frontmatter.setdefault("置信度", conservative_confidence)
            merged_frontmatter["置信度审计值"] = list(confidences)
        if summaries:
            merged_frontmatter["摘要"] = max(summaries, key=len)
        if domains:
            merged_frontmatter["领域"] = ", ".join(sorted(domains))
        for key in provenance_keys:
            values = FragmentMerger._union_frontmatter_structured_list(cluster, key)
            if values:
                merged_frontmatter[key] = values
        return merged_frontmatter

    @staticmethod
    def _union_frontmatter_structured_list(cluster: List[Any], key: str) -> List[Any]:
        """Return an ordered, deep-copied union of fragment provenance values."""
        result: List[Any] = []
        seen: Set[str] = set()
        for fragment in cluster:
            frontmatter = getattr(fragment, "frontmatter", None) or {}
            values = frontmatter.get(key) if isinstance(frontmatter, dict) else None
            if not isinstance(values, (list, tuple)):
                continue
            for value in values:
                canonical_key = FragmentMerger._canonical_collection_key(value)
                if canonical_key in seen:
                    continue
                seen.add(canonical_key)
                result.append(deepcopy(value))
        return result

    def _rule_merge_cluster(self, cluster: List[Any]) -> Any:
        """LLM 不可用时的规则合并：字段并集 + 内容顺序拼接。"""
        KnowledgeFragment = _get_distillation_engine().KnowledgeFragment

        representative = self._select_representative(cluster)
        title = representative.title or ""

        core_contents = [getattr(f, "core_content", "") for f in cluster]
        backgrounds = [getattr(f, "background", "") for f in cluster]
        metadata = self._preserved_cluster_metadata(cluster)

        return KnowledgeFragment(
            form=representative.form or "concept",
            title=title,
            frontmatter=self._merge_frontmatter(cluster),
            background=self._combine_texts(backgrounds),
            core_content=self._combine_texts(core_contents),
            boundaries=metadata["boundaries"],
            anti_patterns=metadata["anti_patterns"],
            related_concepts=metadata["related_concepts"],
            claim_ids=metadata["claim_ids"],
            relations=metadata["relations"],
            self_check_passed=metadata["self_check_passed"],
            self_check_issues=metadata["self_check_issues"],
            self_check_severity=metadata["self_check_severity"],
            cross_agent_links=metadata["cross_agent_links"],
            keywords=metadata["keywords"],
            ai_expansion=metadata["ai_expansion"],
        )

    # ==================== LLM 调用 ====================

    def _call_llm(self, prompt: str, fragment_count: int = 0) -> Optional[str]:
        """使用与蒸馏相同的 API chain 调用 LLM，但独立重试/超时。"""
        try:
            import openai
        except ImportError:
            logger.warning("[FragmentMerger] openai SDK 未安装，无法调用 LLM")
            return None
        openai_error_type = getattr(openai, "OpenAIError", RuntimeError)

        last_error = ""
        configs = self._api_chain.all_configs if self._api_chain else []
        if not configs:
            logger.warning("[FragmentMerger] 没有可用的 LLM API 配置")
            return None

        messages = [{"role": "user", "content": prompt}]
        provider_input = canonical_chat_input(messages)
        input_tokens = utf8_token_upper_bound(provider_input)
        limits = resolve_response_token_limits(
            self._cfg,
            input_tokens=input_tokens,
            analysis_type="merge",
            fragment_count=fragment_count,
        )

        for cfg in configs:
            if not cfg.configured:
                continue
            max_tokens = limits.initial
            for attempt in range(self._max_retries + 1):
                active_cfg = cfg.active()
                if not active_cfg.configured:
                    break
                reservation = None
                try:
                    client_kwargs = {"api_key": active_cfg.api_key}
                    if active_cfg.base_url:
                        client_kwargs["base_url"] = active_cfg.base_url
                    ledger, run_id = self._ledger_for_call()
                    reservation = ledger.reserve(
                        run_id=run_id,
                        operation="distill_merge",
                        provider=active_cfg.provider,
                        model=active_cfg.model,
                        input_text=provider_input,
                        input_tokens=input_tokens,
                        output_tokens=max_tokens,
                        cache_status="miss",
                        retry_attempt=attempt,
                        subject_scopes=self._model_call_subject_scopes or None,
                    )
                    reservation.mark_dispatched()
                    started = time.perf_counter()
                    with non_redirecting_openai_client(
                        openai.OpenAI, **client_kwargs
                    ) as client:  # type: ignore[arg-type]
                        response = client.chat.completions.create(
                            model=active_cfg.model,
                            messages=messages,
                            max_tokens=max_tokens,
                            temperature=0.2,
                            timeout=self._timeout,
                        )
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    choice = response.choices[0]
                    usage = getattr(response, "usage", None)
                    request_id = str(getattr(response, "id", "") or "")
                    provider_usage = metered_provider_usage(
                        usage,
                        request_id=request_id,
                        output_required=True,
                    )
                    if provider_usage is None:
                        reservation.preserve_incurred(error_code="merge_provider_usage_missing")
                    else:
                        reservation.settle(
                            usage=provider_usage,
                            latency_ms=latency_ms,
                        )
                    finish_reason = getattr(choice, "finish_reason", None)
                    if finish_reason == "length" and limits.retry_max > max_tokens:
                        max_tokens = limits.retry_max
                        if attempt < self._max_retries:
                            continue
                    content = choice.message.content
                    cfg.report_success(active_cfg)
                    return content.strip() if content else None
                except ModelCallBudgetExceeded:
                    if reservation is not None:
                        if reservation.dispatched:
                            reservation.preserve_incurred(error_code="merge_budget_after_dispatch")
                        else:
                            reservation.release(error_code="merge_budget_before_dispatch")
                    raise
                except ModelCallSubjectFrozen:
                    if reservation is not None:
                        if reservation.dispatched:
                            reservation.preserve_incurred(
                                error_code="merge_subject_frozen_after_dispatch"
                            )
                        else:
                            reservation.release(error_code="merge_subject_frozen_before_dispatch")
                    raise
                except (
                    openai_error_type,
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    ImportError,
                    AttributeError,
                    IndexError,
                    RuntimeError,
                ) as e:
                    if reservation is not None:
                        if reservation.dispatched:
                            reservation.preserve_incurred(error_code="merge_provider_exception")
                        else:
                            reservation.release(error_code="merge_provider_pre_dispatch_exception")
                    error_category = safe_provider_error_category(e)
                    cfg.report_failure(active_cfg, error_category)
                    last_error = f"{active_cfg.provider}/{active_cfg.model}: {error_category}"
                    logger.debug(
                        "[FragmentMerger] LLM call failed (attempt %s): category=%s",
                        attempt + 1,
                        error_category,
                    )

        logger.warning("[FragmentMerger] 所有 LLM API 均不可用: %s", last_error)
        return None

    def _build_merge_prompt(self, cluster: List[Any]) -> str:
        """构造片段合成 prompt。"""
        data = []
        for i, f in enumerate(cluster, 1):
            data.append(
                {
                    "index": i,
                    "form": f.form,
                    "title": f.title,
                    "background": f.background,
                    "core_content": f.core_content,
                    "boundaries": f.boundaries,
                    "anti_patterns": f.anti_patterns,
                    "related_concepts": f.related_concepts,
                    "claim_ids": f.claim_ids,
                    "relations": f.relations,
                    "self_check_passed": f.self_check_passed,
                    "self_check_issues": f.self_check_issues,
                    "self_check_severity": f.self_check_severity,
                    "cross_agent_links": f.cross_agent_links,
                    "keywords": f.keywords,
                    "ai_expansion": f.ai_expansion,
                    "frontmatter": f.frontmatter,
                }
            )

        return (
            "你是 Mnemos 知识库蒸馏系统的片段合成助手。\n"
            "下面是一个长对话分块蒸馏后得到的多个局部知识片段，它们属于同一个话题。\n"
            "请把它们整合成一条结构完整、逻辑连贯、无冗余的知识片段。\n\n"
            "要求：\n"
            "1. 输出必须是合法的 JSON 对象，匹配以下 schema。\n"
            "2. 不要输出 Markdown 代码块，只输出 JSON。\n"
            "3. title 应能概括整个话题；core_content 应整合所有输入的核心内容，保留关键步骤/原因/解决方案。\n"
            "4. background 可综合多个片段的背景。\n"
            "5. boundaries、anti_patterns、related_concepts、claim_ids、keywords 应做有序去重合并。\n"
            "6. relations、cross_agent_links、ai_expansion、self_check_*，以及 frontmatter 中的 "
            "raw_event_refs 和 chunk_source_spans，必须完整保留输入的有序去重并集；"
            "不得删除、改写或编造。\n"
            "7. core_content 与 background 中每个输入块、代码块、空行和重复行均须按输入顺序保留。\n"
            "8. frontmatter 必须包含：领域（字符串，>=2 字符）、置信度（0-1 浮点数）、摘要（字符串，>=5 字符）；置信度取输入中的保守最小值，并保留置信度审计值。\n"
            "9. core_content 必须至少包含一个 Markdown 标题（# ## ###）或代码块（```）。\n\n"
            "Schema：\n"
            "{\n"
            '  "form": "problem-solution | concept | decision | experience | anti-pattern | comparison | checklist",\n'  # noqa: E501
            '  "title": "...",\n'
            '  "frontmatter": {"领域": "...", "置信度": 0.85, "置信度审计值": [0.85], "摘要": "..."},\n'
            '  "background": "...",\n'
            '  "core_content": "...",\n'
            '  "boundaries": {"applies": "...", "not_applies": "..."},\n'
            '  "anti_patterns": ["..."],\n'
            '  "related_concepts": ["..."],\n'
            '  "claim_ids": ["claim-1"],\n'
            '  "relations": [{"target": "[[...]]", "type": "related_to", "context": "..."}],\n'
            '  "self_check_passed": true,\n'
            '  "self_check_issues": ["..."],\n'
            '  "self_check_severity": "ok | warning | fatal",\n'
            '  "cross_agent_links": ["..."],\n'
            '  "keywords": ["..."],\n'
            '  "ai_expansion": "..."\n'
            "}\n\n"
            "输入片段：\n"
            f"{json.dumps(data, ensure_ascii=False, indent=2)}\n\n"
            "请直接输出合成后的 JSON："
        )

    # ==================== 工具方法 ====================

    def _cluster_text(self, frag: Any) -> str:
        """生成用于聚类的文本：标题 + keywords。"""
        parts = [frag.title or ""]
        keywords = getattr(frag, "keywords", None) or []
        parts.extend(str(k) for k in keywords)
        return " ".join(p for p in parts if p)

    @staticmethod
    def _jaccard_similarity(a: str, b: str) -> float:
        """Jaccard 相似度（基于字符二元组）。

        与 DistillationEngine._jaccard_similarity 保持一致，对中英文标题都更稳定。
        """
        a = a or ""
        b = b or ""
        if not a or not b:
            return 0.0
        sa = set(a[i : i + 2] for i in range(len(a) - 1))
        sb = set(b[i : i + 2] for i in range(len(b) - 1))
        if not sa and not sb:
            return 1.0
        inter = len(sa & sb)
        union = len(sa | sb)
        return inter / union if union > 0 else 0.0

    def _dict_to_fragment(self, data: Dict[str, Any]) -> Optional[Any]:
        """把字典转成 KnowledgeFragment。"""
        KnowledgeFragment = _get_distillation_engine().KnowledgeFragment
        try:
            return KnowledgeFragment(
                form=data.get("form", "concept"),
                title=data.get("title", ""),
                frontmatter=data.get("frontmatter", {}),
                background=data.get("background", ""),
                core_content=data.get("core_content", ""),
                boundaries=data.get("boundaries", {}),
                anti_patterns=data.get("anti_patterns", []),
                related_concepts=data.get("related_concepts", []),
                claim_ids=data.get("claim_ids", []),
                relations=data.get("relations", []),
                self_check_passed=bool(data.get("self_check_passed", True)),
                self_check_issues=data.get("self_check_issues", []),
                self_check_severity=data.get("self_check_severity", "ok"),
                cross_agent_links=data.get("cross_agent_links", []),
                keywords=data.get("keywords", []),
                ai_expansion=data.get("ai_expansion", ""),
            )
        except (TypeError, ValueError, KeyError, AttributeError):
            logger.debug(
                "[FragmentMerger] 构造 KnowledgeFragment 失败: category=invalid_fragment_output"
            )
            return None
