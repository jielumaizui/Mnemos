from __future__ import annotations

import inspect
import sqlite3
import shutil
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import wiki_projection_rebuild_state as rebuild_state_module

from scripts.rebuild_wiki_projection_state import (
    _directory_snapshot,
    _embedding_snapshot,
    _full_and_incremental_states,
    _incremental_page_paths,
    _isolated_incremental_comparator,
    _materialize_incremental_mutation,
    _relation_embedding_coverage,
    _relation_embedding_semantic_comparison,
    _record_rebuild_receipt,
    _reset_projection_artifacts,
    _controlled_projection_runtime,
    _run_full_projection_consumers_after_kg,
    _run_full_projection_cycle,
    _run_incremental_projection_cycle,
    _runtime_isolation_guard_state,
    _semantic_projection_hash,
    _table_snapshot,
    _verified_resume_baseline,
    rebuild,
)


def test_rebuild_receipt_closes_retry_without_replacing_its_trace(tmp_path):
    from core.wiki_projection_lifecycle import WikiProjectionLedger

    ledger = WikiProjectionLedger(tmp_path / "projection.db")
    page = tmp_path / "page.md"
    page.write_text("# Page\n", encoding="utf-8")
    mutation = ledger.record_mutation(page, mutation_type="create")
    ledger.record_projection_receipt(
        mutation_id=mutation.mutation_id,
        consumer="wiki_search_index",
        outcome="retry",
        reason="coverage incomplete",
        event_trace_id=mutation.mutation_id,
    )

    _record_rebuild_receipt(
        ledger,
        mutation_id=mutation.mutation_id,
        consumer="wiki_search_index",
        outcome="ack",
        reason="rebuild verified coverage",
        event_trace_id="wiki-rebuild-new-state",
    )

    receipt = ledger.terminal_projection_receipt(
        mutation.mutation_id, "wiki_search_index"
    )
    assert receipt is not None
    assert receipt["event_trace_id"] == mutation.mutation_id


def test_production_incremental_replay_closes_global_kg_dependencies(
    tmp_path, monkeypatch
):
    from core.wiki_projection_lifecycle import WikiProjectionLedger

    database_dir = tmp_path / "database"
    wiki_dir = tmp_path / "wiki"
    database_dir.mkdir()
    wiki_dir.mkdir()
    page = wiki_dir / "page.md"
    page.write_text("# Current page\n", encoding="utf-8")
    WikiProjectionLedger(database_dir / "wiki_projection.db")
    closure_calls = []

    class DeferredReplay:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    class FakeHandler:
        def deferred_page_update_replay(self):
            return DeferredReplay()

        def on_page_updated(self, _payload):
            return {"status": "ok"}

        def reconcile_pages(self, paths, *, replace_existing=False):
            closure_calls.append((list(paths), replace_existing))
            return {"status": "ok", "errors": []}

    class FakeMetrics:
        def __init__(self, **_kwargs):
            pass

        def reconcile_page_lifecycle(self, **_kwargs):
            return {"status": "ok"}

        def close(self):
            pass

    class FakeCognitiveUpdater:
        def __init__(self, **_kwargs):
            pass

        def on_wiki_page_updated(self, _event):
            from core.event_outcome import HandlerOutcome

            return HandlerOutcome.noop("cognitive_graph")

    class FakeSearch:
        def __init__(self, **_kwargs):
            pass

        def build_index(self, **_kwargs):
            return {"status": "ok"}

        def audit_coverage(self):
            return {"ok": True}

    monkeypatch.setattr("scripts.rebuild_wiki_projection_state.WikiMetrics", FakeMetrics)
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.CognitiveGraphStore",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.CognitiveGraphUpdater",
        FakeCognitiveUpdater,
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._relation_embedding_coverage",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.EmbeddingIndexManager", FakeSearch
    )

    result = _run_incremental_projection_cycle(
        SimpleNamespace(database_dir=database_dir, wiki_dir=wiki_dir),
        kg_handler=FakeHandler(),
        mutations=[
            {
                "sequence_no": 1,
                "mutation_id": "mutation-1",
                "page_id": "page-1",
                "page_revision": "revision-1",
                "page_path": str(page),
                "previous_path": "",
                "mutation_type": "update",
                "tombstone": False,
            }
        ],
        rebuild_moc=False,
        materialize_mutations=False,
    )

    assert closure_calls == [([page], True)]
    assert result["kg_dependency_closure"]["required"] is True
    assert result["kg_dependency_closure"]["changed_source_pages"] == 1


def test_embedding_snapshot_changes_when_vector_bytes_change(tmp_path):
    db = tmp_path / "knowledge_graph.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE relation_context_embeddings (
                   relation_id INTEGER PRIMARY KEY,
                   embedding TEXT NOT NULL,
                   model_version TEXT NOT NULL
               )"""
        )
        conn.execute(
            "INSERT INTO relation_context_embeddings VALUES (1, '[0.1, 0.2]', 'test-model')"
        )
        conn.commit()

    first = _embedding_snapshot(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE relation_context_embeddings SET embedding='[0.1, 0.3]' WHERE relation_id=1"
        )
        conn.commit()
    second = _embedding_snapshot(db)

    assert first["rows"] == second["rows"] == 1
    assert first["sha256"] != second["sha256"]


def test_projection_snapshot_opens_sqlite_read_only(tmp_path, monkeypatch):
    db = tmp_path / "knowledge_graph.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE relations (id INTEGER PRIMARY KEY, source TEXT)"
        )
        conn.execute("INSERT INTO relations VALUES (1, 'source')")
        conn.commit()
    finally:
        conn.close()
    Path(str(db) + "-wal").unlink(missing_ok=True)
    Path(str(db) + "-shm").unlink(missing_ok=True)
    before = set(tmp_path.iterdir())
    real_connect = sqlite3.connect
    calls = []

    def recording_connect(database, *args, **kwargs):
        calls.append((str(database), bool(kwargs.get("uri"))))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(rebuild_state_module.sqlite3, "connect", recording_connect)

    snapshot = _table_snapshot(db, "relations")

    assert snapshot["rows"] == 1
    assert calls
    assert all(uri and "mode=ro" in database for database, uri in calls)
    assert set(tmp_path.iterdir()) == before


def test_embedding_semantic_snapshot_detects_equal_norm_direction_change(tmp_path):
    db = tmp_path / "knowledge_graph.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE relation_context_embeddings (
                   relation_id INTEGER PRIMARY KEY,
                   embedding TEXT NOT NULL,
                   model_version TEXT NOT NULL
               )"""
        )
        conn.execute(
            "INSERT INTO relation_context_embeddings VALUES (1, '[1.0, 0.0]', 'model')"
        )
        conn.commit()

    first = _embedding_snapshot(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE relation_context_embeddings SET embedding='[0.0, 1.0]' WHERE relation_id=1"
        )
        conn.commit()
    second = _embedding_snapshot(db)

    assert first["semantic_sha256"] != second["semantic_sha256"]


def test_relation_embedding_semantic_comparison_rejects_wrong_direction(tmp_path):
    expected = tmp_path / "expected.db"
    actual = tmp_path / "actual.db"
    for path, vector in ((expected, "[1.0, 0.0]"), (actual, "[0.0, 1.0]")):
        with sqlite3.connect(path) as conn:
            conn.execute(
                """CREATE TABLE relation_context_embeddings (
                       relation_id INTEGER PRIMARY KEY,
                       embedding TEXT NOT NULL,
                       model_version TEXT NOT NULL
                   )"""
            )
            conn.execute(
                "INSERT INTO relation_context_embeddings VALUES (1, ?, 'model')",
                (vector,),
            )
            conn.commit()

    comparison = _relation_embedding_semantic_comparison(expected, actual)

    assert comparison["equal"] is False
    assert comparison["below_threshold"] == 1
    assert comparison["minimum_cosine"] == 0.0


def test_relation_embedding_semantics_use_relation_identity_not_surrogate_id(tmp_path):
    expected = tmp_path / "expected.db"
    actual = tmp_path / "actual.db"
    for path, relation_id in ((expected, 7), (actual, 7007)):
        with sqlite3.connect(path) as conn:
            conn.execute(
                """CREATE TABLE relations (
                       id INTEGER PRIMARY KEY,
                       source TEXT NOT NULL,
                       target TEXT NOT NULL,
                       relation_type TEXT NOT NULL
                   )"""
            )
            conn.execute(
                """CREATE TABLE relation_context_embeddings (
                       relation_id INTEGER PRIMARY KEY,
                       embedding TEXT NOT NULL,
                       model_version TEXT NOT NULL
                   )"""
            )
            conn.execute(
                "INSERT INTO relations VALUES (?, 'source.md', 'target.md', 'depends_on')",
                (relation_id,),
            )
            conn.execute(
                "INSERT INTO relation_context_embeddings VALUES (?, '[1.0, 0.0]', 'model')",
                (relation_id,),
            )
            conn.commit()

    comparison = _relation_embedding_semantic_comparison(expected, actual)

    assert comparison["equal"] is True
    assert comparison["matched"] == 1
    assert comparison["missing"] == comparison["orphan"] == 0


def test_relation_table_semantics_ignore_surrogate_ids_but_keep_raw_evidence(tmp_path):
    snapshots = []
    for filename, relation_id, evidence_id in (
        ("first.db", 7, 11),
        ("second.db", 7007, 11011),
    ):
        db = tmp_path / filename
        with sqlite3.connect(db) as conn:
            conn.execute(
                """CREATE TABLE relations (
                       id INTEGER PRIMARY KEY,
                       source TEXT,
                       target TEXT,
                       relation_type TEXT,
                       context TEXT,
                       created_at TEXT,
                       updated_at TEXT
                   )"""
            )
            conn.execute(
                """CREATE TABLE relation_evidence (
                       id INTEGER PRIMARY KEY,
                       relation_id INTEGER,
                       evidence_type TEXT,
                       content TEXT,
                       created_at TEXT
                   )"""
            )
            conn.execute(
                """INSERT INTO relations VALUES (
                       ?, 'a.md', 'b.md', 'depends_on', 'a depends on b', 'old', 'new'
                   )""",
                (relation_id,),
            )
            conn.execute(
                "INSERT INTO relation_evidence VALUES (?, ?, 'quote', 'same', 'now')",
                (evidence_id, relation_id),
            )
            conn.commit()
        snapshots.append(
            {
                "kg_relations": _table_snapshot(db, "relations"),
                "kg_relation_evidence": _table_snapshot(db, "relation_evidence"),
            }
        )

    assert snapshots[0]["kg_relations"]["sha256"] != snapshots[1]["kg_relations"]["sha256"]
    assert snapshots[0]["kg_relation_evidence"]["sha256"] != snapshots[1]["kg_relation_evidence"]["sha256"]
    assert _semantic_projection_hash(snapshots[0]) == _semantic_projection_hash(
        snapshots[1]
    )


def test_materialize_incremental_mutations_preserves_same_page_move_chain(tmp_path):
    source = tmp_path / "source" / "new.md"
    source.parent.mkdir()
    source.write_text("# Final body\n", encoding="utf-8")
    old = tmp_path / "isolated" / "old.md"
    middle = tmp_path / "isolated" / "nested" / "middle.md"
    final = tmp_path / "isolated" / "new.md"
    old.parent.mkdir()
    old.write_text("# Prestate body\n", encoding="utf-8")

    chain = (
        ({"mutation_type": "move", "page_path": "missing-middle.md", "previous_path": "old.md"}, middle, old),
        ({"mutation_type": "move", "page_path": str(source), "previous_path": "middle.md"}, final, middle),
        ({"mutation_type": "update", "page_path": str(source), "previous_path": ""}, final, final),
    )
    for mutation, target, previous in chain:
        _materialize_incremental_mutation(
            mutation,
            target_path=target,
            previous_path=previous,
        )

    assert not old.exists()
    assert not middle.exists()
    assert final.read_text(encoding="utf-8") == "# Final body\n"


def test_incremental_comparator_regenerates_derived_kg_outputs_instead_of_copying_them(
    tmp_path, monkeypatch
):
    source_wiki = tmp_path / "source-wiki"
    target_wiki = tmp_path / "target-wiki"
    database_dir = tmp_path / "database"
    source_page = source_wiki / "L2.4-KG" / "Entities" / "entity.md"
    target_page = target_wiki / "L2.4-KG" / "Entities" / "entity.md"
    source_page.parent.mkdir(parents=True)
    target_page.parent.mkdir(parents=True)
    database_dir.mkdir()
    source_page.write_text("# Production-derived bytes\n", encoding="utf-8")
    target_page.write_text("# Isolated canonical bytes\n", encoding="utf-8")

    from core.wiki_projection_lifecycle import WikiProjectionLedger

    ledger = WikiProjectionLedger(database_dir / "wiki_projection.db")
    ledger.record_mutation(target_page, mutation_type="create")
    materialized = []
    metrics_calls = []

    class DeferredReplay:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            target_page.unlink()
            ledger.record_mutation(target_page, mutation_type="delete")
            return False

    class FakeHandler:
        def __init__(self):
            self.calls = []

        def deferred_page_update_replay(self):
            return DeferredReplay()

        def on_page_updated(self, payload):
            self.calls.append(payload)
            return {"status": "skipped"}

    class FakeMetrics:
        def __init__(self, **_kwargs):
            pass

        def reconcile_page_lifecycle(self, **_kwargs):
            metrics_calls.append(_kwargs)
            return {"status": "ok"}

        def close(self):
            pass

    class FakeCognitiveUpdater:
        def __init__(self, **_kwargs):
            pass

        def on_wiki_page_updated(self, _event):
            from core.event_outcome import HandlerOutcome

            return HandlerOutcome.noop("cognitive_graph")

    class FakeSearch:
        def __init__(self, **_kwargs):
            pass

        def build_index(self, **_kwargs):
            return {"status": "ok"}

        def audit_coverage(self):
            return {"ok": True}

    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._materialize_incremental_mutation",
        lambda *args, **kwargs: materialized.append((args, kwargs)),
    )
    monkeypatch.setattr("scripts.rebuild_wiki_projection_state.WikiMetrics", FakeMetrics)
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.CognitiveGraphStore", lambda **_kwargs: object()
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.CognitiveGraphUpdater",
        FakeCognitiveUpdater,
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._relation_embedding_coverage",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.rebuild_navigation",
        lambda *_args, **_kwargs: {"mode": "test"},
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.EmbeddingIndexManager", FakeSearch
    )
    handler = FakeHandler()

    result = _run_incremental_projection_cycle(
        SimpleNamespace(database_dir=database_dir, wiki_dir=target_wiki),
        kg_handler=handler,
        mutations=[
            {
                "sequence_no": 1,
                "mutation_id": "derived-output-1",
                "page_id": "derived-page-1",
                "page_revision": "revision-1",
                "page_path": str(source_page),
                "previous_path": "",
                "mutation_type": "update",
                "tombstone": False,
            }
        ],
        source_wiki_dir=source_wiki,
        materialize_mutations=True,
    )

    assert materialized == []
    assert handler.calls == []
    assert result["consumer_counts"]["derived_inputs_regenerated"] == 1
    assert result["consumer_counts"]["canonical_derived_outputs"] == 1
    assert metrics_calls[-1]["mutation_type"] == "delete"
    assert metrics_calls[-1]["page_path"] == str(target_page)


def test_clean_rebuild_reset_removes_only_rebuildable_projection_artifacts(tmp_path):
    database_dir = tmp_path / "db"
    wiki_dir = tmp_path / "wiki"
    index_dir = database_dir / "embedding_index"
    profile_dir = wiki_dir / ".kg"
    index_dir.mkdir(parents=True)
    profile_dir.mkdir(parents=True)
    removed = (
        database_dir / "knowledge_graph.db",
        database_dir / "wiki_metrics.db",
        index_dir / "relation_index.bin",
        index_dir / "wiki_index.bin",
        index_dir / "wiki_meta.json",
    )
    preserved = (
        database_dir / "wiki_projection.db",
        database_dir / "cognitive_graph.db",
        profile_dir / "profiles.db",
    )
    for path in (*removed, *preserved):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"state")

    result = _reset_projection_artifacts(
        SimpleNamespace(database_dir=database_dir, wiki_dir=wiki_dir)
    )

    assert set(result["removed"]) == {str(path) for path in removed}
    assert all(not path.exists() for path in removed)
    assert all(path.exists() for path in preserved)


def test_directory_snapshot_changes_with_moc_content_not_mtime(tmp_path):
    nav = tmp_path / "05-MOCs" / "Mnemos-Navigation"
    nav.mkdir(parents=True)
    page = nav / "Vault.md"
    page.write_text("# Stable\n", encoding="utf-8")
    first = _directory_snapshot(nav)
    page.touch()
    second = _directory_snapshot(nav)
    page.write_text("# Changed\n", encoding="utf-8")
    third = _directory_snapshot(nav)

    assert first == second
    assert first["sha256"] != third["sha256"]


def test_relation_embedding_coverage_detects_missing_relation_vector(tmp_path):
    db = tmp_path / "knowledge_graph.db"
    index = tmp_path / "relation_index.bin"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE relations (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE relation_context_embeddings (relation_id INTEGER PRIMARY KEY)"
        )
        conn.executemany("INSERT INTO relations(id) VALUES (?)", [(1,), (2,)])
        conn.execute("INSERT INTO relation_context_embeddings(relation_id) VALUES (1)")
        conn.commit()

    missing = _relation_embedding_coverage(db, index)
    assert missing["ok"] is False
    assert missing["missing_embeddings"] == 1

    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO relation_context_embeddings(relation_id) VALUES (2)")
        conn.commit()
    index.write_bytes(b"index")
    assert _relation_embedding_coverage(db, index)["ok"] is True


def test_empty_incremental_scan_stays_empty_instead_of_falling_back_to_full_vault():
    assert _incremental_page_paths({"mutations": []}) == []


def test_comparator_uses_full_pass_and_immediate_incremental_replay():
    cycles = [
        {"state": {"sha256": "full"}},
        {"state": {"sha256": "incremental"}},
        {"state": {"sha256": "later-fixed-point"}},
    ]

    full, incremental = _full_and_incremental_states(cycles)

    assert full["sha256"] == "full"
    assert incremental["sha256"] == "incremental"


def test_isolated_comparator_clean_builds_prestate_then_materializes_delta(
    tmp_path, monkeypatch
):
    database_dir = tmp_path / "db"
    wiki_dir = tmp_path / "wiki"
    backup_dir = tmp_path / "backup"
    database_dir.mkdir()
    wiki_dir.mkdir()
    backup_dir.mkdir()
    current_page = wiki_dir / "page.md"
    current_page.write_text("# Page", encoding="utf-8")
    with sqlite3.connect(database_dir / "knowledge_graph.db") as conn:
        conn.execute("CREATE TABLE material_target_effects (effect_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO material_target_effects VALUES ('production-effect')")
        conn.commit()
    (backup_dir / "wiki-prestate").mkdir()
    (backup_dir / "wiki-prestate" / "old.md").write_text(
        "# Prestate", encoding="utf-8"
    )
    with sqlite3.connect(backup_dir / "wiki_projection.db") as conn:
        conn.execute(
            """CREATE TABLE wiki_mutations (
                   sequence_no INTEGER PRIMARY KEY,
                   mutation_id TEXT,
                   page_id TEXT,
                   page_path TEXT,
                   tombstone INTEGER
               )"""
        )
        conn.execute(
            "INSERT INTO wiki_mutations VALUES (1, 'baseline-1', 'page-old', ?, 0)",
            (str(wiki_dir / "old.md"),),
        )
        conn.commit()
    calls = []

    def run_full(cfg, **kwargs):
        from core.ops.config_scope import current_config

        calls.append(("full", cfg, kwargs, current_config()))
        return {"mode": "clean_baseline"}

    def run_incremental(cfg, **kwargs):
        from core.ops.config_scope import current_config

        calls.append(("incremental", cfg, kwargs, current_config()))
        return {"mode": "incremental"}

    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._run_full_projection_cycle",
        run_full,
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._run_incremental_projection_cycle",
        run_incremental,
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._projection_state",
        lambda _cfg: {
            "sha256": "raw-different",
            "semantic_sha256": "same",
            "tables": {},
        },
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._relation_embedding_semantic_comparison",
        lambda *_args, **_kwargs: {"equal": True},
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.compare_hnsw_indexes",
        lambda *_args, **_kwargs: {"equal": True},
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.relation_index_integrity",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.relation_label_map",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.wiki_label_map",
        lambda *_args, **_kwargs: {},
    )
    isolation = {}

    class FakeEventBus:
        def __init__(self, *, config, **kwargs):
            isolation["event_config"] = config
            isolation["event_kwargs"] = kwargs
            isolation["event_closed"] = False

        def close(self):
            isolation["event_closed"] = True

    class FakeLifecycle:
        def __init__(self, vault_dir, *, ledger, event_bus):
            isolation["lifecycle_vault"] = Path(vault_dir)
            isolation["lifecycle_ledger"] = ledger
            isolation["lifecycle_event_bus"] = event_bus

    class FakeHandler:
        def __init__(self, **kwargs):
            isolation["handler_kwargs"] = kwargs

    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.EventBus", FakeEventBus
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.DerivedProjectionLifecycle",
        FakeLifecycle,
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.KGEventHandler", FakeHandler
    )

    result = _isolated_incremental_comparator(
        SimpleNamespace(database_dir=database_dir, wiki_dir=wiki_dir),
        backup_dir=backup_dir,
        mutations=[
            {
                "sequence_no": 2,
                "mutation_id": "m1",
                "page_id": "page-current",
                "page_path": str(current_page),
                "previous_path": "",
                "mutation_type": "create",
                "tombstone": False,
            }
        ],
        full_state={"sha256": "raw-full", "semantic_sha256": "same", "tables": {}},
    )

    assert result["isolated"] is True
    assert result["equal"] is True
    assert result["prestate_artifacts"] == []
    assert result["baseline_mode"] == "clean_prestate_full_then_materialized_delta"
    target_root = backup_dir / "clean-incremental-comparator"
    assert not (target_root / "database" / "material_target_effects.db").exists()
    assert (target_root / "wiki" / "old.md").is_file()
    assert not (target_root / "wiki" / "page.md").exists()
    assert calls[0][0] == "full"
    assert calls[0][2]["page_paths"] == [target_root / "wiki" / "old.md"]
    assert calls[0][2]["publish_moc_mutations"] is False
    assert calls[1][0] == "incremental"
    assert calls[1][2]["rebuild_moc"] is True
    assert calls[1][2]["materialize_mutations"] is True
    assert calls[1][2]["publish_moc_mutations"] is False
    assert calls[1][2]["source_wiki_dir"] == wiki_dir
    isolated_cfg = calls[0][1]
    assert calls[1][1] is isolated_cfg
    assert isolated_cfg.database_dir == target_root / "database"
    assert isolated_cfg.mnemos_dir == target_root / "database"
    assert isolated_cfg.wiki_dir == target_root / "wiki"
    assert isolation["event_config"] is isolated_cfg
    assert isolation["event_kwargs"] == {
        "run_startup_maintenance": False,
        "recover_pending": False,
        "enqueue_published_events": False,
    }
    assert isolation["event_closed"] is True
    assert isolation["lifecycle_vault"] == target_root / "wiki"
    assert isolation["lifecycle_ledger"].db_path == (
        target_root / "database" / "wiki_projection.db"
    )
    assert isolation["handler_kwargs"]["config"] is isolated_cfg
    assert isolation["handler_kwargs"]["projection_lifecycle"] is not None
    isolated_embedding_client = isolation["handler_kwargs"]["embedding_client"]
    assert isolated_embedding_client._config is isolated_cfg
    assert calls[0][2]["embedding_client"] is isolated_embedding_client
    assert calls[1][2]["embedding_client"] is isolated_embedding_client
    assert (
        isolation["handler_kwargs"]["emit_projection_runtime_consumption"]
        is False
    )
    assert result["production_isolation_guard"]["equal"] is True
    assert (
        result["production_isolation_guard"]["before"]
        == result["production_isolation_guard"]["after"]
    )
    assert calls[0][3] is isolated_cfg
    assert calls[1][3] is isolated_cfg
    assert set(result["production_isolation_guard"]["before"]) == {
        "events",
        "wiki_projection",
        "producer_consumer",
        "trusted_push",
        "model_call_ledger",
        "embedding_cache",
    }


def test_runtime_isolation_guard_tracks_model_ledger_and_embedding_cache_updates(
    tmp_path,
):
    with sqlite3.connect(tmp_path / "model_call_ledger.db") as conn:
        conn.execute(
            """CREATE TABLE model_call_runs (
                   run_id TEXT PRIMARY KEY,
                   cost_budget REAL NOT NULL,
                   created_at TEXT NOT NULL,
                   schema_version TEXT NOT NULL
               )"""
        )
        conn.execute(
            "INSERT INTO model_call_runs VALUES ('run-1', 1.0, 'fixed', 'v1')"
        )
        conn.commit()
    with sqlite3.connect(tmp_path / "embedding_cache.db") as conn:
        conn.execute(
            """CREATE TABLE embedding_cache (
                   content_hash TEXT PRIMARY KEY,
                   embedding TEXT NOT NULL,
                   model_version TEXT NOT NULL,
                   token_count INTEGER NOT NULL,
                   created_at TEXT NOT NULL,
                   last_used_at TEXT NOT NULL,
                   hit_count INTEGER NOT NULL,
                   last_hit_at TEXT
               )"""
        )
        conn.execute(
            """INSERT INTO embedding_cache
               VALUES ('hash-1', '[0.1]', 'model', 1, 'fixed', 'fixed', 0, NULL)"""
        )
        conn.commit()
    cfg = SimpleNamespace(
        database_dir=tmp_path,
        mnemos_dir=tmp_path,
        wiki_dir=tmp_path / "wiki",
    )

    before = _runtime_isolation_guard_state(cfg)
    with sqlite3.connect(tmp_path / "model_call_ledger.db") as conn:
        conn.execute(
            "UPDATE model_call_runs SET cost_budget=2.0 WHERE run_id='run-1'"
        )
        conn.commit()
    after_model_update = _runtime_isolation_guard_state(cfg)
    with sqlite3.connect(tmp_path / "embedding_cache.db") as conn:
        conn.execute(
            "UPDATE embedding_cache SET hit_count=1 WHERE content_hash='hash-1'"
        )
        conn.commit()
    after_cache_update = _runtime_isolation_guard_state(cfg)

    assert before["model_call_ledger"] != after_model_update["model_call_ledger"]
    assert (
        after_model_update["embedding_cache"]
        != after_cache_update["embedding_cache"]
    )


def test_full_projection_cycle_forwards_moc_publication_policy(monkeypatch, tmp_path):
    calls = []

    class FakeHandler:
        def reconcile_pages(self, page_paths):
            calls.append(("kg", page_paths))
            return {"status": "ok"}

    def finish_consumers(cfg, **kwargs):
        calls.append(("consumers", cfg, kwargs))
        return {"status": "ok"}

    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._run_full_projection_consumers_after_kg",
        finish_consumers,
    )
    cfg = SimpleNamespace(database_dir=tmp_path, wiki_dir=tmp_path / "wiki")
    page = tmp_path / "wiki" / "page.md"

    result = _run_full_projection_cycle(
        cfg,
        kg_handler=FakeHandler(),
        page_paths=[page],
        embedding_client="isolated-client",
        publish_moc_mutations=False,
    )

    assert result == {"status": "ok"}
    assert calls[0] == ("kg", [page])
    assert calls[1][1] is cfg
    assert calls[1][2]["embedding_client"] == "isolated-client"
    assert calls[1][2]["publish_moc_mutations"] is False


def test_full_projection_consumers_can_disable_moc_event_publication(
    monkeypatch, tmp_path
):
    navigation_calls = []

    class FakeMetrics:
        def __init__(self, **_kwargs):
            pass

        def scan_all_pages(self):
            return {"status": "ok"}

        def close(self):
            pass

    class FakeSearch:
        def __init__(self, **_kwargs):
            pass

        def build_index(self, *, force_full):
            assert force_full is True
            return {"status": "ok"}

        def audit_coverage(self):
            return {"ok": True}

    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._relation_embedding_coverage",
        lambda *_args: {"ok": True},
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.rebuild_navigation",
        lambda _wiki, **kwargs: navigation_calls.append(kwargs)
        or {"page_to_nav": {}},
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.WikiMetrics", FakeMetrics
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.CognitiveGraphStore",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.CognitiveGraphUpdater",
        lambda **_kwargs: SimpleNamespace(reconcile=lambda: {"status": "ok"}),
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.EmbeddingIndexManager", FakeSearch
    )
    cfg = SimpleNamespace(database_dir=tmp_path, wiki_dir=tmp_path / "wiki")

    result = _run_full_projection_consumers_after_kg(
        cfg,
        input_page_count=1,
        kg_result={"status": "ok"},
        embedding_client="isolated-client",
        publish_moc_mutations=False,
    )

    assert result["input_page_count"] == 1
    assert navigation_calls == [{"publish_mutations": False}]


def test_rebuild_apply_requires_offline_migration_lock_but_dry_run_does_not(
    monkeypatch, tmp_path
):
    database_dir = tmp_path / "database"
    wiki_dir = tmp_path / "wiki"
    database_dir.mkdir()
    wiki_dir.mkdir()
    cfg = SimpleNamespace(database_dir=database_dir, wiki_dir=wiki_dir)
    lock_calls = []

    class BlockingLock:
        def __enter__(self):
            raise RuntimeError("offline lock required")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.get_config", lambda: cfg
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._projection_state",
        lambda _cfg: {"sha256": "before"},
    )

    def lock(path):
        lock_calls.append(Path(path))
        return BlockingLock()

    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.offline_migration_lock", lock
    )

    preview = rebuild(apply=False)
    assert preview["applied"] is False
    assert lock_calls == []

    with pytest.raises(RuntimeError, match="offline lock required"):
        rebuild(apply=True)
    assert lock_calls == [database_dir]


def test_rebuild_reuses_the_exact_config_instance_inside_offline_lock(
    monkeypatch, tmp_path
):
    database_dir = tmp_path / "database"
    wiki_dir = tmp_path / "wiki"
    database_dir.mkdir()
    wiki_dir.mkdir()
    cfg = SimpleNamespace(database_dir=database_dir, wiki_dir=wiki_dir)
    config_calls = []

    class YieldingLock:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    def get_cfg():
        config_calls.append(cfg)
        return cfg

    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.get_config", get_cfg
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.offline_migration_lock",
        lambda _path: YieldingLock(),
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._projection_state",
        lambda _cfg: (_ for _ in ()).throw(RuntimeError("inside lock")),
    )

    with pytest.raises(RuntimeError, match="inside lock"):
        rebuild(apply=True)
    assert config_calls == [cfg]


def test_rebuild_public_api_exposes_no_lock_bypass_parameter():
    assert set(inspect.signature(rebuild).parameters) == {
        "apply",
        "backup_dir",
        "resume",
        "resume_replay_after_sequence",
    }


def test_controlled_projection_runtime_never_recovers_or_dispatches_events(
    monkeypatch, tmp_path
):
    observed = {}

    class FakeClient:
        def close(self):
            observed["client_closed"] = True

    class FakeBus:
        def __init__(self, *, config, **kwargs):
            observed["bus_config"] = config
            observed["bus_kwargs"] = kwargs

        def close(self):
            observed["bus_closed"] = True

    class FakeLifecycle:
        def __init__(self, vault_dir, *, ledger, event_bus):
            observed["lifecycle"] = (Path(vault_dir), ledger, event_bus)

    class FakeHandler:
        def __init__(self, **kwargs):
            observed["handler_kwargs"] = kwargs

    client = FakeClient()
    ledger = object()
    cfg = SimpleNamespace(database_dir=tmp_path, wiki_dir=tmp_path / "wiki")
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._new_isolated_embedding_client",
        lambda _cfg: client,
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.EventBus", FakeBus
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.DerivedProjectionLifecycle",
        FakeLifecycle,
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.KGEventHandler", FakeHandler
    )

    with _controlled_projection_runtime(cfg, ledger=ledger) as runtime:
        assert runtime.kg_handler is not None
        assert runtime.embedding_client is client
        assert observed["bus_kwargs"] == {
            "run_startup_maintenance": False,
            "recover_pending": False,
            "enqueue_published_events": False,
        }
        assert observed["handler_kwargs"]["config"] is cfg
        assert observed["handler_kwargs"]["projection_lifecycle"] is not None
        assert observed["handler_kwargs"]["emit_projection_runtime_consumption"] is False

    assert observed["bus_closed"] is True
    assert observed["client_closed"] is True


def test_isolated_comparator_fails_closed_on_production_event_ledger_write(
    tmp_path, monkeypatch
):
    database_dir = tmp_path / "db"
    wiki_dir = tmp_path / "wiki"
    backup_dir = tmp_path / "backup"
    database_dir.mkdir()
    wiki_dir.mkdir()
    backup_dir.mkdir()
    (backup_dir / "wiki-prestate").mkdir()
    with sqlite3.connect(backup_dir / "wiki_projection.db") as conn:
        conn.execute(
            """CREATE TABLE wiki_mutations (
                   sequence_no INTEGER PRIMARY KEY,
                   mutation_id TEXT,
                   page_id TEXT,
                   page_path TEXT,
                   tombstone INTEGER
               )"""
        )
        conn.commit()
    with sqlite3.connect(database_dir / "events.db") as conn:
        conn.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO events VALUES (1, 'before')")
        conn.commit()

    class FakeEventBus:
        def __init__(self, **_kwargs):
            pass

        def close(self):
            pass

    class FakeLifecycle:
        def __init__(self, *_args, **_kwargs):
            pass

    class FakeHandler:
        def __init__(self, **_kwargs):
            pass

    def leak_to_production(_cfg, **_kwargs):
        with sqlite3.connect(database_dir / "events.db") as conn:
            conn.execute("INSERT INTO events VALUES (2, 'leaked')")
            conn.commit()
        return {"mode": "incremental"}

    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.EventBus", FakeEventBus
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.DerivedProjectionLifecycle",
        FakeLifecycle,
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.KGEventHandler", FakeHandler
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._run_incremental_projection_cycle",
        leak_to_production,
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._run_full_projection_cycle",
        lambda *_args, **_kwargs: {"mode": "clean_baseline"},
    )

    with pytest.raises(RuntimeError, match="mutated a protected production ledger"):
        _isolated_incremental_comparator(
            SimpleNamespace(
                database_dir=database_dir,
                mnemos_dir=database_dir,
                wiki_dir=wiki_dir,
            ),
            backup_dir=backup_dir,
            mutations=[],
            full_state={"sha256": "full", "semantic_sha256": "full", "tables": {}},
        )


def test_resume_baseline_requires_backup_ledger_to_be_exact_live_prefix(tmp_path):
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    (backup_dir / "wiki-prestate").mkdir()
    backup_ledger = backup_dir / "wiki_projection.db"
    with sqlite3.connect(backup_ledger) as conn:
        conn.execute(
            "CREATE TABLE wiki_mutations (sequence_no INTEGER PRIMARY KEY, mutation_id TEXT)"
        )
        conn.executemany(
            "INSERT INTO wiki_mutations VALUES (?, ?)",
            [(1, "mutation-1"), (2, "mutation-2")],
        )
        conn.commit()
    live_ledger = tmp_path / "wiki_projection.db"
    shutil.copy2(backup_ledger, live_ledger)
    with sqlite3.connect(live_ledger) as conn:
        conn.execute("INSERT INTO wiki_mutations VALUES (3, 'mutation-3')")
        conn.commit()

    verified = _verified_resume_baseline(
        backup_dir=backup_dir,
        live_ledger_path=live_ledger,
    )

    assert verified["baseline_sequence"] == 2
    assert verified["baseline_rows"] == 2
    assert verified["live_sequence"] == 3

    with sqlite3.connect(live_ledger) as conn:
        conn.execute(
            "UPDATE wiki_mutations SET mutation_id='changed' WHERE sequence_no=2"
        )
        conn.commit()
    with pytest.raises(ValueError, match="immutable prefix"):
        _verified_resume_baseline(
            backup_dir=backup_dir,
            live_ledger_path=live_ledger,
        )


@pytest.mark.parametrize("isolated_equal", [True, False])
def test_resume_rebuild_reuses_verified_kg_without_backup_or_reset(
    tmp_path, monkeypatch, isolated_equal
):
    database_dir = tmp_path / "database"
    wiki_dir = tmp_path / "wiki"
    backup_dir = tmp_path / "backup"
    database_dir.mkdir()
    wiki_dir.mkdir()
    backup_dir.mkdir()
    page = wiki_dir / "page.md"
    page.write_text("# Page\n", encoding="utf-8")
    (backup_dir / "wiki-prestate").mkdir()
    backup_ledger = backup_dir / "wiki_projection.db"
    with sqlite3.connect(backup_ledger) as conn:
        conn.execute(
            "CREATE TABLE wiki_mutations (sequence_no INTEGER PRIMARY KEY, mutation_id TEXT)"
        )
        conn.execute("INSERT INTO wiki_mutations VALUES (1, 'mutation-1')")
        conn.commit()
    shutil.copy2(backup_ledger, database_dir / "wiki_projection.db")

    mutation = {
        "mutation_id": "mutation-1",
        "page_id": "page-1",
        "page_revision": "revision-1",
        "mutation_type": "update",
        "page_path": str(page),
        "previous_path": "",
        "sequence_no": 1,
    }

    class FakeLedger:
        def __init__(self, _path):
            pass

        def list_mutations(self):
            return [mutation]

        def reconcile_vault(self, _wiki_dir):
            return {"recorded_mutations": 0, "mutations": []}

        def reconciliation_report(self):
            return {"ok": True, "projection_gap": 0}

    table_names = (
        "kg_relations",
        "cognitive_relations",
        "relation_embeddings",
        "relation_hnsw",
        "wiki_search_meta",
        "wiki_search_hnsw",
        "wiki_metrics",
        "moc_navigation",
    )
    partial_state = {
        "sha256": "partial",
        "semantic_sha256": "partial-semantic",
        "tables": {name: {"sha256": "partial"} for name in table_names},
    }
    stable_state = {
        "sha256": "stable",
        "semantic_sha256": "stable-semantic",
        "tables": {name: {"sha256": "stable"} for name in table_names},
    }
    states = iter((partial_state, stable_state, stable_state))
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.get_config",
        lambda: SimpleNamespace(database_dir=database_dir, wiki_dir=wiki_dir),
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.offline_migration_lock",
        lambda _path: nullcontext(),
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.WikiProjectionLedger", FakeLedger
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.KGEventHandler", lambda **_kwargs: object()
    )
    runtime = SimpleNamespace(kg_handler=object(), embedding_client=object())

    class RuntimeContext:
        def __enter__(self):
            return runtime

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._controlled_projection_runtime",
        lambda *_args, **_kwargs: RuntimeContext(),
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._projection_state",
        lambda _cfg: next(states),
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._run_full_projection_consumers_after_kg",
        lambda _cfg, **_kwargs: {"input_page_count": 1, "mode": "resume"},
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._run_incremental_projection_cycle",
        lambda _cfg, **_kwargs: {"input_page_count": 0, "mode": "incremental"},
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._isolated_incremental_comparator",
        lambda *_args, **_kwargs: {"equal": isolated_equal},
    )
    receipt_calls = []
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._record_rebuild_receipt",
        lambda *_args, **kwargs: receipt_calls.append(kwargs),
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._backup_state",
        lambda *_args, **_kwargs: pytest.fail("resume must not replace its backup"),
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._reset_projection_artifacts",
        lambda *_args, **_kwargs: pytest.fail("resume must not reset projections"),
    )

    result = rebuild(apply=True, backup_dir=backup_dir, resume=True)

    assert result["resume_validation"]["baseline_sequence"] == 1
    assert result["clean_projection_reset"]["skipped"] is True
    assert result["cycles"][0]["input_mode"] == "clean_full_resume_after_kg"
    if isolated_equal:
        assert result["ok"] is True
        assert len(receipt_calls) == 6
    else:
        assert result["ok"] is False
        assert "isolated incremental comparator differs" in result["error"]
        assert receipt_calls == []


def test_resume_stops_before_isolated_comparator_when_immediate_replay_differs(
    tmp_path, monkeypatch
):
    database_dir = tmp_path / "database"
    wiki_dir = tmp_path / "wiki"
    backup_dir = tmp_path / "backup"
    database_dir.mkdir()
    wiki_dir.mkdir()
    backup_dir.mkdir()
    (backup_dir / "wiki-prestate").mkdir()
    backup_ledger = backup_dir / "wiki_projection.db"
    with sqlite3.connect(backup_ledger) as conn:
        conn.execute(
            "CREATE TABLE wiki_mutations (sequence_no INTEGER PRIMARY KEY, mutation_id TEXT)"
        )
        conn.commit()
    shutil.copy2(backup_ledger, database_dir / "wiki_projection.db")
    with sqlite3.connect(database_dir / "wiki_projection.db") as conn:
        conn.execute("INSERT INTO wiki_mutations VALUES (1, 'mutation-1')")
        conn.commit()
    page = wiki_dir / "page.md"
    page.write_text("# Page\n", encoding="utf-8")
    mutation = {
        "mutation_id": "mutation-1",
        "page_id": "page-1",
        "page_revision": "revision-1",
        "mutation_type": "update",
        "page_path": str(page),
        "previous_path": "",
        "sequence_no": 1,
    }

    class FakeLedger:
        def __init__(self, _path):
            pass

        def list_mutations(self):
            return [mutation]

        def reconcile_vault(self, _wiki_dir):
            return {"recorded_mutations": 0, "mutations": []}

    table_names = (
        "kg_relations",
        "cognitive_relations",
        "relation_embeddings",
        "relation_hnsw",
        "wiki_search_meta",
        "wiki_search_hnsw",
        "wiki_metrics",
        "moc_navigation",
    )

    def state(name):
        return {
            "sha256": name,
            "semantic_sha256": name,
            "tables": {table: {"sha256": name} for table in table_names},
        }

    states = iter((state("before"), state("full"), state("incremental"), state("incremental")))
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.get_config",
        lambda: SimpleNamespace(database_dir=database_dir, wiki_dir=wiki_dir),
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.offline_migration_lock",
        lambda _path: nullcontext(),
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.WikiProjectionLedger", FakeLedger
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state.KGEventHandler", lambda **_kwargs: object()
    )
    runtime = SimpleNamespace(kg_handler=object(), embedding_client=object())

    class RuntimeContext:
        def __enter__(self):
            return runtime

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._controlled_projection_runtime",
        lambda *_args, **_kwargs: RuntimeContext(),
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._projection_state",
        lambda _cfg: next(states),
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._run_full_projection_consumers_after_kg",
        lambda _cfg, **_kwargs: {"input_page_count": 0},
    )
    incremental_calls = []
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._run_incremental_projection_cycle",
        lambda _cfg, **kwargs: incremental_calls.append(kwargs)
        or {"input_page_count": len(kwargs["mutations"])},
    )
    monkeypatch.setattr(
        "scripts.rebuild_wiki_projection_state._isolated_incremental_comparator",
        lambda *_args, **_kwargs: pytest.fail(
            "a known full/incremental mismatch must stop before the expensive comparator"
        ),
    )

    result = rebuild(
        apply=True,
        backup_dir=backup_dir,
        resume=True,
        resume_replay_after_sequence=0,
    )

    assert result["comparison"]["equal"] is False
    assert result["ok"] is False
    assert "immediate incremental replay differs" in result["error"]
    assert incremental_calls[0]["mutations"] == [mutation]
