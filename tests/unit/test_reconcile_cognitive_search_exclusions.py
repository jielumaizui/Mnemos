from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

import pytest

from core.cognitive.search_exclusion_ledger import (
    load_search_exclusion_keys,
    validate_search_exclusion_ledger,
)
from scripts.reconcile_cognitive_search_exclusions import reconcile


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_wiki(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "scope: restricted\n"
        "acl_schema_version: 1\n"
        "acl_metadata_complete: true\n"
        "acl_reconciliation_status: restricted_unknown\n"
        "---\n"
        "historical body\n",
        encoding="utf-8",
    )


def _create_sources(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    wiki = tmp_path / "wiki"
    graph = tmp_path / "graph.db"
    evidence = tmp_path / "evidence.db"
    exclusion = tmp_path / "databases" / "cognitive_search_exclusions.db"
    _write_wiki(wiki / "legacy.md")
    with sqlite3.connect(graph) as connection:
        connection.execute(
            "CREATE TABLE cognitive_relations("
            "id TEXT PRIMARY KEY, stale INTEGER, access_control TEXT, body TEXT)"
        )
        connection.execute(
            "CREATE TABLE canonical_nodes("
            "canonical_id TEXT PRIMARY KEY, access_control TEXT, body TEXT)"
        )
        connection.execute(
            "INSERT INTO cognitive_relations VALUES ('relation', 0, '', 'secret')"
        )
        connection.execute(
            "INSERT INTO canonical_nodes VALUES ('malformed', '{bad', 'secret')"
        )
    with sqlite3.connect(evidence) as connection:
        connection.execute(
            "CREATE TABLE evidence_nodes("
            "id TEXT PRIMARY KEY, access_control TEXT, body TEXT)"
        )
        connection.execute(
            "CREATE TABLE evidence_edges("
            "id INTEGER PRIMARY KEY, access_control TEXT, body TEXT)"
        )
        connection.execute("INSERT INTO evidence_nodes VALUES ('node', '', 'secret')")
        connection.execute("INSERT INTO evidence_edges VALUES (1, '', 'secret')")
    return wiki, graph, evidence, exclusion


def _run(
    paths: tuple[Path, Path, Path, Path],
    **kwargs,
):
    wiki, graph, evidence, exclusion = paths
    return reconcile(
        targets=("wiki", "cognitive_graph", "evidence_graph"),
        wiki_dir=wiki,
        cognitive_graph_db=graph,
        evidence_graph_db=evidence,
        exclusion_db=exclusion,
        daemon_check=lambda _path: True,
        **kwargs,
    )


def test_apply_requires_reviewed_hashes_and_stopped_writers(tmp_path: Path) -> None:
    paths = _create_sources(tmp_path)
    dry = _run(paths)

    with pytest.raises(ValueError, match="reviewed inventory"):
        _run(paths, apply=True, backup_dir=tmp_path / "backup-no-hash")
    with pytest.raises(RuntimeError, match="writers must be stopped"):
        wiki, graph, evidence, exclusion = paths
        reconcile(
            targets=("wiki", "cognitive_graph", "evidence_graph"),
            wiki_dir=wiki,
            cognitive_graph_db=graph,
            evidence_graph_db=evidence,
            exclusion_db=exclusion,
            apply=True,
            backup_dir=tmp_path / "backup-writer",
            expected_inventory_hash=dry["inventory_hash"],
            expected_object_manifest_hash=dry["object_manifest_hash"],
            daemon_check=lambda _path: False,
        )


def test_apply_is_exact_backed_up_and_second_apply_is_noop(tmp_path: Path) -> None:
    paths = _create_sources(tmp_path)
    wiki, graph, evidence, exclusion = paths
    source_hashes = {
        str(path): _sha256(path)
        for path in (wiki / "legacy.md", graph, evidence)
    }
    dry = _run(paths)

    applied = _run(
        paths,
        apply=True,
        backup_dir=tmp_path / "backup-first",
        expected_inventory_hash=dry["inventory_hash"],
        expected_object_manifest_hash=dry["object_manifest_hash"],
    )

    assert applied["inserted_count"] == 4
    assert applied["uncovered_count"] == 0
    assert Path(applied["backup"]["path"]).is_file()
    assert applied["backup"]["integrity_check"] == "ok"
    assert source_hashes == {
        str(path): _sha256(path)
        for path in (wiki / "legacy.md", graph, evidence)
    }
    keys, validation = load_search_exclusion_keys(exclusion)
    assert len(keys) == 4
    assert validation["ok"] is True

    target_hash = _sha256(exclusion)
    second_dry = _run(paths)
    second = _run(
        paths,
        apply=True,
        backup_dir=tmp_path / "backup-second",
        expected_inventory_hash=second_dry["inventory_hash"],
        expected_object_manifest_hash=second_dry["object_manifest_hash"],
    )
    assert second["inserted_count"] == 0
    assert second["existing_count"] == 4
    assert second["uncovered_count"] == 0
    assert _sha256(exclusion) == target_hash


def test_new_ledger_failure_never_publishes_partial_target(tmp_path: Path) -> None:
    paths = _create_sources(tmp_path)
    dry = _run(paths)

    with pytest.raises(RuntimeError, match="injected"):
        _run(
            paths,
            apply=True,
            backup_dir=tmp_path / "backup-failed-new",
            expected_inventory_hash=dry["inventory_hash"],
            expected_object_manifest_hash=dry["object_manifest_hash"],
            failpoint="after_first_insert",
        )

    assert not paths[3].exists()


def test_existing_ledger_failure_rolls_back_new_exact_rows(tmp_path: Path) -> None:
    paths = _create_sources(tmp_path)
    dry = _run(paths)
    _run(
        paths,
        apply=True,
        backup_dir=tmp_path / "backup-initial",
        expected_inventory_hash=dry["inventory_hash"],
        expected_object_manifest_hash=dry["object_manifest_hash"],
    )
    exclusion = paths[3]
    before_hash = _sha256(exclusion)
    _write_wiki(paths[0] / "000-new.md")
    changed = _run(paths)

    with pytest.raises(RuntimeError, match="injected"):
        _run(
            paths,
            apply=True,
            backup_dir=tmp_path / "backup-failed-existing",
            expected_inventory_hash=changed["inventory_hash"],
            expected_object_manifest_hash=changed["object_manifest_hash"],
            failpoint="after_first_insert",
        )

    assert _sha256(exclusion) == before_hash
    with sqlite3.connect(exclusion) as connection:
        assert validate_search_exclusion_ledger(connection)["row_count"] == 4


def test_final_verification_failure_restores_existing_ledger(tmp_path: Path) -> None:
    paths = _create_sources(tmp_path)
    dry = _run(paths)
    _run(
        paths,
        apply=True,
        backup_dir=tmp_path / "backup-final-initial",
        expected_inventory_hash=dry["inventory_hash"],
        expected_object_manifest_hash=dry["object_manifest_hash"],
    )
    exclusion = paths[3]
    _write_wiki(paths[0] / "000-final-new.md")
    changed = _run(paths)

    with pytest.raises(RuntimeError, match="committed exclusion verification failure"):
        _run(
            paths,
            apply=True,
            backup_dir=tmp_path / "backup-final-existing",
            expected_inventory_hash=changed["inventory_hash"],
            expected_object_manifest_hash=changed["object_manifest_hash"],
            failpoint="before_final_verification",
        )

    with sqlite3.connect(exclusion) as connection:
        assert validate_search_exclusion_ledger(connection)["row_count"] == 4
    manifest = (tmp_path / "backup-final-existing" / "reviewed-cognitive-search-exclusions.json")
    assert '"status": "rolled_back"' in manifest.read_text(encoding="utf-8")


def test_final_verification_failure_removes_new_target(tmp_path: Path) -> None:
    paths = _create_sources(tmp_path)
    dry = _run(paths)

    with pytest.raises(RuntimeError, match="committed exclusion verification failure"):
        _run(
            paths,
            apply=True,
            backup_dir=tmp_path / "backup-final-new",
            expected_inventory_hash=dry["inventory_hash"],
            expected_object_manifest_hash=dry["object_manifest_hash"],
            failpoint="before_final_verification",
        )

    assert not paths[3].exists()
    manifest = tmp_path / "backup-final-new" / "reviewed-cognitive-search-exclusions.json"
    assert '"status": "rolled_back"' in manifest.read_text(encoding="utf-8")


def test_schema_corruption_and_overlapping_backup_fail_closed(tmp_path: Path) -> None:
    paths = _create_sources(tmp_path)
    dry = _run(paths)
    with pytest.raises(ValueError, match="disjoint"):
        _run(
            paths,
            apply=True,
            backup_dir=paths[0] / "backup",
            expected_inventory_hash=dry["inventory_hash"],
            expected_object_manifest_hash=dry["object_manifest_hash"],
        )

    _run(
        paths,
        apply=True,
        backup_dir=tmp_path / "backup-valid",
        expected_inventory_hash=dry["inventory_hash"],
        expected_object_manifest_hash=dry["object_manifest_hash"],
    )
    with sqlite3.connect(paths[3]) as connection:
        connection.execute("DROP TRIGGER cognitive_search_exclusions_no_delete")
    invalid = _run(paths)
    assert invalid["ledger_validation"]["ok"] is False
    with pytest.raises(RuntimeError, match="ledger is invalid"):
        _run(
            paths,
            apply=True,
            backup_dir=tmp_path / "backup-invalid",
            expected_inventory_hash=invalid["inventory_hash"],
            expected_object_manifest_hash=invalid["object_manifest_hash"],
        )
