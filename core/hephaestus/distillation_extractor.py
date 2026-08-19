# -*- coding: utf-8 -*-
"""Knowledge extraction stage for the distillation pipeline."""

from __future__ import annotations

import json
import logging
import inspect
from contextlib import nullcontext
from typing import Any, Callable, Dict, List, Tuple

from core.evidence.artifact_catalog import resolve_model_artifact_selections
from core.evidence.source_authority import resolve_model_source_authority_selections
from core.hephaestus.distillation_errors import DistillationAPIError
from core.hephaestus.distill_backend import DistillBackend, LLMBackend
from core.hephaestus.distillation_llm import HttpApiHostAgentCaller
from core.hephaestus.distillation_models import ExtractionOutcome, KnowledgeFragment
from core.hephaestus.distill_response import DistillBackendResponse
from core.hephaestus.distillation_contract import (
    SCHEMA_VERSION,
    ContractValidationIssue,
    ContractValidationResult,
    canonical_fragment_payload,
    canonical_extraction_output_hash,
    canonical_output_schema_text,
    canonicalize_extraction_output,
    validate_extraction_output,
)
from core.hephaestus.distill_input_spec import (
    DistillInputSpec,
    ExtractionRequest,
    PreparedExtractionPrompt,
)
from core.hephaestus.prompt_builder import DistillTask, PromptBuilder, Session, TokenBudget
from core.hephaestus.response_budget import ResponseTokenLimits, resolve_response_token_limits
from core.hephaestus.tokenizer import estimate_tokens

logger = logging.getLogger(__name__)


class KnowledgeExtractor:
    """第4层：知识提取 — LLM + assertion_extractor 验证"""

    def __init__(
        self,
        backend: DistillBackend | None = None,
        caller: HttpApiHostAgentCaller | None = None,
        config_getter: Callable[[], Any] | None = None,
        prompt_budget_getter: Callable[[], TokenBudget] | None = None,
        fragment_builder: Callable[[Dict], KnowledgeFragment | None] | None = None,
        strict_validator: Callable[[List[KnowledgeFragment]], Tuple[bool, List[str]]] | None = None,
    ):
        if backend is None and caller is not None:
            backend = LLMBackend(caller)
        if backend is None:
            raise RuntimeError("distill backend is required")
        self._backend = backend
        self._config_getter = config_getter
        self._prompt_budget_getter = prompt_budget_getter
        self._fragment_builder = fragment_builder
        self._strict_validator = strict_validator

    @property
    def backend(self) -> DistillBackend:
        """Expose the active backend for credential-free execution identity."""
        return self._backend

    def prepare_prompt(self, request: ExtractionRequest) -> PreparedExtractionPrompt:
        """Render exactly once and bind the prompt to the immutable request."""
        request.input_spec.artifact_catalog.require_admissible()
        request.input_spec.source_authority_catalog.require_admissible()
        prompt = self._build_prompt(
            request.session_text,
            request.input_spec.source_session_id,
            request.analysis_type,
            request.input_spec,
        )
        return PreparedExtractionPrompt.build(prompt, request)

    def extract(
        self,
        request: ExtractionRequest,
        *,
        prepared: PreparedExtractionPrompt | None = None,
    ) -> ExtractionOutcome:
        """Return a typed result after full contract admission or correction.

        A rejected result stays rejected; the engine must record it as a
        failed extraction rather than interpreting an empty list as skip.
        """
        if self._config_getter is None or self._strict_validator is None:
            raise RuntimeError("config_getter and strict_validator are required")
        request.input_spec.artifact_catalog.require_admissible()
        request.input_spec.source_authority_catalog.require_admissible()
        cfg = self._config_getter()
        max_retries = cfg.get("distill.extract_correction_retries", 1)
        response_limits = resolve_response_token_limits(
            cfg,
            input_tokens=estimate_tokens(request.session_text),
            analysis_type=request.analysis_type,
        )
        spec = request.input_spec
        prepared = prepared or self.prepare_prompt(request)
        prepared.assert_matches(request)
        backend_responses: List[DistillBackendResponse] = []
        response = self._call_llm(
            prepared.text, response_limits, operation="distill_extract"
        )
        backend_responses.append(response)
        result = response.parsed
        fragments, validation, errors, resolved_result = self._assess_output(
            result, spec.source_session_id, spec
        )
        correction_count = 0

        # A valid skip is accepted immediately. Every other malformed output,
        # including an empty fragments array, enters bounded correction.
        for attempt in range(1, max_retries + 1):
            if validation.valid:
                break
            logger.warning(
                "[KnowledgeExtractor] 第 %s 次提取未通过输出契约，尝试修正: %s",
                attempt,
                errors[:3],
            )
            correction_prompt = self._build_correction_prompt(
                request.session_text,
                spec.source_session_id,
                request.analysis_type,
                result,
                errors,
                spec,
            )
            try:
                response = self._call_llm(
                    correction_prompt,
                    response_limits,
                    operation="distill_correct",
                )
                backend_responses.append(response)
                result = response.parsed
            except DistillationAPIError as error:
                if isinstance(error.response_evidence, DistillBackendResponse):
                    backend_responses.append(error.response_evidence)
                break
            correction_count += 1
            fragments, validation, errors, resolved_result = self._assess_output(
                result, spec.source_session_id, spec
            )

        structured_output = self._extract_structured_output(resolved_result)
        if validation.valid and not validation.is_skip and fragments:
            fragments = self._validate_with_assertions(fragments, request.session_text)

        admitted_fragments = fragments if validation.valid else []
        judgment = validation.output_judgment
        canonical_output = canonicalize_extraction_output(
            resolved_result,
            admitted_fragments,
        )
        return ExtractionOutcome(
            judgment=judgment,
            fragments=tuple(admitted_fragments),
            structured_output=structured_output,
            canonical_output=canonical_output,
            admission=validation,
            canonical_output_hash=canonical_extraction_output_hash(
                canonical_output=canonical_output,
            ),
            correction_count=correction_count,
            backend_responses=tuple(backend_responses),
        )

    def _call_llm(
        self,
        prompt: str,
        response_limits: ResponseTokenLimits,
        *,
        operation: str = "distill_extract",
    ) -> DistillBackendResponse:
        """Call the configured LLM with dynamic response caps when supported."""
        call = self._backend.call
        try:
            params = inspect.signature(call).parameters
            accepts_budget = "response_max_tokens" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
        except (TypeError, ValueError):
            accepts_budget = True

        caller = getattr(self._backend, "caller", None)
        operation_context = getattr(caller, "model_call_context", None)
        context = operation_context(operation) if callable(operation_context) else nullcontext()
        with context:
            if not accepts_budget:
                response = call(prompt, expect_json=True)
            else:
                response = call(
                    prompt,
                    expect_json=True,
                    response_max_tokens=response_limits.initial,
                    response_retry_max_tokens=response_limits.retry_max,
                )
        if not isinstance(response, DistillBackendResponse):
            raise TypeError("distillation backend must return DistillBackendResponse")
        return response

    @staticmethod
    def _extract_structured_output(data: Any) -> Dict[str, Any] | None:
        if not isinstance(data, dict):
            return None
        if data.get("schema_version") == SCHEMA_VERSION:
            return data
        structured = data.get("structured_output")
        if isinstance(structured, dict) and structured.get("schema_version") == SCHEMA_VERSION:
            return structured
        return None

    def _assess_output(
        self,
        data: Any,
        session_id: str,
        input_spec: DistillInputSpec,
    ) -> tuple[
        List[KnowledgeFragment],
        ContractValidationResult,
        List[str],
        Any,
    ]:
        """Run the canonical union contract and fragment hard checks together."""
        resolution = resolve_model_artifact_selections(
            data,
            input_spec.artifact_catalog,
        )
        authority_resolution = resolve_model_source_authority_selections(
            resolution.payload,
            input_spec.source_authority_catalog,
        )
        resolved_data = authority_resolution.payload
        validation = validate_extraction_output(resolved_data, input_spec)
        if resolution.issues or authority_resolution.issues:
            resolved_issues = [
                ContractValidationIssue(issue.code, issue.path, issue.message)
                for issue in resolution.issues
            ]
            resolved_issues.extend(
                ContractValidationIssue(issue.code, issue.path, issue.message)
                for issue in authority_resolution.issues
            )
            validation.issues[:0] = resolved_issues
        fragments: List[KnowledgeFragment] = []
        if validation.valid and not validation.is_skip:
            fragments = self._parse_fragments(resolved_data, session_id)
            raw_fragments = resolved_data.get("fragments")
            if not fragments:
                validation.issues.append(
                    ContractValidationIssue(
                        "parsed_fragments_empty",
                        "fragments",
                        "non-skip output could not be converted into admitted fragments",
                    )
                )
            elif not isinstance(raw_fragments, list) or len(fragments) != len(raw_fragments):
                validation.issues.append(
                    ContractValidationIssue(
                        "fragment_parse_mismatch",
                        "fragments",
                        "every schema-valid fragment must be converted before admission",
                    )
                )
            else:
                for index, (raw_fragment, parsed_fragment) in enumerate(
                    zip(raw_fragments, fragments)
                ):
                    raw_payload = canonical_fragment_payload(raw_fragment)
                    parsed_payload = canonical_fragment_payload(parsed_fragment)
                    for field in ("form", "title", "frontmatter", "core_content"):
                        if raw_payload.get(field) != parsed_payload.get(field):
                            validation.issues.append(
                                ContractValidationIssue(
                                    "fragment_parse_mismatch",
                                    f"fragments[{index}].{field}",
                                    "parsed fragment must exactly preserve the admitted output field",
                                )
                            )
                if validation.valid:
                    assert self._strict_validator is not None
                    passed, strict_errors = self._strict_validator(fragments)
                    if not passed:
                        for error in strict_errors:
                            validation.issues.append(
                                ContractValidationIssue(
                                    "fragment_hard_validation_failed",
                                    "fragments",
                                    str(error),
                                )
                            )
        errors = [f"{issue.path}: {issue.message}" for issue in validation.issues]
        return fragments, validation, errors, resolved_data

    def _build_correction_prompt(
        self,
        session_text: str,
        session_id: str,
        analysis_type: str,
        last_output: Any,
        errors: List[str],
        input_spec: DistillInputSpec,
    ) -> str:
        """构造格式修正 prompt，把硬校验错误回传给 LLM。"""
        return (
            "你是 Mnemos 知识蒸馏引擎的格式修正助手。\n"
            "之前的知识提取输出未通过系统硬校验，请根据错误列表修正输出。\n\n"
            "硬校验错误：\n"
            + "".join(f"- {e}\n" for e in errors)
            + "\n上次输出：\n```json\n"
            + json.dumps(last_output, ensure_ascii=False, indent=2)
            + "\n```\n\n"
            "要求：\n"
            "1. 仅输出修正后的合法 JSON，不要解释。\n"
            "2. 必须满足：title ≥10 字符，core_content ≥100 字符且至少包含一个 Markdown 标题（# / ## / ###）或代码块（```），frontmatter 包含非空摘要（≥5 字符）和领域（≥2 字符）。\n"  # noqa: E501
            "3. 输出必须完整匹配不可变输入契约；不得猜测、修改来源 Agent、会话、事件、完整度或 input_spec_hash。\n"
            "4. 如果对话内容确实无法产出满足要求的片段，只能返回合法 skip 分支："
            "judgment=skip、fragments=[]、structured_output.distill_intent=skip、"
            "claims=[]，且必须保留下列不可变字段、skip_reason 和至少一条 no_value_evidence。\n"
            "5. 以下 canonical JSON Schema 与运行时验证器完全相同；必须满足其全部 oneOf、"
            "条件必填和数组长度约束，不能根据本提示自行简化：\n```json\n"
            + canonical_output_schema_text()
            + "\n```\n"
            "不可变输入契约：\n```json\n"
            + json.dumps(input_spec.prompt_contract(), ensure_ascii=False, indent=2)
            + "\n```\n\n"
            f"原始对话（Session ID: {session_id or 'unknown'}, analysis_type: {analysis_type}）：\n"
            f"{session_text}"
        )

    def _build_prompt(
        self,
        session_text: str,
        session_id: str,
        analysis_type: str,
        input_spec: DistillInputSpec,
    ) -> str:
        if self._prompt_budget_getter is None:
            raise RuntimeError("prompt_budget_getter is required")
        session = Session(
            id=session_id or "unknown",
            messages=[{"role": "user", "content": session_text}],
            agent_name=input_spec.source_agent,
            input_contract=input_spec.prompt_contract(),
        )
        task = DistillTask(
            task_type="extract",
            session=session,
            session_type="general",
            budget_config=self._prompt_budget_getter(),
            preformatted=True,
        )
        return PromptBuilder().build(task)

    def _parse_fragments(self, data: Dict, session_id: str) -> List[KnowledgeFragment]:
        """从 LLM JSON 输出解析知识片段 — 兼容列表和旧分层对象格式"""
        if self._fragment_builder is None:
            raise RuntimeError("fragment_builder is required")
        fragments = []
        for frag_data in data.get("fragments", []):
            try:
                fragment = self._fragment_builder(frag_data)
                if fragment is not None:
                    fragments.append(fragment)
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                logging.getLogger(__name__).warning(
                    "片段解析失败，跳过: %s", frag_data.get("title", "未知"), exc_info=True
                )
                continue
        return fragments

    def _validate_with_assertions(
        self, fragments: List[KnowledgeFragment], session_text: str
    ) -> List[KnowledgeFragment]:
        """用 assertion_extractor 交叉验证提取结果"""
        try:
            from core.kia.assertion_extractor import extract_assertions

            assertions = extract_assertions(session_text)
            if not assertions:
                return fragments

            assertion_claims = {a.claim[:60] for a in assertions if a.confidence >= 0.5}
            for frag in fragments:
                content_lower = frag.core_content.lower() + frag.title.lower()
                overlap = sum(1 for claim in assertion_claims if claim.lower() in content_lower)
                if overlap == 0 and len(assertion_claims) > 3:
                    frag.frontmatter["assertion_validated"] = False
                    frag.frontmatter["置信度"] = min(
                        frag.frontmatter.get("置信度", 0.6),
                        0.4,
                    )
                else:
                    frag.frontmatter["assertion_validated"] = True
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logging.getLogger(__name__).warning("断言验证失败，跳过", exc_info=True)
        return fragments
