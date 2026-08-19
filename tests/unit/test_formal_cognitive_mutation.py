from __future__ import annotations

from pathlib import Path

import pytest

from core.cognitive.decision_trace import MaterialActionTerminal
from core.trust.formal_cognitive_mutation import FormalCognitiveMutationJournal
from tests.cognitive_decision_fixtures import (
    HASH_A,
    HASH_B,
    formal_mutation_action_authorization,
)


@pytest.mark.no_canonical_material_actions
def test_formal_cognitive_mutation_fails_closed_without_material_authorization(
    tmp_path: Path,
) -> None:
    journal = FormalCognitiveMutationJournal(tmp_path / "trusted_push.db")

    with pytest.raises(PermissionError, match="material-action authorization"):
        journal.record(
            asset_kind="kg_relation",
            action="upsert_relation",
            target_ref="relation:test",
            actor="test",
            decision="untraced",
            evidence_refs=("test:untraced",),
        )

    assert journal.list_events() == []


def test_formal_cognitive_mutation_is_bound_and_idempotent(tmp_path: Path) -> None:
    journal = FormalCognitiveMutationJournal(tmp_path / "trusted_push.db")
    authorization = formal_mutation_action_authorization(
        tmp_path,
        asset_kind="kg_relation",
        action="upsert_relation",
        target_ref="relation:test",
        actor="relation_manager",
        reason="accepted_relation",
        metadata={"relation_type": "supports"},
        owner="knowledge_graph",
        executor="relation_manager",
    )
    permit = authorization.permit
    authorization.record_terminal(
        MaterialActionTerminal(
            status="committed",
            target_effect_id=permit.effect_id,
            before_hash=HASH_A,
            after_hash=HASH_B,
            evidence_refs=(
                f"material-command:{permit.command_id}",
                f"decision-revision:{permit.decision_revision_id}",
                f"material-effect:{permit.effect_id}",
                f"target-after:{HASH_B}",
                f"target-oracle:relation-manager:{HASH_B}",
            ),
            outcome="relation target committed",
            created_at="2026-07-17T09:01:00+00:00",
        )
    )
    evidence = (
        f"material-command:{permit.command_id}",
        f"decision-revision:{permit.decision_revision_id}",
        f"material-effect:{permit.effect_id}",
        "source-relation:test",
    )
    kwargs = {
        "asset_kind": "kg_relation",
        "action": "upsert_relation",
        "target_ref": "relation:test",
        "actor": "relation_manager",
        "decision": permit.decision_revision_id,
        "reason": "accepted_relation",
        "evidence_refs": evidence,
        "metadata": {"relation_type": "supports"},
        "material_action": authorization,
    }

    first = journal.record(**kwargs)
    replay = journal.record(**kwargs)

    assert replay["event_id"] == first["event_id"]
    assert first["decision"] == permit.decision_revision_id
    assert first["metadata"]["material_action"]["command_id"] == permit.command_id
