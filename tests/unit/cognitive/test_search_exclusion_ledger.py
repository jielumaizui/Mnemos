import sqlite3

import pytest

from core.cognitive.search_exclusion_ledger import (
    initialize_search_exclusion_ledger,
    insert_search_exclusion,
    inventory_search_exclusions,
    iter_search_exclusion_candidates,
    load_search_exclusion_keys,
    search_exclusion_coverage,
    validate_search_exclusion_ledger,
)


def _write_wiki(path, *, status="restricted_unknown"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "scope: restricted\n"
        "acl_schema_version: 1\n"
        "acl_metadata_complete: true\n"
        f"acl_reconciliation_status: {status}\n"
        "---\n"
        "historical body\n",
        encoding="utf-8",
    )


def _graph_db(path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE cognitive_relations("
            "id TEXT PRIMARY KEY, stale INTEGER, access_control TEXT, body TEXT)"
        )
        connection.execute(
            "CREATE TABLE canonical_nodes("
            "canonical_id TEXT PRIMARY KEY, access_control TEXT, body TEXT)"
        )
        connection.execute(
            "INSERT INTO cognitive_relations VALUES ('legacy-relation', 0, '', 'secret')"
        )
        connection.execute(
            "INSERT INTO canonical_nodes VALUES ('malformed-node', '{bad', 'secret')"
        )


def _evidence_db(path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE evidence_nodes("
            "id TEXT PRIMARY KEY, access_control TEXT, body TEXT)"
        )
        connection.execute(
            "CREATE TABLE evidence_edges("
            "id INTEGER PRIMARY KEY, access_control TEXT, body TEXT)"
        )
        connection.execute("INSERT INTO evidence_nodes VALUES ('legacy-node', '', 'secret')")
        connection.execute("INSERT INTO evidence_edges VALUES (1, '', 'secret')")


def _candidates(tmp_path):
    wiki = tmp_path / "wiki"
    graph = tmp_path / "graph.db"
    evidence = tmp_path / "evidence.db"
    _write_wiki(wiki / "legacy.md")
    _graph_db(graph)
    _evidence_db(evidence)
    candidates = list(
        iter_search_exclusion_candidates(
            targets=("wiki", "cognitive_graph", "evidence_graph"),
            wiki_dir=wiki,
            cognitive_graph_db=graph,
            evidence_graph_db=evidence,
        )
    )
    return wiki, graph, evidence, candidates


def test_exclusion_inventory_only_accepts_exact_empty_legacy_acl(tmp_path):
    _wiki, _graph, _evidence, candidates = _candidates(tmp_path)

    assert len(candidates) == 4
    assert {(item.channel, item.source_table) for item in candidates} == {
        ("wiki_page", "markdown_page"),
        ("cognitive_graph", "cognitive_relations"),
        ("evidence_graph", "evidence_nodes"),
        ("evidence_graph", "evidence_edges"),
    }
    report = inventory_search_exclusions(candidates)
    assert report["candidate_count"] == 4
    assert report["channel_counts"] == {
        "cognitive_graph": 1,
        "evidence_graph": 2,
        "wiki_page": 1,
    }


def test_exclusion_ledger_is_append_only_and_exact_hash_bound(tmp_path):
    wiki, graph, evidence, candidates = _candidates(tmp_path)
    ledger = tmp_path / "exclusions.db"
    with sqlite3.connect(ledger) as connection:
        initialize_search_exclusion_ledger(connection)
        for candidate in candidates:
            assert insert_search_exclusion(connection, candidate) == "inserted"
            assert insert_search_exclusion(connection, candidate) == "existing"
        connection.commit()
        assert validate_search_exclusion_ledger(connection)["ok"] is True
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE cognitive_search_exclusions SET approval_basis='changed'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM cognitive_search_exclusions")

    keys, validation = load_search_exclusion_keys(ledger)
    coverage = search_exclusion_coverage(
        iter_search_exclusion_candidates(
            targets=("wiki", "cognitive_graph", "evidence_graph"),
            wiki_dir=wiki,
            cognitive_graph_db=graph,
            evidence_graph_db=evidence,
        ),
        exclusion_keys=keys,
    )
    assert validation["ok"] is True
    assert coverage["covered_count"] == 4
    assert coverage["uncovered_count"] == 0

    with sqlite3.connect(graph) as connection:
        connection.execute(
            "UPDATE cognitive_relations SET body='changed' WHERE id='legacy-relation'"
        )
    changed = search_exclusion_coverage(
        iter_search_exclusion_candidates(
            targets=("cognitive_graph",),
            wiki_dir=wiki,
            cognitive_graph_db=graph,
            evidence_graph_db=evidence,
        ),
        exclusion_keys=keys,
    )
    assert changed["covered_count"] == 0
    assert changed["uncovered_count"] == 1


def test_missing_ledger_is_not_treated_as_covered(tmp_path):
    _wiki, _graph, _evidence, candidates = _candidates(tmp_path)

    keys, validation = load_search_exclusion_keys(tmp_path / "missing.db")
    coverage = search_exclusion_coverage(candidates, exclusion_keys=keys)

    assert validation["schema_present"] is False
    assert coverage["covered_count"] == 0
    assert coverage["uncovered_count"] == 4


def test_forged_approval_cannot_cover_an_exact_source_identity(tmp_path):
    _wiki, _graph, _evidence, candidates = _candidates(tmp_path)
    ledger = tmp_path / "forged.db"
    with sqlite3.connect(ledger) as connection:
        initialize_search_exclusion_ledger(connection)
        insert_search_exclusion(connection, candidates[0])
        connection.commit()
        connection.execute("DROP TRIGGER cognitive_search_exclusions_no_update")
        connection.execute(
            "UPDATE cognitive_search_exclusions SET approval_basis='unreviewed'"
        )
        connection.execute(
            """
            CREATE TRIGGER cognitive_search_exclusions_no_update
            BEFORE UPDATE ON cognitive_search_exclusions BEGIN
                SELECT RAISE(ABORT, 'cognitive_search_exclusions is append-only');
            END
            """
        )
        validation = validate_search_exclusion_ledger(connection)

    keys, loaded_validation = load_search_exclusion_keys(ledger)
    assert validation["schema_signature_ok"] is True
    assert validation["semantic_mismatch_count"] == 1
    assert validation["ok"] is False
    assert loaded_validation["ok"] is False
    assert keys == set()
