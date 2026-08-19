from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from scripts.reconcile_wiki_quality import NAV_MARKER, PAGE_NAV_MARKER, reconcile
from core.trust.vault_mutation_service import TrustedVaultMutationResult
from core.cognitive.decision_trace import (
    MaterialActionRequest,
    resolve_material_action_authorization,
)
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.mnemos_bus import Event
from core.wiki_navigation import rebuild_navigation
from core.wiki_projection_lifecycle import WikiProjectionLedger
from daemon.wiki_projection_handlers import (
    WikiProjectionEffectOracle,
    register_wiki_projection_handlers,
)


def _page(title: str, body: str, links: str = "") -> str:
    return (
        "---\n"
        f"标题: {title}\n"
        "领域: 测试\n"
        f"摘要: {title}的测试摘要\n"
        "---\n"
        f"# {title}\n\n{body}\n{links}\n"
    )


def _wiki_page_updated_event(vault, database_dir):
    page = vault / "page.md"
    if not page.is_file():
        page.write_text(_page("Page", "projection test content"), encoding="utf-8")
    initialize_cognitive_state_schema(
        database_dir / "producer_consumer_ledger.db"
    )
    receipt = WikiProjectionLedger(
        database_dir / "wiki_projection.db"
    ).record_mutation(page, mutation_type="create")
    return Event(
        event_type="wiki_page_updated",
        source="test",
        trace_id="wiki-projection-test",
        timestamp=receipt.created_at,
        payload={
            "page_path": receipt.page_path,
            "previous_path": receipt.previous_path,
            "page_id": receipt.page_id,
            "page_revision": receipt.page_revision,
            "mutation_id": receipt.mutation_id,
            "mutation_type": receipt.mutation_type,
            "tombstone": receipt.tombstone,
        },
    )


def test_reconcile_preserves_body_and_closes_zero_budget_quality(
    tmp_path,
):
    vault = tmp_path / "vault"
    inbox = vault / "00-Inbox"
    concepts = vault / "04-Concepts"
    inbox.mkdir(parents=True)
    concepts.mkdir(parents=True)
    home = vault / "00-Mnemos-Home.md"
    substantial = (
        "This page records concrete evidence, operational context, ownership, and recovery "
        "steps for the projection lifecycle. It remains useful without generated navigation "
        "and is intentionally longer than the quality gate so the test proves real content "
        "rather than boilerplate can satisfy the zero-budget policy. "
    )
    home.write_text(_page("Home", substantial, "[[missing concept]]"), encoding="utf-8")
    first = inbox / "first.md"
    first.write_text(_page("First", substantial + "First-page evidence."), encoding="utf-8")
    second = concepts / "second.md"
    second.write_text(_page("Second", substantial + "Second-page evidence."), encoding="utf-8")
    before = {path: path.read_text(encoding="utf-8") for path in (home, first, second)}

    backup = tmp_path / "backup"
    result = reconcile(vault, backup, apply=True)

    assert result["ok"] is True
    assert all(
        result["after"]["issue_counts"].get(issue_type, 0) == 0
        for issue_type in ("missing_meta", "broken_link", "stub", "orphan")
    )
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["page_count"] == 3
    assert "missing concept" in home.read_text(encoding="utf-8")
    assert "[[missing concept]]" not in home.read_text(encoding="utf-8")
    for path in (first, second):
        after = path.read_text(encoding="utf-8")
        original_body = before[path].split("---", 2)[-1].strip()
        assert original_body in after
        assert PAGE_NAV_MARKER not in after
    assert PAGE_NAV_MARKER in home.read_text(encoding="utf-8")
    generated = list((vault / "05-MOCs" / "Mnemos-Navigation").glob("*.md"))
    assert generated
    assert all(NAV_MARKER in path.read_text(encoding="utf-8") for path in generated)


def test_reconcile_does_not_hide_stub_debt_with_generated_boilerplate(
    tmp_path,
):
    vault = tmp_path / "vault"
    vault.mkdir()
    page = vault / "short.md"
    page.write_text(_page("Short", "unresolved short content"), encoding="utf-8")

    result = reconcile(vault, tmp_path / "backup", apply=True)

    assert result["ok"] is False
    assert result["after"]["issue_counts"]["stub"] == 1
    assert PAGE_NAV_MARKER not in page.read_text(encoding="utf-8")


def test_reconcile_dry_run_does_not_write(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    page = vault / "page.md"
    page.write_text(_page("Page", "short", "[[absent]]"), encoding="utf-8")
    before = page.read_bytes()
    result = reconcile(vault, tmp_path / "backup", apply=False)
    assert result["applied"] is False
    assert result["link_repair"]["candidate_links"] == 1
    assert page.read_bytes() == before
    assert not (tmp_path / "backup").exists()


def test_navigation_enforce_intercept_remains_retryable(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "page.md").write_text(_page("Page", "substantive content"), encoding="utf-8")
    intercepted = TrustedVaultMutationResult(
        action="intercept", mode="enforce", proposal_id="proposal-1", status="pending"
    )
    monkeypatch.setattr(
        "core.wiki_navigation.submit_or_write_markdown_with_decision",
        lambda *args, **kwargs: intercepted,
    )
    monkeypatch.setattr(
        "core.wiki_navigation._publish",
        lambda *args, **kwargs: pytest.fail("intercepted navigation must not publish a mutation"),
    )

    result = rebuild_navigation(vault)
    assert result["changed_pages"] == 0
    assert result["proposed_pages"] > 0
    assert not (vault / "05-MOCs").exists()

    handlers = {}

    class Bus:
        def subscribe(self, event_type, handler, **kwargs):
            if event_type == "wiki_page_updated":
                handlers[kwargs["consumer_id"]] = handler

    monkeypatch.setattr(
        "core.wiki_navigation.rebuild_navigation", lambda _vault: result
    )
    register_wiki_projection_handlers(
        Bus(),
        SimpleNamespace(
            wiki_dir=vault,
            database_dir=tmp_path / "db",
            get=lambda _key, default=None: default,
        ),
    )
    event = _wiki_page_updated_event(vault, tmp_path / "db")
    outcome = handlers["moc_navigation"](event)
    assert outcome.disposition == "defer"
    assert outcome.metadata["proposed_pages"] == result["proposed_pages"]
    assert outcome.metadata["deferred_keys"] == result["proposal_ids"]


def test_wiki_projection_recovers_committed_target_without_duplicate_handler(
    tmp_path,
    monkeypatch,
):
    handlers = {}

    class Bus:
        def subscribe(self, event_type, handler, **kwargs):
            if event_type == "wiki_page_updated":
                handlers[kwargs["consumer_id"]] = handler

    vault = tmp_path / "vault"
    database_dir = tmp_path / "db"
    vault.mkdir()
    event = _wiki_page_updated_event(vault, database_dir)
    apply_calls = 0

    monkeypatch.setattr(
        "core.trust.vault_mutation_service.recover_pending_trusted_markdown_effects",
        lambda **_kwargs: 0,
    )
    monkeypatch.setattr(
        "core.wiki_navigation.plan_navigation",
        lambda _vault: object(),
    )
    monkeypatch.setattr(
        "core.wiki_navigation.navigation_material_action_requests",
        lambda _plan, **_kwargs: (),
    )

    def apply_plan(_plan):
        nonlocal apply_calls
        apply_calls += 1
        (vault / "projection-marker").write_text("committed", encoding="utf-8")
        return {
            "indexed_pages": 1,
            "changed_pages": 1,
            "proposed_pages": 0,
        }

    monkeypatch.setattr("core.wiki_navigation.apply_navigation_plan", apply_plan)
    original_observe = WikiProjectionEffectOracle.observe
    injected = False

    def crash_after_target_journal(self, permit):
        nonlocal injected
        row = self.ledger.material_projection_effect(permit.effect_id)
        if row is not None and row["status"] == "committed" and not injected:
            injected = True
            raise OSError("injected crash before canonical projection receipt")
        return original_observe(self, permit)

    monkeypatch.setattr(
        WikiProjectionEffectOracle,
        "observe",
        crash_after_target_journal,
    )
    register_wiki_projection_handlers(
        Bus(),
        SimpleNamespace(
            wiki_dir=vault,
            database_dir=database_dir,
            get=lambda _key, default=None: default,
        ),
    )

    with pytest.raises(OSError, match="before canonical projection receipt"):
        handlers["moc_navigation"](event)
    recovered = handlers["moc_navigation"](event)

    assert recovered.disposition == "ack"
    assert apply_calls == 1
    with sqlite3.connect(database_dir / "producer_consumer_ledger.db") as conn:
        row = conn.execute(
            """
            SELECT r.status
            FROM cognitive_state_outbox o
            JOIN cognitive_state_effect_receipts r ON r.command_id=o.command_id
            WHERE json_extract(o.payload_json, '$.target_ref') LIKE ?
            """,
            ("wiki-projection:%:moc_navigation",),
        ).fetchone()
    assert row == ("committed",)


@pytest.mark.parametrize("drift_field", ("target_ref", "input_hash"))
def test_wiki_projection_rejects_foreign_nested_target_or_body(
    tmp_path,
    monkeypatch,
    drift_field,
):
    handlers = {}

    class Bus:
        def subscribe(self, event_type, handler, **kwargs):
            if event_type == "wiki_page_updated":
                handlers[kwargs["consumer_id"]] = handler

    vault = tmp_path / "vault"
    database_dir = tmp_path / "db"
    vault.mkdir()
    event = _wiki_page_updated_event(vault, database_dir)
    approved = MaterialActionRequest(
        owner="test_nested_projection",
        executor_id="test_nested_executor",
        action_type="test_nested_effect",
        target_ref="nested-target:approved",
        input_hash="sha256:" + "a" * 64,
        expected_state_db=str(database_dir / "producer_consumer_ledger.db"),
    )
    monkeypatch.setattr(
        "core.trust.vault_mutation_service.recover_pending_trusted_markdown_effects",
        lambda **_kwargs: 0,
    )
    monkeypatch.setattr(
        "core.wiki_navigation.plan_navigation",
        lambda _vault: object(),
    )
    monkeypatch.setattr(
        "core.wiki_navigation.navigation_material_action_requests",
        lambda _plan, **_kwargs: (approved,),
    )
    foreign_effect = tmp_path / f"foreign-wiki-{drift_field}.txt"

    def apply_foreign(_plan):
        target_ref = approved.target_ref
        input_hash = approved.input_hash
        if drift_field == "target_ref":
            target_ref += ":foreign"
        else:
            input_hash = "sha256:" + "b" * 64
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
        raise AssertionError("foreign Wiki effect unexpectedly executed")

    monkeypatch.setattr(
        "core.wiki_navigation.apply_navigation_plan",
        apply_foreign,
    )
    register_wiki_projection_handlers(
        Bus(),
        SimpleNamespace(
            wiki_dir=vault,
            database_dir=database_dir,
            get=lambda _key, default=None: default,
        ),
    )

    with pytest.raises(PermissionError, match="evaluator rejected"):
        handlers["moc_navigation"](event)

    assert not foreign_effect.exists()


def test_relation_embedding_consumer_accepts_fresh_empty_projection(tmp_path):
    handlers = {}

    class Bus:
        def subscribe(self, event_type, handler, **kwargs):
            if event_type == "wiki_page_updated":
                handlers[kwargs["consumer_id"]] = handler

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    database_dir = tmp_path / "db"
    register_wiki_projection_handlers(
        Bus(), SimpleNamespace(wiki_dir=wiki, database_dir=database_dir)
    )

    outcome = handlers["relation_embeddings"](
        _wiki_page_updated_event(wiki, database_dir)
    )
    assert outcome.disposition == "ack"
    assert outcome.metadata["relation_count"] == 0
    assert (database_dir / "knowledge_graph.db").is_file()


def test_kg_consumer_executes_only_preplanned_exact_relation(tmp_path):
    handlers = {}

    class Bus:
        def subscribe(self, event_type, handler, **kwargs):
            if event_type == "wiki_page_updated":
                handlers[kwargs["consumer_id"]] = handler

    class Config:
        def __init__(self, wiki_dir, database_dir):
            self.wiki_dir = wiki_dir
            self.database_dir = database_dir

        def get(self, key, default=None):
            values = {
                "knowledge_graph.implicit_relation_discovery_enabled": False,
                "knowledge_graph.projection_enabled": False,
            }
            return values.get(key, default)

    vault = tmp_path / "wiki"
    database_dir = tmp_path / "db"
    vault.mkdir()
    target = vault / "target.md"
    target.write_text(_page("Target", "stable target page"), encoding="utf-8")
    page = vault / "source.md"
    page.write_text(
        _page("Source", "source page with exact link", "[[target]]"),
        encoding="utf-8",
    )
    initialize_cognitive_state_schema(
        database_dir / "producer_consumer_ledger.db"
    )
    receipt = WikiProjectionLedger(
        database_dir / "wiki_projection.db"
    ).record_mutation(page, mutation_type="create")
    event = Event(
        event_type="wiki_page_updated",
        source="test",
        trace_id=receipt.mutation_id,
        timestamp=receipt.created_at,
        payload={
            "page_path": receipt.page_path,
            "previous_path": receipt.previous_path,
            "page_id": receipt.page_id,
            "page_revision": receipt.page_revision,
            "mutation_id": receipt.mutation_id,
            "mutation_type": receipt.mutation_type,
            "tombstone": receipt.tombstone,
        },
    )
    register_wiki_projection_handlers(
        Bus(),
        Config(vault, database_dir),
    )

    outcome = handlers["knowledge_graph"](event)

    assert outcome.disposition == "ack"
    with sqlite3.connect(database_dir / "knowledge_graph.db") as conn:
        rows = conn.execute(
            "SELECT source, target, relation_type FROM relations"
        ).fetchall()
    assert ("source.md", "target", "references") in rows


def test_wiki_search_consumer_uses_injected_hermetic_embedding_client(
    tmp_path, monkeypatch
):
    handlers = {}

    class Bus:
        def subscribe(self, event_type, handler, **kwargs):
            if event_type == "wiki_page_updated":
                handlers[kwargs["consumer_id"]] = handler

    injected_client = object()
    captured = []

    class Manager:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def build_index(self, force_full=False):
            assert force_full is False
            return {"status": "no_change", "total": 0}

        def audit_coverage(self):
            return {"ok": True, "pages": 0}

    monkeypatch.setattr("core.embeddings.EmbeddingIndexManager", Manager)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    config = SimpleNamespace(wiki_dir=wiki, database_dir=tmp_path / "db")
    register_wiki_projection_handlers(
        Bus(), config, embedding_client=injected_client
    )

    outcome = handlers["wiki_search_index"](
        _wiki_page_updated_event(wiki, tmp_path / "db")
    )

    assert outcome.disposition == "ack"
    assert captured[0]["client"] is injected_client


def test_reconcile_reuses_backup_to_restore_erased_producer_metadata(
    tmp_path,
):
    vault = tmp_path / "vault"
    vault.mkdir()
    page = vault / "report.md"
    page.write_text(
        "---\n"
        "标题: Report\n领域: 测试\n摘要: 报告测试摘要\n"
        "report_id: durable-report\nsource_db: /evidence/report.db\n"
        "---\n# Report\n\nA durable evidence-backed report body records its producer, "
        "source database, recovery contract, validation result, and ownership boundary. "
        "The content is intentionally substantive so metadata restoration is tested "
        "without relying on generated padding to satisfy the Wiki quality budget. "
        "Future maintainers can use these details to verify provenance and repair drift.\n",
        encoding="utf-8",
    )
    backup = tmp_path / "backup"
    assert reconcile(vault, backup, apply=True)["ok"] is True

    damaged = page.read_text(encoding="utf-8")
    damaged = damaged.replace("report_id: durable-report\n", "").replace(
        "source_db: /evidence/report.db\n", ""
    )
    page.write_text(damaged, encoding="utf-8")
    repaired = reconcile(vault, backup, apply=True)

    assert repaired["producer_metadata"]["restored_pages"] == 1
    content = page.read_text(encoding="utf-8")
    assert "report_id: durable-report" in content
    assert "source_db: /evidence/report.db" in content
