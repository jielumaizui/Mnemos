from datetime import datetime, timedelta
from dataclasses import asdict
from unittest.mock import MagicMock

from tests.knowledge_graph_decision_fixtures import authorized_knowledge_graph
from tests.cognitive_decision_fixtures import canonical_material_action_scope


def _assertion(claim, **kwargs):
    from core.kia.assertion_extractor import Assertion, KnowledgeForm

    return Assertion(
        claim=claim,
        form=kwargs.pop("form", KnowledgeForm.INSIGHT),
        confidence=kwargs.pop("confidence", 1.0),
        **kwargs,
    )


def test_domain_tags_must_overlap_to_detect_conflict():
    from core.kia.conflict_resolver import detect_conflicts

    new = _assertion("React 应该使用 Redux 管理状态", tags=["frontend"])
    existing = _assertion("React 不应该使用 Redux 管理状态", tags=["backend"])

    assert detect_conflicts([new], [existing]) == []

    existing.tags = ["frontend"]
    conflicts = detect_conflicts([new], [existing])

    assert len(conflicts) == 1
    assert conflicts[0].conflict_type == "domain"


def test_arbitration_score_uses_confidence_and_dynamic_half_life():
    from core.kia.assertion_extractor import KnowledgeForm
    from core.kia.conflict_resolver import WikiPageMeta, _calculate_arbitration_score

    old_meta = WikiPageMeta(
        page_id="p1",
        created_at=datetime.now() - timedelta(days=90),
        updated_at=datetime.now() - timedelta(days=90),
        evidence_level="curated",
        verification_count=1,
    )
    insight = _assertion(
        "长期洞察", form=KnowledgeForm.INSIGHT, evidence_level="curated", confidence=1.0
    )
    code = _assertion(
        "代码方案", form=KnowledgeForm.PROBLEM_SOLUTION, evidence_level="curated", confidence=1.0
    )
    low_conf = _assertion(
        "低置信断言", form=KnowledgeForm.INSIGHT, evidence_level="curated", confidence=0.2
    )

    assert _calculate_arbitration_score(insight, old_meta) > _calculate_arbitration_score(
        code, old_meta
    )
    assert _calculate_arbitration_score(low_conf, old_meta) < _calculate_arbitration_score(
        insight, old_meta
    )


def test_wiki_page_meta_verification_history_contract():
    from core.kia.conflict_resolver import WikiPageMeta

    history = [
        {
            "source": "user",
            "verified_at": "2026-07-03T02:45:00+08:00",
            "result": "confirmed",
        }
    ]
    meta = WikiPageMeta(
        page_id="p1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        evidence_level="curated",
        verification_count=1,
        verification_history=history,
    )

    assert meta.verification_history == history
    assert asdict(meta)["verification_history"] == history


def test_medium_tie_writes_notes_for_both_assertions():
    from core.kia.conflict_resolver import Conflict, _auto_arbitrate_medium

    conflict = Conflict(
        conflict_type="contextual",
        strength=0.5,
        new_assertion=_assertion("A 应该使用 B"),
        existing_assertion=_assertion("A 不应该使用 B"),
        topic_overlap=0.8,
        direction_conflict=0.8,
    )

    resolution = _auto_arbitrate_medium(conflict, None, None)

    assert resolution.target == "both"
    assert "notes" in resolution.updates["new"]
    assert "notes" in resolution.updates["existing"]


def test_conflict_resolver_arbitrate_uses_meta_provider_when_meta_absent():
    from core.kia.conflict_resolver import Conflict, ConflictResolver, WikiPageMeta

    new = _assertion("A 应该使用 B")
    existing = _assertion("A 不应该使用 B")
    conflict = Conflict(
        conflict_type="contextual",
        strength=0.5,
        new_assertion=new,
        existing_assertion=existing,
        topic_overlap=0.8,
        direction_conflict=0.8,
    )
    seen_claims = []

    def meta_provider(assertion):
        seen_claims.append(assertion.claim)
        return WikiPageMeta(
            page_id=assertion.claim,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            evidence_level="curated",
            verification_count=3 if assertion.claim == existing.claim else 0,
        )

    def scorer(_assertion, meta):
        return meta.verification_count if meta else 0

    resolution = ConflictResolver(
        scorer=scorer,
        meta_provider=meta_provider,
    ).arbitrate(conflict)

    assert seen_claims == [new.claim, existing.claim]
    assert resolution.target == "new"


def test_high_conflict_uses_stable_dispute_id_and_save(tmp_path):
    from core.kia.conflict_resolver import Conflict, _create_dispute_high, save_dispute_page

    conflict = Conflict(
        conflict_type="contextual",
        strength=0.9,
        new_assertion=_assertion("A 应该使用 B"),
        existing_assertion=_assertion("A 不应该使用 B"),
        topic_overlap=0.8,
        direction_conflict=0.9,
    )

    resolution = _create_dispute_high(conflict)
    with canonical_material_action_scope(tmp_path / "material-state"):
        path = save_dispute_page(conflict, resolution, wiki_dir=tmp_path)

    assert resolution.dispute_page.startswith("dispute-")
    assert path.parent == tmp_path / "08-Disputes"
    assert path.name.startswith("争议仲裁-dispute-")
    assert "A 应该使用 B" in path.read_text(encoding="utf-8")


def test_resolve_all_conflicts_uses_stable_claim_keys():
    from core.kia.conflict_resolver import (
        Conflict,
        WikiPageMeta,
        resolve_all_conflicts,
        _stable_claim_key,
    )

    new = _assertion("A 应该使用 B", evidence_level="single-source", confidence=1.0)
    existing = _assertion("A 不应该使用 B", evidence_level="single-source", confidence=1.0)
    conflict = Conflict(
        conflict_type="contextual",
        strength=0.5,
        new_assertion=new,
        existing_assertion=existing,
        topic_overlap=0.8,
        direction_conflict=0.8,
    )
    meta = WikiPageMeta(
        page_id="new",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        evidence_level="curated",
        verification_count=3,
        is_user_verified=True,
    )

    resolutions = resolve_all_conflicts([conflict], new_metas={_stable_claim_key(new.claim): meta})

    assert resolutions[0].action == "supersede"
    assert resolutions[0].target == "existing"


def test_resolve_all_conflicts_delegates_to_resolver_class(monkeypatch):
    from core.kia.conflict_resolver import (
        Conflict,
        ConflictResolver,
        Resolution,
        resolve_all_conflicts,
    )

    conflict = Conflict(
        conflict_type="contextual",
        strength=0.5,
        new_assertion=_assertion("A 应该使用 B"),
        existing_assertion=_assertion("A 不应该使用 B"),
        topic_overlap=0.8,
        direction_conflict=0.8,
    )
    new_metas = {"new": object()}
    existing_metas = {"existing": object()}
    seen = {}

    def fake_resolve_all(self, conflicts, new_metas=None, existing_metas=None):
        seen["resolver"] = self
        seen["conflicts"] = conflicts
        seen["new_metas"] = new_metas
        seen["existing_metas"] = existing_metas
        return [Resolution(action="noop", target="both", reason="delegated")]

    monkeypatch.setattr(ConflictResolver, "resolve_all", fake_resolve_all)

    resolutions = resolve_all_conflicts(
        [conflict],
        new_metas=new_metas,
        existing_metas=existing_metas,
    )

    assert isinstance(seen["resolver"], ConflictResolver)
    assert seen["conflicts"] == [conflict]
    assert seen["new_metas"] is new_metas
    assert seen["existing_metas"] is existing_metas
    assert resolutions[0].reason == "delegated"


def test_relation_conflicts_use_shared_helper():
    from core.kia.conflict_resolver import detect_relation_conflicts
    from core.kia.relation_schema import Relation, RelationType

    conflicts = detect_relation_conflicts(
        [
            Relation("A", "B", RelationType.BUILDS_ON),
            Relation("A", "B", RelationType.CONTRADICTS),
        ]
    )

    assert len(conflicts) == 1
    assert "既建立" in conflicts[0][2]


def test_knowledge_graph_detect_conflicts_accepts_conflict_resolver_class(
    tmp_path, monkeypatch
):
    from core.kia.conflict_resolver import ConflictResolver
    from core.kia.knowledge_graph import KnowledgeGraph
    from core.kia.relation_schema import Relation, RelationType

    graph = authorized_knowledge_graph(
        db_path=str(tmp_path / "kg.db"),
        wiki_base=str(tmp_path / "wiki"),
    )
    mock_mgr = MagicMock()
    monkeypatch.setattr(
        KnowledgeGraph,
        "_rel_emb_mgr",
        property(lambda self: mock_mgr),
    )
    graph.add_relation(Relation("A", "B", RelationType.BUILDS_ON))
    graph.add_relation(Relation("A", "B", RelationType.CONTRADICTS))

    conflicts = graph.detect_conflicts(conflict_resolver=ConflictResolver())

    assert len(conflicts) == 1
    assert "既建立" in conflicts[0][2]
