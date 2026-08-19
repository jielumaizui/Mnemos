from __future__ import annotations

import json
import sqlite3

import pytest

from scripts.wiki_projection_ann_audit import (
    compare_hnsw_indexes,
    compare_retained_hnsw_vectors,
    relation_index_integrity,
    relation_label_map,
)

hnswlib = pytest.importorskip("hnswlib")


def _write_index(path, vectors, labels):
    index = hnswlib.Index(space="cosine", dim=2)
    index.init_index(max_elements=10, ef_construction=20, M=8)
    index.add_items(vectors, labels)
    index.save_index(str(path))


def test_hnsw_semantic_comparison_rejects_wrong_vector_direction(tmp_path):
    expected = tmp_path / "expected.bin"
    actual = tmp_path / "actual.bin"
    _write_index(expected, [[1.0, 0.0]], [3])
    _write_index(actual, [[0.0, 1.0]], [7])

    result = compare_hnsw_indexes(
        expected,
        actual,
        expected_labels={"stable": 3},
        actual_labels={"stable": 7},
        dimension=2,
    )

    assert result["equal"] is False
    assert result["below_threshold"] == 1


def test_relation_index_integrity_rejects_wrong_label_mapping(tmp_path):
    db = tmp_path / "kg.db"
    index_path = tmp_path / "relation.bin"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE relation_context_embeddings (
                   id INTEGER PRIMARY KEY,
                   relation_id INTEGER UNIQUE,
                   embedding TEXT,
                   model_version TEXT
               )"""
        )
        conn.execute(
            "INSERT INTO relation_context_embeddings VALUES (3, 10, ?, 'model')",
            (json.dumps([1.0, 0.0]),),
        )
        conn.commit()
    _write_index(index_path, [[1.0, 0.0]], [4])

    result = relation_index_integrity(db, index_path, dimension=2)

    assert result["ok"] is False
    assert result["label_mismatches"] == 2


def test_relation_label_map_uses_business_identity_when_schema_is_available(tmp_path):
    db = tmp_path / "kg.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE relations (
                   id INTEGER PRIMARY KEY,
                   source TEXT,
                   target TEXT,
                   relation_type TEXT
               )"""
        )
        conn.execute(
            """CREATE TABLE relation_context_embeddings (
                   id INTEGER PRIMARY KEY,
                   relation_id INTEGER UNIQUE,
                   embedding TEXT,
                   model_version TEXT
               )"""
        )
        conn.execute("INSERT INTO relations VALUES (7007, 'a.md', 'b.md', 'depends_on')")
        conn.execute(
            "INSERT INTO relation_context_embeddings VALUES (91, 7007, '[1, 0]', 'model')"
        )
        conn.commit()

    assert relation_label_map(db) == {"a.md\0b.md\0depends_on": 91}


def test_hnsw_semantic_comparison_rejects_duplicate_business_labels(tmp_path):
    expected = tmp_path / "expected.bin"
    actual = tmp_path / "actual.bin"
    _write_index(expected, [[1.0, 0.0], [1.0, 0.0]], [1, 2])
    _write_index(actual, [[1.0, 0.0]], [7])

    result = compare_hnsw_indexes(
        expected,
        actual,
        expected_labels={"page-a": 1, "page-b": 2},
        actual_labels={"page-a": 7, "page-b": 7},
        dimension=2,
    )

    assert result["equal"] is False
    assert result["duplicate_actual_labels"] == 1


def test_retained_vector_comparison_allows_before_index_superset(tmp_path):
    before = tmp_path / "before.bin"
    after = tmp_path / "after.bin"
    _write_index(before, [[1.0, 0.0], [0.0, 1.0]], [3, 9])
    _write_index(after, [[1.0, 0.0]], [0])

    result = compare_retained_hnsw_vectors(
        before,
        after,
        retained_before_labels={"retained": 3},
        after_labels={"retained": 0},
        dimension=2,
    )

    assert result["equal"] is True
    assert result["minimum_cosine"] == 1.0


def test_retained_vector_comparison_rejects_vector_drift(tmp_path):
    before = tmp_path / "before.bin"
    after = tmp_path / "after.bin"
    _write_index(before, [[1.0, 0.0], [0.0, 1.0]], [3, 9])
    _write_index(after, [[0.0, 1.0]], [0])

    result = compare_retained_hnsw_vectors(
        before,
        after,
        retained_before_labels={"retained": 3},
        after_labels={"retained": 0},
        dimension=2,
    )

    assert result["equal"] is False
    assert result["below_threshold"] == 1
