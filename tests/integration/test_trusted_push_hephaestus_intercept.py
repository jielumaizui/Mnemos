from pathlib import Path
import json
import sqlite3
from types import SimpleNamespace
from datetime import datetime, timezone

from core.hephaestus.distill_action_router import DistillActionRouter, DistillActionRouterOptions
from core.hephaestus.distillation_contract import (
    canonical_extraction_output_hash,
    canonicalize_extraction_output,
    validate_extraction_output,
)
from core.hephaestus.distill_input_spec import DistillInputSpec
from core.hephaestus.distillation_engine import DistillationEngine
from core.hephaestus.distillation_models import DistillationResult, KnowledgeFragment
from core.trust.proposal_queue import ProposalQueue
from core.hephaestus.trusted_push_bridge import submit_wiki_write_candidate
from core.trust.vault_mutation_service import record_trusted_markdown_no_effect_terminal
from tests.cognition_episode_fixtures import (
    commit_cognition_episode_result,
    exact_source_message,
    model_cognition_episode,
    model_exact_evidence,
    resolve_model_evidence,
)


def test_enforce_mode_intercepts_hephaestus_markdown_write(monkeypatch, tmp_path: Path):
    wiki = tmp_path / "wiki"
    db = tmp_path / "trusted.db"
    fake_config = SimpleNamespace(
        wiki_dir=wiki,
        database_dir=tmp_path,
        get=lambda key, default=None: {
            "trusted_push.mode": "enforce",
            "trusted_push.db_path": str(db),
            "distill.structured_output_contract.enforce": False,
        }.get(key, default),
    )
    monkeypatch.setattr("core.trust.config.get_config", lambda: fake_config)
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: fake_config)

    engine = DistillationEngine(wiki_base=str(wiki))
    fragment = KnowledgeFragment(
        form="note",
        title="Trusted Push Test Note",
        frontmatter={"领域": "test", "摘要": "trusted push intercept"},
        background="",
        core_content=(
            "## Trusted Push Test Note\n\n"
            "Trusted push intercept body with enough detail to satisfy the strict "
            "distillation hard gate before the trusted push layer receives it. "
            "The proposal must be generated and no Markdown should be written."
        ),
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
        claim_ids=["claim-1"],
    )
    result = DistillationResult(
        session_id="s1",
        judgment="knowledge",
        fragments=[fragment],
        source="test-agent",
    )

    written, file_fragments = engine._persist_pages(result, [fragment])

    assert written == []
    assert file_fragments == []
    assert not list(wiki.rglob("*.md"))
    proposals = ProposalQueue(db, wiki_base=wiki).list()
    assert len(proposals) == 1
    assert proposals[0].status == "needs_manual_review"
    assert proposals[0].candidate.source == "hephaestus_distillation"


def test_hephaestus_wiki_candidate_exact_retry_reuses_pending_decision(
    monkeypatch, tmp_path: Path
):
    wiki = tmp_path / "wiki"
    trusted_db = tmp_path / "trusted.db"
    config = SimpleNamespace(
        wiki_dir=wiki,
        database_dir=tmp_path,
        get=lambda key, default=None: {
            "trusted_push.mode": "enforce",
            "trusted_push.db_path": str(trusted_db),
        }.get(key, default),
    )
    monkeypatch.setattr("core.trust.config.get_config", lambda: config)

    class _Clock:
        calls = 0

        @classmethod
        def now(cls, _tz):
            cls.calls += 1
            return datetime(2026, 7, 18, 1, 2, cls.calls, tzinfo=timezone.utc)

    monkeypatch.setattr("core.hephaestus.trusted_push_bridge.datetime", _Clock)
    kwargs = {
        "wiki_base": wiki,
        "source": "hephaestus_distillation",
        "source_agent": "codex",
        "source_session_id": "retry-session",
        "target_path": wiki / "00-Inbox" / "retry.md",
        "payload": {
            "title": "Exact retry",
            "content": "# Exact retry\n\nBound body.",
            "target_path": str(wiki / "00-Inbox" / "retry.md"),
        },
        "evidence_refs": ["session:retry-session"],
        "proposed_actions": ["create_wiki_page"],
    }

    first = submit_wiki_write_candidate(**kwargs)
    second = submit_wiki_write_candidate(**kwargs)
    other_source = submit_wiki_write_candidate(
        **{
            **kwargs,
            "source_session_id": "other-session",
            "evidence_refs": ["session:other-session"],
        }
    )
    replay_after_other_source = submit_wiki_write_candidate(**kwargs)

    assert first.proposal_id == second.proposal_id
    assert first.material_command_id == second.material_command_id
    assert other_source.material_command_id != first.material_command_id
    assert replay_after_other_source.material_command_id == first.material_command_id
    assert _Clock.calls == 2


def test_hephaestus_wiki_candidate_exact_terminal_replay_returns_original_receipt(
    monkeypatch, tmp_path: Path
):
    wiki = tmp_path / "wiki"
    trusted_db = tmp_path / "trusted.db"
    config = SimpleNamespace(
        wiki_dir=wiki,
        database_dir=tmp_path,
        get=lambda key, default=None: {
            "trusted_push.mode": "enforce",
            "trusted_push.db_path": str(trusted_db),
        }.get(key, default),
    )
    monkeypatch.setattr("core.trust.config.get_config", lambda: config)
    target = wiki / "00-Inbox" / "terminal-replay.md"
    kwargs = {
        "wiki_base": wiki,
        "source": "hephaestus_distillation",
        "source_agent": "codex",
        "source_session_id": "terminal-replay-session",
        "target_path": target,
        "payload": {
            "title": "Terminal replay",
            "content": "# Terminal replay\n\nBound body.",
            "target_path": str(target),
        },
        "evidence_refs": ["session:terminal-replay-session"],
        "proposed_actions": ["create_wiki_page"],
    }
    first = submit_wiki_write_candidate(**kwargs)
    assert first.material_action is not None
    record_trusted_markdown_no_effect_terminal(
        first.material_action,
        target_path=target,
        status="rejected",
        reason_code="test_terminal_replay",
        evidence_ref=f"target-journal:test-reject:{first.proposal_id}",
    )
    state_db = tmp_path / "producer_consumer_ledger.db"
    with sqlite3.connect(state_db) as conn:
        before_state = tuple(
            conn.execute(
                "SELECT (SELECT COUNT(*) FROM cognitive_state_revisions), "
                "(SELECT COUNT(*) FROM cognitive_state_outbox), "
                "(SELECT COUNT(*) FROM cognitive_state_effect_receipts)"
            ).fetchone()
        )
    filler_candidate = json.dumps(
        {"payload": {"material_action": {"command_id": "other-command"}}},
        ensure_ascii=False,
    )
    with sqlite3.connect(trusted_db) as conn:
        conn.executemany(
            """
            INSERT INTO proposals(
                proposal_id, candidate_id, source_ref, target_uri, operation,
                content_hash, risk_level, confidence, status, gate_decision,
                gate_reasons_json, candidate_json, error_message, revision,
                created_at, updated_at
            ) VALUES (?, ?, 'filler', '', 'none', 'sha256:filler', 'low', 0.0,
                      'rejected', 'reject', '[]', ?, '', 0,
                      '9999-12-31T23:59:59+00:00', '9999-12-31T23:59:59+00:00')
            """,
            (
                (f"filler-{index}", f"candidate-{index}", filler_candidate)
                for index in range(10001)
            ),
        )
        before_proposals = int(
            conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
        )

    replay = submit_wiki_write_candidate(**kwargs)

    with sqlite3.connect(state_db) as conn:
        after_state = tuple(
            conn.execute(
                "SELECT (SELECT COUNT(*) FROM cognitive_state_revisions), "
                "(SELECT COUNT(*) FROM cognitive_state_outbox), "
                "(SELECT COUNT(*) FROM cognitive_state_effect_receipts)"
            ).fetchone()
        )
    assert replay.proposal_id == first.proposal_id
    assert replay.material_command_id == first.material_command_id
    assert replay.status == first.status
    assert after_state == before_state
    with sqlite3.connect(trusted_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == (
            before_proposals
        )


def test_enforce_mode_intercepts_distill_action_update(monkeypatch, tmp_path: Path):
    wiki = tmp_path / "wiki"
    db = tmp_path / "trusted.db"
    target = wiki / "03-Tech" / "redis.md"
    target.parent.mkdir(parents=True)
    original = "---\n名称: Redis\n---\n# Redis\n"
    target.write_text(original, encoding="utf-8")
    fake_config = SimpleNamespace(
        wiki_dir=wiki,
        database_dir=tmp_path,
        get=lambda key, default=None: {
            "trusted_push.mode": "enforce",
            "trusted_push.db_path": str(db),
        }.get(key, default),
    )
    monkeypatch.setattr("core.trust.config.get_config", lambda: fake_config)
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *args, **kwargs: None,
    )
    router = DistillActionRouter(
        DistillActionRouterOptions(database_dir=tmp_path, wiki_dir=wiki),
    )
    raw_1 = "用户要求补充超时监控。"
    raw_2 = "用户要求补充连接池上限。"
    input_spec = DistillInputSpec.build(
        source_agent="codex",
        source_session_id="sess-action",
        source_event_ids=("raw-1", "raw-2"),
        raw_completeness="full",
        visible_input=raw_1 + "\n" + raw_2,
        input_mode="standard",
        source_messages=(
            exact_source_message(
                role="user",
                content=raw_1,
                revision_id="raw-1",
            ),
            exact_source_message(
                role="user",
                content=raw_2,
                revision_id="raw-2",
            ),
        ),
    )
    first_evidence = model_exact_evidence(input_spec, source_event_id="raw-1")
    second_evidence = model_exact_evidence(input_spec, source_event_id="raw-2")
    payload = {
        "schema_version": "distill_output_v4",
        **input_spec.prompt_contract(),
        "distill_intent": "update",
        "candidate_summary": "Redis 连接池补充说明。",
        "user_behavior_intent": {
            "content_source": "native_dialogue",
            "user_intent_signal": "seeking_judgment",
            "intent_hypothesis": "seeking_judgment",
            "intent_evidence": [
                {
                    **dict(first_evidence),
                    "reason": "用户要求补充既有页面。",
                }
            ],
            "intent_verification_events": [],
            "intent_confidence": 0.72,
            "intent_status": "unverified",
            "behavior_summary": "用户要求补充 Redis 连接池页面。",
        },
        "claims": [
            {
                "claim_id": "claim-1",
                "claim_text": "Redis 连接池耗尽需要补充超时监控。",
                "claim_type": "technical_fact",
                "scope": {"domain": "backend"},
                "evidence": [dict(first_evidence), dict(second_evidence)],
                "relation_to_existing": {
                    "type": "extends",
                    "target_pages": ["03-Tech/redis.md"],
                    "delta_text": "补充超时监控要求。",
                    "reason": "同一主题补充。",
                    "conflict_strength": 0.0,
                },
                "recommended_action": "update_page",
                "confidence": 0.9,
            }
        ],
        "cognition_episode": model_cognition_episode(
            first_evidence,
            claim_id="claim-1",
        ),
    }

    proof_fragment = KnowledgeFragment(
        form="问题-解决",
        title="可信推送路由准入证明片段",
        frontmatter={"领域": "backend", "摘要": "可信推送路由的已准入根输出。"},
        background="更新路由只能处理经过 extractor 准入的知识。",
        core_content=(
            "## 可信推送路由准入证明\n\n"
            "该片段构造一个完整、可验证的蒸馏根输出，用于确认 direct router "
            "在进入 trusted push 之前也会验证根准入证明和不可变输入绑定。"
            "它还把结构化决策、根输出哈希和 extractor 的准入结论绑定在同一份"
            "证据中，确保后续可信写入不能绕过正式的蒸馏输出校验。"
        ),
        boundaries={"applies": "trusted push router integration"},
        anti_patterns=[],
        related_concepts=[],
        claim_ids=["claim-1"],
    )
    model_root = canonicalize_extraction_output(
        {
            "judgment": "knowledge",
            "judgment_reason": "trusted push router integration root admitted by extractor",
            "structured_output": payload,
        },
        [proof_fragment],
    )
    root = resolve_model_evidence(model_root, input_spec)
    payload = root["structured_output"]
    admission = validate_extraction_output(root, input_spec)
    assert admission.valid, admission.error_text
    result = DistillationResult(
        session_id="sess-action",
        judgment="knowledge",
        structured_output=payload,
        input_spec=input_spec,
        source="codex",
        extraction_judgment="knowledge",
        extraction_contract_valid=True,
        extraction_output=root,
        extraction_output_hash=canonical_extraction_output_hash(canonical_output=root),
    )
    commit_cognition_episode_result(result, tmp_path)

    routed = router.route(
        result,
        [proof_fragment],
        lambda fragments: ([], []),
    )

    assert routed.written == []
    assert target.read_text(encoding="utf-8") == original
    proposals = ProposalQueue(db, wiki_base=wiki).list()
    assert len(proposals) == 1
    assert proposals[0].candidate.source == "hephaestus_distill_action"
    assert proposals[0].candidate.payload["distill_action"] == "update_page"
