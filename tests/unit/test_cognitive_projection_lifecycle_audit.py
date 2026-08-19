"""Acceptance tests for the COG-050 projection lifecycle gate."""

from core.mnemos_bus import HandlerOutcome
from core.wiki_derived_projection import DerivedProjectionLifecycle, ProjectionPageSpec
from core.wiki_projection_lifecycle import DEFAULT_REQUIRED_CONSUMERS, WikiProjectionLedger
from scripts.audit_cognitive_projection_lifecycle import (
    ROOT,
    _handler_swallows_projection_failure,
    _vault_sync_ast_contract,
    audit_live_projection_state,
    build_report,
)


def test_projection_lifecycle_audit_has_zero_residuals():
    report = build_report(repo_root=ROOT)

    assert report["ok"] is True, report
    assert report["failures"] == []
    assert set(report["metrics"].values()) == {0}
    assert report["metrics"]["binding_replay_gap"] == 0
    assert report["synthetic"]["error"] == ""
    assert report["synthetic"]["consumer_probe_mode"] == "isolated_typed_noop"


def test_live_audit_fails_closed_when_pages_have_no_manifest(tmp_path):
    wiki = tmp_path / "wiki"
    page = wiki / "L3-Observations" / "attention.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Unbound projection\n", encoding="utf-8")

    result = audit_live_projection_state(
        wiki_dir=wiki,
        projection_db=tmp_path / "missing.db",
    )

    assert result["initialized"] is False
    assert result["page_count"] == 1
    assert result["projection_binding_gap"] == 1


def test_live_audit_preserves_independent_nested_observation_reports(tmp_path):
    wiki = tmp_path / "wiki"
    dimension = wiki / "L3-Observations" / "attention.md"
    immune = wiki / "L3-Observations" / "immune" / "report.md"
    dimension.parent.mkdir(parents=True)
    immune.parent.mkdir(parents=True)
    dimension.write_text("# Unbound projection\n", encoding="utf-8")
    immune.write_text("# Independent report\n", encoding="utf-8")

    result = audit_live_projection_state(
        wiki_dir=wiki,
        projection_db=tmp_path / "missing.db",
    )

    assert result["page_count"] == 1
    assert result["projection_binding_gap"] == 1


def test_projection_failure_swallowing_mutation_is_detected(tmp_path):
    module = tmp_path / "mutated_engine.py"
    module.write_text(
        """
def _export_projection(exporter):
    try:
        exporter.export_batch([])
    except RuntimeError:
        return None
""",
        encoding="utf-8",
    )

    assert _handler_swallows_projection_failure(module, "_export_projection") == 1


def test_vault_sync_contract_uses_ast_instead_of_comment_markers(tmp_path):
    module = tmp_path / "vault_sync.py"
    module.write_text(
        '''
"""ObservationEngine( .run(persist=True) read_only=True canonical_delta"""

def sync():
    return "load_canonical_persona_versions_read_only"
''',
        encoding="utf-8",
    )

    contract = _vault_sync_ast_contract(module)

    assert contract["mutating_callsite_count"] == 0
    assert contract["uses_read_only_replay"] is False


def test_vault_sync_contract_detects_actual_mutating_entrypoint(tmp_path):
    module = tmp_path / "vault_sync.py"
    module.write_text(
        "def sync():\n"
        "    engine = ObservationEngine()\n"
        "    engine.run(persist=True)\n",
        encoding="utf-8",
    )

    contract = _vault_sync_ast_contract(module)

    assert contract["mutating_callsite_count"] == 2


def test_live_audit_counts_required_receipts_for_stale_deletes(tmp_path):
    wiki = tmp_path / "wiki"
    ledger = WikiProjectionLedger(tmp_path / "wiki_projection.db")

    class ReceiptBus:
        def publish(self, event):
            mutation_id = str(event.payload["mutation_id"])
            for consumer in DEFAULT_REQUIRED_CONSUMERS:
                ledger.record_projection_receipt(
                    mutation_id=mutation_id,
                    consumer=consumer,
                    outcome=HandlerOutcome.noop(
                        consumer,
                        "test lifecycle consumer",
                    ).disposition,
                    event_trace_id=str(event.trace_id),
                )
            return str(event.trace_id)

    lifecycle = DerivedProjectionLifecycle(
        wiki,
        ledger=ledger,
        event_bus=ReceiptBus(),
    )
    page = ProjectionPageSpec(
        path=wiki / "L3-Observations" / "stale.md",
        content="# Stale\n",
        page_role="formal_derived:observation",
        canonical_revision="revision:1",
        source_refs=("observation:stale",),
    )
    lifecycle.publish_generation(
        projection_kind="observation",
        scope_root=wiki / "L3-Observations",
        pages=[page],
        full=True,
    )
    lifecycle.publish_generation(
        projection_kind="observation",
        scope_root=wiki / "L3-Observations",
        pages=[],
        full=True,
    )
    delete_mutation = ledger.list_mutations()[-1]
    with ledger._conn() as connection:
        connection.execute(
            "DELETE FROM projection_receipts WHERE mutation_id=? AND consumer=?",
            (delete_mutation["mutation_id"], DEFAULT_REQUIRED_CONSUMERS[0]),
        )
        connection.commit()

    result = audit_live_projection_state(
        wiki_dir=wiki,
        projection_db=ledger.db_path,
    )

    assert result["page_count"] == 0
    assert result["manifest_item_count"] == 1
    assert result["stale_projection"] == 0
    assert result["required_consumer_receipt_gap"] == 1
