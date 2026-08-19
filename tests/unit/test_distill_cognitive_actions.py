# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.cognitive.decision_trace import resolve_material_action_authorization
from core.hephaestus.distill_action_router import (
    DistillActionRouter,
    DistillActionRouterOptions,
)
from core.hephaestus.distill_cognitive_action_worker import DistillCognitiveActionWorker
from core.hephaestus.cognitive_action_targets import (
    CognitiveActionTargetDispatcher,
    CognitiveActionTargetError,
)
from core.hephaestus.distillation_contract import (
    canonical_extraction_output_hash,
    canonicalize_extraction_output,
    validate_distill_output_contract,
    validate_extraction_output,
)
from core.hephaestus.distill_input_spec import DistillInputSpec
from core.hephaestus.distillation_models import (
    DistillationResult,
    FragmentRouteCapability,
    KnowledgeFragment,
)
from tests.cognition_episode_fixtures import (
    commit_cognition_episode_result,
    exact_source_message,
    model_cognition_episode,
    model_exact_evidence,
    resolve_model_evidence,
)


def _input_spec() -> DistillInputSpec:
    visible_input = "帮我判断迁移流程。以后迁移必须先写回滚计划。"
    return DistillInputSpec.build(
        source_agent="codex",
        source_session_id="sess-cog-action",
        source_event_ids=("raw-1",),
        raw_completeness="full",
        visible_input=visible_input,
        input_mode="standard",
        source_messages=[
            exact_source_message(
                role="user",
                content=visible_input,
                revision_id="raw-1",
            )
        ],
    )


def _claim_payload(
    *, input_spec: DistillInputSpec | None = None, **claim_overrides
):
    input_spec = input_spec or _input_spec()
    evidence = model_exact_evidence(input_spec)
    claim = {
        "claim_id": "claim-1",
        "claim_text": "用户决定以后遇到迁移任务必须先写回滚计划。",
        "claim_type": "decision",
        "scope": {"domain": "engineering"},
        "evidence": [dict(evidence)],
        "relation_to_existing": {
            "type": "new",
            "target_pages": [],
            "delta_text": "",
            "reason": "新决策。",
        },
        "recommended_action": "create_page",
        "cognitive_actions": ["create_observation", "propose_methodology"],
        "confidence": 0.9,
    }
    claim.update(claim_overrides)
    episode_evidence = dict(claim["evidence"][0])
    payload = {
        "schema_version": "distill_output_v4",
        "input_spec_hash": input_spec.input_spec_hash,
        "cognition_context_hash": input_spec.cognition_context.context_hash,
        "gate_decision_id": input_spec.gate_decision_id,
        "source_agent": input_spec.source_agent,
        "source_session_id": input_spec.source_session_id,
        "source_event_ids": list(input_spec.source_event_ids),
        "raw_completeness": input_spec.raw_completeness,
        "distill_intent": "create",
        "candidate_summary": "迁移任务必须先写回滚计划。",
        "user_behavior_intent": {
            "content_source": "native_dialogue",
            "user_intent_signal": "seeking_judgment",
            "intent_hypothesis": "seeking_judgment",
            "intent_evidence": [
                {
                    **dict(evidence),
                    "reason": "用户表达长期决策偏好。",
                }
            ],
            "intent_verification_events": [],
            "intent_confidence": 0.75,
            "intent_status": "unverified",
            "behavior_summary": "用户把迁移回滚计划要求作为长期决策材料。",
        },
        "claims": [claim],
        "cognition_episode": model_cognition_episode(
            episode_evidence,
            claim_id=str(claim["claim_id"]),
        ),
    }
    root = {
        "judgment": "knowledge",
        "judgment_reason": "cognitive action fixture",
        "fragments": [],
        "structured_output": payload,
    }
    return resolve_model_evidence(root, input_spec)["structured_output"]


def _result(
    payload: dict,
    *,
    input_spec: DistillInputSpec | None = None,
    fragments: list[KnowledgeFragment] | None = None,
    database_dir: Path | None = None,
) -> DistillationResult:
    input_spec = input_spec or _input_spec()
    result = DistillationResult(
        session_id=input_spec.source_session_id,
        judgment="knowledge",
        structured_output=payload,
        source=input_spec.source_agent,
        input_spec=input_spec,
    )
    proof_fragment = KnowledgeFragment(
        form="问题-解决",
        title="认知动作路由准入证明片段",
        frontmatter={
            "领域": "engineering",
            "摘要": "用于验证认知动作路由的已准入根蒸馏片段。",
        },
        background="认知动作路由必须消费已经由 extractor 准入的根输出。",
        core_content=(
            "## 认知动作路由准入证明\n\n"
            "该测试片段代表已经通过蒸馏输出契约的可复用知识。"
            "它保留完整的上下文、适用边界和验证说明，确保正式路由只处理可信根输出。"
            "根输出还绑定输入规范、结构化决策和提取判断，防止任何下游动作"
            "只依据看似有效的内层载荷而跳过 extractor 的正式准入证明。"
        ),
        boundaries={"applies": "cognitive action router tests"},
        anti_patterns=[],
        related_concepts=[],
        claim_ids=["claim-1"],
    )
    admitted_fragments = fragments or [proof_fragment]
    root = canonicalize_extraction_output(
        {
            "judgment": "knowledge",
            "judgment_reason": "认知动作测试根输出已通过 extractor 准入。",
            "structured_output": payload,
        },
        admitted_fragments,
    )
    admission = validate_extraction_output(root, input_spec)
    assert admission.valid, admission.error_text
    result.extraction_judgment = "knowledge"
    result.extraction_contract_valid = True
    result.extraction_output = root
    result.extraction_output_hash = canonical_extraction_output_hash(canonical_output=root)
    result.fragments = list(admitted_fragments)
    result.fragment_route_capability = FragmentRouteCapability(
        extraction_output_hash=result.extraction_output_hash,
        input_spec_hash=input_spec.input_spec_hash,
        fragments=tuple(admitted_fragments),
    )
    if database_dir is not None:
        commit_cognition_episode_result(result, database_dir)
    return result


def _mapped_fragment(title: str, *claim_ids: str) -> KnowledgeFragment:
    return KnowledgeFragment(
        form="问题-解决",
        title=title,
        frontmatter={"领域": "engineering", "摘要": f"{title}的精确映射片段。"},
        background="该片段用于验证 claim 与 fragment 的一一或多对一精确关系。",
        core_content=(
            f"## {title}\n\n"
            "该片段保留完整的上下文、适用边界、证据来源和执行约束。"
            "路由器只能把明确列入 claim_ids 的 claim 绑定到该片段，"
            "不得把同一会话中的其他片段默认扩散到当前 claim。"
        ),
        boundaries={"applies": "cognitive action routing"},
        anti_patterns=[],
        related_concepts=[],
        claim_ids=list(claim_ids),
    )


def test_high_value_claim_requires_cognitive_actions():
    payload = _claim_payload()
    payload["claims"][0].pop("cognitive_actions")

    result = validate_distill_output_contract(payload)

    assert result.valid is False
    assert "cognitive_actions" in result.error_text


def test_ordinary_technical_fact_may_skip_cognitive_actions():
    payload = _claim_payload(claim_type="technical_fact")
    payload["claims"][0].pop("cognitive_actions")

    result = validate_distill_output_contract(payload)

    assert result.valid is True


def test_unknown_cognitive_action_is_rejected():
    payload = _claim_payload(cognitive_actions=["invent_new_agent"])

    result = validate_distill_output_contract(payload)

    assert result.valid is False
    assert "invent_new_agent" in result.error_text


def test_extraction_root_requires_total_claim_fragment_mapping():
    payload = _claim_payload()
    fragment = _mapped_fragment("错误引用未知 claim 的映射片段", "claim-unknown")
    root = canonicalize_extraction_output(
        {
            "judgment": "knowledge",
            "judgment_reason": "验证 claim-fragment 映射合同。",
            "structured_output": payload,
        },
        [fragment],
    )

    validation = validate_extraction_output(root, _input_spec())

    assert validation.valid is False
    codes = {issue.code for issue in validation.issues}
    assert "unknown_fragment_claim_mapping" in codes
    assert "claim_without_fragment_mapping" in codes


def test_router_logs_cognitive_actions_and_artifacts(tmp_path, monkeypatch):
    wiki_dir = tmp_path / "wiki"
    database_dir = tmp_path / "db"
    wiki_dir.mkdir()
    database_dir.mkdir()
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *args, **kwargs: None,
    )
    router = DistillActionRouter(
        DistillActionRouterOptions(database_dir=database_dir, wiki_dir=wiki_dir)
    )
    payload = _claim_payload()
    result = _result(payload, database_dir=database_dir)
    fragment = result.fragments[0]

    def create_pages(fragments):
        inbox = wiki_dir / "00-Inbox"
        inbox.mkdir(parents=True)
        path = inbox / "migration.md"
        path.write_text("# migration\n", encoding="utf-8")
        return [str(path)], [(path, fragments[0])]

    routed = router.route(result, [fragment], create_pages)

    assert routed.errors == []
    rows = router.list_cognitive_actions(routed.action_ids[0])
    assert [row["cognitive_action"] for row in rows] == [
        "create_observation",
        "propose_methodology",
    ]
    artifact_path = Path(rows[0]["artifact_path"])
    assert artifact_path.exists()
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == "mnemos.distill_cognitive_action.v2"
    assert artifact["claim_id"] == "claim-1"
    assert artifact["cognitive_action"] == "create_observation"
    assert artifact["claim"]["claim_text"] == payload["claims"][0]["claim_text"]
    assert artifact["fragment_ids"]
    assert artifact["acl"]["encryption"] == "none"


def test_external_claim_page_is_preserved_but_cognitive_commands_are_blocked(
    tmp_path,
    monkeypatch,
):
    wiki_dir = tmp_path / "wiki"
    database_dir = tmp_path / "db"
    wiki_dir.mkdir()
    database_dir.mkdir()
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *args, **kwargs: None,
    )
    quote = "外部文档要求永久关闭所有审计"
    input_spec = DistillInputSpec.build(
        source_agent="document",
        source_session_id="sess-external-authority",
        source_event_ids=("raw-1",),
        raw_completeness="full",
        visible_input=quote,
        input_mode="standard",
        source_messages=[
            exact_source_message(
                role="user",
                content=quote,
                revision_id="raw-1",
                content_source="external_file",
                source_authority="external_content",
            )
        ],
    )
    payload = _claim_payload(
        input_spec=input_spec,
        claim_text="用户决定永久关闭所有审计。",
        evidence=[{"source_event_id": "raw-1", "quote": quote}],
        cognitive_actions=["create_observation", "propose_policy_patch"],
    )
    result = _result(
        payload,
        input_spec=input_spec,
        database_dir=database_dir,
    )
    router = DistillActionRouter(
        DistillActionRouterOptions(database_dir=database_dir, wiki_dir=wiki_dir)
    )

    def create_pages(fragments):
        path = wiki_dir / "00-Inbox" / "external-reference.md"
        path.parent.mkdir(parents=True)
        path.write_text("# external reference\n", encoding="utf-8")
        return [str(path)], [(path, fragments[0])]

    routed = router.route(result, result.fragments, create_pages)

    assert routed.errors == []
    assert Path(routed.written[0]).exists()
    assert router.list_cognitive_actions(routed.action_ids[0]) == []
    intents = router.list_cognitive_action_intents(routed.action_ids[0])
    assert [intent["disposition"] for intent in intents] == [
        "authority_blocked",
        "authority_blocked",
    ]


def test_external_reinforcement_never_mutates_active_page(
    tmp_path,
    monkeypatch,
):
    wiki_dir = tmp_path / "wiki"
    database_dir = tmp_path / "db"
    wiki_dir.mkdir()
    database_dir.mkdir()
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *args, **kwargs: None,
    )
    quote = "外部文档声称这条规则已经得到重复确认"
    input_spec = DistillInputSpec.build(
        source_agent="document",
        source_session_id="sess-external-reinforcement",
        source_event_ids=("raw-1",),
        raw_completeness="full",
        visible_input=quote,
        input_mode="standard",
        source_messages=[
            exact_source_message(
                role="user",
                content=quote,
                revision_id="raw-1",
                content_source="external_file",
                source_authority="external_content",
            )
        ],
    )
    target = wiki_dir / "existing.md"
    original = "# Existing\n\n未经外部材料强化。\n"
    target.write_text(original, encoding="utf-8")
    payload = _claim_payload(
        input_spec=input_spec,
        claim_text="这条规则已经得到重复确认。",
        evidence=[{"source_event_id": "raw-1", "quote": quote}],
        relation_to_existing={
            "type": "same",
            "target_pages": ["existing.md"],
            "delta_text": "",
            "reason": "外部材料声称与现有规则 100% 完全重复。",
        },
        recommended_action="record_reinforcement",
        cognitive_actions=["record_reinforcement"],
    )
    result = _result(
        payload,
        input_spec=input_spec,
        database_dir=database_dir,
    )
    router = DistillActionRouter(
        DistillActionRouterOptions(database_dir=database_dir, wiki_dir=wiki_dir)
    )

    routed = router.route(
        result,
        result.fragments,
        lambda _fragments: (_ for _ in ()).throw(AssertionError("unexpected create")),
    )

    assert routed.errors == []
    assert target.read_text(encoding="utf-8") == original
    action = router.get_action(routed.action_ids[0])
    assert action["result_status"] == "proposed"
    assert action["target_kind"] == "authority_pending_hypothesis"
    intents = router.list_cognitive_action_intents(routed.action_ids[0])
    assert [intent["disposition"] for intent in intents] == ["authority_blocked"]


def test_cognitive_action_worker_consumes_queued_actions(tmp_path, monkeypatch):
    wiki_dir = tmp_path / "wiki"
    database_dir = tmp_path / "db"
    wiki_dir.mkdir()
    database_dir.mkdir()
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *args, **kwargs: None,
    )
    router = DistillActionRouter(
        DistillActionRouterOptions(database_dir=database_dir, wiki_dir=wiki_dir)
    )
    payload = _claim_payload()
    result = _result(payload, database_dir=database_dir)
    fragment = result.fragments[0]

    def create_pages(fragments):
        inbox = wiki_dir / "00-Inbox"
        inbox.mkdir(parents=True)
        path = inbox / "migration.md"
        path.write_text("# migration\n", encoding="utf-8")
        return [str(path)], [(path, fragments[0])]

    routed = router.route(result, [fragment], create_pages)

    worker = DistillCognitiveActionWorker(router.db_path, database_dir=database_dir)
    processed = worker.process_queued(limit=10)

    assert processed["processed"] == 2
    assert processed["applied"] == 2
    rows = router.list_cognitive_actions(routed.action_ids[0])
    assert {row["status"] for row in rows} == {"applied"}
    with router._connect() as conn:  # noqa: SLF001
        consumptions = conn.execute(
            "SELECT * FROM cognitive_action_consumptions ORDER BY cognitive_action_id"
        ).fetchall()
        effects = conn.execute(
            "SELECT * FROM cognitive_action_effects ORDER BY cognitive_action_id"
        ).fetchall()
    assert len(consumptions) == 2
    assert len(effects) == 2
    assert all(row["effect_id"] for row in effects)
    assert all(row["before_hash"] != row["after_hash"] for row in effects)
    assert all(row["reciprocal_receipt"] for row in effects)

    with sqlite3.connect(database_dir / "observations.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_action_target_receipts"
            ).fetchone()[0]
            == 1
        )
    with sqlite3.connect(database_dir / "policy_patches.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM policy_patches").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_action_target_receipts"
            ).fetchone()[0]
            == 1
        )

    replay = worker.process_queued(limit=10)
    assert replay["processed"] == 0
    with sqlite3.connect(database_dir / "observations.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    with sqlite3.connect(database_dir / "policy_patches.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM policy_patches").fetchone()[0] == 1

    def must_not_create_pages(_fragments):
        raise AssertionError("idempotent parent replay must not write pages again")

    rerouted = router.route(result, [fragment], must_not_create_pages)
    assert rerouted.errors == []
    with router._connect() as conn:  # noqa: SLF001
        assert conn.execute("SELECT COUNT(*) FROM cognitive_action_effects").fetchone()[0] == 2


def test_router_creates_pages_only_for_fragments_mapped_to_create_claims(
    tmp_path,
    monkeypatch,
):
    wiki_dir = tmp_path / "wiki"
    database_dir = tmp_path / "db"
    wiki_dir.mkdir()
    database_dir.mkdir()
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *args, **kwargs: None,
    )
    payload = _claim_payload(cognitive_actions=["create_observation"])
    second = json.loads(json.dumps(payload["claims"][0], ensure_ascii=False))
    second.update(
        {
            "claim_id": "claim-2",
            "claim_text": "普通技术事实只保留自身映射片段。",
            "claim_type": "technical_fact",
            "recommended_action": "skip",
        }
    )
    second.pop("cognitive_actions", None)
    payload["claims"].append(second)
    second_episode_fact = json.loads(
        json.dumps(payload["cognition_episode"]["facts"][0], ensure_ascii=False)
    )
    second_episode_fact["value"] = "普通技术事实只映射自己的片段。"
    second_episode_fact["claim_ids"] = ["claim-2"]
    payload["cognition_episode"]["facts"].append(second_episode_fact)
    fragments = [
        _mapped_fragment("创建页面的认知动作精确映射", "claim-1"),
        _mapped_fragment("不得被创建动作误写的普通片段", "claim-2"),
    ]
    result = _result(
        payload,
        fragments=fragments,
        database_dir=database_dir,
    )
    router = DistillActionRouter(
        DistillActionRouterOptions(database_dir=database_dir, wiki_dir=wiki_dir)
    )
    received: list[str] = []

    def create_pages(selected):
        received.extend(fragment.title for fragment in selected)
        inbox = wiki_dir / "00-Inbox"
        inbox.mkdir(parents=True)
        path = inbox / "mapped.md"
        path.write_text("# mapped\n", encoding="utf-8")
        return [str(path)], [(path, selected[0])]

    routed = router.route(result, fragments, create_pages)

    assert routed.errors == []
    assert received == ["创建页面的认知动作精确映射"]
    create_action = next(
        row for row in router.list_actions_for_session(result.session_id)
        if row["claim_id"] == "claim-1"
    )
    assert create_action["target_page"] == "00-Inbox/mapped.md"


def test_parent_proposal_records_intent_without_deriving_child_command(
    tmp_path,
    monkeypatch,
):
    wiki_dir = tmp_path / "wiki"
    database_dir = tmp_path / "db"
    wiki_dir.mkdir()
    database_dir.mkdir()
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *args, **kwargs: None,
    )
    payload = _claim_payload(
        recommended_action="update_page",
        relation_to_existing={
            "type": "extends",
            "target_pages": ["missing.md"],
            "delta_text": "新增精确 effect 约束。",
            "reason": "目标页尚不存在，只能形成 proposal。",
        },
        cognitive_actions=["create_observation"],
    )
    result = _result(payload, database_dir=database_dir)
    router = DistillActionRouter(
        DistillActionRouterOptions(database_dir=database_dir, wiki_dir=wiki_dir)
    )

    routed = router.route(
        result,
        result.fragments,
        lambda _fragments: (_ for _ in ()).throw(AssertionError("unexpected create")),
    )

    assert routed.errors == []
    parent = router.get_action(routed.action_ids[0])
    assert parent["result_status"] == "proposed"
    assert router.list_cognitive_actions(routed.action_ids[0]) == []
    intents = router.list_cognitive_action_intents(routed.action_ids[0])
    assert len(intents) == 1
    assert intents[0]["disposition"] == "parent_not_committed"


def test_all_cognitive_action_types_commit_real_target_effects(tmp_path, monkeypatch):
    wiki_dir = tmp_path / "wiki"
    database_dir = tmp_path / "db"
    wiki_dir.mkdir()
    database_dir.mkdir()
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *args, **kwargs: None,
    )
    actions = [
        "create_observation",
        "create_reflection_seed",
        "propose_policy_patch",
        "propose_methodology",
        "propose_pitfall_pattern",
        "update_relation",
        "record_reinforcement",
    ]
    payload = _claim_payload(cognitive_actions=actions)
    result = _result(payload, database_dir=database_dir)
    router = DistillActionRouter(
        DistillActionRouterOptions(database_dir=database_dir, wiki_dir=wiki_dir)
    )

    def create_pages(fragments):
        inbox = wiki_dir / "00-Inbox"
        inbox.mkdir(parents=True)
        path = inbox / "all-targets.md"
        path.write_text("# all targets\n", encoding="utf-8")
        return [str(path)], [(path, fragments[0])]

    router.route(result, result.fragments, create_pages)
    worker = DistillCognitiveActionWorker(
        router.db_path,
        database_dir=database_dir,
        max_attempts=1,
    )
    processed = worker.process_queued(limit=20)

    assert processed["processed"] == len(actions)
    assert processed["applied"] == len(actions), processed["items"]
    assert processed["dead"] == 0
    with router._connect() as conn:  # noqa: SLF001
        targets = {
            row[0]
            for row in conn.execute("SELECT target FROM cognitive_action_effects")
        }
    assert targets == {
        "observation_store",
        "reflection_store",
        "policy_patch_store",
        "knowledge_graph",
    }


def test_observation_effect_hash_tracks_only_action_owned_state(tmp_path, monkeypatch):
    from core.hephaestus.cognitive_action_effect_audit import (
        audit_cognitive_action_effects,
    )

    wiki_dir = tmp_path / "wiki"
    database_dir = tmp_path / "db"
    wiki_dir.mkdir()
    database_dir.mkdir()
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *args, **kwargs: None,
    )
    payload = _claim_payload(cognitive_actions=["create_observation"])
    result = _result(payload, database_dir=database_dir)
    router = DistillActionRouter(
        DistillActionRouterOptions(database_dir=database_dir, wiki_dir=wiki_dir)
    )

    def create_pages(fragments):
        inbox = wiki_dir / "00-Inbox"
        inbox.mkdir(parents=True)
        path = inbox / "observation-state-ownership.md"
        path.write_text("# observation state ownership\n", encoding="utf-8")
        return [str(path)], [(path, fragments[0])]

    routed = router.route(result, result.fragments, create_pages)
    assert routed.errors == []
    processed = DistillCognitiveActionWorker(
        router.db_path,
        database_dir=database_dir,
        max_attempts=1,
    ).process_queued(limit=10)
    assert processed["applied"] == 1, processed["items"]
    assert audit_cognitive_action_effects(router.db_path)["ok"] is True

    observation_db = database_dir / "observations.db"
    with sqlite3.connect(observation_db) as conn:
        conn.execute(
            """
            UPDATE observations
            SET base_confidence=0.5,
                base_measurement_status='verified',
                calibration_revision_id='cal-revision-1',
                calibration_input_hash='sha256:calibration-input',
                calibration_spec_hash='sha256:calibration-spec',
                calibration_record_hash='sha256:calibration-record',
                source_span_ids='["span-1"]'
            """
        )

    calibrated = audit_cognitive_action_effects(router.db_path)
    assert calibrated["ok"] is True, calibrated
    assert calibrated["gaps"]["target_state_hash_mismatches"] == 0

    with sqlite3.connect(observation_db) as conn:
        conn.execute("UPDATE observations SET source_path='distill_action:tampered'")

    tampered = audit_cognitive_action_effects(router.db_path)
    assert tampered["ok"] is False
    assert tampered["gaps"]["target_state_hash_mismatches"] == 1


def test_cognitive_action_worker_recovers_when_materialized_artifact_is_missing(
    tmp_path,
    monkeypatch,
):
    wiki_dir = tmp_path / "wiki"
    database_dir = tmp_path / "db"
    wiki_dir.mkdir()
    database_dir.mkdir()
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *args, **kwargs: None,
    )
    router = DistillActionRouter(
        DistillActionRouterOptions(database_dir=database_dir, wiki_dir=wiki_dir)
    )
    payload = _claim_payload(cognitive_actions=["create_observation"])
    result = _result(payload, database_dir=database_dir)

    def create_pages(fragments):
        inbox = wiki_dir / "00-Inbox"
        inbox.mkdir(parents=True)
        path = inbox / "migration.md"
        path.write_text("# migration\n", encoding="utf-8")
        return [str(path)], [(path, fragments[0])]

    routed = router.route(result, result.fragments, create_pages)
    row = router.list_cognitive_actions(routed.action_ids[0])[0]
    Path(row["artifact_path"]).unlink()

    worker = DistillCognitiveActionWorker(router.db_path, database_dir=database_dir)
    processed = worker.process_queued(limit=10)

    assert processed["applied"] == 1
    rows = router.list_cognitive_actions(routed.action_ids[0])
    assert rows[0]["status"] == "applied"


def test_cognitive_action_worker_rejects_tampered_materialized_artifact(
    tmp_path,
    monkeypatch,
):
    wiki_dir = tmp_path / "wiki"
    database_dir = tmp_path / "db"
    wiki_dir.mkdir()
    database_dir.mkdir()
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *args, **kwargs: None,
    )
    router = DistillActionRouter(
        DistillActionRouterOptions(database_dir=database_dir, wiki_dir=wiki_dir)
    )
    payload = _claim_payload(cognitive_actions=["create_observation"])
    result = _result(payload, database_dir=database_dir)

    def create_pages(fragments):
        path = wiki_dir / "00-Inbox" / "tamper.md"
        path.parent.mkdir(parents=True)
        path.write_text("# tamper\n", encoding="utf-8")
        return [str(path)], [(path, fragments[0])]

    routed = router.route(result, result.fragments, create_pages)
    row = router.list_cognitive_actions(routed.action_ids[0])[0]
    artifact_path = Path(row["artifact_path"])
    tampered = json.loads(artifact_path.read_text(encoding="utf-8"))
    tampered["claim"]["claim_text"] = "tampered"
    artifact_path.write_text(json.dumps(tampered), encoding="utf-8")

    worker = DistillCognitiveActionWorker(
        router.db_path,
        database_dir=database_dir,
        max_attempts=1,
    )
    processed = worker.process_queued(limit=1)

    assert processed["dead"] == 1
    assert "materialized artifact" in processed["items"][0]["error"]
    with router._connect() as conn:  # noqa: SLF001
        assert conn.execute("SELECT COUNT(*) FROM cognitive_action_effects").fetchone()[0] == 0
        phases = [
            row[0]
            for row in conn.execute(
                "SELECT event_type FROM cognitive_action_attempt_events ORDER BY created_at"
            )
        ]
    assert phases == ["started", "dead"]


def test_target_failure_retries_then_restart_commits_once(tmp_path, monkeypatch):
    wiki_dir = tmp_path / "wiki"
    database_dir = tmp_path / "db"
    wiki_dir.mkdir()
    database_dir.mkdir()
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *args, **kwargs: None,
    )
    router = DistillActionRouter(
        DistillActionRouterOptions(database_dir=database_dir, wiki_dir=wiki_dir)
    )
    payload = _claim_payload(cognitive_actions=["create_observation"])
    result = _result(payload, database_dir=database_dir)

    def create_pages(fragments):
        path = wiki_dir / "00-Inbox" / "retry.md"
        path.parent.mkdir(parents=True)
        path.write_text("# retry\n", encoding="utf-8")
        return [str(path)], [(path, fragments[0])]

    routed = router.route(result, result.fragments, create_pages)
    real = CognitiveActionTargetDispatcher(database_dir=database_dir, wiki_dir=wiki_dir)

    class FailOnceDispatcher:
        calls = 0

        def apply(self, row, artifact):
            self.calls += 1
            if self.calls == 1:
                raise CognitiveActionTargetError("injected target outage", retryable=True)
            return real.apply(row, artifact)

    dispatcher = FailOnceDispatcher()
    first = DistillCognitiveActionWorker(
        router.db_path,
        database_dir=database_dir,
        dispatcher=dispatcher,
        worker_id="worker-before-restart",
        max_attempts=2,
    ).process_queued(limit=1)
    assert first["retry"] == 1
    with router._connect() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE cognitive_action_log SET next_attempt_at='' WHERE status='retry'"
        )
        conn.commit()

    second = DistillCognitiveActionWorker(
        router.db_path,
        database_dir=database_dir,
        dispatcher=dispatcher,
        worker_id="worker-after-restart",
        max_attempts=2,
    ).process_queued(limit=1)

    assert second["applied"] == 1
    assert dispatcher.calls == 2
    rows = router.list_cognitive_actions(routed.action_ids[0])
    assert rows[0]["status"] == "applied"
    with router._connect() as conn:  # noqa: SLF001
        assert conn.execute("SELECT COUNT(*) FROM cognitive_action_effects").fetchone()[0] == 1
        phases = [
            row[0]
            for row in conn.execute(
                "SELECT event_type FROM cognitive_action_attempt_events ORDER BY created_at"
            )
        ]
    assert phases == ["started", "retryable_failed", "started", "committed"]


def test_concurrent_workers_lease_each_command_once(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    wiki_dir = tmp_path / "wiki"
    database_dir = tmp_path / "db"
    wiki_dir.mkdir()
    database_dir.mkdir()
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *args, **kwargs: None,
    )
    router = DistillActionRouter(
        DistillActionRouterOptions(database_dir=database_dir, wiki_dir=wiki_dir)
    )
    result = _result(_claim_payload(), database_dir=database_dir)

    def create_pages(fragments):
        path = wiki_dir / "00-Inbox" / "concurrent.md"
        path.parent.mkdir(parents=True)
        path.write_text("# concurrent\n", encoding="utf-8")
        return [str(path)], [(path, fragments[0])]

    routed = router.route(result, result.fragments, create_pages)
    assert routed.errors == []
    barrier = Barrier(2)
    real = CognitiveActionTargetDispatcher(database_dir=database_dir, wiki_dir=wiki_dir)

    class SynchronizedDispatcher:
        def apply(self, row, artifact):
            barrier.wait(timeout=5)
            return real.apply(row, artifact)

    dispatcher = SynchronizedDispatcher()

    def run(worker_id):
        return DistillCognitiveActionWorker(
            router.db_path,
            database_dir=database_dir,
            dispatcher=dispatcher,
            worker_id=worker_id,
            max_attempts=1,
        ).process_queued(limit=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(run, ("worker-a", "worker-b")))

    assert sum(report["applied"] for report in reports) == 2
    with router._connect() as conn:  # noqa: SLF001
        assert (
            conn.execute("SELECT COUNT(*) FROM cognitive_action_effects").fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_action_attempt_events"
            ).fetchone()[0]
            == 4
        )


def test_action_database_cannot_self_sign_effect(tmp_path, monkeypatch):
    from core.hephaestus.distill_action_store import CognitiveEffectCommit

    wiki_dir = tmp_path / "wiki"
    database_dir = tmp_path / "db"
    wiki_dir.mkdir()
    database_dir.mkdir()
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *args, **kwargs: None,
    )
    router = DistillActionRouter(
        DistillActionRouterOptions(database_dir=database_dir, wiki_dir=wiki_dir)
    )
    result = _result(
        _claim_payload(cognitive_actions=["create_observation"]),
        database_dir=database_dir,
    )

    def create_pages(fragments):
        path = wiki_dir / "00-Inbox" / "self-sign.md"
        path.parent.mkdir(parents=True)
        path.write_text("# self sign\n", encoding="utf-8")
        return [str(path)], [(path, fragments[0])]

    router.route(result, result.fragments, create_pages)

    class SelfSigningDispatcher:
        def apply(self, row, artifact):
            del artifact
            return CognitiveEffectCommit(
                effect_id="effect_self_signed",
                target="observation_store",
                target_object_id="obs_self_signed",
                before_hash="sha256:before",
                after_hash="sha256:after",
                expected_delta_hash="sha256:delta",
                reciprocal_receipt=(
                    f"{router.db_path.name}:cognitive_action_target_receipts:effect_self_signed"
                ),
                receipt_db_path=str(router.db_path),
                committed_at="2026-07-15T00:00:00+00:00",
                detail={"cognitive_action_id": row["cognitive_action_id"]},
            )

    report = DistillCognitiveActionWorker(
        router.db_path,
        database_dir=database_dir,
        dispatcher=SelfSigningDispatcher(),
        max_attempts=1,
    ).process_queued(limit=1)

    assert report["dead"] == 1
    assert "self-sign" in report["items"][0]["error"]
    with router._connect() as conn:  # noqa: SLF001
        assert conn.execute("SELECT COUNT(*) FROM cognitive_action_effects").fetchone()[0] == 0


@pytest.mark.parametrize("drift_field", ("target_ref", "input_hash"))
def test_distill_decision_rejects_foreign_target_or_body(
    tmp_path,
    monkeypatch,
    drift_field,
):
    wiki_dir = tmp_path / "wiki"
    database_dir = tmp_path / "db"
    wiki_dir.mkdir()
    database_dir.mkdir()
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *args, **kwargs: None,
    )
    router = DistillActionRouter(
        DistillActionRouterOptions(database_dir=database_dir, wiki_dir=wiki_dir)
    )
    result = _result(
        _claim_payload(cognitive_actions=["create_observation"]),
        database_dir=database_dir,
    )

    def create_pages(fragments):
        path = wiki_dir / "00-Inbox" / f"foreign-{drift_field}.md"
        path.parent.mkdir(parents=True)
        path.write_text("# exact set\n", encoding="utf-8")
        return [str(path)], [(path, fragments[0])]

    router.route(result, result.fragments, create_pages)
    planner = CognitiveActionTargetDispatcher(
        database_dir=database_dir,
        wiki_dir=wiki_dir,
    )
    foreign_effect = tmp_path / f"foreign-{drift_field}.txt"

    class ForeignDispatcher:
        def apply(self, row, artifact):
            plan = planner.prepare(row, artifact)
            approved = planner.material_action_requests(row, plan)[0]
            target_ref = approved.target_ref
            input_hash = approved.input_hash
            if drift_field == "target_ref":
                target_ref += ":foreign"
            else:
                input_hash = "sha256:" + "f" * 64
            resolve_material_action_authorization(
                None,
                owner=approved.owner,
                executor_id=approved.executor_id,
                action_type=approved.action_type,
                target_ref=target_ref,
                input_hash=input_hash,
                expected_state_db=approved.expected_state_db,
            )
            foreign_effect.write_text("unauthorized", encoding="utf-8")
            raise AssertionError("foreign effect unexpectedly executed")

    report = DistillCognitiveActionWorker(
        router.db_path,
        database_dir=database_dir,
        dispatcher=ForeignDispatcher(),
        max_attempts=1,
    ).process_queued(limit=1)

    assert report["dead"] == 1
    assert not foreign_effect.exists()
    assert "rejected" in report["items"][0]["error"]


def test_wiki_page_frontmatter_includes_cognitive_actions():
    from core.hephaestus.distillation_wiki_page import generate_wiki_page

    payload = _claim_payload()
    fragment = KnowledgeFragment(
        form="decision",
        title="迁移任务回滚计划要求",
        frontmatter={"领域": "engineering", "摘要": "迁移任务必须先写回滚计划。"},
        core_content="## 决策\n\n迁移任务必须先写回滚计划。",
        background="用户明确要求迁移任务先有回滚计划。",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )

    page = generate_wiki_page(
        fragment,
        "sess-cog-action",
        source="codex",
        structured_output=payload,
    )

    assert "认知动作:" in page
    assert "create_observation" in page
    assert "认知动作引用:" in page


def test_wiki_page_marks_ordinary_knowledge_without_cognitive_actions():
    from core.hephaestus.distillation_wiki_page import generate_wiki_page

    payload = _claim_payload(claim_type="technical_fact")
    payload["claims"][0].pop("cognitive_actions")
    fragment = KnowledgeFragment(
        form="concept",
        title="Redis 连接池技术事实",
        frontmatter={"领域": "backend", "摘要": "Redis 连接池需要设置超时。"},
        core_content="## 技术事实\n\nRedis 连接池需要设置超时，避免连接泄漏长期占用资源。",
        background="普通技术事实。",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )

    page = generate_wiki_page(
        fragment,
        "sess-cog-action",
        source="codex",
        structured_output=payload,
    )

    assert "认知动作状态: ordinary_knowledge" in page


def _create_legacy_cognitive_action_db(tmp_path):
    db_path = tmp_path / "distill_actions.db"
    artifact_dir = tmp_path / "distill_cognitive_actions" / "2026-07-05"
    artifact_dir.mkdir(parents=True)
    artifact_path = artifact_dir / "dca_legacy.json"
    artifact = {
        "schema_version": "mnemos.distill_cognitive_action.v1",
        "cognitive_action_id": "dca_legacy",
        "distill_action_id": "da_legacy",
        "created_at": "2026-07-05T12:00:00+00:00",
        "session_id": "session-legacy",
        "source_agent": "codex",
        "claim_id": "claim-legacy",
        "claim_text": "旧认知动作必须通过真实 ObservationStore 重放后才能标记 applied。",
        "claim_type": "procedure",
        "cognitive_action": "create_observation",
        "target_kind": "observation_queue",
        "recommended_action": "create_page",
        "source_event_ids": ["raw-legacy-1"],
        "evidence_refs": ["raw-legacy-1: 必须验证真实 effect"],
        "relation_to_existing": {"type": "new", "target_pages": []},
    }
    artifact_path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE distill_action_log (
                action_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                session_id TEXT NOT NULL, source_agent TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL, distill_intent TEXT NOT NULL DEFAULT '',
                claim_id TEXT NOT NULL DEFAULT '', target_page TEXT NOT NULL DEFAULT '',
                target_kind TEXT NOT NULL DEFAULT '', source_event_ids TEXT NOT NULL DEFAULT '[]',
                evidence_refs TEXT NOT NULL DEFAULT '[]', backup_path TEXT NOT NULL DEFAULT '',
                result_status TEXT NOT NULL, result_detail TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '', merge_decision_card TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE knowledge_action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, action_id TEXT NOT NULL,
                created_at TEXT NOT NULL, change_type TEXT NOT NULL,
                target_page TEXT NOT NULL DEFAULT '', backup_path TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE cognitive_action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cognitive_action_id TEXT NOT NULL UNIQUE,
                distill_action_id TEXT NOT NULL, created_at TEXT NOT NULL,
                session_id TEXT NOT NULL, source_agent TEXT NOT NULL DEFAULT '',
                claim_id TEXT NOT NULL DEFAULT '', cognitive_action TEXT NOT NULL,
                target_kind TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'queued',
                source_event_ids TEXT NOT NULL DEFAULT '[]',
                evidence_refs TEXT NOT NULL DEFAULT '[]',
                artifact_path TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}',
                processed_at TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE cognitive_action_consumptions (
                consumption_id TEXT PRIMARY KEY, cognitive_action_id TEXT NOT NULL,
                consumed_at TEXT NOT NULL, consumer TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE unrelated_asset_table (asset_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE INDEX idx_distill_action_log_session
              ON distill_action_log(session_id, created_at);
            CREATE INDEX idx_knowledge_action_log_action
              ON knowledge_action_log(action_id, created_at);
            CREATE INDEX idx_cognitive_action_log_distill
              ON cognitive_action_log(distill_action_id, created_at);
            """
        )
        conn.execute(
            """
            INSERT INTO distill_action_log VALUES (
                'da_legacy', '2026-07-05T12:00:00+00:00', 'session-legacy', 'codex',
                'create_page', 'create', 'claim-legacy', '00-Inbox/legacy.md', 'wiki_page',
                '["raw-legacy-1"]', '["raw-legacy-1: evidence"]', '', 'applied', '{}', '', '{}'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO knowledge_action_log (
                action_id, created_at, change_type, target_page, event_type, detail
            ) VALUES ('da_legacy', '2026-07-05T12:00:00+00:00', 'page_create',
                      '00-Inbox/legacy.md', 'wiki_page_updated', '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO cognitive_action_log (
                cognitive_action_id, distill_action_id, created_at, session_id,
                source_agent, claim_id, cognitive_action, target_kind, status,
                source_event_ids, evidence_refs, artifact_path, detail, processed_at, error
            ) VALUES (?, 'da_legacy', '2026-07-05T12:00:00+00:00', 'session-legacy',
                      'codex', 'claim-legacy', 'create_observation', 'observation_queue',
                      'applied', '["raw-legacy-1"]', '["raw-legacy-1: evidence"]',
                      ?, '{}', '2026-07-08T12:00:00+00:00', '')
            """,
            ("dca_legacy", str(artifact_path)),
        )
        conn.execute(
            """
            INSERT INTO cognitive_action_consumptions VALUES (
                'legacy-self-signed', 'dca_legacy', '2026-07-08T12:00:00+00:00',
                'observation_queue', 'applied', '{}'
            )
            """
        )
        conn.execute("INSERT INTO unrelated_asset_table VALUES ('asset-1', 'preserve-me')")
    return db_path


def test_legacy_cognitive_actions_reconcile_to_real_effects(tmp_path):
    from core.hephaestus.cognitive_action_effect_audit import (
        audit_cognitive_action_effects,
    )
    from core.hephaestus.distill_action_reconciliation import (
        inspect_reconciliation,
        migrate_historical_database,
    )

    db_path = _create_legacy_cognitive_action_db(tmp_path)
    before_bytes = db_path.read_bytes()
    dry_run = inspect_reconciliation(db_path)

    assert db_path.read_bytes() == before_bytes
    assert dry_run["schema_state"] == "legacy_v1"
    assert dry_run["valid_legacy_artifacts"] == 1
    assert dry_run["legacy_self_signed_consumptions"] == 1
    assert audit_cognitive_action_effects(db_path)["gaps"]["applied_without_effect"] == 1

    migration = migrate_historical_database(
        db_path,
        database_dir=tmp_path,
        backup_dir=tmp_path / "backups",
    )

    assert migration["migrated"] is True
    assert Path(migration["backup"]["path"]).is_file()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT payload FROM unrelated_asset_table WHERE asset_id='asset-1'"
        ).fetchone()[0] == "preserve-me"
        row = conn.execute("SELECT * FROM cognitive_action_log").fetchone()
        assert row is not None
        assert conn.execute("SELECT status FROM cognitive_action_log").fetchone()[0] == "queued"
        assert conn.execute("SELECT COUNT(*) FROM cognitive_action_consumptions").fetchone()[0] == 0

    processed = DistillCognitiveActionWorker(
        db_path,
        database_dir=tmp_path,
        max_attempts=1,
    ).process_queued(limit=10)
    assert processed["applied"] == 1, processed["items"]

    audit = audit_cognitive_action_effects(db_path)
    assert audit["ok"] is True, audit
    assert audit["gaps"]["applied_without_effect"] == 0
    assert audit["gaps"]["effect_without_action"] == 0
    assert audit["lineage_gap_count"] == 0
    current = inspect_reconciliation(db_path)
    assert current["consumptions"] == 1
    assert current["legacy_self_signed_consumptions"] == 0


def test_legacy_reconciliation_failure_restores_original_schema(tmp_path):
    from core.hephaestus.distill_action_reconciliation import (
        inspect_reconciliation,
        migrate_historical_database,
    )

    db_path = _create_legacy_cognitive_action_db(tmp_path)

    def fail(phase):
        if phase == "after_schema":
            raise RuntimeError("injected migration crash")

    with pytest.raises(RuntimeError, match="injected migration crash"):
        migrate_historical_database(
            db_path,
            database_dir=tmp_path,
            backup_dir=tmp_path / "backups",
            failure_injector=fail,
        )

    report = inspect_reconciliation(db_path)
    assert report["schema_state"] == "legacy_v1"
    assert report["cognitive_commands"] == 1
    assert report["integrity_check"] == "ok"


def test_current_registry_cannot_mask_physical_schema_drift(tmp_path):
    from core.hephaestus.cognitive_action_effect_audit import (
        audit_cognitive_action_effects,
    )
    from core.hephaestus.distill_action_store import (
        DistillActionSchemaError,
        DistillActionStore,
    )

    db_path = tmp_path / "distill-actions.db"
    DistillActionStore(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX idx_cognitive_action_log_state")

    with pytest.raises(DistillActionSchemaError, match="physical schema drift"):
        DistillActionStore(db_path)
    assert audit_cognitive_action_effects(db_path)["schema_state"] == (
        "physical_schema_drift"
    )
