# -*- coding: utf-8 -*-
from __future__ import annotations


def _external_file_structured_output(input_spec) -> dict:
    return {
        "schema_version": "distill_output_v4",
        **input_spec.prompt_contract(),
        "distill_intent": "create",
        "candidate_summary": "用户提供外部方案文档供整理和决策。",
        "user_behavior_intent": {
            "content_source": "external_file",
            "user_intent_signal": "curate_or_decision_material",
            "intent_hypothesis": "curate_or_decision_material",
            "intent_evidence": [
                {
                    "source_event_id": "raw-file-1",
                    "quote": "我把这份方案文档发给你，整理后帮我判断怎么选。",
                    "reason": "用户主动引入外部文件并要求整理决策。",
                }
            ],
            "intent_verification_events": [
                {
                    "source_event_id": "raw-confirm-1",
                    "status": "verified",
                    "quote": "对，这份文档就是后面做决策用。",
                    "note": "用户确认外部文档用途。",
                }
            ],
            "intent_confidence": 0.9,
            "intent_status": "verified",
            "behavior_summary": "用户主动提供外部方案文档作为整理和决策素材。",
        },
        "claims": [
            {
                "claim_id": "claim-1",
                "claim_text": "方案文档应先提炼约束、选项、取舍理由，再进入决策。",
                "claim_type": "procedure",
                "scope": {"domain": "product"},
                "evidence": [
                    {
                        "source_event_id": "raw-file-1",
                        "quote": "整理后帮我判断怎么选",
                    }
                ],
                "relation_to_existing": {
                    "type": "new",
                    "target_pages": [],
                    "delta_text": "",
                    "reason": "测试 vault 没有同等页面。",
                },
                "recommended_action": "create_page",
                "cognitive_actions": ["create_observation", "propose_methodology"],
                "confidence": 0.84,
            }
        ],
    }


def test_behavior_intent_distillation_flow_reaches_observation_consumer(tmp_path):
    from core.cognitive.observation_engine import ObservationEngine
    from core.cognitive.sources import ContentSource, SourceReader, UserIntent
    from core.hephaestus.distill_input_spec import DistillInputSpec
    from core.hephaestus.distillation_models import KnowledgeFragment
    from core.hephaestus.distillation_wiki_page import generate_wiki_page

    wiki = tmp_path / "wiki"
    inbox = wiki / "00-Inbox"
    inbox.mkdir(parents=True)
    input_spec = DistillInputSpec.build(
        source_agent="codex",
        source_session_id="sess-behavior-flow",
        source_event_ids=("raw-file-1", "raw-confirm-1"),
        raw_completeness="full",
        visible_input="external decision document behavior-intent integration test",
        input_mode="standard",
    )
    payload = _external_file_structured_output(input_spec)
    fragment = KnowledgeFragment(
        form="decision",
        title="外部方案文档决策整理流程",
        frontmatter={"领域": "product", "摘要": "外部方案文档进入决策前的整理流程。"},
        background="用户主动提供外部方案文档，希望转成可决策素材。",
        core_content="## 决策整理流程\n\n先提炼约束、选项、取舍理由，再进入正式决策。",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )
    page = generate_wiki_page(
        fragment,
        "sess-behavior-flow",
        source="codex",
        structured_output=payload,
    )
    (inbox / "external-decision.md").write_text(page, encoding="utf-8")

    assert "行为意图摘要: 用户主动提供外部方案文档作为整理和决策素材。" in page
    assert "意图假设: curate_or_decision_material" in page
    assert "- 用户引入原因: 用户主动提供外部方案文档作为整理和决策素材。" in page

    reader = SourceReader(wiki_dir=str(wiki))
    items = list(reader.read_all())

    assert len(items) == 1
    assert items[0].content_source == ContentSource.EXTERNAL_FILE
    assert items[0].user_intent == UserIntent.CURATE_OR_DECISION_MATERIAL

    from core.cognitive.observation_store import ObservationStore

    engine = ObservationEngine(
        wiki_dir=str(wiki),
        store=ObservationStore(str(tmp_path / "observations.db")),
        export_to_wiki=False,
    )
    engine.extractors = []
    batch = engine.run(persist=False)
    intent_observations = [
        obs for obs in batch.observations if "user_intent_distribution" in obs.value
    ]

    assert intent_observations
    assert intent_observations[0].value["user_intent_distribution"] == {
        "curate_or_decision_material": 1
    }
