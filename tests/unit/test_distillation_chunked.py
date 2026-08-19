import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.cognition_episode_fixtures import (
    model_cognition_episode,
    model_exact_evidence,
    resolve_model_evidence,
)


def _spanned_chunk_message(role: str, content: str, turn: int) -> dict[str, object]:
    """Build one capture-shaped message with its exact Raw byte-range contract."""
    return {
        "role": role,
        "content": content,
        "turn": turn,
        "turn_number": turn,
        "source_span": {
            "revision_id": f"raw-revision-{turn}",
            "logical_event_id": f"logical-event-{turn}",
            "turn_number": turn,
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "role": role,
            "span_start": 0,
            "span_end": len(content),
        },
    }


def _seed_exact_raw_spans(
    messages: list[dict[str, object]], *, session_id: str, config: object
) -> list[dict[str, object]]:
    """Back fixture source spans with the immutable Raw revisions they name."""
    from core.sync_framework.raw_event_store import RawEventStore

    store = RawEventStore(config=config)
    refs: list[dict[str, object]] = []
    try:
        for message in messages:
            role = str(message["role"])
            content = str(message["content"])
            turn = int(message["turn"])
            revision_id = store.upsert_turn(
                source_agent="test",
                session_id=session_id,
                turn_number=turn,
                user_content=content if role == "user" else "",
                assistant_content=content if role == "assistant" else "",
                metadata={"native_event_id": f"{session_id}:{turn}:{role}"},
            )
            raw_turn = store.get_turn(revision_id)
            assert raw_turn is not None
            source_span = message["source_span"]
            assert isinstance(source_span, dict)
            source_span.update(
                {
                    "revision_id": revision_id,
                    "logical_event_id": str(raw_turn["logical_event_id"]),
                    "content_hash": str(raw_turn["content_hash"]),
                }
            )
            refs.append(dict(source_span))
    finally:
        store.close()
    return refs


@pytest.fixture(autouse=True)
def _reset_distillation_pause_state():
    """每个测试前重置蒸馏暂停状态，避免测试隔离问题。"""
    from core.hephaestus.distillation_pause import resume_distillation

    resume_distillation()


def test_clean_field_removes_embedded_json_without_regex_error():
    from core.hephaestus.distillation_json import _clean_field

    dirty = '可读标题 {"judgment": "knowledge", "reason": "ok", "knowledge": []}'

    assert _clean_field(dirty) == "可读标题"


def _fragment(title: str):
    from core.hephaestus.distillation_engine import KnowledgeFragment

    return KnowledgeFragment(
        # Keep the test payload on the same public v4 schema as production
        # extraction results.  The typed port must not quietly admit the old
        # internal English form aliases.
        form="决策记录",
        title=title + "方案分析",
        frontmatter={
            "领域": "测试",
            "置信度": 0.8,
            "摘要": "这是一个测试摘要，用于验证分块蒸馏功能",
        },
        background="采用分块蒸馏保留长会话覆盖范围。",
        core_content="# 测试内容\n\n中长会话应逐块提取后合并，不应因为旧变量名丢失片段。"
        + "需要超过一百字符才能通过硬校验。" * 5,
        boundaries={"applies": "长会话蒸馏", "not_applies": "短会话"},
        anti_patterns=[],
        related_concepts=[],
        claim_ids=["claim-1"],
    )


def _receipt_config(tmp_path: Path):
    from core.config import get_config
    from core.cognitive.state_schema import initialize_cognitive_state_schema

    config = get_config()
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")
    return SimpleNamespace(database_dir=tmp_path, get=config.get)


def _fragment_payload(fragment):
    """Return the model-side fragment shape used by the v4 union validator."""
    return {
        "form": fragment.form,
        "title": fragment.title,
        "frontmatter": dict(fragment.frontmatter),
        "background": fragment.background,
        "core_content": fragment.core_content,
        "boundaries": dict(fragment.boundaries),
        "anti_patterns": list(fragment.anti_patterns),
        "related_concepts": list(fragment.related_concepts),
        "claim_ids": list(fragment.claim_ids),
    }


def _structured_output(input_spec):
    """Build a real v4 structured output bound to one immutable request."""
    evidence = model_exact_evidence(input_spec)
    return {
        "schema_version": "distill_output_v4",
        "input_spec_hash": input_spec.input_spec_hash,
        "cognition_context_hash": input_spec.cognition_context.context_hash,
        "gate_decision_id": input_spec.gate_decision_id,
        "source_agent": input_spec.source_agent,
        "source_session_id": input_spec.source_session_id,
        "source_event_ids": list(input_spec.source_event_ids),
        "raw_completeness": input_spec.raw_completeness,
        "distill_intent": "create",
        "candidate_summary": "长会话分块蒸馏覆盖度测试。",
        "user_behavior_intent": {
            "content_source": "native_dialogue",
            "user_intent_signal": "seeking_judgment",
            "intent_hypothesis": "seeking_judgment",
            "intent_evidence": [
                {
                    **dict(evidence),
                    "reason": "用户要求验证分块蒸馏覆盖。",
                }
            ],
            "intent_verification_events": [],
            "intent_confidence": 0.7,
            "intent_status": "unverified",
            "behavior_summary": "用户需要验证长会话分块蒸馏覆盖度。",
        },
        "claims": [
            {
                "claim_id": "claim-1",
                "claim_text": "长会话应使用分块蒸馏并在写入页面时保留覆盖度 frontmatter。",
                "claim_type": "technical_fact",
                "scope": {"domain": "test", "applies_to": ["chunked distillation"], "not_applies_to": []},
                "evidence": [dict(evidence)],
                "relation_to_existing": {
                    "type": "new",
                    "target_pages": [],
                    "delta_text": "",
                    "reason": "测试临时 vault 中没有既有页面。",
                },
                "recommended_action": "create_page",
                "confidence": 0.8,
            }
        ],
        "cognition_episode": model_cognition_episode(
            evidence,
            claim_id="claim-1",
        ),
    }


def _outcome(request, fragment):
    """Construct an admitted typed extractor result, not a legacy list fake."""
    from core.hephaestus.distillation_contract import (
        canonical_extraction_output_hash,
        canonicalize_extraction_output,
        validate_extraction_output,
    )
    from core.hephaestus.distillation_models import ExtractionOutcome

    structured = _structured_output(request.input_spec)
    payload = {
        "judgment": "knowledge",
        "judgment_reason": "测试输入具有可复用的分块蒸馏价值。",
        "fragments": [_fragment_payload(fragment)],
        "structured_output": structured,
    }
    payload = resolve_model_evidence(payload, request.input_spec)
    structured = payload["structured_output"]
    admission = validate_extraction_output(payload, request.input_spec)
    assert admission.valid, admission.error_text
    canonical_output = canonicalize_extraction_output(payload, (fragment,))
    return ExtractionOutcome(
        judgment="knowledge",
        fragments=(fragment,),
        structured_output=structured,
        canonical_output=canonical_output,
        admission=admission,
        canonical_output_hash=canonical_extraction_output_hash(
            canonical_output=canonical_output,
        ),
    )


def _skip_outcome(request):
    """Construct a legal v4 skip outcome for one chunk."""
    from core.hephaestus.distillation_contract import (
        canonical_extraction_output_hash,
        canonicalize_extraction_output,
        validate_extraction_output,
    )
    from core.hephaestus.distillation_models import ExtractionOutcome

    event_id = request.input_spec.source_event_ids[0]
    structured = {
        "schema_version": "distill_output_v4",
        "input_spec_hash": request.input_spec.input_spec_hash,
        "cognition_context_hash": request.input_spec.cognition_context.context_hash,
        "gate_decision_id": request.input_spec.gate_decision_id,
        "source_agent": request.input_spec.source_agent,
        "source_session_id": request.input_spec.source_session_id,
        "source_event_ids": list(request.input_spec.source_event_ids),
        "raw_completeness": request.input_spec.raw_completeness,
        "distill_intent": "skip",
        "candidate_summary": "这一局部 chunk 没有新的长期可复用知识。",
        "skip_reason": "局部重复确认不增加新的决策或方法。",
        "no_value_evidence": [
            {"source_event_id": event_id, "reason": "该局部仅重复确认前文结论。"}
        ],
        "claims": [],
    }
    payload = {
        "judgment": "skip",
        "judgment_reason": "该 chunk 是合法的无新增价值确认。",
        "fragments": [],
        "structured_output": structured,
    }
    admission = validate_extraction_output(payload, request.input_spec)
    assert admission.valid, admission.error_text
    canonical_output = canonicalize_extraction_output(payload, ())
    return ExtractionOutcome(
        judgment="skip",
        fragments=(),
        structured_output=structured,
        canonical_output=canonical_output,
        admission=admission,
        canonical_output_hash=canonical_extraction_output_hash(
            canonical_output=canonical_output,
        ),
    )


class _FakeBackend:
    def checkpoint_identity(self):
        return {"provider": "test", "model": "fake-extractor"}


class _FakeExtractor:
    def __init__(self):
        self.calls = []
        self.backend = _FakeBackend()
        self._titles = [
            "Redis 吞吐策略",
            "Docker 网络边界",
            "向量索引刷新",
            "后台预算调度",
        ]

    def prepare_prompt(self, request):
        from core.hephaestus.distill_input_spec import PreparedExtractionPrompt

        return PreparedExtractionPrompt.build(
            f"typed-test-extractor|{request.analysis_type}|{request.session_text}",
            request,
        )

    def extract(self, request, *, prepared=None):
        assert prepared is not None
        prepared.assert_matches(request)
        self.calls.append((request.session_text, request.input_spec, prepared))
        title = self._titles[(len(self.calls) - 1) % len(self._titles)]
        return _outcome(request, _fragment(title))


class _KnowledgeThenSkipExtractor(_FakeExtractor):
    """First chunk is knowledge; the later chunk is a valid local skip."""

    def extract(self, request, *, prepared=None):
        assert prepared is not None
        prepared.assert_matches(request)
        self.calls.append((request.session_text, request.input_spec, prepared))
        if len(self.calls) == 1:
            return _outcome(request, _fragment("第一块关键决策保留"))
        return _skip_outcome(request)


class _LegacyListExtractor:
    """The pre-COG-011 test-double shape that must no longer be accepted."""

    backend = _FakeBackend()

    def extract(self, session_text, session_id, analysis_type, prepared_prompt=None):
        del prepared_prompt
        return [_fragment("旧接口返回列表")]


def test_chunked_path_rejects_legacy_list_extractor_port(monkeypatch, tmp_path):
    """A typed request/outcome boundary prevents stale list fakes from masking drift."""
    from core.hephaestus.distillation_engine import DistillationEngine, DistillationResult

    _patch_chunk_checkpoint_config(monkeypatch, tmp_path)
    engine = DistillationEngine(
        wiki_base=str(tmp_path), receipt_config=_receipt_config(tmp_path)
    )
    engine._extractor = _LegacyListExtractor()
    chunk = [_spanned_chunk_message("user", "typed port required", 1)]
    engine._chunk_messages = lambda *args, **kwargs: [chunk]
    result = DistillationResult(session_id="legacy-port")

    fragments, chunk_info = engine._extract_chunked(
        result,
        chunk,
        {"cfg": engine._runtime_receipt_config, "chunk_size": 400},
    )

    assert fragments is None
    assert chunk_info == []
    assert result.judgment == "error"
    assert result.error == "extractor_protocol_violation"
    assert result.extraction_contract_valid is False
    # Port negotiation fails before admission, so a legacy list output can
    # never create a completed checkpoint that later looks reusable.
    assert not (tmp_path / "db" / "chunks.db").exists()


def test_chunk_fingerprint_includes_execution_spec_hash():
    from core.hephaestus.chunk_checkpoint import build_chunk_fingerprint

    chunk = [_spanned_chunk_message("user", "ROOT005 checkpoint input", 1)]
    current = build_chunk_fingerprint(
        chunk, 0, 400, None, "sha256:current-spec"
    )
    legacy = build_chunk_fingerprint(
        chunk, 0, 400, None, "sha256:legacy-spec"
    )

    assert current != legacy


def test_chunk_info_records_lossless_input_contract():
    from core.hephaestus.chunk_checkpoint import build_chunk_info
    from core.hephaestus.distillation_engine import (
        DISTILLATION_INPUT_CONTRACT_VERSION,
    )

    chunk = [_spanned_chunk_message("user", "ROOT005 observable checkpoint", 1)]
    info = build_chunk_info(
        0,
        chunk,
        {},
        [_fragment("ROOT005 checkpoint evidence")],
        {id(chunk[0]): 1},
    )

    assert info["input_contract_version"] == DISTILLATION_INPUT_CONTRACT_VERSION


def _patch_chunk_checkpoint_config(monkeypatch, tmp_path):
    from core.config import get_config as real_get_config

    real_cfg = real_get_config()

    class _FakeConfig:
        database_dir = tmp_path / "db"

        def get(self, key, default=None):
            overrides = {
                "distill.chunk_checkpoint_enabled": True,
                "distill.chunk_checkpoint_db_path": str(tmp_path / "db" / "chunks.db"),
            }
            if key in overrides:
                return overrides[key]
            return real_cfg.get(key, default)

        def __getattr__(self, name):
            if name == "database_dir":
                return self.database_dir
            return getattr(real_cfg, name)

    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: _FakeConfig())


def test_chunked_distillation_retries_only_failed_chunks(monkeypatch, tmp_path):
    """分块蒸馏失败重试应复用已成功 chunk，而不是从头重复。"""
    from core.hephaestus import distillation_engine as mod
    from core.hephaestus.distillation_engine import DistillationEngine, DistillationResult

    _patch_chunk_checkpoint_config(monkeypatch, tmp_path)

    engine = DistillationEngine(
        wiki_base=str(tmp_path), receipt_config=_receipt_config(tmp_path)
    )
    chunks = [
        [_spanned_chunk_message("user", "CHUNK-0 稳定内容", 1)],
        [_spanned_chunk_message("assistant", "CHUNK-1 补跑内容", 2)],
    ]
    engine._chunk_messages = lambda *args, **kwargs: chunks

    calls = []
    failed_once = [False]

    def _fake_safe_extract(
        request,
        result,
        prepared=None,
    ):
        assert prepared is not None
        prepared.assert_matches(request)
        session_text = request.session_text
        calls.append(session_text)
        if "CHUNK-0" in session_text:
            return _outcome(request, _fragment("第一块稳定结果"))
        if "CHUNK-1" in session_text and not failed_once[0]:
            failed_once[0] = True
            result.judgment = "error"
            result.judgment_reason = "chunk 1 transient failure"
            return None
        if "CHUNK-1" in session_text:
            return _outcome(request, _fragment("第二块补跑结果"))
        return _outcome(request, _fragment("未知块结果"))

    # _extract_chunked must prepare and bind the request before it reaches the
    # injected fault.  A stale list-returning fake is deliberately not a
    # compatible extractor protocol any more.
    engine._extractor = _FakeExtractor()
    engine._safe_extract = _fake_safe_extract
    filtered = [item for chunk in chunks for item in chunk]
    budget = {
        "cfg": mod.get_config(),
        "chunk_size": 400,
    }

    first = DistillationResult(session_id="sess-resume-chunks")
    first_fragments, first_infos = engine._extract_chunked(first, filtered, budget)
    assert first.judgment == "error"
    assert first_fragments is None
    assert first_infos == []
    assert sum("CHUNK-0" in call for call in calls) == 1
    assert sum("CHUNK-1" in call for call in calls) == 1

    second = DistillationResult(session_id="sess-resume-chunks")
    second_fragments, second_infos = engine._extract_chunked(second, filtered, budget)

    assert [fragment.title for fragment in second_fragments] == [
        "第一块稳定结果方案分析",
        "第二块补跑结果方案分析",
    ]
    assert sum("CHUNK-0" in call for call in calls) == 1
    assert sum("CHUNK-1" in call for call in calls) == 2
    assert any(chunk.get("checkpoint_reused") for chunk in second_infos)
    # Identical chunk-local absence statements describe one session-level
    # unknown, while known entries retain their evidence-distinct occurrence.
    assert len(second.structured_output["cognition_episode"]["assumptions"]) == 1
    assert len(second.structured_output["cognition_episode"]["situation"]) == 2


def test_chunked_distillation_uses_merged_fragments_and_writes_coverage(
    tmp_path,
    _canonical_material_actions,
):
    from core.hephaestus.distillation_engine import DistillationEngine, ValuePrejudgment

    config = _receipt_config(tmp_path)
    engine = DistillationEngine(wiki_base=str(tmp_path), receipt_config=config)
    extractor = _FakeExtractor()
    engine._extractor = extractor
    engine._noise_filter = SimpleNamespace(
        filter=lambda messages: (messages, {"kept": len(messages)})
    )
    engine._value_prejudgment = SimpleNamespace(
        judge=lambda messages: (ValuePrejudgment.CERTAINLY_YES, 0.9)
    )
    engine._self_check = SimpleNamespace(check=lambda fragments, messages: (True, []))
    engine._cross_linker = SimpleNamespace(link=lambda fragments: fragments)
    engine._feedback_loop = SimpleNamespace(evaluate=lambda result: [])
    engine._kia_linker = False
    # 这两个用例只验证分块与覆盖度逻辑，不测试 FragmentMerger；
    # 关闭合并可避免环境中有 API key 时 LLM 改写标题导致断言失败。
    engine._fragment_merger = SimpleNamespace(
        merge=lambda fragments: fragments,
        checkpoint_identity=lambda: {"strategy": "test-no-merge"},
    )

    messages = [
        _spanned_chunk_message(
            "user" if i % 2 == 0 else "assistant",
            f"第 {i} 轮：决定采用稳定的后台预算和完整覆盖追踪。"
            + (" 分块蒸馏证据" * 220),
            i + 1,
        )
        for i in range(70)
    ]
    raw_event_refs = _seed_exact_raw_spans(
        messages,
        session_id="sess-chunked",
        config=config,
    )

    result = engine.process(
        "sess-chunked",
        messages,
        meta={"source": "test", "raw_event_refs": raw_event_refs},
    )

    assert result.judgment == "knowledge"
    assert result.analysis_type == "chunked"
    assert result.distill_input_mode == "chunked"
    assert "分块蒸馏" in result.session_coverage
    assert len(extractor.calls) >= 2
    assert result.fragments

    receipt = engine.write_pages_with_receipt(result)
    paths = list(receipt.written_pages)
    assert paths, (receipt, result.layer_results[-1])
    page = Path(paths[0]).read_text(encoding="utf-8")
    assert "蒸馏输入模式: chunked" in page
    assert "来源覆盖度:" in page
    assert "分块蒸馏" in page


def test_chunked_knowledge_is_not_overwritten_by_later_legal_skip_and_routes_page(
    tmp_path,
    _canonical_material_actions,
):
    """A legal local skip must not erase an earlier admitted knowledge root."""
    from core.hephaestus.distillation_engine import DistillationEngine, ValuePrejudgment

    config = _receipt_config(tmp_path)
    engine = DistillationEngine(wiki_base=str(tmp_path), receipt_config=config)
    extractor = _KnowledgeThenSkipExtractor()
    engine._extractor = extractor
    chunks = [
        [_spanned_chunk_message("user", "CHUNK-0: 保留关键决策", 1)],
        [_spanned_chunk_message("assistant", "CHUNK-1: 仅重复确认", 2)],
    ]
    engine._chunk_messages = lambda *args, **kwargs: chunks
    engine._noise_filter = SimpleNamespace(
        filter=lambda messages: (messages, {"kept": len(messages)})
    )
    engine._value_prejudgment = SimpleNamespace(
        judge=lambda messages: (ValuePrejudgment.CERTAINLY_YES, 0.9)
    )
    engine._build_distillation_budget = lambda *args, **kwargs: {
        "raw_tokens": 2,
        "cfg": engine._runtime_receipt_config,
        "std_threshold": 1,
        "chunk_threshold": 10,
        "chunk_size": 400,
    }
    engine._self_check = SimpleNamespace(check=lambda fragments, messages: (True, []))
    engine._cross_linker = SimpleNamespace(link=lambda fragments: fragments)
    engine._feedback_loop = SimpleNamespace(evaluate=lambda result: [])
    engine._kia_linker = False
    engine._fragment_merger = SimpleNamespace(
        merge=lambda fragments: fragments,
        checkpoint_identity=lambda: {"strategy": "test-no-merge"},
    )
    messages = [item for chunk in chunks for item in chunk]
    raw_event_refs = _seed_exact_raw_spans(
        messages,
        session_id="sess-knowledge-then-skip",
        config=config,
    )

    result = engine.process(
        "sess-knowledge-then-skip",
        messages,
        meta={"source": "test", "raw_event_refs": raw_event_refs},
    )

    assert [fragment.title for fragment in result.fragments] == [
        "第一块关键决策保留方案分析"
    ]
    assert result.extraction_judgment == "knowledge"
    aggregate_claim = result.structured_output["claims"][0]
    assert aggregate_claim["claim_id"].startswith("claim-")
    assert aggregate_claim["aggregate_original_claim_ids"] == ["claim-1"]
    assert engine.write_pages(result)


def test_over_500k_uses_chunked_not_head_tail(
    tmp_path,
    _canonical_material_actions,
):
    """[P1-1] >500k 字符会话应使用分块蒸馏，不应用 head-tail，frontmatter 应标记 coverage=full_chunked"""
    from core.hephaestus.distillation_engine import DistillationEngine, ValuePrejudgment

    config = _receipt_config(tmp_path)
    engine = DistillationEngine(wiki_base=str(tmp_path), receipt_config=config)
    extractor = _FakeExtractor()
    engine._extractor = extractor
    engine._noise_filter = SimpleNamespace(
        filter=lambda messages: (messages, {"kept": len(messages)})
    )
    engine._value_prejudgment = SimpleNamespace(
        judge=lambda messages: (ValuePrejudgment.CERTAINLY_YES, 0.9)
    )
    engine._self_check = SimpleNamespace(check=lambda fragments, messages: (True, []))
    engine._cross_linker = SimpleNamespace(link=lambda fragments: fragments)
    engine._feedback_loop = SimpleNamespace(evaluate=lambda result: [])
    engine._kia_linker = False
    # 这两个用例只验证分块与覆盖度逻辑，不测试 FragmentMerger；
    # 关闭合并可避免环境中有 API key 时 LLM 改写标题导致断言失败。
    engine._fragment_merger = SimpleNamespace(
        merge=lambda fragments: fragments,
        checkpoint_identity=lambda: {"strategy": "test-no-merge"},
    )

    # 构造 >500k 字符的会话（每条约 10k 字符，60 条 = 600k）
    long_content = "x" * 10000
    messages = [
        _spanned_chunk_message(
            "user" if i % 2 == 0 else "assistant", long_content, i + 1
        )
        for i in range(60)
    ]
    raw_event_refs = _seed_exact_raw_spans(
        messages,
        session_id="sess-500k",
        config=config,
    )

    result = engine.process(
        "sess-500k",
        messages,
        meta={"source": "test", "raw_event_refs": raw_event_refs},
    )

    assert result.judgment == "knowledge"
    assert result.analysis_type == "chunked"
    assert result.distill_input_mode == "chunked"
    assert "分块蒸馏" in result.session_coverage
    # 确认没有使用 head_tail
    assert "head_tail" not in result.session_coverage.lower()

    paths = engine.write_pages(result)
    assert paths
    page = Path(paths[0]).read_text(encoding="utf-8")
    assert "覆盖度: full_chunked" in page
