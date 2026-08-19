# -*- coding: utf-8 -*-
from __future__ import annotations

"""
distillation_engine 核心公共行为单元测试

覆盖项（按优先级排序）：
1. DistillationEngine.process() — 七层流水线核心入口，含 L1-L7 全链路
2. DistillationResult / PipelineLayerResult dataclass 行为
3. KnowledgeFragment 数据模型行为
4. extract_json() — JSON 解析容错（纯函数，无外部依赖）
5. build_session_text() — 会话文本构建与 head-tail 截断策略
6. clean_message_content() — 隐私块清理，不删除可见代码/命令证据
7. ValuePrejudgment.judge() — 价值预判公共接口
8. NoiseFilter.filter() — 噪音过滤公共接口

不覆盖：
- 私有方法（_ 前缀）
- HttpApiHostAgentCaller（涉及真实 HTTP API 调用）
- LLMValueJudge / KnowledgeExtractor（依赖 LLM，已有独立测试覆盖）
- write_pages 中的 frontmatter 回写（涉及文件系统和外部模块导入）

测试策略：
- 所有外部依赖使用 SimpleNamespace 或 @patch 进行 mock
- 不使用真实 HTTP/API 调用
- 使用 tmp_path 作为临时目录
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.hephaestus.distillation_engine import KnowledgeFragment
from tests.cognition_episode_fixtures import (
    exact_source_message,
    model_cognition_episode,
    model_exact_evidence,
    resolve_model_evidence,
)

import pytest

# ========== Fixtures ==========


@pytest.fixture
def minimal_messages():
    """最小有效消息列表，用于触发知识提取路径"""
    return [
        {"role": "user", "content": "为什么 Redis 连接池会耗尽？"},
        {
            "role": "assistant",
            "content": "原因是连接池上限过低且缺少超时监控。解决方案是增加 max_connections 并设置 socket_timeout。",
        },
    ]


@pytest.fixture
def noise_messages():
    """纯噪音消息列表，应被 L1/L2 过滤掉"""
    return [
        {"role": "user", "content": "好的"},
        {"role": "assistant", "content": "收到，谢谢"},
        {"role": "user", "content": "ok"},
    ]


def _engine_config(tmp_path):
    from core.cognitive.state_schema import initialize_cognitive_state_schema
    from core.config import get_config
    from core.ops.operational_incident import initialize_operational_incident_schema

    global_config = get_config()
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")
    initialize_operational_incident_schema(tmp_path / "operational_incidents.db")
    return SimpleNamespace(database_dir=tmp_path, get=global_config.get)


def _set_runtime_config(engine, config):
    """Replace the injected writer config and provision its canonical state DB."""

    from core.cognitive.state_schema import initialize_cognitive_state_schema

    fallback_dir = engine._runtime_receipt_config.database_dir
    database_dir = Path(getattr(config, "database_dir", fallback_dir))
    initialize_cognitive_state_schema(database_dir / "producer_consumer_ledger.db")
    engine._runtime_receipt_config = SimpleNamespace(
        database_dir=database_dir,
        get=config.get,
    )
    return engine._runtime_receipt_config


def _captured_process(engine, session_id, messages, *, meta=None):
    """Run process with the exact Raw-span handoff production extraction requires."""

    captured = []
    raw_event_refs = []
    for index, message in enumerate(messages, start=1):
        bound = exact_source_message(
            role=str(message.get("role") or "unknown"),
            content=str(message.get("content") or ""),
            revision_id=f"{session_id}:raw:{index}",
        )
        captured.append({**dict(message), **bound})
        raw_event_refs.append(dict(bound["source_span"]))
    process_meta = dict(meta or {})
    process_meta["raw_event_refs"] = raw_event_refs
    return engine.process(session_id, captured, meta=process_meta)


def _extraction_request(
    *,
    text: str = ("请判断 Redis 连接池耗尽的根因。" "连接池上限过低且缺少超时监控。"),
    session_id: str = "sess-extract",
    source_agent: str = "test-agent",
    source_event_ids: tuple[str, ...] = ("raw-1",),
    raw_completeness: str = "full",
    analysis_type: str = "standard",
    artifact_refs=(),
):
    """Build the immutable request which the v4 response must echo exactly."""
    from core.hephaestus.distill_input_spec import DistillInputSpec, ExtractionRequest

    spec = DistillInputSpec.build(
        source_agent=source_agent,
        source_session_id=session_id,
        source_event_ids=source_event_ids,
        raw_completeness=raw_completeness,
        visible_input=text,
        input_mode=analysis_type,
        artifact_refs=artifact_refs,
        source_messages=(
            exact_source_message(
                role="user",
                content=text,
                revision_id=source_event_ids[0],
            ),
        ),
    )
    return ExtractionRequest(
        session_text=text,
        analysis_type=analysis_type,
        input_spec=spec,
    )


def _fragment_payload(fragment):
    """Serialize a fragment as the canonical v4 extraction schema expects."""
    return {
        "form": fragment.form,
        "title": fragment.title,
        "frontmatter": dict(fragment.frontmatter),
        "background": fragment.background,
        "core_content": fragment.core_content,
        "boundaries": dict(fragment.boundaries),
        "anti_patterns": list(fragment.anti_patterns),
        "related_concepts": list(fragment.related_concepts),
        "relations": list(fragment.relations),
        "claim_ids": list(fragment.claim_ids),
    }


def _v4_structured_output(input_spec, *, distill_intent: str = "create"):
    """Return a complete model-side, input-bound v4 structured output."""
    evidence = model_exact_evidence(input_spec)
    event_id = evidence["source_event_id"]
    structured_output = {
        "schema_version": "distill_output_v4",
        "input_spec_hash": input_spec.input_spec_hash,
        "cognition_context_hash": input_spec.cognition_context.context_hash,
        "gate_decision_id": input_spec.gate_decision_id,
        "source_agent": input_spec.source_agent,
        "source_session_id": input_spec.source_session_id,
        "source_event_ids": list(input_spec.source_event_ids),
        "raw_completeness": input_spec.raw_completeness,
        "distill_intent": distill_intent,
        "candidate_summary": "Redis 连接池耗尽的排查方案可供后续复用。",
    }
    if distill_intent == "skip":
        structured_output.update(
            {
                "candidate_summary": "输入没有可长期复用的知识。",
                "skip_reason": "输入只包含一次性寒暄，没有可验证的长期结论。",
                "no_value_evidence": [
                    {
                        "source_event_id": event_id,
                        "reason": "该事件只有寒暄和一次性问候。",
                    }
                ],
                "claims": [],
            }
        )
        return structured_output

    structured_output.update(
        {
            "user_behavior_intent": {
                "content_source": "native_dialogue",
                "user_intent_signal": "seeking_judgment",
                "intent_hypothesis": "seeking_judgment",
                "intent_evidence": [
                    {
                        **dict(evidence),
                        "reason": "用户要求对技术根因作出判断。",
                    }
                ],
                "intent_verification_events": [],
                "intent_confidence": 0.72,
                "intent_status": "unverified",
                "behavior_summary": "用户希望得到 Redis 连接池问题的可复用判断。",
            },
            "claims": [
                {
                    "claim_id": "claim-redis-pool",
                    "claim_text": "Redis 连接池耗尽通常需要同时检查连接上限、超时和释放路径。",
                    "claim_type": "technical_fact",
                    "scope": {
                        "domain": "backend",
                        "applies_to": ["高并发 Redis 服务"],
                        "not_applies_to": [],
                    },
                    "evidence": [dict(evidence)],
                    "relation_to_existing": {
                        "type": "new",
                        "target_pages": [],
                        "delta_text": "",
                        "reason": "当前上下文提供了新的排查结论。",
                    },
                    "recommended_action": "create_page",
                    "confidence": 0.86,
                }
            ],
            "cognition_episode": model_cognition_episode(
                evidence,
                claim_id="claim-redis-pool",
            ),
        }
    )
    return structured_output


def _typed_extraction_payload(input_spec, *, judgment="knowledge", fragments=None):
    """Build the full discriminated-union model response used by the extractor."""
    is_skip = judgment == "skip"
    return {
        "judgment": judgment,
        "judgment_reason": (
            "输入没有长期认知价值。" if is_skip else "输入包含可长期复用的技术结论。"
        ),
        "fragments": [] if is_skip else list(fragments or []),
        "structured_output": _v4_structured_output(
            input_spec,
            distill_intent="skip" if is_skip else "create",
        ),
    }


def _admitted_outcome(request, *, fragments=(), judgment="knowledge"):
    """Create an extractor-protocol outcome which survives engine revalidation."""
    from core.hephaestus.distillation_contract import (
        canonical_extraction_output_hash,
        validate_extraction_output,
    )
    from core.hephaestus.distillation_models import ExtractionOutcome

    fragments = tuple(fragments)
    payload = _typed_extraction_payload(
        request.input_spec,
        judgment=judgment,
        fragments=[_fragment_payload(fragment) for fragment in fragments],
    )
    payload = resolve_model_evidence(payload, request.input_spec)
    admission = validate_extraction_output(payload, request.input_spec)
    assert admission.valid, admission.error_text
    return ExtractionOutcome(
        judgment=judgment,
        fragments=fragments,
        structured_output=payload["structured_output"],
        canonical_output=payload,
        admission=admission,
        canonical_output_hash=canonical_extraction_output_hash(
            canonical_output=payload,
        ),
    )


def _typed_extractor(*, fragments=(), judgment="knowledge"):
    """Small test double implementing the exact COG-011 extractor protocol."""

    def _prepare(request):
        from core.hephaestus.distill_input_spec import PreparedExtractionPrompt

        return PreparedExtractionPrompt.build("typed extractor test prompt", request)

    return SimpleNamespace(
        prepare_prompt=_prepare,
        extract=lambda request, *, prepared=None: _admitted_outcome(
            request,
            fragments=fragments,
            judgment=judgment,
        ),
    )


@pytest.fixture
def engine_with_mocks(tmp_path):
    """返回一个 DistillationEngine 实例，所有外部依赖均已 mock"""
    from core.hephaestus.distillation_engine import DistillationEngine, ValuePrejudgment
    from core.hephaestus.distillation_pause import resume_distillation

    # 重置全局暂停状态，避免其他测试或运行时留下的暂停标志影响当前测试
    resume_distillation()

    def _mock_backend():
        return SimpleNamespace(
            call=MagicMock(
                return_value={
                    "skill_name": "测试认知决策资产",
                    "skill_purpose": "验证 skill 分支不访问外部 LLM",
                }
            ),
            caller=None,
        )

    engine = DistillationEngine(
        wiki_base=str(tmp_path),
        backend_factory=_mock_backend,
        receipt_config=_engine_config(tmp_path),
    )
    # Mock 噪音过滤：直接透传
    engine._noise_filter = SimpleNamespace(
        filter=lambda messages: (
            messages,
            {"total": len(messages), "noise": 0, "kept": len(messages)},
        ),
    )
    # Mock 价值预判：返回 MAYBE，强制进入 L3
    engine._value_prejudgment = SimpleNamespace(
        judge=lambda messages: (ValuePrejudgment.MAYBE, 0.5),
    )
    # Mock LLM 判断
    engine._llm_judge = SimpleNamespace(
        judge=lambda session_text, session_id: ("knowledge", "有价值", 0.8),
    )
    # Mock 知识提取器：必须遵守 COG-011 的 typed request/outcome 协议。
    engine._extractor = _typed_extractor(judgment="skip")
    # Mock 自检
    engine._self_check = SimpleNamespace(
        check=lambda fragments, messages: (True, []),
    )
    # Mock 跨 Agent 关联
    engine._cross_linker = SimpleNamespace(link=lambda fragments: fragments)
    # Mock 反馈循环
    engine._feedback_loop = SimpleNamespace(evaluate=lambda result: [])
    # 禁用 KIA linker
    engine._kia_linker = False
    return engine


@pytest.fixture
def sample_fragment():
    """返回一个标准的 KnowledgeFragment 实例（满足硬校验标准）"""
    from core.hephaestus.distillation_engine import KnowledgeFragment

    return KnowledgeFragment(
        form="问题-解决",
        title="Redis 连接池耗尽问题的排查方案",
        frontmatter={
            "领域": "backend",
            "置信度": 0.85,
            "时效性": "contextual",
            "摘要": "高并发下 Redis 连接池耗尽的根本原因是上限过低且缺少超时监控，解决方法是设置合理的连接上限和超时时间。",
        },
        background="高并发场景下 Redis 连接池偶发耗尽，导致服务响应变慢甚至不可用。",
        core_content=(
            "## 问题现象\n\n"
            "高并发场景下 Redis 连接池偶发耗尽，导致服务响应变慢甚至不可用。\n\n"
            "## 根因分析\n\n"
            "1. 连接池上限设置过低，无法支撑突发流量。\n"
            "2. 缺少连接超时监控，无法及时发现泄漏。\n"
            "3. 部分长连接未正确释放，导致连接数持续增长。\n\n"
            "## 解决方案\n\n"
            "```python\n"
            "pool = redis.ConnectionPool(max_connections=100, socket_timeout=5)\n"
            "```\n\n"
            "同时启用连接池监控告警，当连接数超过 80% 时触发告警。"
        ),
        boundaries={"applies": "高并发 Redis 场景", "not_applies": "单机低并发"},
        anti_patterns=["直接调大连接池而不设超时"],
        related_concepts=["连接池"],
        keywords=["Redis", "connection_pool"],
        claim_ids=["claim-redis-pool"],
    )


# ========== 1. DistillationEngine.process() ==========


def test_process_empty_messages_returns_skip(engine_with_mocks):
    """空消息列表应直接返回 skip，不进入后续层"""
    result = _captured_process(engine_with_mocks, "sess-empty", [])
    assert result.judgment == "skip"
    assert result.session_id == "sess-empty"


def test_process_all_noise_returns_skip(engine_with_mocks, noise_messages):
    """全部消息为噪音时，L1 过滤后无消息，应返回 skip"""
    engine_with_mocks._noise_filter = SimpleNamespace(
        filter=lambda messages: ([], {"total": len(messages), "noise": len(messages), "kept": 0}),
    )
    result = _captured_process(engine_with_mocks, "sess-noise", noise_messages)
    assert result.judgment == "skip"
    assert "噪声" in result.judgment_reason


def test_process_certainly_no_returns_skip(engine_with_mocks, minimal_messages):
    """L2 预判为 CERTAINLY_NO 时，应直接返回 skip，不调用 LLM"""
    from core.hephaestus.distillation_engine import ValuePrejudgment

    engine_with_mocks._value_prejudgment = SimpleNamespace(
        judge=lambda messages: (ValuePrejudgment.CERTAINLY_NO, 0.95),
    )
    llm_called = [False]
    original_llm_judge = engine_with_mocks._llm_judge

    def _track_llm(*args, **kwargs):
        llm_called[0] = True
        return original_llm_judge.judge(*args, **kwargs)

    engine_with_mocks._llm_judge = SimpleNamespace(judge=_track_llm)
    result = _captured_process(engine_with_mocks, "sess-no", minimal_messages)
    assert result.judgment == "skip"
    assert result.prejudgment == ValuePrejudgment.CERTAINLY_NO
    assert llm_called[0] is False


def test_process_certainly_yes_high_confidence_skips_llm(
    engine_with_mocks, minimal_messages, sample_fragment
):
    """L2 预判为 CERTAINLY_YES 且置信度 > 0.85 时，应跳过 L3 LLM 判断"""
    from core.hephaestus.distillation_engine import ValuePrejudgment

    engine_with_mocks._value_prejudgment = SimpleNamespace(
        judge=lambda messages: (ValuePrejudgment.CERTAINLY_YES, 0.9),
    )
    # 提供已准入的 typed outcome，避免 L4 走合法 skip 分支。
    engine_with_mocks._extractor = _typed_extractor(fragments=[sample_fragment])
    llm_called = [False]

    def _track_llm(*args, **kwargs):
        llm_called[0] = True
        return ("knowledge", "", 0.0)

    engine_with_mocks._llm_judge = SimpleNamespace(judge=_track_llm)
    result = _captured_process(engine_with_mocks, "sess-yes", minimal_messages)
    assert result.judgment == "knowledge"
    assert "跳过LLM" in result.judgment_reason
    assert llm_called[0] is False


def test_process_skill_judgment_runs_full_cognition_pipeline(
    engine_with_mocks, minimal_messages, sample_fragment
):
    """skill 是认知资产类型，不得在 L3 后提前返回。"""
    engine_with_mocks._llm_judge = SimpleNamespace(
        judge=lambda session_text, session_id: ("skill", "认知决策资产候选", 0.6),
    )
    engine_with_mocks._extractor = _typed_extractor(fragments=[sample_fragment], judgment="skill")
    result = _captured_process(engine_with_mocks, "sess-skill", minimal_messages)

    assert result.judgment == "skill"
    assert result.fragments == [sample_fragment]
    assert any(layer.name == "knowledge_extraction" for layer in result.layer_results)
    assert any(layer.name == "self_check" for layer in result.layer_results)
    assert any(layer.name == "cross_agent_linking" for layer in result.layer_results)
    assert any(layer.name == "feedback_loop" for layer in result.layer_results)
    # 派生 proposal 只能在完整认知资产落盘后生成。
    engine_with_mocks._backends.skill.call.assert_not_called()


def test_process_promotes_admitted_skill_even_when_l3_said_knowledge(
    engine_with_mocks, minimal_messages, sample_fragment
):
    """The admitted extraction union may promote a preliminary knowledge verdict."""
    engine_with_mocks._extractor = _typed_extractor(fragments=[sample_fragment], judgment="skill")

    result = _captured_process(
        engine_with_mocks,
        "sess-promoted-skill",
        minimal_messages,
    )

    assert result.extraction_judgment == "skill"
    assert result.judgment == "skill"
    assert result.fragments == [sample_fragment]
    assert any(
        layer.name == "typed_judgment_resolution" and layer.passed for layer in result.layer_results
    )


def test_process_l3_skill_cannot_be_silently_downgraded_by_extractor(
    engine_with_mocks, minimal_messages, sample_fragment
):
    """A preliminary skill verdict must retry if extraction returns knowledge."""
    engine_with_mocks._llm_judge = SimpleNamespace(
        judge=lambda session_text, session_id: ("skill", "认知资产候选", 0.7),
    )
    engine_with_mocks._extractor = _typed_extractor(
        fragments=[sample_fragment], judgment="knowledge"
    )

    result = _captured_process(
        engine_with_mocks,
        "sess-skill-mismatch",
        minimal_messages,
    )

    assert result.judgment == "error"
    assert result.error == "skill_extraction_judgment_mismatch"
    assert any(
        layer.name == "typed_judgment_resolution" and not layer.passed
        for layer in result.layer_results
    )
    assert not any(layer.name == "self_check" for layer in result.layer_results)


def test_process_knowledge_judgment_extracts_fragments(
    engine_with_mocks, minimal_messages, sample_fragment
):
    """L3 判断为 knowledge 时，应进入 L4 提取片段，并执行 L5-L7"""
    engine_with_mocks._extractor = _typed_extractor(fragments=[sample_fragment])
    result = _captured_process(
        engine_with_mocks,
        "sess-knowledge",
        minimal_messages,
        meta={"source": "test-agent"},
    )
    assert result.judgment == "knowledge"
    assert len(result.fragments) == 1
    assert result.fragments[0].title == "Redis 连接池耗尽问题的排查方案"
    assert result.source == "test-agent"
    # L5-L7 层结果应存在
    assert any(lr.name == "self_check" for lr in result.layer_results)
    assert any(lr.name == "cross_agent_linking" for lr in result.layer_results)
    assert any(lr.name == "feedback_loop" for lr in result.layer_results)


def test_process_non_skip_empty_fragments_fails_closed(engine_with_mocks, minimal_messages):
    """非 skip 的空片段不得被降级为 skip，必须以契约错误终止。"""
    from core.hephaestus.distillation_contract import (
        ContractValidationResult,
        canonical_extraction_output_hash,
    )
    from core.hephaestus.distillation_models import ExtractionOutcome

    def _unsafe_empty_outcome(request, *, prepared=None):
        structured_output = _v4_structured_output(request.input_spec)
        # 模拟一个错误/旧实现伪称其已准入；引擎必须重新验证，不能信任它。
        return ExtractionOutcome(
            judgment="knowledge",
            fragments=(),
            structured_output=structured_output,
            canonical_output={},
            admission=ContractValidationResult(output_judgment="knowledge"),
            canonical_output_hash=canonical_extraction_output_hash(
                canonical_output={},
            ),
        )

    def _prepare(request):
        from core.hephaestus.distill_input_spec import PreparedExtractionPrompt

        return PreparedExtractionPrompt.build("unsafe empty outcome fixture", request)

    engine_with_mocks._extractor = SimpleNamespace(
        extract=_unsafe_empty_outcome,
        prepare_prompt=_prepare,
    )
    result = _captured_process(engine_with_mocks, "sess-none", minimal_messages)
    assert result.judgment == "error"
    assert result.error == "extraction_contract_rejected"
    assert result.extraction_contract_valid is False
    assert "提取无有效" not in result.judgment_reason


def test_process_records_layer_results(engine_with_mocks, minimal_messages, sample_fragment):
    """process() 应在 result.layer_results 中记录每层执行结果"""
    engine_with_mocks._extractor = _typed_extractor(fragments=[sample_fragment])
    result = _captured_process(engine_with_mocks, "sess-layers", minimal_messages)
    layer_names = [lr.name for lr in result.layer_results]
    assert "noise_filter" in layer_names
    assert "value_prejudgment" in layer_names
    assert "llm_value_judge" in layer_names
    assert "knowledge_extraction" in layer_names
    assert "self_check" in layer_names
    assert "cross_agent_linking" in layer_names
    assert "feedback_loop" in layer_names


def test_process_stops_after_fatal_self_check(engine_with_mocks, minimal_messages, sample_fragment):
    """fatal 自检失败应停在 L5，不再进入跨 Agent 关联和反馈循环。"""

    def _fatal_check(fragments, messages):
        for frag in fragments:
            frag.self_check_passed = False
            frag.self_check_severity = "fatal"
            frag.self_check_issues = ["Python代码块可能存在语法错误"]
        return False, ["Python代码块可能存在语法错误"]

    engine_with_mocks._extractor = _typed_extractor(fragments=[sample_fragment])
    engine_with_mocks._self_check = SimpleNamespace(check=_fatal_check)
    engine_with_mocks._cross_linker = SimpleNamespace(
        link=MagicMock(return_value=[sample_fragment])
    )
    engine_with_mocks._feedback_loop = SimpleNamespace(evaluate=MagicMock(return_value=[]))

    result = _captured_process(engine_with_mocks, "sess-fatal", minimal_messages)
    layer_names = [lr.name for lr in result.layer_results]

    assert result.self_check_severity == "fatal"
    assert "self_check" in layer_names
    assert "cross_agent_linking" not in layer_names
    assert "feedback_loop" not in layer_names
    engine_with_mocks._cross_linker.link.assert_not_called()
    engine_with_mocks._feedback_loop.evaluate.assert_not_called()


# ========== 2. DistillationResult / PipelineLayerResult dataclass ==========


def test_distillation_result_defaults():
    """DistillationResult 默认值应正确"""
    from core.hephaestus.distillation_engine import DistillationResult

    result = DistillationResult(session_id="test")
    assert result.judgment == "skip"
    assert result.fragments == []
    assert result.layer_results == []
    assert result.self_check_passed is True
    assert result.self_check_severity == "ok"
    assert result.truncated is False


def test_distillation_result_output_contract_fields_serialize():
    """DistillationResult 输出契约字段必须随 dataclass 序列化保留。"""
    from dataclasses import asdict

    from core.hephaestus.distillation_engine import DistillationResult

    result = DistillationResult(
        session_id="contract",
        data_profile={"kind": "tabular", "rows": 12},
        anomalies=[{"type": "missing_column", "field": "owner"}],
        needs_reconfirm=True,
        reconfirm_question="是否确认 owner 字段缺失？",
        prejudgment="MAYBE",
        prejudgment_confidence=0.73,
    )

    payload = asdict(result)

    assert payload["data_profile"] == {"kind": "tabular", "rows": 12}
    assert payload["anomalies"] == [{"type": "missing_column", "field": "owner"}]
    assert payload["needs_reconfirm"] is True
    assert payload["reconfirm_question"] == "是否确认 owner 字段缺失？"
    assert payload["prejudgment_confidence"] == 0.73


def test_pipeline_layer_result_structure():
    """PipelineLayerResult 字段应完整"""
    from core.hephaestus.distillation_engine import PipelineLayerResult

    lr = PipelineLayerResult(
        layer=3, name="llm_value_judge", passed=True, detail={"judgment": "knowledge"}
    )
    assert lr.layer == 3
    assert lr.name == "llm_value_judge"
    assert lr.passed is True
    assert lr.detail["judgment"] == "knowledge"


# ========== 3. KnowledgeFragment 数据模型 ==========


def test_knowledge_fragment_defaults():
    """KnowledgeFragment 可选字段应有正确默认值"""
    from core.hephaestus.distillation_engine import KnowledgeFragment

    frag = KnowledgeFragment(
        form="decision",
        title="测试决策",
        frontmatter={},
        background="",
        core_content="内容",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )
    assert frag.relations == []
    assert frag.self_check_passed is True
    assert frag.self_check_issues == []
    assert frag.cross_agent_links == []
    assert frag.keywords == []
    assert frag.ai_expansion == ""


def test_knowledge_fragment_with_all_fields():
    """KnowledgeFragment 应支持全部字段初始化"""
    from core.hephaestus.distillation_engine import KnowledgeFragment

    frag = KnowledgeFragment(
        form="heuristic",
        title="经验法则",
        frontmatter={"领域": "devops"},
        background="背景信息",
        core_content="核心内容",
        boundaries={"applies": "适用场景"},
        anti_patterns=["反例1"],
        related_concepts=["概念A"],
        relations=[{"target": "概念B", "type": "related_to"}],
        self_check_passed=False,
        self_check_issues=["标题过短"],
        cross_agent_links=["其他页面"],
        keywords=["keyword1"],
        ai_expansion="AI 扩充内容",
    )
    assert frag.form == "heuristic"
    assert frag.self_check_passed is False
    assert len(frag.relations) == 1
    assert frag.ai_expansion == "AI 扩充内容"


# ========== 4. extract_json() ==========


def test_extract_json_direct_parse():
    """标准 JSON 字符串应直接解析"""
    from core.hephaestus.distillation_engine import extract_json

    data = extract_json('{"judgment": "knowledge", "confidence": 0.8}')
    assert data == {"judgment": "knowledge", "confidence": 0.8}


def test_extract_json_from_markdown_code_block():
    """markdown 代码块中的 JSON 应被正确提取"""
    from core.hephaestus.distillation_engine import extract_json

    text = '```json\n{"judgment": "knowledge"}\n```'
    data = extract_json(text)
    assert data == {"judgment": "knowledge"}


def test_extract_json_with_surrounding_text():
    """前后有说明文字的 JSON 应被正确提取"""
    from core.hephaestus.distillation_engine import extract_json

    text = 'Here is the result:\n{"judgment": "skip", "reason": "too short"}\nHope this helps.'
    data = extract_json(text)
    assert data == {"judgment": "skip", "reason": "too short"}


def test_extract_json_trailing_comma_fix():
    """尾随逗号的 JSON 应被自动修复"""
    from core.hephaestus.distillation_engine import extract_json

    text = '{"a": 1, "b": 2,}'
    data = extract_json(text)
    assert data == {"a": 1, "b": 2}


def test_extract_json_empty_returns_none():
    """空字符串应返回 None"""
    from core.hephaestus.distillation_engine import extract_json

    assert extract_json("") is None
    assert extract_json(None) is None


def test_extract_json_unparseable_returns_none():
    """完全无法解析的文本应返回 None"""
    from core.hephaestus.distillation_engine import extract_json

    assert extract_json("not json at all") is None


# ========== 5. build_session_text() ==========


def test_build_session_text_full_mode():
    """短会话应使用 full 模式，不截断"""
    from core.hephaestus.distillation_engine import build_session_text

    messages = [
        {"role": "user", "content": "问题1"},
        {"role": "assistant", "content": "回答1"},
    ]
    meta = {}
    text = build_session_text(messages, max_tokens=800, out_meta=meta)
    assert "[user] 问题1" in text
    assert "[assistant] 回答1" in text
    assert meta["distill_input_mode"] == "full"
    assert meta["truncated"] is False


def test_build_session_text_head_tail_truncation():
    """超长会话应使用 head-tail 模式截断"""
    from core.hephaestus.distillation_engine import build_session_text

    messages = [{"role": "user", "content": f"消息{i}: " + "x" * 500} for i in range(20)]
    meta = {}
    text = build_session_text(messages, max_tokens=800, out_meta=meta)
    assert "omitted" in text or "省略" in text
    assert meta["truncated"] is True
    assert meta["distill_input_mode"] == "head_tail"
    assert meta["head_turns"] > 0
    assert meta["tail_turns"] > 0


def test_build_session_text_skips_empty_content():
    """空内容消息应被跳过"""
    from core.hephaestus.distillation_engine import build_session_text

    messages = [
        {"role": "user", "content": ""},
        {"role": "assistant", "content": "   "},
        {"role": "user", "content": "有效内容"},
    ]
    text = build_session_text(messages, max_tokens=800)
    assert "有效内容" in text
    assert "[user]" in text
    # 只有一条有效消息
    assert text.count("[") == 1


def test_build_session_text_uses_explicit_token_limits():
    """Token budget resolution preserves explicit total and per-message limits."""
    from core.hephaestus.distillation_text import _resolve_token_limits

    effective_max_tokens, effective_msg_limit = _resolve_token_limits(
        max_tokens=800,
        per_message_token_limit=160,
    )

    assert effective_max_tokens == 800
    assert effective_msg_limit == 160


# ========== 6. clean_message_content() ==========


def test_clean_message_content_removes_thinking_blocks():
    """thinking 块应被移除"""
    from core.hephaestus.distillation_engine import clean_message_content

    content = "正常内容\n[thinking]\n内部思考\n[/thinking]\n后续内容"
    cleaned = clean_message_content(content)
    assert "内部思考" not in cleaned
    assert "正常内容" in cleaned
    assert "后续内容" in cleaned


def test_clean_message_content_keeps_long_code_block_lossless():
    """长代码块属于蒸馏证据，必须完整保留。"""
    from core.hephaestus.distillation_engine import clean_message_content

    code = "```python\n" + "\n".join(f"line{i}" for i in range(15)) + "\n```"
    cleaned = clean_message_content(code)
    assert cleaned == code
    assert "omitted" not in cleaned


def test_clean_message_content_keeps_short_code_block():
    """不超过 10 行的代码块应保留完整"""
    from core.hephaestus.distillation_engine import clean_message_content

    code = "```python\nline1\nline2\nline3\n```"
    cleaned = clean_message_content(code)
    assert "line1" in cleaned
    assert "line2" in cleaned
    assert "line3" in cleaned
    assert "omitted" not in cleaned


def test_clean_message_content_keeps_all_shell_commands():
    """连续 shell 命令可能承载关键步骤，不能只保留前三条。"""
    from core.hephaestus.distillation_engine import clean_message_content

    content = "git init\ngit add .\ngit commit -m 'init'\ngit push\ngit log"
    cleaned = clean_message_content(content)
    assert "git init" in cleaned
    assert "git add" in cleaned
    assert "git commit" in cleaned
    assert "git push" in cleaned
    assert "git log" in cleaned
    assert "omitted" not in cleaned


def test_clean_message_content_keeps_chinese_shell_lines():
    """含中文解释的命令行应保留"""
    from core.hephaestus.distillation_engine import clean_message_content

    content = "git status 可以用来查看当前改动\nnpm install lodash"
    cleaned = clean_message_content(content)
    assert "git status 可以用来查看当前改动" in cleaned
    assert "npm install lodash" in cleaned


# ========== 7. ValuePrejudgment.judge() ==========


def test_value_prejudgment_certainly_no_for_empty():
    """空会话应返回 CERTAINLY_NO"""
    from core.hephaestus.distillation_engine import ValuePrejudgment

    vp = ValuePrejudgment()
    verdict, confidence = vp.judge([])
    assert verdict == ValuePrejudgment.CERTAINLY_NO
    assert confidence >= 0.5


def test_value_prejudgment_certainly_yes_for_rich_content():
    """包含丰富知识信号的会话应返回 CERTAINLY_YES 或高置信度 MAYBE"""
    from core.hephaestus.distillation_engine import ValuePrejudgment

    vp = ValuePrejudgment()
    messages = [
        {
            "role": "assistant",
            "content": "因为根因是连接池上限过低，所以解决方案是增加 max_connections。"
            "最佳实践是设置 socket_timeout。踩坑经验：不要直接调大连接池。",
        },
    ]
    verdict, confidence = vp.judge(messages)
    # 丰富内容应至少为 MAYBE，通常为 CERTAINLY_YES
    assert verdict in (ValuePrejudgment.CERTAINLY_YES, ValuePrejudgment.MAYBE)
    assert confidence > 0.3


def test_value_prejudgment_certainly_no_for_noise():
    """纯噪音会话应返回 CERTAINLY_NO"""
    from core.hephaestus.distillation_engine import ValuePrejudgment

    vp = ValuePrejudgment()
    messages = [
        {"role": "user", "content": "好的 收到 谢谢"},
        {"role": "assistant", "content": "ok thanks got it"},
    ]
    verdict, confidence = vp.judge(messages)
    assert verdict == ValuePrejudgment.CERTAINLY_NO


# ========== 8. NoiseFilter.filter() ==========


def test_noise_filter_keeps_system_messages():
    """系统消息应始终保留"""
    from core.hephaestus.distillation_engine import NoiseFilter

    nf = NoiseFilter()
    messages = [
        {"role": "system", "content": "你是一个助手"},
        {"role": "user", "content": "好的"},
    ]
    filtered, stats = nf.filter(messages)
    assert any(m["role"] == "system" for m in filtered)
    assert stats["total"] == 2


def test_noise_filter_removes_noise():
    """噪音消息应被过滤"""
    from core.hephaestus.distillation_engine import NoiseFilter

    nf = NoiseFilter()
    messages = [
        {"role": "user", "content": "好的"},
        {"role": "user", "content": "谢谢"},
        {"role": "assistant", "content": "这是一个有实质内容的回答，包含解决方案和步骤。"},
    ]
    filtered, stats = nf.filter(messages)
    # "好的" 和 "谢谢" 应被过滤，assistant 有实质内容应保留
    assert stats["noise"] >= 1
    assert stats["kept"] < stats["total"]


def test_noise_filter_empty_input():
    """空输入应返回空列表和零统计"""
    from core.hephaestus.distillation_engine import NoiseFilter

    nf = NoiseFilter()
    filtered, stats = nf.filter([])
    assert filtered == []
    assert stats["total"] == 0
    assert stats["kept"] == 0


# ========== 9. generate_wiki_page() ==========


def test_generate_wiki_page_structure(sample_fragment):
    """生成的 wiki 页面应包含必需的 frontmatter 和 body 结构"""
    from core.hephaestus.distillation_engine import generate_wiki_page

    input_spec = _write_input_spec("sess-001", source_agent="test-agent")
    page = generate_wiki_page(
        sample_fragment,
        "sess-001",
        source="test-agent",
        structured_output=_valid_structured_distill_output(input_spec),
    )
    # 应包含 YAML frontmatter
    assert page.startswith("---")
    assert "类型:" in page
    assert "名称:" in page
    # 应包含标题
    assert "# Redis 连接池耗尽问题的排查方案" in page
    # 应包含来源信息
    assert "test-agent" in page
    assert "来源事件ID:" in page
    assert "raw-1" in page
    assert "门禁决策ID:" in page
    assert input_spec.gate_decision_id in page
    assert "## 来源追踪" in page
    assert "- Raw 事件: `raw-1`" in page
    # 应包含可信度提示
    assert "置信度" in page


def test_generate_wiki_page_truncated_flag(sample_fragment):
    """truncated=True 时页面应包含截断警告"""
    from core.hephaestus.distillation_engine import generate_wiki_page

    page = generate_wiki_page(sample_fragment, "sess-001", truncated=True)
    assert "截断" in page or "truncated" in page.lower() or "不完整" in page


# ========== 10. _map_form_to_type() ==========


def test_map_form_to_type_known_forms():
    """已知知识形态应正确映射到实体类型"""
    from core.hephaestus.distillation_engine import _map_form_to_type

    assert _map_form_to_type("问题-解决") == "concept"
    assert _map_form_to_type("decision-log") == "project"
    assert _map_form_to_type("heuristic") == "concept"
    assert _map_form_to_type("snippet") == "technology"


def test_map_form_to_type_unknown_defaults_concept():
    """未知知识形态应默认映射为 concept"""
    from core.hephaestus.distillation_engine import _map_form_to_type

    assert _map_form_to_type("unknown-form") == "concept"


# ========== 11. DistillationEngine.write_pages() ==========


def test_write_pages_skip_non_knowledge(engine_with_mocks, tmp_path):
    """judgment != knowledge 时不应写入任何文件"""
    from core.hephaestus.distillation_engine import DistillationResult

    result = DistillationResult(session_id="sess-skip", judgment="skip")
    written = engine_with_mocks.write_pages(result)
    assert written == []
    assert not list(tmp_path.rglob("*.md"))


def test_write_pages_creates_files(
    engine_with_mocks,
    tmp_path,
    sample_fragment,
    _canonical_material_actions,
):
    """judgment == knowledge 时应创建 wiki 文件"""
    from core.hephaestus.distillation_engine import DistillationResult

    input_spec = _write_input_spec("sess-write")
    result = DistillationResult(
        session_id="sess-write",
        judgment="knowledge",
        fragments=[sample_fragment],
        structured_output=_valid_structured_distill_output(input_spec),
        **_bound_write_result_kwargs(input_spec, fragments=[sample_fragment]),
    )
    written = engine_with_mocks.write_pages(result)
    assert len(written) == 1
    assert written[0].endswith(".md")
    assert Path(written[0]).exists()


def _write_input_spec(session_id: str, *, source_agent: str = "codex"):
    """Build the exact immutable provenance required for a live write fixture."""
    from core.hephaestus.distill_input_spec import DistillInputSpec

    visible_input = f"write fixture input for {session_id}"
    return DistillInputSpec.build(
        source_agent=source_agent,
        source_session_id=session_id,
        source_event_ids=("raw-1",),
        raw_completeness="full",
        visible_input=visible_input,
        input_mode="standard",
        source_messages=(
            exact_source_message(
                role="user",
                content=visible_input,
                revision_id="raw-1",
            ),
        ),
    )


def _bound_write_result_kwargs(input_spec, *, fragments=None, structured_output=None):
    """Build a typed root-admission proof for direct strict-write fixtures."""
    kwargs = {
        "input_spec": input_spec,
        "source": input_spec.source_agent,
        "raw_completeness": input_spec.raw_completeness,
    }
    if fragments is None:
        return kwargs

    from core.hephaestus.distillation_contract import (
        canonical_extraction_output_hash,
        canonicalize_extraction_output,
        validate_extraction_output,
    )
    from core.hephaestus.distillation_models import FragmentRouteCapability

    structured = structured_output or _valid_structured_distill_output(input_spec)
    root = canonicalize_extraction_output(
        {
            "judgment": "knowledge",
            "judgment_reason": "直接写入 fixture 已携带受准入的根输出。",
            "fragments": [],
            "structured_output": structured,
        },
        fragments,
    )
    validation = validate_extraction_output(root, input_spec)
    assert validation.valid, validation.error_text
    root_hash = canonical_extraction_output_hash(canonical_output=root)
    kwargs.update(
        {
            "extraction_judgment": "knowledge",
            "extraction_contract_valid": True,
            "extraction_output": root,
            "extraction_output_hash": root_hash,
            "fragment_route_capability": FragmentRouteCapability(
                extraction_output_hash=root_hash,
                input_spec_hash=input_spec.input_spec_hash,
                fragments=tuple(fragments),
            ),
        }
    )
    return kwargs


def _valid_structured_distill_output(input_spec=None):
    """A complete resolved v4 payload bound to the exact result input spec."""
    input_spec = input_spec or _write_input_spec("sess-write")
    payload = {
        "judgment": "knowledge",
        "judgment_reason": "write fixture root",
        "fragments": [],
        "structured_output": _v4_structured_output(input_spec),
    }
    return resolve_model_evidence(payload, input_spec)["structured_output"]


def _admit_write_result(result):
    """Bind a direct-write fixture to the same immutable root used in production."""
    input_spec = _write_input_spec(result.session_id)
    for fragment in result.fragments:
        if not fragment.claim_ids:
            fragment.claim_ids = ["claim-redis-pool"]
    structured_output = _valid_structured_distill_output(input_spec)
    result.structured_output = structured_output
    for key, value in _bound_write_result_kwargs(
        input_spec,
        fragments=result.fragments,
        structured_output=structured_output,
    ).items():
        setattr(result, key, value)
    return result


def _disable_structured_contract_gate(monkeypatch, engine):
    """Keep write-quality tests focused without bypassing any other runtime behavior."""
    from core.config import get_config as real_get_config

    base = real_get_config()

    class _GateDisabledConfig:
        def get(self, key, default=None):
            if key == "distill.structured_output_contract.enforce":
                return False
            return base.get(key, default)

        def __getattr__(self, name):
            return getattr(base, name)

    config = _set_runtime_config(engine, _GateDisabledConfig())
    monkeypatch.setattr(
        "core.hephaestus.distillation_engine.get_config",
        lambda: config,
    )


def test_write_pages_strict_contract_blocks_missing_structured_output(
    engine_with_mocks, sample_fragment, monkeypatch, tmp_path
):
    """严格契约开启时，缺少 distill_output_v4 不应写入正式 Wiki。"""
    from core.hephaestus.distillation_engine import DistillationResult

    class _Cfg:
        def get(self, key, default=None):
            values = {
                "distill.structured_output_contract.enforce": True,
                "quality_gate.enabled": False,
                "distill.auto_expression_formatting": False,
                "distill.fragment_boundary_chars": 8000,
                "distill.min_session_fragment_pass_ratio": 0.5,
            }
            return values.get(key, default)

    saved = []

    def _fake_save(
        sid,
        frags,
        errs,
        source="",
        raw_response="",
        exc_info="",
        parse_metadata=None,
        database_dir=None,
    ):
        saved.append((sid, errs, len(frags)))
        return tmp_path / "failed.json"

    cfg = _set_runtime_config(engine_with_mocks, _Cfg())
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: cfg)
    monkeypatch.setattr("core.hephaestus.distillation_engine._save_failed_distill", _fake_save)

    input_spec = _write_input_spec("sess-strict-missing-contract")
    result = DistillationResult(
        session_id="sess-strict-missing-contract",
        judgment="knowledge",
        fragments=[sample_fragment],
        **_bound_write_result_kwargs(input_spec, fragments=[sample_fragment]),
    )

    written = engine_with_mocks.write_pages(result)

    assert written == []
    assert saved
    assert "structured distillation output" in saved[0][1][0]


def test_write_pages_strict_contract_rejects_missing_input_spec(
    engine_with_mocks, sample_fragment, monkeypatch, tmp_path
):
    """Live writes fail closed even if a detached v4 payload itself looks valid."""
    from core.hephaestus.distillation_engine import DistillationResult

    class _Cfg:
        def get(self, key, default=None):
            values = {
                "distill.structured_output_contract.enforce": True,
                "quality_gate.enabled": False,
                "distill.auto_expression_formatting": False,
                "distill.fragment_boundary_chars": 8000,
                "distill.min_session_fragment_pass_ratio": 0.5,
            }
            return values.get(key, default)

    saved = []
    cfg = _set_runtime_config(engine_with_mocks, _Cfg())
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: cfg)
    monkeypatch.setattr(
        "core.hephaestus.distillation_engine._save_failed_distill",
        lambda sid, frags, errs, **kwargs: saved.append((sid, errs)) or tmp_path / "failed.json",
    )

    detached_spec = _write_input_spec("sess-strict-missing-input")
    result = DistillationResult(
        session_id="sess-strict-missing-input",
        judgment="knowledge",
        fragments=[sample_fragment],
        structured_output=_valid_structured_distill_output(detached_spec),
    )

    assert engine_with_mocks.write_pages(result) == []
    assert saved and "input_spec" in saved[0][1][0]


def test_write_pages_strict_contract_rejects_missing_root_admission_proof(
    engine_with_mocks, sample_fragment, monkeypatch, tmp_path
):
    """A hand-assembled result cannot write merely by echoing a valid v4 payload."""
    from core.hephaestus.distillation_engine import DistillationResult

    class _Cfg:
        def get(self, key, default=None):
            values = {
                "distill.structured_output_contract.enforce": True,
                "quality_gate.enabled": False,
                "distill.auto_expression_formatting": False,
                "distill.fragment_boundary_chars": 8000,
                "distill.min_session_fragment_pass_ratio": 0.5,
            }
            return values.get(key, default)

    saved = []
    cfg = _set_runtime_config(engine_with_mocks, _Cfg())
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: cfg)
    monkeypatch.setattr(
        "core.hephaestus.distillation_engine._save_failed_distill",
        lambda sid, frags, errs, **kwargs: saved.append((sid, errs)) or tmp_path / "failed.json",
    )

    input_spec = _write_input_spec("sess-strict-missing-root-proof")
    result = DistillationResult(
        session_id="sess-strict-missing-root-proof",
        judgment="knowledge",
        fragments=[sample_fragment],
        structured_output=_valid_structured_distill_output(input_spec),
        **_bound_write_result_kwargs(input_spec),
    )

    assert engine_with_mocks.write_pages(result) == []
    assert saved and "admission proof is missing" in saved[0][1][0]


def test_write_pages_strict_contract_allows_valid_create_output(
    engine_with_mocks,
    sample_fragment,
    monkeypatch,
    _canonical_material_actions,
):
    """严格契约开启时，合法 create_page 契约允许写入。"""
    from core.hephaestus.distillation_engine import DistillationResult

    class _Cfg:
        def get(self, key, default=None):
            values = {
                "distill.structured_output_contract.enforce": True,
                "quality_gate.enabled": False,
                "distill.auto_expression_formatting": False,
                "distill.fragment_boundary_chars": 8000,
                "distill.min_session_fragment_pass_ratio": 0.5,
            }
            return values.get(key, default)

    cfg = _set_runtime_config(engine_with_mocks, _Cfg())
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: cfg)

    input_spec = _write_input_spec("sess-strict-valid-contract")
    structured_output = _valid_structured_distill_output(input_spec)
    result = DistillationResult(
        session_id="sess-strict-valid-contract",
        judgment="knowledge",
        fragments=[sample_fragment],
        structured_output=structured_output,
        **_bound_write_result_kwargs(
            input_spec,
            fragments=[sample_fragment],
            structured_output=structured_output,
        ),
    )

    written = engine_with_mocks.write_pages(result)

    assert len(written) == 1
    assert Path(written[0]).exists()
    page = Path(written[0]).read_text(encoding="utf-8")
    assert "来源事件ID:" in page
    assert "raw-1" in page
    assert "证据引用:" in page
    assert "Raw完整度:" in page
    assert "门禁决策ID:" in page
    assert f"- 门禁决策: `{input_spec.gate_decision_id}`" in page


def test_write_pages_strict_contract_routes_dispute_without_normal_inbox(
    engine_with_mocks,
    sample_fragment,
    monkeypatch,
    tmp_path,
    _canonical_material_actions,
):
    """冲突契约应进入 DisputeResolver，不能被普通 Inbox 写入误当成新知识页。"""
    from core.hephaestus.distillation_engine import DistillationResult

    class _Cfg:
        def get(self, key, default=None):
            values = {
                "distill.structured_output_contract.enforce": True,
                "quality_gate.enabled": False,
                "distill.auto_expression_formatting": False,
                "distill.fragment_boundary_chars": 8000,
                "distill.min_session_fragment_pass_ratio": 0.5,
            }
            return values.get(key, default)

    input_spec = _write_input_spec("sess-strict-dispute-contract")
    structured_output = _valid_structured_distill_output(input_spec)
    structured_output["distill_intent"] = "dispute"
    structured_output["claims"][0]["relation_to_existing"] = {
        "type": "contradicts",
        "target_pages": ["03-Tech/redis-连接池.md"],
        "delta_text": "新证据认为连接泄漏比上限过低更关键。",
        "reason": "和既有结论的因果权重冲突。",
    }
    structured_output["claims"][0]["recommended_action"] = "route_to_dispute"

    cfg = _set_runtime_config(engine_with_mocks, _Cfg())
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: cfg)
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *args, **kwargs: None,
    )

    result = DistillationResult(
        session_id="sess-strict-dispute-contract",
        judgment="knowledge",
        fragments=[sample_fragment],
        structured_output=structured_output,
        **_bound_write_result_kwargs(
            input_spec,
            fragments=[sample_fragment],
            structured_output=structured_output,
        ),
    )

    written = engine_with_mocks.write_pages(result)

    assert len(written) == 1
    assert "08-Disputes" in written[0]
    assert Path(written[0]).exists()
    assert not any((tmp_path / "00-Inbox").glob("*.md"))


def test_write_pages_blocks_fatal_self_check(
    engine_with_mocks, sample_fragment, monkeypatch, tmp_path
):
    """fatal 自检失败不应写入正式 Wiki，应保存事故证据。"""
    from core.hephaestus.distillation_engine import DistillationResult

    saved = []

    def _fake_save(
        sid,
        frags,
        errs,
        source="",
        raw_response="",
        exc_info="",
        severity="high",
        parse_metadata=None,
        database_dir=None,
    ):
        saved.append((sid, errs, len(frags), severity))
        return tmp_path / "failed.json"

    monkeypatch.setattr("core.hephaestus.distillation_engine._save_failed_distill", _fake_save)

    sample_fragment.self_check_passed = False
    sample_fragment.self_check_severity = "fatal"
    sample_fragment.self_check_issues = ["Python代码块可能存在语法错误"]
    input_spec = _write_input_spec("sess-fatal-self-check")
    result = DistillationResult(
        session_id="sess-fatal-self-check",
        judgment="knowledge",
        fragments=[sample_fragment],
        structured_output=_valid_structured_distill_output(input_spec),
        **_bound_write_result_kwargs(input_spec, fragments=[sample_fragment]),
    )

    written = engine_with_mocks.write_pages(result)

    assert written == []
    assert saved and saved[0][0] == "sess-fatal-self-check"
    assert "自检fatal" in saved[0][1][0]
    assert saved[0][3] == "high"


def test_write_pages_allows_warning_self_check_with_pending_marker(
    engine_with_mocks,
    sample_fragment,
    _canonical_material_actions,
):
    """warning 自检失败允许写入，但页面必须显式标记待验证。"""
    from core.hephaestus.distillation_engine import DistillationResult

    sample_fragment.self_check_passed = False
    sample_fragment.self_check_severity = "warning"
    sample_fragment.self_check_issues = ["包含当前性表述，已标记为 contextual"]
    sample_fragment.frontmatter["verification"] = "pending-verification"
    input_spec = _write_input_spec("sess-warning-self-check")
    structured_output = _valid_structured_distill_output(input_spec)
    result = DistillationResult(
        session_id="sess-warning-self-check",
        judgment="knowledge",
        fragments=[sample_fragment],
        structured_output=structured_output,
        **_bound_write_result_kwargs(
            input_spec,
            fragments=[sample_fragment],
            structured_output=structured_output,
        ),
    )

    written = engine_with_mocks.write_pages(result)

    assert len(written) == 1
    page = Path(written[0]).read_text(encoding="utf-8")
    assert "验证状态: pending-verification" in page
    assert "验证等级: warning" in page


def test_write_pages_rejects_quality_gate_failure(
    engine_with_mocks, sample_fragment, monkeypatch, tmp_path
):
    """QualityGate reject 应进入失败记录，不写正式 Wiki。"""
    from core.hephaestus.distillation_engine import DistillationResult

    class _Cfg:
        def get(self, key, default=None):
            values = {
                "distill.structured_output_contract.enforce": False,
                "quality_gate.enabled": True,
                "quality_gate.base_threshold": 0.95,
                "quality_gate.review_margin": 0.01,
                "distill.auto_expression_formatting": False,
                "distill.fragment_boundary_chars": 8000,
                "distill.min_session_fragment_pass_ratio": 0.5,
            }
            return values.get(key, default)

    saved = []

    def _fake_save(
        sid,
        frags,
        errs,
        source="",
        raw_response="",
        exc_info="",
        parse_metadata=None,
        database_dir=None,
    ):
        saved.append((sid, errs, len(frags)))
        return tmp_path / "failed.json"

    cfg = _set_runtime_config(engine_with_mocks, _Cfg())
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: cfg)
    monkeypatch.setattr("core.hephaestus.distillation_engine._save_failed_distill", _fake_save)

    result = DistillationResult(
        session_id="sess-quality-reject",
        judgment="knowledge",
        fragments=[sample_fragment],
    )

    written = engine_with_mocks.write_pages(result)

    assert written == []
    assert saved and "质量门禁拒绝" in saved[0][1][0]


def test_write_pages_marks_quality_gate_review_pending(
    engine_with_mocks,
    sample_fragment,
    monkeypatch,
    _canonical_material_actions,
):
    """QualityGate review 应允许写入，但页面必须 pending verification。"""
    from core.hephaestus.distillation_engine import DistillationResult

    class _Cfg:
        def get(self, key, default=None):
            values = {
                "distill.structured_output_contract.enforce": False,
                "quality_gate.enabled": True,
                "quality_gate.base_threshold": 0.95,
                "quality_gate.review_margin": 0.3,
                "distill.auto_expression_formatting": False,
                "distill.fragment_boundary_chars": 8000,
                "distill.min_session_fragment_pass_ratio": 0.5,
            }
            return values.get(key, default)

    cfg = _set_runtime_config(engine_with_mocks, _Cfg())
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: cfg)
    result = _admit_write_result(
        DistillationResult(
            session_id="sess-quality-review",
            judgment="knowledge",
            fragments=[sample_fragment],
        )
    )

    written = engine_with_mocks.write_pages(result)

    assert len(written) == 1
    page = Path(written[0]).read_text(encoding="utf-8")
    assert "质量门禁状态: review" in page
    assert "验证状态: pending-verification" in page
    assert "质量门禁建议人工复核" in page


def test_write_pages_rejects_low_cognitive_value_fragment(engine_with_mocks, monkeypatch, tmp_path):
    """格式合格但没有认知贡献的片段不能直接入正式 Wiki。"""
    from core.hephaestus.distillation_engine import DistillationResult, KnowledgeFragment

    class _Cfg:
        def get(self, key, default=None):
            values = {
                "distill.structured_output_contract.enforce": False,
                "quality_gate.enabled": True,
                "quality_gate.base_threshold": 0.55,
                "quality_gate.review_margin": 0.15,
                "quality_gate.cognitive_value.enabled": True,
                "quality_gate.cognitive_value.base_threshold": 0.55,
                "quality_gate.cognitive_value.review_margin": 0.15,
                "distill.auto_expression_formatting": False,
                "distill.fragment_boundary_chars": 8000,
                "distill.min_session_fragment_pass_ratio": 0.5,
            }
            return values.get(key, default)

    generic_fragment = KnowledgeFragment(
        form="concept",
        title="Generic System Components Overview",
        frontmatter={
            "领域": "architecture",
            "置信度": 0.9,
            "摘要": "A structured overview of system components and their broad responsibilities.",
        },
        background="A generic reference page for system components.",
        core_content=(
            "## Overview\n\n"
            "This page describes a component catalog in neutral terms. "
            "It lists modules, boundaries, interfaces, and broad responsibilities. "
            "The content is organized and readable, with plain descriptions for each area. "
            "It is intentionally generic and should remain a reference note until another "
            "source adds concrete operational learning and actionability."
        ),
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )
    saved = []

    def _fake_save(
        sid,
        frags,
        errs,
        source="",
        raw_response="",
        exc_info="",
        parse_metadata=None,
        database_dir=None,
    ):
        saved.append((sid, errs, len(frags)))
        return tmp_path / "failed.json"

    cfg = _set_runtime_config(engine_with_mocks, _Cfg())
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: cfg)
    monkeypatch.setattr("core.hephaestus.distillation_engine._save_failed_distill", _fake_save)

    result = DistillationResult(
        session_id="sess-low-cognitive-value",
        judgment="knowledge",
        fragments=[generic_fragment],
    )

    written = engine_with_mocks.write_pages(result)

    assert written == []
    assert saved and "missing_cognitive_contribution" in saved[0][1][0]


def test_write_pages_records_cognitive_contribution_frontmatter(
    engine_with_mocks,
    sample_fragment,
    monkeypatch,
    _canonical_material_actions,
):
    """入库页面必须说明它贡献给认知系统的类型和消费者。"""
    from core.hephaestus.distillation_engine import DistillationResult

    class _Cfg:
        def get(self, key, default=None):
            values = {
                "distill.structured_output_contract.enforce": False,
                "quality_gate.enabled": True,
                "quality_gate.base_threshold": 0.55,
                "quality_gate.review_margin": 0.15,
                "quality_gate.cognitive_value.enabled": True,
                "quality_gate.cognitive_value.base_threshold": 0.55,
                "quality_gate.cognitive_value.review_margin": 0.15,
                "distill.auto_expression_formatting": False,
                "distill.fragment_boundary_chars": 8000,
                "distill.min_session_fragment_pass_ratio": 0.5,
            }
            return values.get(key, default)

    cfg = _set_runtime_config(engine_with_mocks, _Cfg())
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: cfg)
    sample_fragment.frontmatter["source_event_ids"] = ["evt-redis-1"]

    result = _admit_write_result(
        DistillationResult(
            session_id="sess-cognitive-value",
            judgment="knowledge",
            fragments=[sample_fragment],
        )
    )

    written = engine_with_mocks.write_pages(result)

    assert len(written) == 1
    page = Path(written[0]).read_text(encoding="utf-8")
    assert "认知价值门禁状态: accept" in page
    assert "认知贡献类型:" in page
    assert "认知消费者:" in page


def test_write_pages_records_quality_gate_action_ledger(
    engine_with_mocks,
    sample_fragment,
    monkeypatch,
    tmp_path,
    _canonical_material_actions,
):
    """写入前质量门禁必须留下可审计的全局 ActionLedger 记录。"""
    from core.hephaestus.distillation_engine import DistillationResult
    from core.system_contracts import ActionLedger

    class _Cfg:
        database_dir = tmp_path / "db"

        def get(self, key, default=None):
            values = {
                "distill.structured_output_contract.enforce": False,
                "quality_gate.enabled": True,
                "quality_gate.base_threshold": 0.55,
                "quality_gate.review_margin": 0.15,
                "quality_gate.cognitive_value.enabled": True,
                "quality_gate.cognitive_value.base_threshold": 0.55,
                "quality_gate.cognitive_value.review_margin": 0.15,
                "distill.auto_expression_formatting": False,
                "distill.fragment_boundary_chars": 8000,
                "distill.min_session_fragment_pass_ratio": 0.5,
            }
            return values.get(key, default)

    cfg = _set_runtime_config(engine_with_mocks, _Cfg())
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: cfg)
    sample_fragment.frontmatter["source_event_ids"] = ["evt-redis-ledger"]

    result = _admit_write_result(
        DistillationResult(
            session_id="sess-quality-ledger",
            judgment="knowledge",
            fragments=[sample_fragment],
        )
    )

    written = engine_with_mocks.write_pages(result)

    assert len(written) == 1
    page = Path(written[0]).read_text(encoding="utf-8")
    assert "质量门禁账本ID: obsact-" in page
    rows = ActionLedger(tmp_path / "db" / "action_ledger.db").recent()
    assert rows[0]["action_type"] == "quality_gate"
    assert rows[0]["target"] == "distill:sess-quality-ledger:fragment:0"
    assert rows[0]["status"] == "verified"
    assert rows[0]["verification"]["final_disposition"] == "accept"
    assert "core/hephaestus/cognitive_value_gate.py" in rows[0]["evidence_refs"]


def test_write_pages_applies_expression_formatting_when_enabled(
    engine_with_mocks,
    monkeypatch,
    _canonical_material_actions,
):
    """表达格式化开启后，主写页链路应格式化 core_content 且可通过配置关闭。"""
    from core.hephaestus.distillation_engine import DistillationResult, KnowledgeFragment

    class _Cfg:
        def get(self, key, default=None):
            values = {
                "distill.structured_output_contract.enforce": False,
                "quality_gate.enabled": False,
                "distill.auto_expression_formatting": True,
                "distill.fragment_boundary_chars": 8000,
                "distill.min_session_fragment_pass_ratio": 0.5,
            }
            return values.get(key, default)

    fragment = KnowledgeFragment(
        form="方法论",
        title="部署前检查清单的完整记录",
        frontmatter={"领域": "ops", "摘要": "部署前检查清单的标准化记录。"},
        background="发布前需要把人工核对项变成可以逐项勾选的清单。",
        core_content=(
            "## 检查清单\n\n"
            "检查清单用于上线前确认关键步骤。\n"
            "- 安装最新版 Obsidian\n"
            "- 运行 mnemos doctor\n"
            "- 确认 LLM、embedding、reranker API 地址和 key 均可配置\n"
            "- 完成一次 dry-run，确认不会写入生产数据\n"
        ),
        boundaries={"适用": "部署前核对", "不适用": "运行中故障排查"},
        anti_patterns=[],
        related_concepts=[],
    )
    cfg = _set_runtime_config(engine_with_mocks, _Cfg())
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: cfg)
    result = _admit_write_result(
        DistillationResult(
            session_id="sess-expression-format",
            judgment="knowledge",
            fragments=[fragment],
        )
    )

    written = engine_with_mocks.write_pages(result)

    assert len(written) == 1
    page = Path(written[0]).read_text(encoding="utf-8")
    assert "- [ ] 安装最新版 Obsidian" in page
    assert "表达格式: checklist" in page


def test_write_pages_records_expression_format_when_body_formatting_disabled(
    engine_with_mocks,
    monkeypatch,
    _canonical_material_actions,
):
    """格式化正文关闭时，仍应写 expression_format 供 Obsidian 展示层消费。"""
    from core.hephaestus.distillation_engine import DistillationResult, KnowledgeFragment

    class _Cfg:
        def get(self, key, default=None):
            values = {
                "distill.structured_output_contract.enforce": False,
                "quality_gate.enabled": False,
                "distill.auto_expression_formatting": False,
                "distill.fragment_boundary_chars": 8000,
                "distill.min_session_fragment_pass_ratio": 0.5,
            }
            return values.get(key, default)

    fragment = KnowledgeFragment(
        form="方法论",
        title="部署前检查清单格式建议记录",
        frontmatter={"领域": "ops", "摘要": "部署前检查清单格式建议记录。"},
        background="发布前需要把人工核对项变成可以逐项勾选的清单。",
        core_content=(
            "## 检查清单\n\n"
            "- 安装最新版 Obsidian\n"
            "- 运行 mnemos doctor\n"
            "- 检查 daemon heartbeat 是否记录 wiki_route 最近运行结果\n"
            "- 确认 vault audit 中没有超预算的 Inbox 页面\n"
            "- 记录人工确认人和回滚方式，避免格式建议替代真实审核\n"
        ),
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )
    cfg = _set_runtime_config(engine_with_mocks, _Cfg())
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: cfg)
    result = _admit_write_result(
        DistillationResult(
            session_id="sess-expression-format-suggestion",
            judgment="knowledge",
            fragments=[fragment],
        )
    )

    written = engine_with_mocks.write_pages(result)

    assert len(written) == 1
    page = Path(written[0]).read_text(encoding="utf-8")
    assert "- 安装最新版 Obsidian" in page
    assert "- [ ] 安装最新版 Obsidian" not in page
    assert "表达格式: checklist" in page


def test_write_pages_adds_domain_scores_when_enabled(
    engine_with_mocks,
    sample_fragment,
    monkeypatch,
    _canonical_material_actions,
):
    """domain scorers 开启时，蒸馏页面 frontmatter 应包含领域评分。"""
    from core.hephaestus.distillation_engine import DistillationResult

    class _Cfg:
        def get(self, key, default=None):
            values = {
                "distill.structured_output_contract.enforce": False,
                "quality_gate.enabled": False,
                "scoring.domain_scorers_enabled": True,
                "distill.auto_expression_formatting": False,
                "distill.fragment_boundary_chars": 8000,
                "distill.min_session_fragment_pass_ratio": 0.5,
            }
            return values.get(key, default)

    cfg = _set_runtime_config(engine_with_mocks, _Cfg())
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: cfg)
    result = _admit_write_result(
        DistillationResult(
            session_id="sess-domain-score",
            judgment="knowledge",
            fragments=[sample_fragment],
        )
    )

    written = engine_with_mocks.write_pages(result)

    assert len(written) == 1
    page = Path(written[0]).read_text(encoding="utf-8")
    assert "领域评分:" in page
    assert "kg:" in page
    assert "ops:" in page
    assert "entity_quality:" in page
    assert "capacity_risk:" in page


def test_write_pages_skips_domain_scores_when_disabled(
    engine_with_mocks,
    sample_fragment,
    monkeypatch,
    _canonical_material_actions,
):
    """domain scorers 关闭时，不应写入领域评分 metadata。"""
    from core.hephaestus.distillation_engine import DistillationResult

    class _Cfg:
        def get(self, key, default=None):
            values = {
                "distill.structured_output_contract.enforce": False,
                "quality_gate.enabled": False,
                "scoring.domain_scorers_enabled": False,
                "distill.auto_expression_formatting": False,
                "distill.fragment_boundary_chars": 8000,
                "distill.min_session_fragment_pass_ratio": 0.5,
            }
            return values.get(key, default)

    cfg = _set_runtime_config(engine_with_mocks, _Cfg())
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: cfg)
    result = _admit_write_result(
        DistillationResult(
            session_id="sess-domain-disabled",
            judgment="knowledge",
            fragments=[sample_fragment],
        )
    )

    written = engine_with_mocks.write_pages(result)

    assert len(written) == 1
    page = Path(written[0]).read_text(encoding="utf-8")
    assert "领域评分:" not in page


def test_write_pages_dedups_by_slug(
    engine_with_mocks,
    tmp_path,
    _canonical_material_actions,
):
    """同名 fragment 应通过加序号去重"""
    from core.hephaestus.distillation_engine import DistillationResult, KnowledgeFragment

    frag = KnowledgeFragment(
        form="决策记录",
        title="同名标题的去重测试验证",
        frontmatter={
            "领域": "test",
            "摘要": "这是一个用于测试去重逻辑的知识片段。",
        },
        background="",
        core_content=(
            "## 测试内容\n\n"
            "这段内容用于验证同名 fragment 的去重逻辑。"
            "在知识管理系统中，当多个片段具有相同标题时，"
            "系统需要通过添加序号的方式来区分它们，"
            "确保每个片段都能被正确写入到独立的文件中。\n\n"
            "```python\n"
            "print('hello world')\n"
            "```\n"
        ),
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
        claim_ids=["claim-redis-pool"],
    )
    duplicate_fragment = KnowledgeFragment(
        form=frag.form,
        title=frag.title,
        frontmatter=dict(frag.frontmatter),
        background=frag.background,
        core_content=frag.core_content,
        boundaries=dict(frag.boundaries),
        anti_patterns=list(frag.anti_patterns),
        related_concepts=list(frag.related_concepts),
        claim_ids=list(frag.claim_ids),
    )
    input_spec = _write_input_spec("sess-dedup")
    structured_output = _valid_structured_distill_output(input_spec)
    result = DistillationResult(
        session_id="sess-dedup",
        judgment="knowledge",
        fragments=[frag, duplicate_fragment],
        structured_output=structured_output,
        **_bound_write_result_kwargs(
            input_spec,
            fragments=[frag, duplicate_fragment],
            structured_output=structured_output,
        ),
    )
    written = engine_with_mocks.write_pages(result)
    assert len(written) == 2
    # 第二个文件应有序号后缀
    slugs = [Path(p).stem for p in written]
    assert "同名标题的去重测试验证" in slugs[0]
    assert any("-1" in s for s in slugs[1:])


def test_check_python_syntax_valid():
    """合法 Python 代码应返回 False（无语法错误）"""
    from core.hephaestus.distillation_engine import DistillSelfCheck

    checker = DistillSelfCheck()
    assert checker._check_python_syntax("x = 1 + 2\nprint(x)") is False
    assert checker._check_python_syntax("def foo(): pass") is False
    assert checker._check_python_syntax("class Bar:\n    pass") is False


def test_check_python_syntax_invalid():
    """非法 Python 代码应返回 True（有语法错误）"""
    from core.hephaestus.distillation_engine import DistillSelfCheck

    checker = DistillSelfCheck()
    assert checker._check_python_syntax("def foo(\n") is True
    assert checker._check_python_syntax("if x ==") is True
    assert checker._check_python_syntax("class :\n  pass") is True


def test_check_python_syntax_empty():
    """空字符串也是合法 Python（返回 False）"""
    from core.hephaestus.distillation_engine import DistillSelfCheck

    checker = DistillSelfCheck()
    assert checker._check_python_syntax("") is False


# ========== 9. 超长单消息完整蒸馏（P1-5 修复） ==========


def test_process_long_single_message_is_not_truncated(tmp_path, monkeypatch):
    """单条超长消息应被分片处理，所有内容都进入 LLM，不应截断。"""
    from core.config import get_config as real_get_config
    from core.hephaestus.distillation_engine import DistillationEngine, ValuePrejudgment

    real_cfg = real_get_config()

    class _FakeConfig:
        def __init__(self, base):
            self._base = base

        def get(self, key, default=None):
            if key == "distill.token_budget_total":
                return 4000
            if key == "distill.enable_llm_fragment_merge":
                return False
            if key == "distill.fragment_merge_threshold":
                return 0.4
            return self._base.get(key, default)

        def __getattr__(self, name):
            return getattr(self._base, name)

    monkeypatch.setattr(
        "core.hephaestus.distillation_engine.get_config", lambda: _FakeConfig(real_cfg)
    )

    engine = DistillationEngine(wiki_base=str(tmp_path), receipt_config=_engine_config(tmp_path))

    recorded_texts = []

    def _recording_extract(request, *, prepared=None):
        recorded_texts.append(request.session_text)
        return _admitted_outcome(request, judgment="skip")

    def _prepare_recording_prompt(request):
        from core.hephaestus.distill_input_spec import PreparedExtractionPrompt

        return PreparedExtractionPrompt.build("recording extractor prompt", request)

    engine._noise_filter = SimpleNamespace(
        filter=lambda messages: (messages, {"kept": len(messages)})
    )
    engine._value_prejudgment = SimpleNamespace(
        judge=lambda messages: (ValuePrejudgment.CERTAINLY_YES, 0.9)
    )
    engine._extractor = SimpleNamespace(
        extract=_recording_extract,
        prepare_prompt=_prepare_recording_prompt,
        backend=SimpleNamespace(
            checkpoint_identity=lambda: {"provider": "test", "model": "recording"}
        ),
        checkpoint_identity=lambda: {"provider": "test", "model": "recording"},
    )
    engine._self_check = SimpleNamespace(check=lambda fragments, messages: (True, []))
    engine._cross_linker = SimpleNamespace(link=lambda fragments: fragments)
    engine._feedback_loop = SimpleNamespace(evaluate=lambda result: [])
    # 关闭 LLM 片段合成，避免无 API 时触发回退改变 fragments 数量
    engine._fragment_merger = SimpleNamespace(
        merge=lambda frags: frags,
        checkpoint_identity=lambda: {"strategy": "test-no-merge"},
    )
    engine._kia_linker = False

    marker = "[UNIQUE_MARKER_FOR_LONG_MESSAGE]"
    long_content = marker + " word" * 12000
    messages = [
        {
            "role": "user",
            "content": long_content,
            "turn": 1,
            "turn_number": 1,
            "source_span": {
                "revision_id": "raw-long-single-1",
                "logical_event_id": "logical-long-single-1",
                "turn_number": 1,
                "content_hash": "sha256:test-long-single",
                "role": "user",
                "span_start": 0,
                "span_end": len(long_content),
            },
        }
    ]

    result = _captured_process(engine, "sess-long-single", messages)

    assert result.judgment == "skip"  # typed legal skip，不是空片段隐式降级
    assert result.truncated is False
    # 所有 chunk 的输入文本合起来必须包含完整 marker
    combined = "\n".join(recorded_texts)
    assert marker in combined
    # 内容无丢失：去掉前缀后，剩余 word 数量应一致
    assert combined.count("word") == long_content.count("word")


# ========== 10. KnowledgeExtractor COG-011 typed contract / correction ==========


def _make_caller(responses: list):
    """返回一个按顺序返回 responses 的 mock caller。"""

    class _MockCaller:
        def __init__(self):
            self.calls = []
            self._responses = responses
            self._idx = 0

        def call_with_evidence(self, prompt, expect_json=True, **_kwargs):
            from core.hephaestus.distill_response import DistillBackendResponse

            self.calls.append(prompt)
            resp = self._responses[self._idx]
            self._idx = (self._idx + 1) % len(self._responses)
            if isinstance(resp, str):
                import json

                parsed = json.loads(resp)
                raw = resp
            else:
                import json

                parsed = resp
                raw = json.dumps(resp, ensure_ascii=False, separators=(",", ":"))
            return DistillBackendResponse.create(
                raw_text=raw,
                parsed=parsed,
                usage={"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
                provider="test-provider",
                model="test-model",
                request_id=f"request-{len(self.calls)}",
                finish_reason="stop",
                parse_path="direct_json",
                attempt_history=(
                    {"attempt": 0, "status": "success", "operation_call": len(self.calls)},
                ),
            )

        def checkpoint_identity(self):
            return {"provider": "test-provider", "model": "test-model"}

    return _MockCaller()


def test_extractor_invalid_empty_output_enters_bounded_correction(sample_fragment):
    """空的非 skip 输出也必须进入一次有界修正，不能被当成合法 skip。"""
    from core.hephaestus.distillation_engine import (
        KnowledgeExtractor,
        _strict_validate_fragments,
    )

    request = _extraction_request(session_id="sess-fix", source_agent="codex")
    invalid = _typed_extraction_payload(request.input_spec, fragments=[])
    valid = _typed_extraction_payload(
        request.input_spec,
        fragments=[_fragment_payload(sample_fragment)],
    )

    caller = _make_caller([invalid, valid])
    extractor = KnowledgeExtractor(caller=caller)
    outcome = extractor.extract(request)

    assert outcome.admitted is True
    assert outcome.judgment == "knowledge"
    assert outcome.correction_count == 1
    assert len(outcome.backend_responses) == 2
    assert outcome.backend_responses[-1].request_id == "request-2"
    assert outcome.backend_responses[-1].raw_text
    assert len(outcome.fragments) == 1
    passed, errors = _strict_validate_fragments(list(outcome.fragments))
    assert passed, errors
    assert len(caller.calls) == 2
    assert "硬校验错误" in caller.calls[1]


def test_extractor_retries_when_schema_valid_fragments_cannot_be_built(sample_fragment):
    """模型 JSON 合法但 builder 丢掉全部片段时，同样不能被准入为空知识。"""
    from core.hephaestus.distillation_engine import KnowledgeExtractor

    request = _extraction_request(session_id="sess-unbuildable-fragment")
    schema_valid = _typed_extraction_payload(
        request.input_spec,
        fragments=[_fragment_payload(sample_fragment)],
    )
    caller = _make_caller([schema_valid, schema_valid])
    extractor = KnowledgeExtractor(caller=caller)
    extractor._fragment_builder = lambda _payload: None

    outcome = extractor.extract(request)

    assert len(caller.calls) == 2
    assert outcome.correction_count == 1
    assert outcome.admitted is False
    assert outcome.judgment == "knowledge"
    assert outcome.fragments == ()


def test_extractor_preserves_fenced_code_block_through_canonical_admission(sample_fragment):
    """Visible fenced code must survive parsed and canonical admitted output byte-for-byte."""
    from core.hephaestus.distillation_engine import KnowledgeExtractor

    request = _extraction_request(session_id="sess-code-fidelity")
    core_content = (
        "## SQLite 修复步骤\n\n"
        "```python\n"
        "def rebuild_index(database_path: str) -> None:\n"
        "    # 保留此处的空行和缩进，便于用户直接复用。\n"
        "    with connect(database_path) as connection:\n"
        "        connection.execute('REINDEX')\n"
        "\n"
        "    return None\n"
        "```\n\n"
        "运行前先备份数据库；这段可见代码是蒸馏证据的一部分，不能清洗、截断或重排。\n"
    )
    fragment = _fragment_payload(sample_fragment)
    fragment["core_content"] = core_content
    response = _typed_extraction_payload(
        request.input_spec,
        fragments=[fragment],
    )

    outcome = KnowledgeExtractor(caller=_make_caller([response])).extract(request)

    assert outcome.admitted is True
    assert outcome.fragments[0].core_content.encode("utf-8") == core_content.encode("utf-8")
    assert outcome.canonical_output["fragments"][0]["core_content"].encode("utf-8") == (
        core_content.encode("utf-8")
    )


def test_extractor_valid_typed_v4_skip_passes_without_correction():
    """完整的 v4 skip 分支一次准入，不需要 claims 或格式修正调用。"""
    from core.hephaestus.distillation_engine import KnowledgeExtractor

    request = _extraction_request(session_id="sess-typed-skip", source_agent="codex")
    caller = _make_caller([_typed_extraction_payload(request.input_spec, judgment="skip")])
    outcome = KnowledgeExtractor(caller=caller).extract(request)

    assert outcome.admitted is True
    assert outcome.judgment == "skip"
    assert outcome.fragments == ()
    assert outcome.correction_count == 0
    assert outcome.structured_output["schema_version"] == "distill_output_v4"
    assert outcome.structured_output["claims"] == []
    assert "user_behavior_intent" not in outcome.structured_output
    assert len(caller.calls) == 1


def test_extractor_passes_dynamic_response_budget_to_caller(monkeypatch, sample_fragment):
    """长输入提取应把 long/retry 输出预算传给 LLM caller。"""
    from core.hephaestus.distillation_engine import KnowledgeExtractor

    class _RecordingCaller:
        def __init__(self):
            self.kwargs = []

        def call_with_evidence(self, prompt, expect_json=True, **kwargs):
            import json

            from core.hephaestus.distill_response import DistillBackendResponse

            self.kwargs.append(kwargs)
            return DistillBackendResponse.create(
                raw_text=json.dumps(valid, ensure_ascii=False),
                parsed=valid,
                usage={"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
                provider="test-provider",
                model="test-model",
                parse_path="direct_json",
                attempt_history=({"attempt": 0, "status": "success"},),
            )

        def checkpoint_identity(self):
            return {"provider": "test-provider", "model": "test-model"}

    monkeypatch.setattr(
        "core.hephaestus.distillation_extractor.estimate_tokens",
        lambda _text: 24000,
    )

    caller = _RecordingCaller()
    extractor = KnowledgeExtractor(caller=caller)
    request = _extraction_request(session_id="sess-budget")
    valid = _typed_extraction_payload(
        request.input_spec,
        fragments=[_fragment_payload(sample_fragment)],
    )
    outcome = extractor.extract(request)

    assert outcome.admitted is True
    assert len(outcome.fragments) == 1
    assert caller.kwargs[0]["response_max_tokens"] == 12000
    assert caller.kwargs[0]["response_retry_max_tokens"] == 16000


def test_extractor_correction_exhaustion_stays_rejected_not_implicit_skip():
    """修正耗尽后保留 rejected outcome，绝不能把失败改写为 skip。"""
    from core.hephaestus.distillation_engine import KnowledgeExtractor

    request = _extraction_request(session_id="sess-fail")
    invalid = _typed_extraction_payload(request.input_spec, fragments=[])
    caller = _make_caller([invalid, invalid, invalid])
    extractor = KnowledgeExtractor(caller=caller)
    outcome = extractor.extract(request)

    # 默认 retries=1，所以总共调用 1 + 1 = 2 次
    assert len(caller.calls) == 2
    assert outcome.admitted is False
    assert outcome.judgment == "knowledge"
    assert outcome.fragments == ()
    assert outcome.correction_count == 1
    assert "fragments" in outcome.admission.error_text


def test_cog029_extractor_resolves_model_ref_to_system_identity(tmp_path, sample_fragment):
    """The admitted root contains system fields that were absent from model output."""
    import hashlib

    from core.evidence.artifact_uri import build_artifact_uri
    from core.hephaestus.distillation_engine import KnowledgeExtractor

    report = tmp_path / "pytest.txt"
    report.write_text("3 tests failed", encoding="utf-8")
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    request = _extraction_request(
        session_id="sess-cog029-resolve",
        artifact_refs=(
            {
                "uri": build_artifact_uri(
                    "test-agent",
                    "sess-cog029-resolve",
                    1,
                    "test_report",
                    0,
                ),
                "artifact_type": "test_report",
                "summary": "pytest failure report",
                "source_event_id": "raw-1",
                "path": str(report),
                "sha256": digest,
                "mime_type": "text/plain",
            },
        ),
    )
    ref_id = request.input_spec.artifact_catalog.entries[0].artifact_ref_id
    model_output = _typed_extraction_payload(
        request.input_spec,
        fragments=[_fragment_payload(sample_fragment)],
    )
    evidence = model_output["structured_output"]["claims"][0]["evidence"][0]
    evidence["artifact_ref_id"] = ref_id
    evidence["quote"] = "pytest failure report"
    evidence.pop("source_authority_id", None)

    outcome = KnowledgeExtractor(caller=_make_caller([model_output])).extract(request)

    assert outcome.admitted is True
    assert "artifact_uri" not in evidence
    admitted = outcome.structured_output["claims"][0]["evidence"][0]
    assert admitted["artifact_ref_id"] == ref_id
    assert admitted["artifact_type"] == "test_report"
    assert admitted["artifact_sha256"] == f"sha256:{digest}"
    assert admitted["artifact_acl"] == "local_user"


def test_cog029_rejected_source_catalog_blocks_before_model_call(tmp_path):
    from core.evidence.artifact_catalog import ArtifactCatalogRejectedError
    from core.evidence.artifact_uri import build_artifact_uri
    from core.hephaestus.distillation_engine import KnowledgeExtractor

    request = _extraction_request(
        session_id="sess-cog029-rejected-catalog",
        artifact_refs=(
            {
                "uri": build_artifact_uri(
                    "test-agent",
                    "sess-cog029-rejected-catalog",
                    1,
                    "attachment",
                    0,
                ),
                "artifact_type": "attachment",
                "summary": "missing attachment",
                "source_event_id": "raw-1",
                "path": str(tmp_path / "missing.txt"),
                "sha256": "b" * 64,
            },
        ),
    )
    caller = _make_caller([_typed_extraction_payload(request.input_spec, judgment="skip")])

    with pytest.raises(
        ArtifactCatalogRejectedError,
        match="artifact_content_missing",
    ):
        KnowledgeExtractor(caller=caller).extract(request)

    assert caller.calls == []


def test_cog029_engine_records_catalog_rejection_without_model_call(
    engine_with_mocks,
    monkeypatch,
    tmp_path,
):
    from core.evidence.artifact_uri import build_artifact_uri
    from core.hephaestus.distillation_engine import KnowledgeExtractor
    from core.hephaestus.distillation_models import DistillationResult

    request = _extraction_request(
        session_id="sess-cog029-engine-rejection",
        artifact_refs=(
            {
                "uri": build_artifact_uri(
                    "test-agent",
                    "sess-cog029-engine-rejection",
                    1,
                    "attachment",
                    0,
                ),
                "artifact_type": "attachment",
                "summary": "missing attachment",
                "source_event_id": "raw-1",
                "path": str(tmp_path / "missing.txt"),
                "sha256": "c" * 64,
            },
        ),
    )
    caller = _make_caller([_typed_extraction_payload(request.input_spec, judgment="skip")])
    engine_with_mocks._extractor = KnowledgeExtractor(caller=caller)
    result = DistillationResult(
        session_id=request.input_spec.source_session_id,
        source="test-agent",
    )
    saved = {}

    def _capture_failure(
        session_id,
        fragments,
        validation_errors,
        source="",
        raw_response="",
        exc_info="",
        parse_metadata=None,
        database_dir=None,
    ):
        saved.update(
            validation_errors=validation_errors,
            parse_metadata=parse_metadata,
        )

    monkeypatch.setattr(
        "core.hephaestus.distillation_engine._save_failed_distill",
        _capture_failure,
    )

    outcome = engine_with_mocks._safe_extract(request, result)

    assert outcome is None
    assert result.error == "artifact_catalog_rejected"
    assert caller.calls == []
    assert saved["validation_errors"] == ["artifact catalog rejected: artifact_content_missing"]
    assert saved["parse_metadata"]["failure_path"] == "artifact_catalog_rejected"
    assert saved["parse_metadata"]["artifact_catalog_rejected_count"] == 1


def test_cog029_forged_ref_enters_bounded_correction(tmp_path, sample_fragment):
    from core.hephaestus.distillation_engine import KnowledgeExtractor

    request = _extraction_request(session_id="sess-cog029-forged")
    invalid = _typed_extraction_payload(
        request.input_spec,
        fragments=[_fragment_payload(sample_fragment)],
    )
    invalid["structured_output"]["claims"][0]["evidence"][0][
        "artifact_ref_id"
    ] = "artifact-ref:forged"
    invalid["structured_output"]["claims"][0]["claim_type"] = "invented_claim_type"

    caller = _make_caller([invalid, invalid])
    outcome = KnowledgeExtractor(caller=caller).extract(request)

    assert len(caller.calls) == 2
    assert outcome.admitted is False
    assert "artifact_ref_id is not present" in outcome.admission.error_text
    assert "artifact_ref_id is not present" in caller.calls[1]
    assert "claim_type" in outcome.admission.error_text
    assert "claim_type" in caller.calls[1]


@pytest.mark.parametrize(
    ("failure_case", "error_marker"),
    [
        ("missing_source", "source_event_id"),
        ("invalid_claim_type", "claim_type"),
        ("invalid_action", "recommended_action"),
        ("artifact_mismatch", "artifact_type"),
    ],
)
def test_cog028_structural_failure_matrix_enters_bounded_correction(
    failure_case, error_marker, sample_fragment
):
    """Every schema/semantic failure is corrected before Engine admission."""
    from core.hephaestus.distillation_engine import KnowledgeExtractor

    request = _extraction_request(session_id=f"sess-cog028-{failure_case}")
    invalid = _typed_extraction_payload(
        request.input_spec,
        fragments=[_fragment_payload(sample_fragment)],
    )
    claim = invalid["structured_output"]["claims"][0]
    if failure_case == "missing_source":
        claim["evidence"][0]["source_event_id"] = "missing-event"
    elif failure_case == "invalid_claim_type":
        claim["claim_type"] = "invented_claim_type"
    elif failure_case == "invalid_action":
        claim["recommended_action"] = "write_without_receipt"
    else:
        claim["evidence"][0].update(
            {
                "artifact_uri": (
                    f"mnemos-artifact://codex/{request.input_spec.source_session_id}/"
                    "turn/1/tool_result/0"
                ),
                "artifact_type": "screenshot",
                "artifact_summary": "mismatched artifact type fixture",
            }
        )

    caller = _make_caller([invalid, invalid])
    outcome = KnowledgeExtractor(caller=caller).extract(request)

    assert len(caller.calls) == 2
    assert outcome.correction_count == 1
    assert outcome.admitted is False
    assert error_marker in outcome.admission.error_text
    assert error_marker in caller.calls[1]
    assert len(outcome.backend_responses) == 2
    assert outcome.backend_responses[-1].raw_text


def test_safe_extract_persists_rejected_raw_response_and_full_metadata(
    engine_with_mocks, monkeypatch
):
    from core.hephaestus.distillation_engine import KnowledgeExtractor
    from core.hephaestus.distillation_models import DistillationResult

    request = _extraction_request(session_id="sess-rejected-evidence")
    invalid = _typed_extraction_payload(request.input_spec, fragments=[])
    engine_with_mocks._extractor = KnowledgeExtractor(caller=_make_caller([invalid, invalid]))
    result = DistillationResult(session_id="sess-rejected-evidence", source="codex")
    saved = {}

    def _capture_failure(
        session_id,
        fragments,
        validation_errors,
        source="",
        raw_response="",
        exc_info="",
        parse_metadata=None,
        database_dir=None,
    ):
        saved.update(
            session_id=session_id,
            raw_response=raw_response,
            validation_errors=validation_errors,
            parse_metadata=parse_metadata,
        )

    monkeypatch.setattr(
        "core.hephaestus.distillation_engine._save_failed_distill",
        _capture_failure,
    )

    outcome = engine_with_mocks._safe_extract(request, result)

    assert outcome is None
    assert result.error == "extraction_contract_rejected"
    assert result.raw_response
    assert len(result.response_evidence) == 2
    assert saved["raw_response"] == result.raw_response
    assert saved["parse_metadata"]["correction_attempts"] == 1
    assert len(saved["parse_metadata"]["responses"]) == 2
    assert saved["parse_metadata"]["responses"][-1]["provider"] == "test-provider"
    assert saved["parse_metadata"]["responses"][-1]["response_hash"]
    assert saved["parse_metadata"]["prompt_hash"].startswith("sha256:")
    assert saved["parse_metadata"]["input_spec_hash"] == request.input_spec.input_spec_hash
    assert (
        saved["parse_metadata"]["artifact_catalog_hash"]
        == request.input_spec.artifact_catalog.catalog_hash
    )
    assert saved["parse_metadata"]["response_hash"]


@pytest.mark.parametrize(
    ("raw_text", "parse_path", "transport_empty"),
    [
        ("not-json-response", "failed", False),
        ("", "transport_empty", True),
    ],
)
def test_cog028_parse_and_transport_failures_persist_complete_evidence(
    engine_with_mocks,
    monkeypatch,
    raw_text,
    parse_path,
    transport_empty,
):
    from core.hephaestus.distill_response import DistillBackendResponse
    from core.hephaestus.distillation_errors import DistillationAPIError
    from core.hephaestus.distillation_models import DistillationResult
    from core.hephaestus.distill_input_spec import PreparedExtractionPrompt

    request = _extraction_request(session_id=f"sess-cog028-{parse_path}")
    evidence = DistillBackendResponse.create(
        raw_text=raw_text,
        parsed=None,
        usage={"prompt_tokens": 3, "completion_tokens": 1, "cost": 0.0},
        provider="test-provider",
        model="test-model",
        request_id="request-cog028",
        finish_reason="stop",
        parse_path=parse_path,
        attempt_history=({"attempt": 0, "status": parse_path},),
    )

    class _FailingExtractor:
        def prepare_prompt(self, extraction_request):
            return PreparedExtractionPrompt.build("cog028 failure prompt", extraction_request)

        def extract(self, extraction_request, *, prepared=None):
            raise DistillationAPIError(
                "provider response rejected",
                response_evidence=evidence,
            )

    engine_with_mocks._extractor = _FailingExtractor()
    result = DistillationResult(session_id=request.input_spec.source_session_id, source="codex")
    saved = {}

    def _capture_failure(
        session_id,
        fragments,
        validation_errors,
        source="",
        raw_response="",
        exc_info="",
        parse_metadata=None,
        database_dir=None,
    ):
        saved.update(raw_response=raw_response, parse_metadata=parse_metadata)

    def _handle_error(error, distill_result):
        distill_result.judgment = "error"
        distill_result.judgment_reason = str(error)

    monkeypatch.setattr(
        "core.hephaestus.distillation_engine._save_failed_distill",
        _capture_failure,
    )
    monkeypatch.setattr(engine_with_mocks, "_handle_api_error", _handle_error)

    outcome = engine_with_mocks._safe_extract(request, result)

    assert outcome is None
    assert result.error == "distillation_api_error"
    assert saved["raw_response"] == raw_text
    metadata = saved["parse_metadata"]
    assert metadata["failure_path"] == "provider_or_parse_failure"
    assert metadata["transport_empty"] is transport_empty
    assert metadata["prompt_hash"].startswith("sha256:")
    assert metadata["input_spec_hash"] == request.input_spec.input_spec_hash
    assert metadata["response_hash"] == evidence.response_hash
    assert metadata["responses"][0]["provider"] == "test-provider"
    assert metadata["responses"][0]["request_id"] == "request-cog028"


def test_extractor_correction_disabled_when_retries_zero(monkeypatch):
    """当 retries=0 时，不应修正，也不得把 invalid output 降级成 skip。"""
    from core.hephaestus.distillation_engine import KnowledgeExtractor

    class _FakeConfig:
        def get(self, key, default=None):
            if key == "distill.extract_correction_retries":
                return 0
            return default

    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: _FakeConfig())

    request = _extraction_request(session_id="sess-no-retry")
    invalid = _typed_extraction_payload(request.input_spec, fragments=[])
    caller = _make_caller([invalid])
    extractor = KnowledgeExtractor(caller=caller)
    outcome = extractor.extract(request)

    assert len(caller.calls) == 1
    assert outcome.admitted is False
    assert outcome.judgment == "knowledge"
    assert outcome.fragments == ()


def test_extractor_binds_source_agent_to_input_spec_and_rejects_forgery(sample_fragment):
    """prompt 使用输入 spec 的 agent，模型伪造该字段时也必须 fail closed。"""
    from core.hephaestus.distillation_engine import KnowledgeExtractor

    request = _extraction_request(
        session_id="sess-agent-binding",
        source_agent="trusted-agent",
    )
    forged = _typed_extraction_payload(
        request.input_spec,
        fragments=[_fragment_payload(sample_fragment)],
    )
    forged["structured_output"]["source_agent"] = "forged-agent"

    caller = _make_caller([forged, forged])
    outcome = KnowledgeExtractor(caller=caller).extract(request)

    assert "trusted-agent" in caller.calls[0]
    assert outcome.admitted is False
    assert outcome.judgment == "knowledge"
    assert outcome.fragments == ()
    assert outcome.correction_count == 1
    assert "source_agent" in outcome.admission.error_text
    assert "immutable distillation input spec" in outcome.admission.error_text


# ========== 12. 硬校验根因修复与片段级降级（P0-7） ==========


def _make_remediable_fragment() -> KnowledgeFragment:
    """构造一个存在多种硬校验问题、但可自动修复的片段。"""
    return KnowledgeFragment(
        form="problem-solution",
        title="短",
        frontmatter={},
        background="",
        core_content="这是需要被记录的核心知识内容。" * 5,
        boundaries={},
        anti_patterns=["不要直接复制命令而不检查环境"],
        related_concepts=["Redis"],
    )


def _make_unfixable_fragment() -> KnowledgeFragment:
    """构造一个缺少任何有效信息、自动修复后仍无法通过的片段。"""
    return KnowledgeFragment(
        form="",
        title="",
        frontmatter={},
        background="",
        core_content="",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )


def test_auto_remediate_fragment_fixes_common_failures():
    """_auto_remediate_fragment 应修复标题、内容、frontmatter、结构等根因问题。"""
    from core.hephaestus.distillation_engine import (
        KnowledgeFragment,
        _auto_remediate_fragment,
        _validate_fragment,
    )

    frag = KnowledgeFragment(
        form="concept",
        title="无标题",
        frontmatter={"摘要": "", "领域": ""},
        background=(
            "背景说明内容足够长，可以作为核心内容的补充。"
            "这个片段来自一次并发控制讨论，需要记录适用场景、使用边界和误用风险。"
        ),
        core_content="使用 asyncio.Semaphore 限制并发数。",
        boundaries={"适用": "异步任务数量可控但外部服务有并发限制", "不适用": "CPU 密集型任务"},
        anti_patterns=["避免把 Semaphore 当作线程安全锁使用", "避免只限流不设置超时"],
        related_concepts=["缓存", "异步限流"],
    )

    assert _validate_fragment(frag)
    changed = _auto_remediate_fragment(frag)
    assert changed
    assert not _validate_fragment(frag)
    assert frag.title and not frag.title.startswith("无标题")
    assert frag.frontmatter.get("领域")
    assert frag.frontmatter.get("摘要")
    assert "#" in frag.core_content or "```" in frag.core_content
    assert len(frag.core_content) >= 100


def test_validate_fragment_rejects_short_core_even_with_context_or_code():
    """runtime 必须与 prompt/schema 一致：短 core_content 不因上下文或代码块例外通过。"""
    from core.hephaestus.distillation_engine import KnowledgeFragment, _validate_fragment

    frag = KnowledgeFragment(
        form="problem-solution",
        title="Redis 连接池排查方案完整记录",
        frontmatter={"摘要": "记录 Redis 连接池排查方案。", "领域": "backend"},
        background="背景已经足够长，但硬标准要求 core_content 自身完整。",
        core_content="```python\nprint('too short')\n```",
        boundaries={"适用": "Redis 高并发"},
        anti_patterns=["不要只调大连接池"],
        related_concepts=["Redis"],
    )

    errors = _validate_fragment(frag)

    assert any("核心内容过短" in err for err in errors)


def test_auto_remediate_fragment_expands_short_core_to_hard_standard():
    """短片段有真实上下文时应被扩写成合格结构，而不是靠例外放行。"""
    from core.hephaestus.distillation_engine import (
        KnowledgeFragment,
        _auto_remediate_fragment,
        _validate_fragment,
    )

    frag = KnowledgeFragment(
        form="methodology",
        title="异步接口限流方法的完整记录",
        frontmatter={"摘要": "异步接口限流方法的摘要。", "领域": "backend"},
        background=(
            "在批量调用外部 API 时，服务端通常会限制并发数和请求速率。"
            "如果没有限流，短时间内的任务洪峰会导致 429、超时和重试风暴。"
        ),
        core_content="使用 asyncio.Semaphore 控制并发。",
        boundaries={"适用": "I/O 密集型异步请求", "不适用": "CPU 密集型计算"},
        anti_patterns=["只加并发锁但不设置超时", "失败后无限重试"],
        related_concepts=["asyncio", "rate limit"],
        keywords=["Semaphore", "限流", "重试"],
    )

    changed = _auto_remediate_fragment(frag)
    errors = _validate_fragment(frag)

    assert changed
    assert not errors
    assert len(frag.core_content) >= 100
    assert "## 背景" in frag.core_content
    assert "## 适用边界" in frag.core_content


def test_extract_schema_matches_runtime_hard_fragment_standard():
    """prompt 注入的 JSON schema 必须表达 runtime 的硬校验标准。"""
    import json

    schema_path = (
        Path(__file__).resolve().parents[2] / "prompts/distill/_output_schemas/extract.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    fragment_schema = schema["properties"]["fragments"]["items"]
    structured_schema = schema["properties"]["structured_output"]
    frontmatter_schema = fragment_schema["properties"]["frontmatter"]
    core_schema = fragment_schema["properties"]["core_content"]

    assert "structured_output" in schema["required"]
    assert len(schema["oneOf"]) == 2
    assert structured_schema["properties"]["schema_version"]["const"] == "distill_output_v4"
    assert "input_spec_hash" in structured_schema["required"]
    union = structured_schema["allOf"][0]
    assert "claims" in union["then"]["required"]
    assert "claims" in union["else"]["required"]
    assert "user_behavior_intent" in union["else"]["required"]
    behavior_schema = structured_schema["properties"]["user_behavior_intent"]
    assert "intent_confidence" in behavior_schema["required"]
    claim_schema = structured_schema["properties"]["claims"]["items"]
    assert "relation_to_existing" in claim_schema["required"]
    assert "recommended_action" in claim_schema["required"]
    assert "cognitive_actions" in claim_schema["properties"]
    assert "frontmatter" in fragment_schema["required"]
    assert set(frontmatter_schema["required"]) == {"摘要", "领域"}
    assert frontmatter_schema["properties"]["摘要"]["minLength"] == 5
    assert frontmatter_schema["properties"]["领域"]["minLength"] == 2
    assert core_schema["minLength"] == 100
    assert core_schema["pattern"] == "(^|\\n)#{1,3}\\s|```"


def test_auto_remediate_fragment_infers_backend_domain():
    """领域缺失时应根据关键词推断 backend 等领域。"""
    from core.hephaestus.distillation_engine import (
        KnowledgeFragment,
        _auto_remediate_fragment,
    )

    frag = KnowledgeFragment(
        form="problem-solution",
        title="Redis 连接池排查",
        frontmatter={"摘要": "摘要足够长。"},
        background="",
        core_content="## 问题\n\nRedis 连接池在高并发下耗尽，需要调整 max_connections 参数。",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )

    _auto_remediate_fragment(frag)
    assert frag.frontmatter.get("领域") == "backend"


def test_write_pages_never_routes_unadmitted_auto_remediation(
    engine_with_mocks, tmp_path, monkeypatch
):
    """局部自动修复不能替代 extractor 根准入或绕过动作路由。"""
    from core.hephaestus.distillation_engine import DistillationResult

    _disable_structured_contract_gate(monkeypatch, engine_with_mocks)
    frag = _make_remediable_fragment()
    result = DistillationResult(
        session_id="sess-remediate",
        judgment="knowledge",
        fragments=[frag],
    )
    receipt = engine_with_mocks.write_pages_with_receipt(result)
    assert receipt.status == "retryable_failed"
    assert receipt.terminal_reason == "cognition_episode_commit_failed"
    assert list(tmp_path.rglob("*.md")) == []


def test_write_pages_partial_degradation_still_requires_admitted_root(
    engine_with_mocks, tmp_path, sample_fragment, monkeypatch
):
    """部分片段过滤可保存诊断，但不能把剩余片段直接写入 Wiki。"""
    from core.hephaestus.distillation_engine import DistillationResult

    saved = []

    def _fake_save(
        sid,
        frags,
        errs,
        source="",
        raw_response="",
        exc_info="",
        severity="high",
        parse_metadata=None,
        database_dir=None,
    ):
        saved.append((sid, len(frags), errs, source, raw_response, severity))
        return tmp_path / "failed.json"

    monkeypatch.setattr("core.hephaestus.distillation_engine._save_failed_distill", _fake_save)

    _disable_structured_contract_gate(monkeypatch, engine_with_mocks)
    result = DistillationResult(
        session_id="sess-partial",
        judgment="knowledge",
        fragments=[sample_fragment, _make_unfixable_fragment()],
    )
    receipt = engine_with_mocks.write_pages_with_receipt(result)

    assert receipt.status == "retryable_failed"
    assert receipt.terminal_reason == "cognition_episode_commit_failed"
    assert len(saved) == 1
    assert saved[0][1] == 1  # 仅保存失败片段
    assert saved[0][5] == "medium"


def test_write_pages_rejects_session_when_most_fail(engine_with_mocks, tmp_path, monkeypatch):
    """失败片段占比超过阈值时仍应拒绝整个 session，避免低质量批量入库。"""
    from core.hephaestus.distillation_engine import DistillationResult

    saved = []

    def _fake_save(
        sid,
        frags,
        errs,
        source="",
        raw_response="",
        exc_info="",
        severity="high",
        parse_metadata=None,
        database_dir=None,
    ):
        saved.append((sid, len(frags), source, raw_response, severity))
        return tmp_path / "failed.json"

    monkeypatch.setattr("core.hephaestus.distillation_engine._save_failed_distill", _fake_save)

    _disable_structured_contract_gate(monkeypatch, engine_with_mocks)
    result = DistillationResult(
        session_id="sess-all-fail",
        judgment="knowledge",
        fragments=[_make_unfixable_fragment(), _make_unfixable_fragment()],
    )
    written = engine_with_mocks.write_pages(result)

    assert written == []
    assert len(saved) == 1
    assert saved[0][1] == 2  # 保存全部片段
    assert saved[0][4] == "high"


def test_write_pages_emits_wiki_page_updated(
    engine_with_mocks,
    sample_fragment,
    _canonical_material_actions,
):
    """write_pages 应为每个写入的 wiki 页面发射 wiki_page_updated 事件（P1-9）。"""
    from core.hephaestus.distillation_engine import DistillationResult
    import json
    import sqlite3

    input_spec = _write_input_spec("sess-wiki-updated")
    structured_output = _valid_structured_distill_output(input_spec)
    result = DistillationResult(
        session_id="sess-wiki-updated",
        judgment="knowledge",
        fragments=[sample_fragment],
        structured_output=structured_output,
        **_bound_write_result_kwargs(
            input_spec,
            fragments=[sample_fragment],
            structured_output=structured_output,
        ),
    )
    written = engine_with_mocks.write_pages(result)

    assert len(written) == 1
    event_db = Path(engine_with_mocks._runtime_receipt_config.database_dir) / "events.db"
    with sqlite3.connect(event_db) as conn:
        updated_events = conn.execute("""SELECT source, payload_json FROM events
               WHERE event_type='wiki_page_updated' ORDER BY id""").fetchall()
    assert len(updated_events) == 1
    payload = json.loads(updated_events[0][1])
    assert updated_events[0][0] == "wiki_mutation"
    assert payload["page_path"] == written[0]
    assert payload["update_type"] == "create"


def test_distill_and_write_runs_full_high_level_entrypoint(tmp_path, monkeypatch, sample_fragment):
    """distill_and_write delegates event ownership to the engine write boundary."""
    from core.hephaestus import distillation_engine as mod
    from core.hephaestus.distillation_engine import DistillationResult

    result = DistillationResult(
        session_id="sess-high-level",
        judgment="knowledge",
        fragments=[sample_fragment],
    )
    written_page = str(tmp_path / "Redis-连接池耗尽问题的排查方案.md")
    calls = {}

    class FakeEngine:
        def __init__(self, wiki_base=None):
            calls["wiki_base"] = wiki_base

        def process(self, session_id, messages, meta=None):
            calls["process"] = (session_id, messages, meta)
            return result

        def write_pages(self, distillation_result):
            calls["write_pages"] = distillation_result
            return [written_page]

    published = []

    def fake_publish_event(event_type, source, payload, *, trace_id=""):
        published.append((event_type, source, payload))
        return trace_id or "distillation-test-trace"

    class FakeThread:
        def __init__(self, target, daemon=False, name=None):
            calls["thread"] = {
                "target": target,
                "daemon": daemon,
                "name": name,
            }

        def start(self):
            calls["thread_started"] = True

    monkeypatch.setattr(mod, "DistillationEngine", FakeEngine)
    monkeypatch.setattr("core.mnemos_bus.publish_event", fake_publish_event)
    monkeypatch.setattr("threading.Thread", FakeThread)

    output_result, written = mod.distill_and_write(
        "sess-high-level",
        [{"role": "user", "content": "Redis 连接池为什么耗尽？"}],
        wiki_base=str(tmp_path),
        meta={"source": "test"},
    )

    assert output_result is result
    assert written == [written_page]
    assert calls["wiki_base"] == str(tmp_path)
    assert calls["process"][0] == "sess-high-level"
    assert calls["process"][2] == {"source": "test"}
    assert calls["write_pages"] is result
    assert published == []
    assert calls["thread"]["daemon"] is True
    assert calls["thread"]["name"] == "EmbedIndexUpdate"
    assert calls["thread_started"] is True


# ========== P209: distill_failed 保存原始输出与堆栈 ==========


def test_save_failed_distill_includes_raw_response_and_exc_info(
    tmp_path, monkeypatch, sample_fragment
):
    """_save_failed_distill 应保存原始 LLM 输出和异常堆栈，便于根因分析。"""
    from core.hephaestus.distillation_engine import _save_failed_distill
    import json

    fake_config = MagicMock()
    fake_config.database_dir = tmp_path
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: fake_config)
    from core.ops.operational_incident import initialize_operational_incident_schema

    initialize_operational_incident_schema(tmp_path / "operational_incidents.db")

    path = _save_failed_distill(
        session_id="sess-raw",
        fragments=[sample_fragment],
        validation_errors=["title empty"],
        source="test",
        raw_response='{"raw": "llm output"}',
        exc_info="Traceback (most recent call last):\n  File ...",
    )

    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["session_id"] == "sess-raw"
    assert data["raw_response"] == '{"raw": "llm output"}'
    assert "Traceback" in data["exc_info"]
    assert data["validation_errors"] == ["title empty"]
    assert len(data["fragments"]) == 1
    assert "core_content" in data["fragments"][0]


def test_save_failed_distill_redacts_only_private_literals(tmp_path, monkeypatch, sample_fragment):
    import json

    from core.hephaestus.distillation_engine import _save_failed_distill

    fake_config = MagicMock()
    fake_config.database_dir = tmp_path
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: fake_config)
    from core.ops.operational_incident import initialize_operational_incident_schema

    initialize_operational_incident_schema(tmp_path / "operational_incidents.db")
    raw = (
        "ordinary diagnostic text; "
        "api_key=DUMMY_SECRET_VALUE; "
        "email=user@example.com; "
        "bank_card=4111111111111111"
    )

    path = _save_failed_distill(
        session_id="sess-private-raw",
        fragments=[sample_fragment],
        validation_errors=["contract rejected"],
        raw_response=raw,
        parse_metadata={"path": "direct_json", "request_id": "request-safe"},
    )

    data = json.loads(path.read_text(encoding="utf-8"))
    assert "ordinary diagnostic text" in data["raw_response"]
    assert "DUMMY_SECRET_VALUE" not in data["raw_response"]
    assert "user@example.com" not in data["raw_response"]
    assert "4111111111111111" not in data["raw_response"]
    assert data["privacy_redaction"]["policy"] == "pii_credentials_only_v1"
    assert data["privacy_redaction"]["total"] >= 3


# ========== P212: PromptBuilder token 预算与 engine 对齐 ==========


def test_distill_prompt_budget_covers_engine_max_input(monkeypatch):
    """_distill_prompt_budget 的可用输入预算应覆盖 engine 的最大 session_text（6x token_budget_total）。"""
    from core.hephaestus.distillation_engine import (
        _distill_prompt_budget,
        DEFAULT_TOKEN_BUDGET_TOTAL,
        RESPONSE_TOKENS,
    )

    fake_config = MagicMock()
    fake_config.get = MagicMock(return_value=DEFAULT_TOKEN_BUDGET_TOTAL)
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: fake_config)

    budget = _distill_prompt_budget()
    # engine 最大 session_text = std_threshold * 2 = 6 * token_budget_total
    max_engine_input = DEFAULT_TOKEN_BUDGET_TOTAL * 6
    assert budget.total_limit >= max_engine_input + RESPONSE_TOKENS
    assert budget.available_for_input >= max_engine_input


# ========== 配置键接入回归测试 ==========


def test_chunk_messages_respects_max_turns_per_chunk():
    """incremental_batch_turns 应控制每 chunk 最多包含的原始 turn 数。"""
    from core.hephaestus.distillation_engine import DistillationEngine

    messages = [
        {"role": "user", "content": "turn1"},
        {"role": "assistant", "content": "turn2"},
        {"role": "user", "content": "turn3"},
        {"role": "assistant", "content": "turn4"},
    ]
    chunks = DistillationEngine._chunk_messages(
        messages,
        max_tokens_per_chunk=100000,
        max_turns_per_chunk=2,
    )
    turn_sets = [set(m.get("turn", i + 1) for i, m in enumerate(chunk)) for chunk in chunks]
    assert all(len(ts) <= 2 for ts in turn_sets)
    assert sum(len(chunk) for chunk in chunks) == len(messages)


def test_expand_long_messages_preserves_original_turn_and_exact_part_spans():
    """切分不能用消息序号覆盖 Capture 的原始 turn，也不能猜测 raw span。"""
    from core.hephaestus.distillation_engine import DistillationEngine

    class _Tokenizer:
        @staticmethod
        def estimate(text):
            return len(text)

        @staticmethod
        def split_to_tokens(text, _limit):
            return [text[:3], text[3:]]

    expanded = DistillationEngine._expand_long_messages(
        [
            {"role": "user", "content": "ok", "turn_number": 11},
            {
                "role": "assistant",
                "content": "abcdef",
                "turn_number": 47,
                "source_span": {
                    "revision_id": "rawrev-47",
                    "logical_event_id": "logical-47",
                    "turn_number": 47,
                    "content_hash": "hash-47",
                    "role": "assistant",
                    "span_start": 8,
                    "span_end": 14,
                },
            },
        ],
        _Tokenizer(),
        split_limit=3,
        need_turns=True,
    )

    assert [message["turn"] for message in expanded] == [11, 47, 47]
    assert [message["turn_number"] for message in expanded] == [11, 47, 47]
    assert [message["content"] for message in expanded] == ["ok", "abc", "def"]
    assert [message.get("part") for message in expanded] == [None, "1/2", "2/2"]
    assert [message["source_span"]["span_start"] for message in expanded[1:]] == [8, 11]
    assert [message["source_span"]["span_end"] for message in expanded[1:]] == [11, 14]
    assert all(message["source_span"]["revision_id"] == "rawrev-47" for message in expanded[1:])


def test_process_returns_budget_exceeded_when_budget_zero(tmp_path, monkeypatch, minimal_messages):
    """llm_cost_budget_per_session 为 0 时应在进入 LLM 前返回 budget_exceeded。"""
    from core.config import get_config as real_get_config
    from core.hephaestus.distillation_engine import DistillationEngine, ValuePrejudgment

    real_cfg = real_get_config()

    class _FakeConfig:
        def __init__(self, base):
            self._base = base

        def get(self, key, default=None):
            if key == "distill.llm_cost_budget_per_session":
                return 0
            if key == "distill.enable_llm_fragment_merge":
                return False
            if key == "distill.fragment_merge_threshold":
                return 0.4
            return self._base.get(key, default)

        def __getattr__(self, name):
            return getattr(self._base, name)

    monkeypatch.setattr(
        "core.hephaestus.distillation_engine.get_config", lambda: _FakeConfig(real_cfg)
    )

    engine = DistillationEngine(wiki_base=str(tmp_path), receipt_config=_engine_config(tmp_path))
    engine._noise_filter = SimpleNamespace(
        filter=lambda messages: (
            messages,
            {"total": len(messages), "noise": 0, "kept": len(messages)},
        ),
    )
    engine._value_prejudgment = SimpleNamespace(
        judge=lambda messages: (ValuePrejudgment.MAYBE, 0.5),
    )
    engine._llm_judge = SimpleNamespace(
        judge=lambda session_text, session_id: ("knowledge", "有价值", 0.8),
    )

    result = _captured_process(engine, "sess-budget", minimal_messages)
    assert result.judgment == "budget_exceeded"
    assert "预算" in result.judgment_reason or "budget" in result.judgment_reason.lower()


def test_http_api_host_agent_caller_accumulates_cost(monkeypatch):
    """HttpApiHostAgentCaller 应累加 usage 成本到 session_cost。"""
    from core.hephaestus.distillation_engine import HttpApiHostAgentCaller
    from core.llm_config import LLMApiChain, LLMApiConfig

    chain = LLMApiChain(
        primary=LLMApiConfig(
            "dmxapi",
            "fake-key",
            "https://www.dmxapi.cn/v1",
            "kimi-k2.5-free",
            "test",
            cost_level="free",
        ),
    )
    caller = HttpApiHostAgentCaller(api_chain=chain)
    caller.reset_session_cost_budget(10.0)

    def fake_try(prompt, timeout, cfg):
        return '{"judgment": "knowledge"}', {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "cost": 0.002,
        }

    monkeypatch.setattr(caller, "_try_api_config", fake_try)
    caller.call('{"x": 1}', expect_json=True)
    assert caller.session_cost == pytest.approx(0.002)
    assert caller.last_usage["prompt_tokens"] == 1000


def test_http_api_host_agent_caller_blocks_when_budget_exceeded():
    """HttpApiHostAgentCaller 在预算耗尽后应拒绝新的 LLM 调用。"""
    from core.hephaestus.distillation_engine import HttpApiHostAgentCaller, DistillationAPIError

    caller = HttpApiHostAgentCaller()
    caller.reset_session_cost_budget(0.0)
    with pytest.raises(DistillationAPIError):
        caller.call("any prompt")


# ========== Error report deduplication ==========


def test_generate_distillation_error_report_dedups_same_error(tmp_path, monkeypatch):
    """相同 API 故障应只生成一份报告，避免 99-Reports 重复文件堆积。"""
    from core.hephaestus.distillation_engine import (
        generate_distillation_error_report,
        DistillationAPIError,
    )

    monkeypatch.setattr(
        "core.hephaestus.distillation_engine._get_wiki_dir",
        lambda: tmp_path,
    )

    error = DistillationAPIError("test error", chain_desc="primary -> backup")

    first = generate_distillation_error_report(error)
    second = generate_distillation_error_report(error)

    assert first == second
    assert len(list((tmp_path / "99-Reports").glob("蒸馏API故障报告-*.md"))) == 1


def test_generate_distillation_error_report_creates_new_for_different_error(tmp_path, monkeypatch):
    """不同 API 故障应生成不同报告。"""
    from core.hephaestus.distillation_engine import (
        generate_distillation_error_report,
        DistillationAPIError,
    )

    monkeypatch.setattr(
        "core.hephaestus.distillation_engine._get_wiki_dir",
        lambda: tmp_path,
    )

    first = generate_distillation_error_report(
        DistillationAPIError("error a", chain_desc="chain-a")
    )
    second = generate_distillation_error_report(
        DistillationAPIError("error b", chain_desc="chain-b")
    )

    assert first != second
    assert len(list((tmp_path / "99-Reports").glob("蒸馏API故障报告-*.md"))) == 2


def test_allocate_page_path_raises_when_filename_collision_exhausted(
    engine_with_mocks, monkeypatch
):
    """文件路径碰撞超过最大尝试次数时应抛出 RuntimeError。"""
    from pathlib import Path

    monkeypatch.setattr(Path, "exists", lambda self: True)
    frag = KnowledgeFragment(
        form="concept",
        title="title",
        frontmatter={},
        background="",
        core_content="content",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
        relations=[],
    )
    with pytest.raises(RuntimeError, match="已尝试 10000 次"):
        engine_with_mocks._allocate_page_path(frag, "sess", set())


def test_session_short_strips_common_session_prefix():
    """session_id 以 session_ 开头时，不应再产生 session__ 双下划线前缀。"""
    from core.hephaestus.distillation_engine import DistillationEngine

    assert DistillationEngine._session_short("session_20260415_181305_193daf") == "20260415"
    assert DistillationEngine._session_short("session-abc-123") == "abc-123"
    assert DistillationEngine._session_short("sess-kg-001") == "sess-kg"
    assert DistillationEngine._session_short("") == "unknown"
    assert DistillationEngine._session_short(None) == "unknown"


def test_allocate_page_path_avoids_session_double_underscore(engine_with_mocks, tmp_path):
    """页面文件名应以展示标题为主，不暴露 session 前缀。"""
    from core.hephaestus.distillation_engine import DistillationEngine, KnowledgeFragment

    engine_with_mocks.wiki_base = tmp_path
    engine_with_mocks.inbox_dir = tmp_path / "00-Inbox"
    frag = KnowledgeFragment(
        form="concept",
        title="为什么助手不应该自称 AI 助手",
        frontmatter={},
        background="",
        core_content="content",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
        relations=[],
    )
    page_id, file_path = engine_with_mocks._allocate_page_path(
        frag,
        DistillationEngine._session_short("session_20260415_181305_193daf"),
        set(),
    )
    assert "session__" not in page_id
    assert "__" not in file_path.name
    assert page_id == "为什么助手不应该自称-ai-助手"
    assert file_path.name == "为什么助手不应该自称-ai-助手.md"


def test_allocate_page_path_routes_classified_pages_to_formal_dir(engine_with_mocks, tmp_path):
    """可确定分类的页面应直接写入正式目录，而不是先落 Inbox。"""
    from core.hephaestus.distillation_engine import DistillationEngine, KnowledgeFragment

    engine_with_mocks.wiki_base = tmp_path
    engine_with_mocks.inbox_dir = tmp_path / "00-Inbox"
    frag = KnowledgeFragment(
        form="concept",
        title="Redis 写入路由测试",
        frontmatter={
            "type": "tech",
            "name": "Redis 写入路由测试",
            "domain": "redis",
            "summary": "验证蒸馏页面可以直接路由到正式技术目录。",
        },
        background="",
        core_content="content",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
        relations=[],
    )

    page_id, file_path = engine_with_mocks._allocate_page_path(
        frag,
        DistillationEngine._session_short("session_20260415_181305_193daf"),
        set(),
    )

    assert page_id == "redis-写入路由测试"
    assert tmp_path / "03-Tech" in file_path.parents
    assert "00-Inbox" not in file_path.parts
    assert frag.frontmatter["wiki_route_status"] == "direct"
    assert frag.frontmatter["wiki_route_target"].startswith("03-Tech")


def test_allocate_page_path_keeps_unclassified_pages_in_inbox(engine_with_mocks, tmp_path):
    """无法确定分类的页面应保留 Inbox，并在 frontmatter 记录原因。"""
    from core.hephaestus.distillation_engine import DistillationEngine, KnowledgeFragment

    engine_with_mocks.wiki_base = tmp_path
    engine_with_mocks.inbox_dir = tmp_path / "00-Inbox"
    frag = KnowledgeFragment(
        form="concept",
        title="无分类线索测试",
        frontmatter={"name": "无分类线索测试"},
        background="",
        core_content="content",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
        relations=[],
    )

    _page_id, file_path = engine_with_mocks._allocate_page_path(
        frag,
        DistillationEngine._session_short("session_20260415_181305_193daf"),
        set(),
    )

    assert file_path.parent == tmp_path / "00-Inbox"
    assert frag.frontmatter["wiki_route_status"] == "inbox"
    assert frag.frontmatter["wiki_route_reason"] == "unclassified"


def test_allocate_page_path_keeps_formal_basename_collision_in_inbox(engine_with_mocks, tmp_path):
    """正式知识区已有同名 basename 时，新页应回 Inbox 等待人工处理。"""
    from core.hephaestus.distillation_engine import DistillationEngine, KnowledgeFragment

    engine_with_mocks.wiki_base = tmp_path
    engine_with_mocks.inbox_dir = tmp_path / "00-Inbox"
    tech_dir = tmp_path / "03-Tech"
    tech_dir.mkdir(parents=True)
    (tech_dir / "redis-写入路由测试.md").write_text("# existing\n", encoding="utf-8")
    frag = KnowledgeFragment(
        form="concept",
        title="Redis 写入路由测试",
        frontmatter={
            "type": "tech",
            "name": "Redis 写入路由测试",
            "domain": "redis",
            "summary": "验证正式目录同名页面不会被新蒸馏结果覆盖。",
        },
        background="",
        core_content="content",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
        relations=[],
    )

    _page_id, file_path = engine_with_mocks._allocate_page_path(
        frag,
        DistillationEngine._session_short("session_20260415_181305_193daf"),
        set(),
    )

    assert file_path.parent == tmp_path / "00-Inbox"
    assert frag.frontmatter["wiki_route_status"] == "inbox"
    assert frag.frontmatter["wiki_route_reason"] == "formal_basename_collision"


def test_allocate_page_path_uses_short_hash_only_on_disk_collision(engine_with_mocks, tmp_path):
    """已有同名文件时才追加短哈希，避免默认把 source id 暴露到文件名。"""
    from core.hephaestus.distillation_engine import DistillationEngine, KnowledgeFragment

    engine_with_mocks.wiki_base = tmp_path
    engine_with_mocks.inbox_dir = tmp_path / "00-Inbox"
    engine_with_mocks.inbox_dir.mkdir()
    (engine_with_mocks.inbox_dir / "无分类页面短哈希验证.md").write_text(
        "# existing\n",
        encoding="utf-8",
    )
    frag = KnowledgeFragment(
        form="concept",
        title="无分类页面短哈希验证",
        frontmatter={},
        background="",
        core_content="content",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
        relations=[],
    )
    page_id, file_path = engine_with_mocks._allocate_page_path(
        frag,
        DistillationEngine._session_short("session_20260415_181305_193daf"),
        set(),
    )

    assert page_id.startswith("无分类页面短哈希验证-")
    assert not page_id.startswith("20260415_")
    assert "session" not in page_id
    assert file_path.name == f"{page_id}.md"
