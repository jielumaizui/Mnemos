from __future__ import annotations

from types import SimpleNamespace

from core.ops.health_check import _check_wiki_projection
from core.wiki_projection_lifecycle import WikiProjectionLedger


def test_wiki_projection_health_is_degraded_until_required_receipts_exist(tmp_path):
    cfg = SimpleNamespace(database_dir=tmp_path)
    missing = _check_wiki_projection(cfg)
    assert missing["status"] == "degraded"
    assert missing["projection_gap"] == -1

    page = tmp_path / "page.md"
    page.write_text("# page", encoding="utf-8")
    ledger = WikiProjectionLedger(tmp_path / "wiki_projection.db")
    mutation = ledger.record_mutation(page, mutation_type="create")
    gap = _check_wiki_projection(cfg)
    assert gap["status"] == "degraded"
    assert gap["projection_gap"] == 1
    for consumer, outcome in (
        ("knowledge_graph", "ack"),
        ("cognitive_graph", "noop"),
        ("relation_embeddings", "ack"),
        ("wiki_search_index", "ack"),
        ("wiki_metrics", "ack"),
        ("moc_navigation", "ack"),
    ):
        ledger.record_projection_receipt(
            mutation_id=mutation.mutation_id,
            consumer=consumer,
            outcome=outcome,
        )
    healthy = _check_wiki_projection(cfg)
    assert healthy["status"] == "ok"
    assert healthy["projection_gap"] == 0
