# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.hephaestus.distill_action_router import (
    DistillActionRouter,
    DistillActionRouterOptions,
)
from core.hephaestus.distillation_contract import (
    canonical_extraction_output_hash,
    canonicalize_extraction_output,
    validate_extraction_output,
)
from core.hephaestus.distill_input_spec import DistillInputSpec
from core.hephaestus.distillation_models import (
    DistillationResult,
    FragmentRouteCapability,
    KnowledgeFragment,
)
from core.trust.knowledge_vault_writer import KnowledgeVaultWriter
from core.trust.proposal_queue import ProposalQueue
from tests.cognition_episode_fixtures import (
    commit_cognition_episode_result,
    exact_source_message,
    model_cognition_episode,
    model_exact_evidence,
    resolve_model_evidence,
)


@pytest.fixture
def router_env(tmp_path, monkeypatch):
    wiki_dir = tmp_path / "wiki"
    database_dir = tmp_path / "db"
    wiki_dir.mkdir()
    database_dir.mkdir()
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *args, **kwargs: None,
    )
    options = DistillActionRouterOptions(
        database_dir=database_dir,
        wiki_dir=wiki_dir,
        min_merge_confidence=0.72,
    )
    return wiki_dir, database_dir, DistillActionRouter(options)


@pytest.fixture
def fragment():
    return KnowledgeFragment(
        form="problem-solution",
        title="Redis 连接池耗尽问题的排查方案",
        frontmatter={"领域": "backend", "摘要": "Redis 连接池耗尽的处理方法。"},
        background="高并发场景下 Redis 连接池耗尽。",
        core_content="## 处理方法\n\n增加连接上限并设置超时。",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
        claim_ids=["claim-1"],
    )


def _input_spec(
    *,
    source_agent: str = "codex",
    source_session_id: str = "sess-router",
    source_event_ids: tuple[str, ...] = ("raw-1", "raw-2"),
    raw_1: str = "帮我判断 Redis 连接池耗尽的根因。连接池上限过低。",
    raw_2: str = "缺少超时监控。",
) -> DistillInputSpec:
    source_messages = [
        exact_source_message(
            role="user",
            content=raw_1,
            revision_id=source_event_ids[0],
        )
    ]
    if len(source_event_ids) > 1:
        source_messages.append(
            exact_source_message(
                role="user",
                content=raw_2,
                revision_id=source_event_ids[1],
            )
        )
    return DistillInputSpec.build(
        source_agent=source_agent,
        source_session_id=source_session_id,
        source_event_ids=source_event_ids,
        raw_completeness="full",
        visible_input=raw_1 + "\n" + raw_2,
        input_mode="standard",
        source_messages=source_messages,
    )


def _payload(
    action: str,
    *,
    input_spec: DistillInputSpec | None = None,
    relation_type: str = "new",
    target_pages: list[str] | None = None,
    confidence: float = 0.9,
    intent: str = "create",
    conflict_strength: float = 0.0,
    reason: str = "和既有页面同域。",
) -> dict:
    input_spec = input_spec or _input_spec()
    first_evidence = model_exact_evidence(
        input_spec,
        source_event_id=input_spec.source_event_ids[0],
    )
    evidence_refs = [dict(first_evidence)]
    if len(input_spec.source_event_ids) > 1:
        evidence_refs.append(
            model_exact_evidence(
                input_spec,
                source_event_id=input_spec.source_event_ids[1],
            )
        )
    payload = {
        "schema_version": "distill_output_v4",
        "input_spec_hash": input_spec.input_spec_hash,
        "cognition_context_hash": input_spec.cognition_context.context_hash,
        "gate_decision_id": input_spec.gate_decision_id,
        "source_agent": input_spec.source_agent,
        "source_session_id": input_spec.source_session_id,
        "source_event_ids": list(input_spec.source_event_ids),
        "raw_completeness": input_spec.raw_completeness,
        "distill_intent": intent,
        "candidate_summary": "Redis 连接池耗尽的排查方案。",
        "user_behavior_intent": {
            "content_source": "native_dialogue",
            "user_intent_signal": "seeking_judgment",
            "intent_hypothesis": "seeking_judgment",
            "intent_evidence": [
                {
                    **dict(first_evidence),
                    "reason": "用户要求判断根因。",
                }
            ],
            "intent_verification_events": [],
            "intent_confidence": 0.72,
            "intent_status": "unverified",
            "behavior_summary": "用户需要判断 Redis 连接池耗尽的原因。",
        },
        "claims": [
            {
                "claim_id": "claim-1",
                "claim_text": "Redis 连接池耗尽通常和连接上限过低、超时配置缺失有关。",
                "claim_type": "technical_fact",
                "scope": {"domain": "backend"},
                "evidence": evidence_refs,
                "relation_to_existing": {
                    "type": relation_type,
                    "target_pages": target_pages or [],
                    "delta_text": "补充超时监控要求。",
                    "reason": reason,
                    "conflict_strength": conflict_strength,
                },
                "recommended_action": action,
                "confidence": confidence,
            }
        ],
        "cognition_episode": model_cognition_episode(
            first_evidence,
            claim_id="claim-1",
        ),
    }
    root = {
        "judgment": "knowledge",
        "judgment_reason": "router fixture",
        "fragments": [],
        "structured_output": payload,
    }
    return resolve_model_evidence(root, input_spec)["structured_output"]


def _explicit_user_payload(action: str, **kwargs) -> tuple[dict, DistillInputSpec]:
    input_spec = _input_spec()
    payload = _payload(action, input_spec=input_spec, **kwargs)
    return payload, input_spec


def _result(
    payload: dict,
    *,
    input_spec: DistillInputSpec | None = None,
    source: str | None = None,
    database_dir: Path | None = None,
) -> DistillationResult:
    input_spec = input_spec or _input_spec()
    result = DistillationResult(
        session_id=input_spec.source_session_id,
        judgment="knowledge",
        structured_output=payload,
        source=source if source is not None else input_spec.source_agent,
        input_spec=input_spec,
    )
    proof_fragment = KnowledgeFragment(
        form="问题-解决",
        title="路由准入证明的完整知识片段",
        frontmatter={
            "领域": "testing",
            "摘要": "用于验证路由根准入证明的完整测试片段。",
        },
        background="路由测试需要一个可验证的根蒸馏片段。",
        core_content=(
            "## 路由根准入证明\n\n"
            "该片段只用于构造已经过输出契约验证的根输出。"
            "它保留足够的上下文、边界和证据描述，以满足正式蒸馏片段的最小长度要求。"
            "测试根同时明确来源事件、结构化决策和后续写入之间的绑定关系，"
            "避免调用方仅凭内层载荷绕过 extractor 已完成的准入判断。"
        ),
        boundaries={"applies": "direct router tests"},
        anti_patterns=[],
        related_concepts=[],
        claim_ids=["claim-1"],
    )
    root = canonicalize_extraction_output(
        {
            "judgment": "knowledge",
            "judgment_reason": "路由测试的根输出已经由 extractor 准入。",
            "structured_output": payload,
        },
        [proof_fragment],
    )
    admission = validate_extraction_output(root, input_spec)
    if admission.valid:
        result.extraction_judgment = "knowledge"
        result.extraction_contract_valid = True
        result.extraction_output = root
        result.extraction_output_hash = canonical_extraction_output_hash(
            canonical_output=root
        )
        result.fragments = [proof_fragment]
        result.fragment_route_capability = FragmentRouteCapability(
            extraction_output_hash=result.extraction_output_hash,
            input_spec_hash=input_spec.input_spec_hash,
            fragments=(proof_fragment,),
        )
        if database_dir is not None:
            commit_cognition_episode_result(result, database_dir)
    return result


def _fake_create_pages(wiki_dir: Path):
    def create_pages(fragments):
        inbox = wiki_dir / "00-Inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        path = inbox / "redis.md"
        path.write_text("# Redis\n", encoding="utf-8")
        return [str(path)], [(path, fragments[0])]

    return create_pages


def _enable_trusted_push_enforce(monkeypatch, wiki_dir: Path, database_dir: Path):
    db_path = database_dir / "trusted_push.db"
    fake_config = SimpleNamespace(
        wiki_dir=wiki_dir,
        database_dir=database_dir,
        get=lambda key, default=None: {
            "trusted_push.mode": "enforce",
            "trusted_push.db_path": str(db_path),
        }.get(key, default),
    )
    monkeypatch.setattr("core.trust.config.get_config", lambda: fake_config)
    return db_path


def test_create_page_logs_action_and_knowledge_change(router_env, fragment):
    wiki_dir, database_dir, router = router_env
    payload = _payload("create_page")
    result = _result(payload, database_dir=database_dir)
    admitted_fragment = result.fragments[0]
    # Formatting/quality layers may mutate admitted objects before routing.
    admitted_fragment.frontmatter["expression_format"] = "checklist"
    admitted_fragment.core_content += "\n\n- 格式化后的合法内容仍使用同一对象。"

    routed = router.route(result, [admitted_fragment], _fake_create_pages(wiki_dir))

    assert len(routed.written) == 1
    assert routed.file_fragments[0][0].name == "redis.md"
    rows = router.list_actions_for_session("sess-router")
    assert rows[0]["action"] == "create_page"
    assert json.loads(rows[0]["source_event_ids"]) == ["raw-1", "raw-2"]
    assert "00-Inbox/redis.md" in rows[0]["target_page"]
    result_detail = json.loads(rows[0]["result_detail"])
    trust_decision_id = result_detail["trust_decision_id"]
    with sqlite3.connect(database_dir / "trust_decisions.db") as conn:
        trust_row = conn.execute(
            "SELECT action, decision FROM trust_decisions WHERE decision_id=?",
            (trust_decision_id,),
        ).fetchone()
    assert trust_row == ("extract", "accept")
    knowledge_rows = router.list_knowledge_actions(rows[0]["action_id"])
    assert knowledge_rows[0]["change_type"] == "page_create"


def test_router_redacts_only_durable_claim_projection(router_env):
    """Admission/provenance keep the immutable claim while ledgers redact PII."""
    wiki_dir, database_dir, router = router_env
    email = "owner@example.com"
    input_spec = _input_spec(
        source_event_ids=("raw-1",),
        raw_1=f"联系 {email} 复核",
    )
    payload = _payload("create_page", input_spec=input_spec)
    result = _result(
        payload,
        input_spec=input_spec,
        database_dir=database_dir,
    )

    routed = router.route(result, result.fragments, _fake_create_pages(wiki_dir))

    assert not routed.errors
    assert email in result.structured_output["claims"][0]["evidence"][0]["quote"]
    action = router.list_actions_for_session("sess-router")[0]
    assert email not in action["evidence_refs"]
    assert "[REDACTED:EMAIL]" in action["evidence_refs"]


def test_router_rejects_create_page_fragment_sequence_outside_admitted_result(
    router_env, fragment
):
    """A valid root cannot be paired with a separate forged page fragment."""
    wiki_dir, database_dir, router = router_env
    result = _result(_payload("create_page"), database_dir=database_dir)
    create_calls = []

    def create_pages(fragments):
        create_calls.append(fragments)
        return _fake_create_pages(wiki_dir)(fragments)

    routed = router.route(result, [fragment], create_pages)

    assert routed.written == []
    assert routed.action_ids == []
    assert create_calls == []
    assert any("identity-subsequence of the post-admission" in error for error in routed.errors)


def test_router_rejects_duplicate_admitted_fragment_identity(router_env):
    """Repeating one admitted instance cannot multiply a create-page write."""
    wiki_dir, database_dir, router = router_env
    result = _result(_payload("create_page"), database_dir=database_dir)
    admitted_fragment = result.fragments[0]
    create_calls = []

    def create_pages(fragments):
        create_calls.append(fragments)
        return _fake_create_pages(wiki_dir)(fragments)

    routed = router.route(
        result,
        [admitted_fragment, admitted_fragment],
        create_pages,
    )

    assert routed.written == []
    assert routed.action_ids == []
    assert create_calls == []
    assert any("identity-subsequence of the post-admission" in error for error in routed.errors)


def test_router_rejects_replacing_result_fragments_after_root_admission(
    router_env, fragment
):
    """Keeping a valid root cannot authorize a later result.fragments swap."""
    wiki_dir, database_dir, router = router_env
    result = _result(_payload("create_page"), database_dir=database_dir)
    result.fragments = [fragment]
    create_calls = []

    def create_pages(fragments):
        create_calls.append(fragments)
        return _fake_create_pages(wiki_dir)(fragments)

    routed = router.route(result, [fragment], create_pages)

    assert routed.written == []
    assert routed.action_ids == []
    assert create_calls == []
    assert any("identity-subsequence of the post-admission" in error for error in routed.errors)


def test_router_rejects_missing_or_stale_fragment_route_capability(router_env):
    wiki_dir, database_dir, router = router_env
    missing = _result(_payload("create_page"), database_dir=database_dir)
    admitted_fragment = missing.fragments[0]
    missing.fragment_route_capability = None
    stale = _result(_payload("create_page"), database_dir=database_dir)
    stale_fragment = stale.fragments[0]
    stale.fragment_route_capability = FragmentRouteCapability(
        extraction_output_hash="sha256:stale-root",
        input_spec_hash=stale.input_spec.input_spec_hash,
        fragments=(stale_fragment,),
    )

    missing_routed = router.route(
        missing,
        [admitted_fragment],
        _fake_create_pages(wiki_dir),
    )
    stale_routed = router.route(
        stale,
        [stale_fragment],
        _fake_create_pages(wiki_dir),
    )

    assert any("require a post-admission route capability" in error for error in missing_routed.errors)
    assert any("not bound to the admitted root" in error for error in stale_routed.errors)


@pytest.mark.parametrize("proof_state", ["missing", "hash_mismatch"])
def test_router_rejects_direct_valid_payload_without_matching_root_admission_proof(
    router_env, fragment, proof_state
):
    """A direct route call cannot bypass extractor admission with inner payload alone."""
    wiki_dir, database_dir, router = router_env
    input_spec = _input_spec()
    payload = _payload("create_page", input_spec=input_spec)
    create_calls = []

    def create_pages(fragments):
        create_calls.append(fragments)
        return _fake_create_pages(wiki_dir)(fragments)

    if proof_state == "missing":
        result = DistillationResult(
            session_id=input_spec.source_session_id,
            judgment="knowledge",
            structured_output=payload,
            source=input_spec.source_agent,
            input_spec=input_spec,
        )
    else:
        result = _result(
            payload,
            input_spec=input_spec,
            database_dir=database_dir,
        )
        result.extraction_output_hash = "sha256:forged-root-hash"

    routed = router.route(result, [fragment], create_pages)

    assert routed.written == []
    assert routed.action_ids == []
    assert create_calls == []
    assert router.list_actions_for_session(input_spec.source_session_id) == []
    if proof_state == "missing":
        assert any("admission proof is missing" in error for error in routed.errors)
    else:
        assert any("root hash mismatch" in error for error in routed.errors)


def test_router_rejects_forged_source_agent_against_immutable_input_spec(
    router_env, fragment
):
    wiki_dir, database_dir, router = router_env
    input_spec = _input_spec()
    payload = _payload("create_page", input_spec=input_spec)
    payload["source_agent"] = "forged-agent"
    create_calls = []

    def create_pages(fragments):
        create_calls.append(fragments)
        return _fake_create_pages(wiki_dir)(fragments)

    result = _result(
        payload,
        input_spec=input_spec,
        source="forged-agent",
        database_dir=database_dir,
    )
    routed = router.route(result, [fragment], create_pages)

    assert routed.written == []
    assert routed.action_ids == []
    assert create_calls == []
    assert router.list_actions_for_session(input_spec.source_session_id) == []
    assert any(
        "source_agent must match the immutable distillation input spec" in error
        for error in routed.errors
    )


def test_router_rejects_nonexistent_cognition_episode_revision(router_env, fragment):
    wiki_dir, database_dir, router = router_env
    input_spec = _input_spec()
    payload = _payload("create_page", input_spec=input_spec)
    result = _result(
        payload,
        input_spec=input_spec,
        database_dir=database_dir,
    )
    result.cognition_episode_revision_id = "cogrev-not-committed"

    routed = router.route(result, [result.fragments[0]], _fake_create_pages(wiki_dir))

    assert routed.written == []
    assert routed.action_ids == []
    assert any(
        "cognition episode revision is not committed canonically" in error
        for error in routed.errors
    )


def test_router_rejects_committed_episode_from_another_admitted_root(router_env):
    wiki_dir, database_dir, router = router_env
    first_spec = _input_spec()
    first_result = _result(
        _payload("create_page", input_spec=first_spec),
        input_spec=first_spec,
        database_dir=database_dir,
    )
    foreign_revision_id = first_result.cognition_episode_revision_id

    second_spec = _input_spec(
        source_session_id="sess-router-other",
        source_event_ids=("raw-other",),
        raw_1="另一个会话要求分析 PostgreSQL 锁等待。",
        raw_2="",
    )
    second_result = _result(
        _payload("create_page", input_spec=second_spec),
        input_spec=second_spec,
    )
    second_result.cognition_episode_revision_id = foreign_revision_id

    routed = router.route(
        second_result,
        [second_result.fragments[0]],
        _fake_create_pages(wiki_dir),
    )

    assert routed.written == []
    assert routed.action_ids == []
    assert any("not bound to this admitted distillation root" in error for error in routed.errors)


def test_update_page_writes_backup_appends_body_and_logs_decision(
    router_env,
    fragment,
    _canonical_material_actions,
):
    wiki_dir, database_dir, router = router_env
    target = wiki_dir / "03-Tech" / "redis.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\n名称: Redis\n---\n# Redis\n", encoding="utf-8")
    payload = _payload(
        "update_page",
        relation_type="extends",
        target_pages=["03-Tech/redis.md"],
        intent="update",
    )

    routed = router.route(
        _result(payload, database_dir=database_dir),
        [fragment],
        _fake_create_pages(wiki_dir),
    )

    assert str(target) in routed.written
    body = target.read_text(encoding="utf-8")
    assert "mnemos-distill-action" in body
    assert "raw-1" in body
    rows = router.list_actions_for_session("sess-router")
    assert rows[0]["action"] == "update_page"
    assert rows[0]["target_page"] == "03-Tech/redis.md"
    assert rows[0]["backup_path"].startswith(str(database_dir))
    card = json.loads(rows[0]["merge_decision_card"])
    assert card["safe_to_apply"] is True
    assert card["rollback_path"] == rows[0]["backup_path"]
    result_detail = json.loads(rows[0]["result_detail"])
    trust_decision_id = result_detail["trust_decision_id"]
    with sqlite3.connect(database_dir / "trust_decisions.db") as conn:
        trust_row = conn.execute(
            "SELECT action, decision FROM trust_decisions WHERE decision_id=?",
            (trust_decision_id,),
        ).fetchone()
    assert trust_row == ("update_page", "apply")


def test_update_page_enforce_submits_proposal_without_touching_target(
    router_env,
    fragment,
    monkeypatch,
    _canonical_material_actions,
):
    wiki_dir, database_dir, router = router_env
    db_path = _enable_trusted_push_enforce(monkeypatch, wiki_dir, database_dir)
    target = wiki_dir / "03-Tech" / "redis.md"
    target.parent.mkdir(parents=True)
    original = "---\n名称: Redis\n---\n# Redis\n"
    target.write_text(original, encoding="utf-8")
    payload = _payload(
        "update_page",
        relation_type="extends",
        target_pages=["03-Tech/redis.md"],
        intent="update",
    )

    routed = router.route(
        _result(payload, database_dir=database_dir),
        [fragment],
        _fake_create_pages(wiki_dir),
    )

    assert routed.written == []
    assert target.read_text(encoding="utf-8") == original
    rows = router.list_actions_for_session("sess-router")
    assert rows[0]["target_kind"] == "trusted_proposal"
    assert rows[0]["result_status"] == "proposed"
    result_detail = json.loads(rows[0]["result_detail"])
    proposal_id = result_detail["trusted_push"]["proposal_id"]
    proposal = ProposalQueue(db_path, wiki_base=wiki_dir).get(proposal_id)
    assert proposal.candidate.source == "hephaestus_distill_action"
    assert proposal.candidate.payload["distill_action"] == "update_page"

    committed = KnowledgeVaultWriter(wiki_base=wiki_dir, db_path=db_path).write_proposal(
        proposal_id,
        allow_high_risk=True,
    )

    assert committed["status"] == "committed"
    assert "mnemos-distill-action" in target.read_text(encoding="utf-8")


def test_low_confidence_merge_routes_to_shadow_without_touching_target(router_env, fragment):
    wiki_dir, database_dir, router = router_env
    target = wiki_dir / "03-Tech" / "redis.md"
    target.parent.mkdir(parents=True)
    original = "---\n名称: Redis\n---\n# Redis\n"
    target.write_text(original, encoding="utf-8")
    payload = _payload(
        "merge_into_page",
        relation_type="extends",
        target_pages=["03-Tech/redis.md"],
        confidence=0.4,
        intent="merge",
    )

    routed = router.route(
        _result(payload, database_dir=database_dir),
        [fragment],
        _fake_create_pages(wiki_dir),
    )

    assert target.read_text(encoding="utf-8") == original
    assert routed.written == []
    rows = router.list_actions_for_session("sess-router")
    assert rows[0]["target_kind"] == "shadow"
    assert rows[0]["result_status"] == "proposed"
    assert (wiki_dir / rows[0]["target_page"]).exists()
    card = json.loads(rows[0]["merge_decision_card"])
    assert card["safe_to_apply"] is False
    assert any("low_confidence" in signal for signal in card["conflicting_signals"])


def test_route_to_dispute_creates_dispute_not_inbox(
    router_env,
    fragment,
    _canonical_material_actions,
):
    wiki_dir, database_dir, router = router_env
    payload = _payload(
        "route_to_dispute",
        relation_type="contradicts",
        target_pages=["03-Tech/redis.md"],
        intent="dispute",
        conflict_strength=0.9,
    )

    routed = router.route(
        _result(payload, database_dir=database_dir),
        [fragment],
        _fake_create_pages(wiki_dir),
    )

    assert not (wiki_dir / "00-Inbox").exists()
    dispute_paths = [Path(path) for path in routed.written if "08-Disputes" in path]
    assert len(dispute_paths) == 1
    assert dispute_paths[0].exists()
    rows = router.list_actions_for_session("sess-router")
    assert rows[0]["action"] == "route_to_dispute"
    assert rows[0]["target_kind"] == "dispute"


def test_record_reinforcement_updates_frontmatter_and_metrics_without_new_page(
    router_env,
    fragment,
    monkeypatch,
    _canonical_material_actions,
):
    wiki_dir, database_dir, router = router_env
    target = wiki_dir / "03-Tech" / "redis.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\n名称: Redis\nreinforcement_count: 1\ncustom_field: keep-me\n---\n# Redis\n",
        encoding="utf-8",
    )
    upserts = []

    class FakeMetrics:
        def __init__(self, wiki_dir):
            self.wiki_dir = wiki_dir

        def get_page(self, rel_target):
            return None

        def upsert_page(self, rel_target, **kwargs):
            upserts.append((rel_target, kwargs))

    monkeypatch.setattr("core.hephaestus.distill_action_router.WikiMetrics", FakeMetrics)
    payload, input_spec = _explicit_user_payload(
        "record_reinforcement",
        relation_type="same",
        target_pages=["03-Tech/redis.md"],
        intent="reinforce",
        reason="100% 完全重复，只需要强化既有页面。",
    )

    routed = router.route(
        _result(
            payload,
            input_spec=input_spec,
            database_dir=database_dir,
        ),
        [fragment],
        _fake_create_pages(wiki_dir),
    )

    assert not (wiki_dir / "00-Inbox").exists()
    assert str(target) in routed.written
    text = target.read_text(encoding="utf-8")
    assert "reinforcement_count: 2" in text or "强化次数: 2" in text
    assert "custom_field: keep-me" in text
    assert upserts and upserts[0][0] == "03-Tech/redis.md"
    rows = router.list_actions_for_session("sess-router")
    assert rows[0]["action"] == "record_reinforcement"
    assert rows[0]["backup_path"]


def test_record_reinforcement_enforce_submits_proposal_without_touching_target(
    router_env,
    fragment,
    monkeypatch,
    _canonical_material_actions,
):
    wiki_dir, database_dir, router = router_env
    db_path = _enable_trusted_push_enforce(monkeypatch, wiki_dir, database_dir)
    target = wiki_dir / "03-Tech" / "redis.md"
    target.parent.mkdir(parents=True)
    original = "---\n名称: Redis\nreinforcement_count: 1\n---\n# Redis\n"
    target.write_text(original, encoding="utf-8")
    payload, input_spec = _explicit_user_payload(
        "record_reinforcement",
        relation_type="same",
        target_pages=["03-Tech/redis.md"],
        intent="reinforce",
        reason="100% 完全重复，只需要强化既有页面。",
    )

    routed = router.route(
        _result(
            payload,
            input_spec=input_spec,
            database_dir=database_dir,
        ),
        [fragment],
        _fake_create_pages(wiki_dir),
    )

    assert routed.written == []
    assert target.read_text(encoding="utf-8") == original
    rows = router.list_actions_for_session("sess-router")
    assert rows[0]["target_kind"] == "trusted_proposal"
    proposal_id = json.loads(rows[0]["result_detail"])["trusted_push"]["proposal_id"]
    proposal = ProposalQueue(db_path, wiki_base=wiki_dir).get(proposal_id)
    assert proposal.candidate.payload["distill_action"] == "record_reinforcement"

    KnowledgeVaultWriter(wiki_base=wiki_dir, db_path=db_path).write_proposal(
        proposal_id,
        allow_high_risk=True,
    )

    text = target.read_text(encoding="utf-8")
    assert "reinforcement_count: 2" in text or "强化次数: 2" in text


def test_claim_skip_action_is_logged_without_writing_pages(router_env, fragment):
    wiki_dir, database_dir, router = router_env
    payload = _payload("skip", relation_type="new", intent="create")

    routed = router.route(
        _result(payload, database_dir=database_dir),
        [fragment],
        _fake_create_pages(wiki_dir),
    )

    assert routed.written == []
    assert not (wiki_dir / "00-Inbox").exists()
    rows = router.list_actions_for_session("sess-router")
    assert rows[0]["action"] == "skip"
    assert rows[0]["result_status"] == "skipped"


def test_read_only_router_does_not_create_log_database(tmp_path):
    wiki_dir = tmp_path / "wiki"
    database_dir = tmp_path / "db"
    wiki_dir.mkdir()
    database_dir.mkdir()
    options = DistillActionRouterOptions(database_dir=database_dir, wiki_dir=wiki_dir)

    router = DistillActionRouter(options, ensure_db=False)

    assert router.list_recent_actions() == []
    assert router.get_action("missing") is None
    assert not (database_dir / "distill_actions.db").exists()
    assert not (database_dir / "trust_decisions.db").exists()


def test_options_from_config_keeps_custom_wiki_base_self_contained(tmp_path):
    class FakeConfig:
        wiki_dir = tmp_path / "real-wiki"
        database_dir = tmp_path / "real-db"

        def get(self, key, default=None):
            return default

    custom_wiki = tmp_path / "custom-wiki"

    options = DistillActionRouterOptions.from_config(FakeConfig(), wiki_base=custom_wiki)

    assert options.wiki_dir == custom_wiki
    assert options.database_dir == custom_wiki / ".mnemos"
    assert options.cognitive_state_database_dir == tmp_path / "real-db"


def test_options_from_config_without_wiki_dir_keeps_canonical_database(tmp_path):
    class FakeConfig:
        database_dir = tmp_path / "canonical-db"

        def get(self, key, default=None):
            return default

    custom_wiki = tmp_path / "custom-wiki"

    options = DistillActionRouterOptions.from_config(FakeConfig(), wiki_base=custom_wiki)

    assert options.wiki_dir == custom_wiki
    assert options.database_dir == tmp_path / "canonical-db"
    assert options.cognitive_state_database_dir == tmp_path / "canonical-db"
