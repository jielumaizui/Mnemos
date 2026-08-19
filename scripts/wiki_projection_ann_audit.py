"""Semantic integrity checks for persisted Wiki projection ANN indexes."""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Iterable

try:
    import hnswlib
except ImportError:  # pragma: no cover - production rebuild requires hnswlib
    hnswlib = None


def _cosine(left: Iterable[float], right: Iterable[float]) -> float:
    left_values = [float(value) for value in left]
    right_values = [float(value) for value in right]
    if len(left_values) != len(right_values):
        return -1.0
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if not left_norm or not right_norm:
        return 1.0 if left_norm == right_norm else 0.0
    return sum(
        left_value * right_value
        for left_value, right_value in zip(left_values, right_values)
    ) / (left_norm * right_norm)


def _load_index(path: Path, *, dimension: int, capacity: int) -> Any:
    if hnswlib is None:
        raise RuntimeError("hnswlib is unavailable")
    index = hnswlib.Index(space="cosine", dim=dimension)
    index.load_index(str(path), max_elements=max(capacity * 2, 100))
    return index


def compare_hnsw_indexes(
    expected_path: Path,
    actual_path: Path,
    *,
    expected_labels: dict[str, int],
    actual_labels: dict[str, int],
    dimension: int = 1024,
    cosine_threshold: float = 0.99,
) -> dict[str, Any]:
    """Compare ANN label sets and vectors through stable business keys."""

    result: dict[str, Any] = {
        "schema_version": "mnemos.hnsw_semantic_comparison.v1",
        "cosine_threshold": cosine_threshold,
        "expected_keys": len(expected_labels),
        "actual_keys": len(actual_labels),
        "missing_keys": 0,
        "orphan_keys": 0,
        "expected_label_mismatches": 0,
        "actual_label_mismatches": 0,
        "duplicate_expected_labels": 0,
        "duplicate_actual_labels": 0,
        "below_threshold": 0,
        "minimum_cosine": None,
        "error": None,
        "equal": False,
    }
    try:
        expected_index = _load_index(
            expected_path, dimension=dimension, capacity=len(expected_labels)
        )
        actual_index = _load_index(
            actual_path, dimension=dimension, capacity=len(actual_labels)
        )
        expected_index_labels = {int(value) for value in expected_index.get_ids_list()}
        actual_index_labels = {int(value) for value in actual_index.get_ids_list()}
        result["duplicate_expected_labels"] = len(expected_labels) - len(
            set(expected_labels.values())
        )
        result["duplicate_actual_labels"] = len(actual_labels) - len(
            set(actual_labels.values())
        )
        result["expected_label_mismatches"] = len(
            expected_index_labels ^ set(expected_labels.values())
        )
        result["actual_label_mismatches"] = len(
            actual_index_labels ^ set(actual_labels.values())
        )
        expected_keys = set(expected_labels)
        actual_keys = set(actual_labels)
        result["missing_keys"] = len(expected_keys - actual_keys)
        result["orphan_keys"] = len(actual_keys - expected_keys)
        minimum = 1.0
        below = 0
        for key in sorted(expected_keys & actual_keys):
            expected_vector = expected_index.get_items([expected_labels[key]])[0]
            actual_vector = actual_index.get_items([actual_labels[key]])[0]
            cosine = _cosine(expected_vector, actual_vector)
            minimum = min(minimum, cosine)
            if cosine < cosine_threshold:
                below += 1
        result["below_threshold"] = below
        result["minimum_cosine"] = round(minimum, 9) if expected_keys & actual_keys else None
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    result["equal"] = not any(
        result[key]
        for key in (
            "missing_keys",
            "orphan_keys",
            "expected_label_mismatches",
            "actual_label_mismatches",
            "duplicate_expected_labels",
            "duplicate_actual_labels",
            "below_threshold",
        )
    )
    return result


def compare_retained_hnsw_vectors(
    before_path: Path,
    after_path: Path,
    *,
    retained_before_labels: dict[str, int],
    after_labels: dict[str, int],
    dimension: int = 1024,
    cosine_threshold: float = 0.999999,
) -> dict[str, Any]:
    """Prove ACL compaction preserved every retained business-key vector.

    The before index may contain denied pages that are intentionally absent
    afterwards.  The after index must contain exactly the retained key set,
    with unique durable labels and vector direction preserved.
    """

    result: dict[str, Any] = {
        "schema_version": "mnemos.hnsw_retained_vector_comparison.v1",
        "cosine_threshold": cosine_threshold,
        "retained_keys": len(retained_before_labels),
        "actual_keys": len(after_labels),
        "missing_before_labels": 0,
        "missing_after_labels": 0,
        "orphan_after_keys": 0,
        "duplicate_before_labels": 0,
        "duplicate_after_labels": 0,
        "below_threshold": 0,
        "minimum_cosine": None,
        "error": None,
        "equal": False,
    }
    try:
        before_index = _load_index(
            before_path,
            dimension=dimension,
            capacity=len(retained_before_labels),
        )
        after_index = _load_index(
            after_path,
            dimension=dimension,
            capacity=len(after_labels),
        )
        before_ids = {int(value) for value in before_index.get_ids_list()}
        after_ids = {int(value) for value in after_index.get_ids_list()}
        result["duplicate_before_labels"] = len(retained_before_labels) - len(
            set(retained_before_labels.values())
        )
        result["duplicate_after_labels"] = len(after_labels) - len(
            set(after_labels.values())
        )
        result["missing_before_labels"] = len(
            set(retained_before_labels.values()) - before_ids
        )
        result["missing_after_labels"] = len(set(after_labels.values()) ^ after_ids)
        retained_keys = set(retained_before_labels)
        actual_keys = set(after_labels)
        result["orphan_after_keys"] = len(actual_keys - retained_keys)
        missing_keys = retained_keys - actual_keys
        result["missing_after_labels"] += len(missing_keys)
        minimum = 1.0
        below = 0
        for key in sorted(retained_keys & actual_keys):
            before_vector = before_index.get_items([retained_before_labels[key]])[0]
            after_vector = after_index.get_items([after_labels[key]])[0]
            cosine = _cosine(before_vector, after_vector)
            minimum = min(minimum, cosine)
            if cosine < cosine_threshold:
                below += 1
        result["below_threshold"] = below
        result["minimum_cosine"] = (
            round(minimum, 9) if retained_keys & actual_keys else None
        )
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    result["equal"] = not any(
        result[key]
        for key in (
            "missing_before_labels",
            "missing_after_labels",
            "orphan_after_keys",
            "duplicate_before_labels",
            "duplicate_after_labels",
            "below_threshold",
        )
    )
    return result


def relation_label_map(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(str(db_path), timeout=30) as conn:
        has_relations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='relations'"
        ).fetchone()
        if has_relations:
            return {
                "\0".join((str(source), str(target), str(relation_type))): int(row_id)
                for source, target, relation_type, row_id in conn.execute(
                    """SELECT relation.source, relation.target,
                              relation.relation_type, embedding.id
                       FROM relation_context_embeddings AS embedding
                       JOIN relations AS relation
                         ON relation.id=embedding.relation_id"""
                )
            }
        return {
            str(relation_id): int(row_id)
            for relation_id, row_id in conn.execute(
                "SELECT relation_id, id FROM relation_context_embeddings"
            )
        }


def relation_index_integrity(
    db_path: Path,
    index_path: Path,
    *,
    dimension: int = 1024,
    cosine_threshold: float = 0.99,
) -> dict[str, Any]:
    """Verify every relation ANN label points to its authoritative SQLite vector."""

    labels = relation_label_map(db_path)
    result: dict[str, Any] = {
        "relations": len(labels),
        "label_mismatches": 0,
        "below_threshold": 0,
        "minimum_cosine": None,
        "error": None,
        "ok": False,
    }
    try:
        index = _load_index(index_path, dimension=dimension, capacity=len(labels))
        index_labels = {int(value) for value in index.get_ids_list()}
        result["label_mismatches"] = len(index_labels ^ set(labels.values()))
        minimum = 1.0
        below = 0
        with sqlite3.connect(str(db_path), timeout=30) as conn:
            for _relation_key, label in labels.items():
                row = conn.execute(
                    "SELECT embedding FROM relation_context_embeddings WHERE id=?",
                    (int(label),),
                ).fetchone()
                if row is None:
                    below += 1
                    continue
                cosine = _cosine(
                    index.get_items([label])[0],
                    [float(value) for value in json.loads(row[0])],
                )
                minimum = min(minimum, cosine)
                if cosine < cosine_threshold:
                    below += 1
        result["below_threshold"] = below
        result["minimum_cosine"] = round(minimum, 9) if labels else None
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    result["ok"] = not result["label_mismatches"] and not result["below_threshold"]
    return result


def wiki_label_map(meta_path: Path) -> dict[str, int]:
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    return {
        f"{path}\0{int(chunk.get('chunk_idx', 0))}": int(chunk["id"])
        for path, metadata in payload.items()
        for chunk in metadata.get("chunks", [])
    }
