from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from core.privacy.data_ownership import DataOwnershipManager, DataSubjectRef
from core.privacy.wiki_subject_deletion import (
    WikiSubjectDeletionService,
    subject_scope_hash,
)
from core.cognitive.decision_trace import resolve_material_action_authorization
from core.trust.vault_mutation_service import (
    TRUSTED_MARKDOWN_ACTION_TYPE,
    TRUSTED_MARKDOWN_EXECUTOR,
    TRUSTED_MARKDOWN_OWNER,
    TrustedVaultMutationResult,
    trusted_markdown_material_action_binding,
)
from core.wiki_projection_lifecycle import DEFAULT_REQUIRED_CONSUMERS, WikiProjectionLedger
from core.wiki_metrics import WikiMetrics
from tests.cognitive_decision_fixtures import material_action_authorization


class _OwnershipConfig:
    def __init__(self, root: Path):
        self.mnemos_dir = root
        self.data_dir = root
        self.database_dir = root / "db"
        self.database_dir.mkdir(parents=True)
        self._vault = root / "vault"
        self._vault.mkdir()

    def vault_dir(self, name: str) -> Path:
        if name != "mnemos":
            raise KeyError(name)
        return self._vault

    def get(self, _key: str, default=None):
        return default


def _write_private_page(path: Path, *, session_id: str = "subject-session") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                "---",
                "scope: private",
                "source_agent: codex",
                f"session_id: {session_id}",
                "project: mnemos",
                "acl_schema_version: 1",
                "acl_metadata_complete: true",
                "acl_reconciliation_status: server_principal",
                "---",
                "private cognitive content that must not remain on disk",
            )
        ),
        encoding="utf-8",
    )


def _patch_direct_delete(monkeypatch) -> None:
    class _TrustedService:
        def __init__(self, **_kwargs):
            pass

        def submit_markdown(self, **_kwargs):
            return SimpleNamespace(intercepted=False, proposal_id="")

    def _commit(_receipt, *, target_path: Path):
        target_path.unlink()
        return True

    def _publish(receipt, *, ledger, source, event_bus=None):
        ledger.attach_event(receipt.mutation_id, receipt.mutation_id)
        return {**receipt.to_dict(), "event_trace_id": receipt.mutation_id, "source": source}

    monkeypatch.setattr(
        "core.privacy.wiki_subject_deletion.TrustedVaultMutationService",
        _TrustedService,
    )
    monkeypatch.setattr(
        "core.privacy.wiki_subject_deletion.commit_trusted_markdown_delete",
        _commit,
    )
    monkeypatch.setattr(
        "core.privacy.wiki_subject_deletion.publish_wiki_mutation",
        _publish,
    )


def test_subject_delete_tombstones_wiki_before_unlink_and_waits_for_consumers(
    tmp_path, monkeypatch
):
    _patch_direct_delete(monkeypatch)
    vault = tmp_path / "vault"
    page = vault / "00-Inbox" / "private.md"
    _write_private_page(page)
    ledger = WikiProjectionLedger(tmp_path / "wiki_projection.db")
    created = ledger.record_mutation(page, mutation_type="create")
    service = WikiSubjectDeletionService(
        wiki_dir=vault,
        projection_db_path=ledger.db_path,
    )

    result = service.delete_subject_scope(
        request_id="delete-wiki-subject",
        scope_kind="session",
        scope_value="subject-session",
    )

    assert result["status"] == "applied"
    assert result["physical_deleted_count"] == 1
    assert result["verified"] is False
    assert result["pending_required_consumer_count"] == len(DEFAULT_REQUIRED_CONSUMERS)
    assert page.exists() is False
    assert WikiProjectionLedger.tombstone_state(ledger.db_path, page) is True
    receipts = ledger.subject_deletion_receipts_for_scope(
        scope_kind="session",
        scope_value_hash="sha256:"
        "b2083a5c9a7237cec1b0bd44701c37bbf5ae9d215ea7094a5bb41fe7222a46f7",
    )
    assert len(receipts) == 1
    assert receipts[0]["status"] == "applied"
    assert "private cognitive content" not in json.dumps(receipts[0], ensure_ascii=False)

    for consumer in DEFAULT_REQUIRED_CONSUMERS:
        ledger.record_projection_receipt(
            mutation_id=created.mutation_id,
            consumer=consumer,
            outcome="ack",
        )
        ledger.record_projection_receipt(
            mutation_id=receipts[0]["mutation_id"],
            consumer=consumer,
            outcome="ack",
        )

    retry = service.delete_subject_scope(
        request_id="delete-wiki-subject",
        scope_kind="session",
        scope_value="subject-session",
    )
    assert retry["status"] == "existing"
    assert retry["verified"] is True
    assert retry["physical_deleted_count"] == 0


def test_subject_delete_refuses_untracked_or_acl_unknown_wiki_pages(tmp_path, monkeypatch):
    _patch_direct_delete(monkeypatch)
    vault = tmp_path / "vault"
    page = vault / "00-Inbox" / "untracked.md"
    _write_private_page(page)
    ledger = WikiProjectionLedger(tmp_path / "wiki_projection.db")
    service = WikiSubjectDeletionService(
        wiki_dir=vault,
        projection_db_path=ledger.db_path,
    )

    result = service.delete_subject_scope(
        request_id="delete-untracked-wiki",
        scope_kind="session",
        scope_value="subject-session",
    )

    assert result["status"] == "blocked"
    assert result["verified"] is False
    assert page.is_file()

    page.write_text("---\nscope: private\n---\nlegacy body", encoding="utf-8")
    malformed = service.delete_subject_scope(
        request_id="delete-unknown-acl-wiki",
        scope_kind="session",
        scope_value="subject-session",
    )
    assert malformed["status"] == "blocked"
    assert malformed["acl_unknown_count"] == 1
    assert page.is_file()


def test_subject_delete_routes_registered_derived_pages_through_required_consumers(
    tmp_path, monkeypatch
):
    """Generated projections are consumers, not ACL-unknown source targets."""

    _patch_direct_delete(monkeypatch)
    vault = tmp_path / "vault"
    source = vault / "00-Inbox" / "private.md"
    _write_private_page(source)
    generated = vault / "05-MOCs" / "Mnemos-Navigation" / "Vault-导航.md"
    generated.parent.mkdir(parents=True)
    generated.write_text("# generated navigation without source ACL\n", encoding="utf-8")
    ledger = WikiProjectionLedger(tmp_path / "wiki_projection.db")
    ledger.record_mutation(source, mutation_type="create")
    ledger.record_mutation(generated, mutation_type="create")

    result = WikiSubjectDeletionService(
        wiki_dir=vault,
        projection_db_path=ledger.db_path,
    ).delete_subject_scope(
        request_id="delete-with-derived-projection",
        scope_kind="session",
        scope_value="subject-session",
    )

    assert result["status"] == "applied"
    assert result["acl_unknown_count"] == 0
    assert result["derived_projection_page_count"] == 1
    assert source.exists() is False
    assert generated.exists() is True
    assert result["verified"] is False


def test_evidence_ref_after_oracle_rejects_ack_without_metrics_effect(tmp_path, monkeypatch):
    """A lifecycle ack alone cannot prove evidence references are gone."""

    _patch_direct_delete(monkeypatch)
    config = _OwnershipConfig(tmp_path)
    page = config.vault_dir("mnemos") / "00-Inbox" / "evidence.md"
    _write_private_page(page, session_id="evidence-session")
    ledger = WikiProjectionLedger(config.database_dir / "wiki_projection.db")
    created = ledger.record_mutation(page, mutation_type="create")
    with WikiMetrics(
        db_path=str(config.database_dir / "wiki_metrics.db"),
        wiki_dir=str(config.vault_dir("mnemos")),
    ) as metrics:
        metrics.reconcile_page_lifecycle(
            page_path=str(page),
            previous_path="",
            mutation_type="update",
        )
        service = WikiSubjectDeletionService(
            wiki_dir=config.vault_dir("mnemos"),
            projection_db_path=ledger.db_path,
        )
        initial = service.delete_subject_scope(
            request_id="delete-evidence-subject",
            scope_kind="session",
            scope_value="evidence-session",
        )
        assert initial["verified"] is False
        for consumer in DEFAULT_REQUIRED_CONSUMERS:
            ledger.record_projection_receipt(
                mutation_id=created.mutation_id,
                consumer=consumer,
                outcome="ack",
            )
        receipt = ledger.subject_deletion_receipts_for_scope(
            scope_kind="session",
            scope_value_hash=subject_scope_hash("session", "evidence-session"),
        )[0]
        for consumer in DEFAULT_REQUIRED_CONSUMERS:
            ledger.record_projection_receipt(
                mutation_id=receipt["mutation_id"],
                consumer=consumer,
                outcome="ack",
            )
        wiki_terminal = service.delete_subject_scope(
            request_id="delete-evidence-subject",
            scope_kind="session",
            scope_value="evidence-session",
        )
        assert wiki_terminal["verified"] is True

        manager = DataOwnershipManager(config)
        subject = DataSubjectRef("session", "evidence-session")
        before = manager._apply_evidence_ref_deletion(
            subject=subject,
            wiki_deletion=wiki_terminal,
        )
        assert before["verified"] is False
        assert before["after_count"] == 1

        metrics.reconcile_page_lifecycle(
            page_path=str(page),
            previous_path=str(page),
            mutation_type="delete",
        )
        after = manager._apply_evidence_ref_deletion(
            subject=subject,
            wiki_deletion=wiki_terminal,
        )

    assert after == {
        "status": "existing",
        "target_count": 1,
        "after_count": 0,
        "verified": True,
        "owner": "wiki_metrics_lifecycle",
    }


def test_subject_delete_publishes_to_its_injected_event_bus(
    tmp_path,
    monkeypatch,
):
    """The delete command, lifecycle row, and consumer event share one owner."""

    class _TrustedService:
        def __init__(self, **_kwargs):
            pass

        def submit_markdown(self, **_kwargs):
            target = Path(_kwargs["target_path"])
            content = target.read_text(encoding="utf-8")
            from core.trust.models import sha256_text

            expected_existing_hash = sha256_text(content)
            binding = trusted_markdown_material_action_binding(
                target_path=target,
                content="",
                proposed_action=str(_kwargs["proposed_action"]),
                expected_existing_hash=expected_existing_hash,
            )
            material_action, permit = resolve_material_action_authorization(
                _kwargs.get("material_action"),
                owner=TRUSTED_MARKDOWN_OWNER,
                executor_id=TRUSTED_MARKDOWN_EXECUTOR,
                action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
            )
            return TrustedVaultMutationResult(
                action="allow",
                mode="off",
                proposal_id="",
                target_path=str(target),
                content_hash=sha256_text(""),
                expected_existing_hash=expected_existing_hash,
                source_path="",
                source_content_hash="",
                proposed_action=str(_kwargs["proposed_action"]),
                material_command_id=permit.command_id,
                material_target_ref=binding["target_ref"],
                material_input_hash=binding["input_hash"],
                material_action=material_action,
            )

    monkeypatch.setattr(
        "core.privacy.wiki_subject_deletion.TrustedVaultMutationService",
        _TrustedService,
    )
    vault = tmp_path / "vault"
    page = vault / "00-Inbox" / "private.md"
    _write_private_page(page)
    ledger = WikiProjectionLedger(tmp_path / "wiki_projection.db")
    ledger.record_mutation(page, mutation_type="create")
    events = []

    class Bus:
        def publish(self, event):
            events.append(event)
            return event.trace_id

    def authorize_delete(request, _deletion_receipt):
        return material_action_authorization(
            Path(request.expected_state_db).parent,
            action_type=request.action_type,
            owner=request.owner,
            executor=request.executor_id,
            target_ref=request.target_ref,
            input_hash=request.input_hash,
        )

    service = WikiSubjectDeletionService(
        wiki_dir=vault,
        projection_db_path=ledger.db_path,
        event_bus=Bus(),
        material_action_resolver=authorize_delete,
    )
    result = service.delete_subject_scope(
        request_id="delete-injected-bus",
        scope_kind="session",
        scope_value="subject-session",
    )

    assert result["status"] == "applied"
    assert len(events) == 1
    assert events[0].payload["mutation_type"] == "delete"
    assert events[0].payload["mutation_id"] == events[0].trace_id


def test_data_ownership_passes_its_event_bus_to_the_wiki_owner(tmp_path, monkeypatch):
    config = _OwnershipConfig(tmp_path)
    page = config.vault_dir("mnemos") / "00-Inbox" / "private.md"
    _write_private_page(page)
    projection = WikiProjectionLedger(config.database_dir / "wiki_projection.db")
    projection.record_mutation(page, mutation_type="create")

    class InjectedBus:
        projection_db_path = config.database_dir / "wiki_projection.db"

    injected_bus = InjectedBus()
    captured = []

    class Service:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def delete_subject_scope(self, **_kwargs):
            return {"status": "no_targets", "target_count": 0, "verified": True}

    monkeypatch.setattr(
        "core.privacy.wiki_subject_deletion.WikiSubjectDeletionService",
        Service,
    )
    manager = DataOwnershipManager(config, event_bus=injected_bus)
    result = manager._apply_wiki_subject_deletion(
        request_id="delete-owner-injection",
        subject=DataSubjectRef("session", "subject-session"),
    )

    assert result["verified"] is True
    assert captured[0]["event_bus"] is injected_bus


def test_data_ownership_blocks_wiki_delete_without_config_bound_event_bus(tmp_path):
    config = _OwnershipConfig(tmp_path)
    page = config.vault_dir("mnemos") / "00-Inbox" / "private.md"
    _write_private_page(page)
    projection = WikiProjectionLedger(config.database_dir / "wiki_projection.db")
    projection.record_mutation(page, mutation_type="create")

    result = DataOwnershipManager(config)._apply_wiki_subject_deletion(
        request_id="delete-owner-without-bus",
        subject=DataSubjectRef("session", "subject-session"),
    )

    assert result == {
        "status": "blocked",
        "target_count": 0,
        "verified": False,
        "error": "wiki_event_bus_required",
    }
    assert page.exists()


def test_data_ownership_blocks_wiki_delete_when_bus_uses_another_projection_db(tmp_path):
    config = _OwnershipConfig(tmp_path)
    page = config.vault_dir("mnemos") / "00-Inbox" / "private.md"
    _write_private_page(page)
    projection = WikiProjectionLedger(config.database_dir / "wiki_projection.db")
    projection.record_mutation(page, mutation_type="create")

    class WrongBus:
        projection_db_path = tmp_path / "other" / "wiki_projection.db"

    result = DataOwnershipManager(config, event_bus=WrongBus())._apply_wiki_subject_deletion(
        request_id="delete-owner-wrong-bus",
        subject=DataSubjectRef("session", "subject-session"),
    )

    assert result == {
        "status": "blocked",
        "target_count": 0,
        "verified": False,
        "error": "wiki_event_bus_projection_mismatch",
    }
    assert page.exists()
