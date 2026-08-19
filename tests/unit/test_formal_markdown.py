import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.trust.formal_markdown import submit_or_write_markdown
from core.trust.models import sha256_text
from core.trust.proposal_queue import ProposalQueue
from core.trust.vault_mutation_service import (
    TrustedVaultMutationService,
    commit_trusted_markdown,
    commit_trusted_markdown_move,
)
from tests.cognitive_decision_fixtures import trusted_markdown_action_authorization


def _trusted_config(wiki: Path, db: Path, mode: str) -> SimpleNamespace:
    return SimpleNamespace(
        wiki_dir=wiki,
        database_dir=db.parent,
        get=lambda key, default=None: {
            "trusted_push.mode": mode,
            "trusted_push.db_path": str(db),
        }.get(key, default),
    )


def test_submit_or_write_markdown_writes_when_trusted_push_off(monkeypatch, tmp_path):
    wiki = tmp_path / "wiki"
    db = tmp_path / "trusted.db"
    monkeypatch.setattr("core.trust.config.get_config", lambda: _trusted_config(wiki, db, "off"))

    target = wiki / "03-Tech" / "redis.md"
    material_action = trusted_markdown_action_authorization(
        tmp_path,
        target_path=target,
        content="# Redis\n",
        proposed_action="update_markdown",
    )
    result = submit_or_write_markdown(
        wiki_base=wiki,
        target_path=target,
        content="# Redis\n",
        source="unit_test",
        evidence_refs=["test:off"],
        material_action=material_action,
    )

    assert result.action == "write"
    assert target.read_text(encoding="utf-8") == "# Redis\n"


@pytest.mark.no_canonical_material_actions
def test_submit_or_write_markdown_fails_closed_without_material_authorization(
    monkeypatch,
    tmp_path,
):
    wiki = tmp_path / "wiki"
    db = tmp_path / "trusted.db"
    monkeypatch.setattr(
        "core.trust.config.get_config",
        lambda: _trusted_config(wiki, db, "off"),
    )
    target = wiki / "03-Tech" / "untraced.md"

    with pytest.raises(PermissionError, match="material-action authorization"):
        submit_or_write_markdown(
            wiki_base=wiki,
            target_path=target,
            content="# Untraced\n",
            source="unit_test",
            evidence_refs=["test:missing-decision"],
        )

    assert not target.exists()


def test_submit_or_write_markdown_intercepts_when_enforced(monkeypatch, tmp_path):
    wiki = tmp_path / "wiki"
    db = tmp_path / "trusted.db"
    monkeypatch.setattr(
        "core.trust.config.get_config",
        lambda: _trusted_config(wiki, db, "enforce"),
    )

    target = wiki / "03-Tech" / "redis.md"
    material_action = trusted_markdown_action_authorization(
        tmp_path,
        target_path=target,
        content="# Redis\n",
        proposed_action="create_wiki_page",
    )
    result = submit_or_write_markdown(
        wiki_base=wiki,
        target_path=target,
        content="# Redis\n",
        source="unit_test",
        evidence_refs=["test:enforce"],
        proposed_action="create_wiki_page",
        material_action=material_action,
    )

    assert result.intercepted
    assert not target.exists()
    proposals = ProposalQueue(db, wiki_base=wiki).list()
    assert len(proposals) == 1
    assert proposals[0].candidate.source == "unit_test"


def test_commit_rejects_receipt_reuse_for_another_target(monkeypatch, tmp_path):
    wiki = tmp_path / "wiki"
    db = tmp_path / "trusted.db"
    monkeypatch.setattr("core.trust.config.get_config", lambda: _trusted_config(wiki, db, "off"))
    service = TrustedVaultMutationService(wiki_base=wiki)
    material_action = trusted_markdown_action_authorization(
        tmp_path,
        target_path=wiki / "a.md",
        content="# A\n",
        proposed_action="update_markdown",
    )
    receipt = service.submit_markdown(
        target_path=wiki / "a.md",
        content="# A\n",
        source="unit_test",
        material_action=material_action,
    )

    with pytest.raises(ValueError, match="target does not match"):
        commit_trusted_markdown(
            receipt,
            target_path=wiki / "b.md",
            content="# A\n",
            material_action=material_action,
        )


def test_commit_recovers_after_target_write_without_rewriting(
    monkeypatch,
    tmp_path,
):
    import core.trust.vault_mutation_service as mutation_module

    wiki = tmp_path / "wiki"
    db = tmp_path / "trusted.db"
    monkeypatch.setattr(
        "core.trust.config.get_config",
        lambda: _trusted_config(wiki, db, "off"),
    )
    target = wiki / "crash.md"
    content = "# Crash-safe Markdown\n"
    material_action = trusted_markdown_action_authorization(
        tmp_path,
        target_path=target,
        content=content,
        proposed_action="update_markdown",
    )
    receipt = TrustedVaultMutationService(wiki_base=wiki).submit_markdown(
        target_path=target,
        content=content,
        source="unit_test",
        material_action=material_action,
    )
    original = mutation_module._recover_trusted_markdown_effect
    crashed = False

    def crash_after_target(authorization, oracle):
        nonlocal crashed
        if not crashed and oracle.observe(authorization.permit) is not None:
            crashed = True
            raise OSError("crash after trusted Markdown target write")
        return original(authorization, oracle)

    monkeypatch.setattr(
        mutation_module,
        "_recover_trusted_markdown_effect",
        crash_after_target,
    )
    with pytest.raises(OSError, match="after trusted Markdown target write"):
        commit_trusted_markdown(
            receipt,
            target_path=target,
            content=content,
            material_action=material_action,
        )

    monkeypatch.setattr(
        mutation_module,
        "_recover_trusted_markdown_effect",
        original,
    )
    assert commit_trusted_markdown(
        receipt,
        target_path=target,
        content=content,
        material_action=material_action,
    )
    assert target.read_text(encoding="utf-8") == content
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM trusted_markdown_effect_intents"
        ).fetchone()[0] == 1
    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_state_effect_receipts"
        ).fetchone()[0] == 1


def test_commit_rejects_target_changed_after_submission(monkeypatch, tmp_path):
    wiki = tmp_path / "wiki"
    db = tmp_path / "trusted.db"
    monkeypatch.setattr("core.trust.config.get_config", lambda: _trusted_config(wiki, db, "off"))
    target = wiki / "a.md"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    material_action = trusted_markdown_action_authorization(
        tmp_path,
        target_path=target,
        content="new\n",
        proposed_action="update_markdown",
        expected_existing_hash=sha256_text("old\n"),
    )
    receipt = TrustedVaultMutationService(wiki_base=wiki).submit_markdown(
        target_path=target,
        content="new\n",
        source="unit_test",
        expected_existing_hash=sha256_text("old\n"),
        material_action=material_action,
    )
    target.write_text("external edit\n", encoding="utf-8")

    with pytest.raises(ValueError, match="changed after submission"):
        commit_trusted_markdown(
            receipt,
            target_path=target,
            content="new\n",
            material_action=material_action,
        )

    assert target.read_text(encoding="utf-8") == "external edit\n"


def test_move_commit_rejects_source_changed_after_submission(monkeypatch, tmp_path):
    wiki = tmp_path / "wiki"
    db = tmp_path / "trusted.db"
    monkeypatch.setattr("core.trust.config.get_config", lambda: _trusted_config(wiki, db, "off"))
    source = wiki / "source.md"
    target = wiki / "target.md"
    source.parent.mkdir(parents=True)
    source.write_text("old\n", encoding="utf-8")
    material_action = trusted_markdown_action_authorization(
        tmp_path,
        target_path=target,
        content="classified\n",
        proposed_action="update_markdown",
        source_path=source,
        source_content_hash=sha256_text("old\n"),
    )
    receipt = TrustedVaultMutationService(wiki_base=wiki).submit_markdown(
        target_path=target,
        content="classified\n",
        source="unit_test",
        metadata={
            "source_path": str(source),
            "source_content_hash": sha256_text("old\n"),
        },
        material_action=material_action,
    )
    source.write_text("external edit\n", encoding="utf-8")

    with pytest.raises(ValueError, match="move source changed"):
        commit_trusted_markdown_move(
            receipt,
            source_path=source,
            target_path=target,
            content="classified\n",
            material_action=material_action,
        )

    assert source.read_text(encoding="utf-8") == "external edit\n"
    assert not target.exists()
