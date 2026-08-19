from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.hephaestus.distillation_engine import DistillationEngine
from core.hephaestus.distill_input_spec import DistillInputSpec
from core.hephaestus.distill_response import DistillBackendResponse
from core.hephaestus.distillation_models import (
    DistillationResult,
    FragmentRouteCapability,
    KnowledgeFragment,
)
from tests.cognition_episode_fixtures import (
    exact_source_message,
    model_cognition_episode,
    model_exact_evidence,
    resolve_model_evidence,
)


def _fragment(title: str = "A sufficiently long knowledge title") -> KnowledgeFragment:
    return KnowledgeFragment(
        form="问题-解决",
        title=title,
        frontmatter={"摘要": "足够清晰的知识摘要", "领域": "工程"},
        background="background",
        core_content=f"# {title}\n" + ("complete evidence " * 20),
        boundaries={"applies": "tests"},
        anti_patterns=[],
        related_concepts=[],
        claim_ids=["terminal-state-claim"],
    )


def _bound_structured_output(
    session_id: str, *, source_agent: str = "terminal-state-test"
) -> tuple[DistillInputSpec, dict]:
    visible_input = f"验证蒸馏终态收据。\nterminal-state integration test: {session_id}"
    input_spec = DistillInputSpec.build(
        source_agent=source_agent,
        source_session_id=session_id,
        source_event_ids=("raw-terminal-1",),
        raw_completeness="full",
        visible_input=visible_input,
        input_mode="standard",
        source_messages=[
            exact_source_message(
                role="user",
                content=visible_input,
                revision_id="raw-terminal-1",
            )
        ],
    )
    evidence = model_exact_evidence(input_spec)
    structured = {
        "schema_version": "distill_output_v4",
        **input_spec.prompt_contract(),
        "distill_intent": "create",
        "candidate_summary": "终态收据测试所需的绑定结构化蒸馏输出。",
        "user_behavior_intent": {
            "content_source": "native_dialogue",
            "user_intent_signal": "seeking_judgment",
            "intent_hypothesis": "seeking_judgment",
            "intent_evidence": [
                {
                    **dict(evidence),
                    "reason": "测试输入要求验证终态。",
                }
            ],
            "intent_verification_events": [],
            "intent_confidence": 0.8,
            "intent_status": "unverified",
            "behavior_summary": "用户要求验证蒸馏终态收据。",
        },
        "claims": [
            {
                "claim_id": "terminal-state-claim",
                "claim_text": "蒸馏终态必须由可验证的结构化动作收据表达。",
                "claim_type": "technical_fact",
                "scope": {"domain": "testing"},
                "evidence": [dict(evidence)],
                "relation_to_existing": {
                    "type": "new",
                    "target_pages": [],
                    "delta_text": "",
                    "reason": "测试 vault 没有既有页面。",
                },
                "recommended_action": "create_page",
                "confidence": 0.8,
            }
        ],
        "cognition_episode": model_cognition_episode(
            evidence,
            claim_id="terminal-state-claim",
        ),
    }
    root = {
        "judgment": "knowledge",
        "judgment_reason": "terminal state fixture",
        "fragments": [],
        "structured_output": structured,
    }
    return input_spec, resolve_model_evidence(root, input_spec)["structured_output"]


def _disable_unrelated_structured_gate(monkeypatch, tmp_path):
    from core.cognitive.state_schema import initialize_cognitive_state_schema

    database_dir = tmp_path / ".db"
    initialize_cognitive_state_schema(database_dir / "producer_consumer_ledger.db")
    cfg = SimpleNamespace(
        wiki_dir=tmp_path,
        database_dir=database_dir,
        get=lambda key, default=None: (
            False if key == "distill.structured_output_contract.enforce" else default
        ),
    )
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: cfg)


def _accept_test_fragments(monkeypatch, engine):
    monkeypatch.setattr(
        engine,
        "_filter_accepted_fragments",
        lambda _result, fragments, _cfg: fragments,
    )


def _initialize_cognitive_state(database_dir):
    from core.cognitive.state_schema import initialize_cognitive_state_schema

    initialize_cognitive_state_schema(
        database_dir / "producer_consumer_ledger.db"
    )


def _bound_skill_result(session_id: str, fragment: KnowledgeFragment) -> DistillationResult:
    """Build a strict skill result with the same admission proof as production."""
    from core.hephaestus.distillation_contract import (
        canonical_extraction_output_hash,
        canonicalize_extraction_output,
        validate_extraction_output,
    )

    input_spec, structured_output = _bound_structured_output(
        session_id, source_agent="codex"
    )
    root = canonicalize_extraction_output(
        {
            "judgment": "skill",
            "judgment_reason": "该会话包含可复用的决策方法与验证配方。",
            "fragments": [],
            "structured_output": structured_output,
        },
        [fragment],
    )
    validation = validate_extraction_output(root, input_spec)
    assert validation.valid, validation.error_text
    root_hash = canonical_extraction_output_hash(canonical_output=root)
    return DistillationResult(
        session_id=session_id,
        judgment="skill",
        judgment_reason="认知决策资产候选",
        source="codex",
        fragments=[fragment],
        structured_output=structured_output,
        input_spec=input_spec,
        extraction_judgment="skill",
        extraction_contract_valid=True,
        extraction_output=root,
        extraction_output_hash=root_hash,
        fragment_route_capability=FragmentRouteCapability(
            extraction_output_hash=root_hash,
            input_spec_hash=input_spec.input_spec_hash,
            fragments=(fragment,),
        ),
        raw_event_refs=[
            {
                "revision_id": "raw-terminal-1",
                "span_start": 0,
                "span_end": len(fragment.core_content),
                "span_status": "exact",
            }
        ],
    )


def _bound_knowledge_result(
    session_id: str, fragments: list[KnowledgeFragment]
) -> DistillationResult:
    """Build a routed knowledge fixture with an immutable admitted root."""
    from core.hephaestus.distillation_contract import (
        canonical_extraction_output_hash,
        canonicalize_extraction_output,
        validate_extraction_output,
    )

    input_spec, structured_output = _bound_structured_output(session_id)
    root = canonicalize_extraction_output(
        {
            "judgment": "knowledge",
            "judgment_reason": "该会话包含可复用且已准入的测试知识。",
            "structured_output": structured_output,
        },
        fragments,
    )
    validation = validate_extraction_output(root, input_spec)
    assert validation.valid, validation.error_text
    root_hash = canonical_extraction_output_hash(canonical_output=root)
    return DistillationResult(
        session_id=session_id,
        judgment="knowledge",
        source=input_spec.source_agent,
        fragments=fragments,
        structured_output=structured_output,
        input_spec=input_spec,
        extraction_judgment="knowledge",
        extraction_contract_valid=True,
        extraction_output=root,
        extraction_output_hash=root_hash,
        fragment_route_capability=FragmentRouteCapability(
            extraction_output_hash=root_hash,
            input_spec_hash=input_spec.input_spec_hash,
            fragments=tuple(fragments),
        ),
    )


def test_skill_write_commits_full_cognition_before_optional_proposal_and_wiki(
    tmp_path, monkeypatch
):
    """COG-013: skill 必须先有完整 typed asset，再派生 proposal 和 Wiki。"""
    db_dir = tmp_path / ".db"

    class _Cfg:
        wiki_dir = tmp_path
        database_dir = db_dir

        def get(self, key, default=None):
            values = {
                "distill.structured_output_contract.enforce": True,
                "distill.action_router.enabled": True,
                "distill.auto_expression_formatting": False,
                "quality_gate.enabled": False,
            }
            return values.get(key, default)

    _initialize_cognitive_state(db_dir)
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: _Cfg())

    db_path = db_dir / "distill_actions.db"
    proposal_payload = {
        "skill_name": "高价值对话完整认知资产",
        "skill_purpose": "保留方法、反模式、边界与验证配方。",
        "asset_schema": "cognitive_decision_asset.v1",
        "asset_type": "methodology",
        "evidence_refs": ["raw-terminal-1"],
        "applicability": ["高价值对话蒸馏"],
        "failure_modes": ["只保留建议标题"],
        "verification_recipe": ["核对认知资产、Wiki 与索引事件"],
        "automation_derivative_allowed": False,
    }

    def _proposal_after_asset(_prompt, *, expect_json):
        assert expect_json is True
        with sqlite3.connect(db_path) as conn:
            committed = conn.execute(
                "SELECT COUNT(*) FROM cognition_asset_commits"
            ).fetchone()[0]
        assert committed == 1
        return DistillBackendResponse.create(
            raw_text=json.dumps(proposal_payload, ensure_ascii=False),
            parsed=proposal_payload,
            usage={"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
            provider="test-provider",
            model="test-model",
            parse_path="direct_json",
        )

    def _backend_factory():
        return SimpleNamespace(
            call=MagicMock(side_effect=_proposal_after_asset),
            caller=None,
        )

    fragment = _fragment("高价值对话完整认知资产的持久化方案")
    fragment.form = "方法论"
    tail_evidence = "TAIL_EVIDENCE_MUST_SURVIVE"
    secrets = {
        "api": "sk-" + "live-example-1234567890",
        "password": "private-password-987654",
        "email": "alice.private@example.com",
        "card": "4111 1111 1111 1111",
    }
    fragment.core_content += (
        f"\n## 尾部证据\n{tail_evidence}\n"
        f"api_key={secrets['api']} password={secrets['password']} "
        f"email={secrets['email']} bank_card={secrets['card']}"
    )
    result = _bound_skill_result("s-skill-cognition", fragment)
    engine = DistillationEngine(
        wiki_base=str(tmp_path),
        backend_factory=_backend_factory,
        receipt_config=_Cfg(),
    )
    _accept_test_fragments(monkeypatch, engine)
    monkeypatch.setattr(engine, "_link_cross_agent", lambda *_args: None)
    monkeypatch.setattr(engine, "_write_metrics_back", lambda *_args: None)
    monkeypatch.setattr(
        "core.hephaestus.raw_provenance.record_page_provenance",
        lambda *_args, **_kwargs: (),
    )
    emit_events = MagicMock(wraps=engine._emit_distill_events)
    monkeypatch.setattr(engine, "_emit_distill_events", emit_events)

    receipt = engine.write_pages_with_receipt(result)

    assert receipt.status == "committed"
    assert len(receipt.written_pages) == 1
    assert result.cognition_asset_receipt is not None
    assert result.cognition_asset_receipt.committed is True
    assert result.cognitive_decision_proposal_receipt is not None
    assert result.cognitive_decision_proposal_receipt.committed is True
    assert result.skill_suggestion.startswith("methodology: ")
    assert any(
        item.startswith("cognition_asset:") and item.endswith(":committed")
        for item in receipt.required_consumer_receipts
    )
    assert any(
        item.startswith("cognitive_decision_proposal:") and item.endswith(":committed")
        for item in receipt.required_consumer_receipts
    )
    emit_events.assert_called_once()

    with sqlite3.connect(db_path) as conn:
        payload_json = conn.execute(
            "SELECT asset_payload FROM cognition_asset_commits"
        ).fetchone()[0]
        orphan_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM cognitive_decision_asset_proposals AS proposal
            LEFT JOIN cognition_asset_commits AS asset
              ON asset.asset_id = proposal.asset_id
            WHERE asset.asset_id IS NULL
            """
        ).fetchone()[0]
    payload = json.loads(payload_json)
    assert tail_evidence in payload_json
    assert payload["acl"]["scope"] == "private"
    assert payload["acl"]["session_id"] == "s-skill-cognition"
    assert payload["redaction"]["policy"] == "pii_credentials_only_v1"
    assert payload["cognition"]["source_span_contract"] == {
        "status": "exact",
        "count": 1,
    }
    assert orphan_count == 0

    wiki_text = next(tmp_path.rglob("*.md")).read_text(encoding="utf-8")
    for secret in secrets.values():
        assert secret not in payload_json
        assert secret not in wiki_text


def test_skill_proposal_failure_does_not_rollback_cognition_or_wiki(
    tmp_path, monkeypatch
):
    """The proposal is optional after the canonical cognition asset is durable."""
    db_dir = tmp_path / ".db"

    class _Cfg:
        wiki_dir = tmp_path
        database_dir = db_dir

        def get(self, key, default=None):
            values = {
                "distill.structured_output_contract.enforce": True,
                "distill.action_router.enabled": True,
                "distill.auto_expression_formatting": False,
                "quality_gate.enabled": False,
            }
            return values.get(key, default)

    _initialize_cognitive_state(db_dir)
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: _Cfg())

    def _backend_factory():
        return SimpleNamespace(
            call=MagicMock(side_effect=RuntimeError("provider unavailable")),
            caller=None,
        )

    fragment = _fragment("建议生成失败时依然保留完整认知资产")
    fragment.form = "方法论"
    result = _bound_skill_result("s-skill-proposal-failure", fragment)
    engine = DistillationEngine(
        wiki_base=str(tmp_path),
        backend_factory=_backend_factory,
        receipt_config=_Cfg(),
    )
    _accept_test_fragments(monkeypatch, engine)
    monkeypatch.setattr(engine, "_link_cross_agent", lambda *_args: None)
    monkeypatch.setattr(engine, "_write_metrics_back", lambda *_args: None)
    monkeypatch.setattr(
        "core.hephaestus.raw_provenance.record_page_provenance",
        lambda *_args, **_kwargs: (),
    )

    receipt = engine.write_pages_with_receipt(result)

    assert receipt.status == "committed"
    assert len(receipt.written_pages) == 1
    assert result.cognition_asset_receipt is not None
    assert result.cognition_asset_receipt.committed is True
    assert result.cognitive_decision_proposal_receipt is not None
    assert result.cognitive_decision_proposal_receipt.status == "optional_failed"
    assert result.skill_suggestion == ""
    assert any(item.endswith(":optional_failed") for item in receipt.required_consumer_receipts)

    db_path = db_dir / "distill_actions.db"
    with sqlite3.connect(db_path) as conn:
        asset_count = conn.execute(
            "SELECT COUNT(*) FROM cognition_asset_commits"
        ).fetchone()[0]
        proposal_count = conn.execute(
            "SELECT COUNT(*) FROM cognitive_decision_asset_proposals"
        ).fetchone()[0]
        failed_attempts = conn.execute(
            """
            SELECT COUNT(*) FROM cognitive_decision_proposal_attempts
            WHERE status='optional_failed'
            """
        ).fetchone()[0]
    assert asset_count == 1
    assert proposal_count == 0
    assert failed_attempts == 1


def test_skill_without_typed_asset_commit_never_reaches_wiki_sink(
    tmp_path, monkeypatch
):
    """A failed full-asset commit leaves the session retryable and writes no page."""
    db_dir = tmp_path / ".db"

    class _Cfg:
        wiki_dir = tmp_path
        database_dir = db_dir

        def get(self, key, default=None):
            values = {
                "distill.structured_output_contract.enforce": True,
                "distill.action_router.enabled": True,
                "distill.auto_expression_formatting": False,
                "quality_gate.enabled": False,
            }
            return values.get(key, default)

    _initialize_cognitive_state(db_dir)
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: _Cfg())
    backend_call = MagicMock(return_value={})

    def _backend_factory():
        return SimpleNamespace(call=backend_call, caller=None)

    fragment = _fragment("认知资产收据缺失时的失败关闭方案")
    fragment.form = "方法论"
    result = _bound_skill_result("s-skill-no-asset", fragment)
    # Simulate a corrupted caller identity after the admitted extraction root.
    result.source = "claude"
    engine = DistillationEngine(
        wiki_base=str(tmp_path),
        backend_factory=_backend_factory,
        receipt_config=_Cfg(),
    )
    _accept_test_fragments(monkeypatch, engine)
    wiki_sink = MagicMock(return_value=([], []))
    monkeypatch.setattr(engine, "_persist_pages", wiki_sink)

    receipt = engine.write_pages_with_receipt(result)

    assert receipt.status == "retryable_failed"
    assert receipt.terminal_reason == "cognition_asset_commit_failed"
    assert result.cognition_asset_receipt is not None
    assert result.cognition_asset_receipt.committed is False
    assert result.cognitive_decision_proposal_receipt is None
    wiki_sink.assert_not_called()
    backend_call.assert_not_called()
    assert not list(tmp_path.rglob("*.md"))


def test_knowledge_with_zero_written_pages_is_retryable_not_done(tmp_path, monkeypatch):
    _disable_unrelated_structured_gate(monkeypatch, tmp_path)
    engine = DistillationEngine(wiki_base=str(tmp_path))
    _accept_test_fragments(monkeypatch, engine)
    result = _bound_knowledge_result("s-zero", [_fragment()])
    monkeypatch.setattr(engine, "_persist_pages", lambda *_args: ([], []))

    receipt = engine.write_pages_with_receipt(result)

    assert receipt.status == "retryable_failed"
    assert receipt.written_count == 0
    assert receipt.terminal_reason == "wiki_write_produced_no_durable_artifact"


def test_typed_write_receipt_rejects_fake_committed_state():
    import pytest

    from core.pipeline_receipts import DistillationWriteReceipt

    with pytest.raises(ValueError, match="requires a durable page"):
        DistillationWriteReceipt(status="committed", terminal_reason="fake success")


def test_partial_fragment_write_is_explicit_partial(tmp_path, monkeypatch):
    _disable_unrelated_structured_gate(monkeypatch, tmp_path)
    engine = DistillationEngine(wiki_base=str(tmp_path))
    _accept_test_fragments(monkeypatch, engine)
    result = _bound_knowledge_result(
        "s-partial",
        [
            _fragment("First sufficiently long knowledge title"),
            _fragment("Second sufficiently long knowledge title"),
        ],
    )
    path = tmp_path / "00-Inbox" / "one.md"
    monkeypatch.setattr(
        engine, "_persist_pages", lambda *_args: ([str(path)], [(path, result.fragments[0])])
    )
    monkeypatch.setattr(engine, "_link_cross_agent", lambda *_args: None)
    monkeypatch.setattr(engine, "_write_metrics_back", lambda *_args: None)
    monkeypatch.setattr(engine, "_emit_distill_events", lambda *_args: None)

    receipt = engine.write_pages_with_receipt(result)

    assert receipt.status == "partial"
    assert receipt.written_count == 1
    assert receipt.expected_count == 2


def test_enforced_trusted_proposal_is_pending_not_zero_page_failure(tmp_path, monkeypatch):
    _disable_unrelated_structured_gate(monkeypatch, tmp_path)
    engine = DistillationEngine(wiki_base=str(tmp_path))
    _accept_test_fragments(monkeypatch, engine)
    result = _bound_knowledge_result("s-proposal", [_fragment()])
    from core.hephaestus.distillation_models import PipelineLayerResult

    result.layer_results.append(
        PipelineLayerResult(
            10,
            "trusted_push",
            True,
            {
                "proposal_id": "prop-1",
                "status": "validated",
                "target_path": "page.md",
                "intercepted": True,
            },
        )
    )
    monkeypatch.setattr(engine, "_persist_pages", lambda *_args: ([], []))

    receipt = engine.write_pages_with_receipt(result)

    assert receipt.status == "proposal_pending"
    assert receipt.proposal_ids == ("prop-1",)
    assert receipt.written_count == 0


def test_shadow_proposal_does_not_block_committed_page(tmp_path, monkeypatch):
    _disable_unrelated_structured_gate(monkeypatch, tmp_path)
    engine = DistillationEngine(wiki_base=str(tmp_path))
    _accept_test_fragments(monkeypatch, engine)
    result = _bound_knowledge_result("s-shadow", [_fragment()])
    from core.hephaestus.distillation_models import PipelineLayerResult

    result.layer_results.append(
        PipelineLayerResult(
            10,
            "trusted_push",
            True,
            {
                "proposal_id": "shadow-1",
                "status": "shadow_validated",
                "target_path": "page.md",
                "intercepted": False,
            },
        )
    )
    page = tmp_path / "page.md"
    monkeypatch.setattr(
        engine, "_persist_pages", lambda *_args: ([str(page)], [(page, _fragment())])
    )
    monkeypatch.setattr(engine, "_link_cross_agent", lambda *_args: None)
    monkeypatch.setattr(engine, "_write_metrics_back", lambda *_args: None)
    monkeypatch.setattr(engine, "_emit_distill_events", lambda *_args: None)

    receipt = engine.write_pages_with_receipt(result)

    assert receipt.status == "committed"
    assert receipt.proposal_ids == ()


def test_skip_has_typed_intentional_terminal_reason(tmp_path):
    engine = DistillationEngine(wiki_base=str(tmp_path))
    result = DistillationResult(
        session_id="s-skip",
        judgment="skip",
        judgment_reason="all noise",
    )

    receipt = engine.write_pages_with_receipt(result)

    assert receipt.status == "intentional_skip"
    assert receipt.terminal_reason == "all noise"


def test_structured_skip_action_has_durable_intentional_receipt(tmp_path, monkeypatch):
    _disable_unrelated_structured_gate(monkeypatch, tmp_path)
    engine = DistillationEngine(wiki_base=str(tmp_path))
    _accept_test_fragments(monkeypatch, engine)
    result = _bound_knowledge_result(
        "s-action-skip",
        [_fragment()],
    )
    from core.hephaestus.distillation_models import PipelineLayerResult

    def route(*_args):
        result.layer_results.append(
            PipelineLayerResult(
                9,
                "distill_action_router",
                True,
                {
                    "action_receipts": [
                        {"action_id": "action-1", "status": "skipped", "proposal_id": ""}
                    ],
                    "errors": [],
                },
            )
        )
        return [], []

    monkeypatch.setattr(engine, "_route_structured_actions", route)

    receipt = engine.write_pages_with_receipt(result)

    assert receipt.status == "intentional_skip"
    assert receipt.terminal_reason == "all_structured_actions_intentionally_skipped"
    assert receipt.required_consumer_receipts[0] == "action:action-1:skipped"
    assert receipt.required_consumer_receipts[1].startswith("cognition_episode:cogrev-")
    assert receipt.required_consumer_receipts[1].endswith(":committed")


def test_structured_action_proposal_preserves_existing_page_receipt(tmp_path, monkeypatch):
    _disable_unrelated_structured_gate(monkeypatch, tmp_path)
    engine = DistillationEngine(wiki_base=str(tmp_path))
    _accept_test_fragments(monkeypatch, engine)
    result = _bound_knowledge_result(
        "s-action-proposal",
        [_fragment(), _fragment("Second sufficiently long knowledge title")],
    )
    from core.hephaestus.distillation_models import PipelineLayerResult

    written = str(tmp_path / "existing.md")

    def route(*_args):
        result.layer_results.append(
            PipelineLayerResult(
                9,
                "distill_action_router",
                True,
                {
                    "action_receipts": [
                        {"action_id": "action-1", "status": "applied", "proposal_id": ""},
                        {"action_id": "action-2", "status": "proposed", "proposal_id": "prop-2"},
                    ],
                    "errors": [],
                },
            )
        )
        return [written], []

    monkeypatch.setattr(engine, "_route_structured_actions", route)

    receipt = engine.write_pages_with_receipt(result)

    assert receipt.status == "proposal_pending"
    assert receipt.written_pages == (written,)
    assert receipt.proposal_ids == ("prop-2",)
    assert receipt.failed_count == 0


def test_worker_reconciles_committed_proposal_only_after_target_exists(tmp_path, monkeypatch):
    from core.hephaestus_worker import HephaestusWorker
    from core.kia import amphora

    target = tmp_path / "wiki" / "page.md"
    target.parent.mkdir(parents=True)
    target.write_text("# committed", encoding="utf-8")
    task = {
        "task_id": "task-1",
        "session_id": "session-1",
        "proposal_ids": ["prop-1"],
        "written_count": 0,
    }
    captured = []
    monkeypatch.setattr(amphora, "list_tasks", lambda **_kwargs: [task])
    monkeypatch.setattr(
        amphora,
        "mark_terminal",
        lambda _task_id, receipt, **_kwargs: captured.append(receipt)
        or True,
    )
    monkeypatch.setattr(
        "core.trust.config.load_trusted_push_config",
        lambda **_kwargs: SimpleNamespace(db_path=tmp_path / "trusted.db"),
    )

    class _Queue:
        def __init__(self, *_args, **_kwargs):
            pass

        def get(self, _proposal_id):
            return SimpleNamespace(
                status="committed", candidate=SimpleNamespace(target_path=str(target))
            )

    monkeypatch.setattr("core.trust.proposal_queue.ProposalQueue", _Queue)
    worker = object.__new__(HephaestusWorker)
    worker.config = SimpleNamespace(wiki_dir=tmp_path / "wiki", database_dir=tmp_path)
    worker._mark_l1_distilled = lambda _session_id: None

    assert worker.reconcile_proposal_tasks() == 1
    assert captured[0].status == "committed"
    assert captured[0].written_pages == (str(target),)


def test_worker_treats_explicit_proposal_rejection_as_intentional_skip(tmp_path, monkeypatch):
    from core.hephaestus_worker import HephaestusWorker
    from core.kia import amphora

    task = {
        "task_id": "task-2",
        "session_id": "session-2",
        "proposal_ids": ["prop-2"],
        "written_count": 0,
    }
    captured = []
    monkeypatch.setattr(amphora, "list_tasks", lambda **_kwargs: [task])
    monkeypatch.setattr(
        amphora,
        "mark_terminal",
        lambda _task_id, receipt, **_kwargs: captured.append(receipt)
        or True,
    )
    monkeypatch.setattr(
        "core.trust.config.load_trusted_push_config",
        lambda **_kwargs: SimpleNamespace(db_path=tmp_path / "trusted.db"),
    )

    class _Queue:
        def __init__(self, *_args, **_kwargs):
            pass

        def get(self, _proposal_id):
            return SimpleNamespace(status="rejected", candidate=SimpleNamespace(target_path=""))

    monkeypatch.setattr("core.trust.proposal_queue.ProposalQueue", _Queue)
    worker = object.__new__(HephaestusWorker)
    worker.config = SimpleNamespace(wiki_dir=tmp_path / "wiki", database_dir=tmp_path)
    worker._mark_l1_distilled = lambda _session_id: None

    assert worker.reconcile_proposal_tasks() == 1
    assert captured[0].status == "intentional_skip"
    assert "rejected" in captured[0].terminal_reason


def test_worker_keeps_existing_page_when_action_proposal_is_rejected(tmp_path, monkeypatch):
    from core.hephaestus_worker import HephaestusWorker
    from core.kia import amphora

    existing = tmp_path / "wiki" / "existing.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("# existing", encoding="utf-8")
    task = {
        "task_id": "task-mixed",
        "session_id": "session-mixed",
        "proposal_ids": ["prop-rejected"],
        "written_count": 1,
        "written_paths": [str(existing)],
    }
    captured = []
    monkeypatch.setattr(amphora, "list_tasks", lambda **_kwargs: [task])
    monkeypatch.setattr(
        amphora,
        "mark_terminal",
        lambda _task_id, receipt, **_kwargs: captured.append(receipt)
        or True,
    )
    monkeypatch.setattr(
        "core.trust.config.load_trusted_push_config",
        lambda **_kwargs: SimpleNamespace(db_path=tmp_path / "trusted.db"),
    )

    class _Queue:
        def __init__(self, *_args, **_kwargs):
            pass

        def get(self, _proposal_id):
            return SimpleNamespace(status="rejected", candidate=SimpleNamespace(target_path=""))

    monkeypatch.setattr("core.trust.proposal_queue.ProposalQueue", _Queue)
    worker = object.__new__(HephaestusWorker)
    worker.config = SimpleNamespace(wiki_dir=tmp_path / "wiki", database_dir=tmp_path)
    worker._mark_l1_distilled = lambda _session_id: None

    assert worker.reconcile_proposal_tasks() == 1
    assert captured[0].status == "committed"
    assert captured[0].written_pages == (str(existing),)


def test_same_input_revision_reuses_page_but_distinct_fragment_does_not(tmp_path):
    from core.hephaestus.distillation_page_identity import allocate_revision_page_path

    target = tmp_path / "00-Inbox"
    target.mkdir()
    first_id, first_path = allocate_revision_page_path(
        wiki_base=tmp_path,
        inbox_dir=target,
        title="Revision aware page",
        frontmatter={},
        source_id="session:revision",
        source_session="session-1",
        input_revision="revision-1",
        fragment_hash="fragment-1",
        seen_slugs=set(),
    )
    first_path.write_text(
        "---\nsource_session: session-1\ninput_revision: revision-1\n"
        "fragment_hash: fragment-1\n---\n# page\n",
        encoding="utf-8",
    )

    retry_id, retry_path = allocate_revision_page_path(
        wiki_base=tmp_path,
        inbox_dir=target,
        title="Revision aware page",
        frontmatter={},
        source_id="session:revision",
        source_session="session-1",
        input_revision="revision-1",
        fragment_hash="fragment-1",
        seen_slugs=set(),
    )
    other_id, other_path = allocate_revision_page_path(
        wiki_base=tmp_path,
        inbox_dir=target,
        title="Revision aware page",
        frontmatter={},
        source_id="session:revision",
        source_session="session-1",
        input_revision="revision-1",
        fragment_hash="fragment-2",
        seen_slugs=set(),
    )

    assert (retry_id, retry_path) == (first_id, first_path)
    assert other_id != first_id
    assert other_path != first_path
