from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.embeddings.index_manager import HNSWLIB_AVAILABLE, EmbeddingIndexManager
from scripts import reconcile_wiki_search_index as module


def _authorized_page(body: str) -> str:
    return (
        "---\nscope: public\nsource_agent: human\nacl_schema_version: 1\n"
        "acl_metadata_complete: true\n"
        "acl_reconciliation_status: server_principal\n---\n\n" + body
    )


def _restricted_page(body: str) -> str:
    return (
        "---\nscope: restricted\nacl_schema_version: 1\n"
        "acl_metadata_complete: true\n"
        "acl_reconciliation_status: restricted_unknown\n---\n\n" + body
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prepare_index(tmp_path, patched_get_config):
    if not HNSWLIB_AVAILABLE:
        pytest.skip("hnswlib is unavailable")
    wiki = tmp_path / "wiki"
    database = tmp_path / "database"
    index_dir = database / "embedding_index"
    wiki.mkdir()
    body = "Retained vector evidence for local ACL compaction. " * 10
    for index in range(12):
        (wiki / f"page-{index:02d}.md").write_text(
            _authorized_page(f"# Page {index}\n\n{body}{index}"),
            encoding="utf-8",
        )
    client = MagicMock()
    client.health_check.return_value = {"available": True}
    client.embed.side_effect = lambda texts: [[0.01] * 1024 for _ in texts]
    manager = EmbeddingIndexManager(
        wiki_base=wiki,
        index_dir=index_dir,
        client=client,
        config=patched_get_config,
    )
    assert manager.build_index(force_full=True)["status"] == "ok"
    (wiki / "page-00.md").write_text(
        _restricted_page(f"# Page 0\n\n{body}0"),
        encoding="utf-8",
    )
    patched_get_config.wiki_dir = wiki
    patched_get_config.database_dir = database
    return wiki, database, index_dir


def test_apply_compacts_acl_denominator_and_preserves_vectors(
    tmp_path, patched_get_config, monkeypatch
):
    _wiki, _database, index_dir = _prepare_index(tmp_path, patched_get_config)
    monkeypatch.setattr(module, "get_config", lambda: patched_get_config)
    monkeypatch.setattr(module, "runtime_writers_are_inactive", lambda _path: True)
    before_index = _sha256(index_dir / "wiki_index.bin")
    before_meta = _sha256(index_dir / "wiki_meta.json")

    dry = module.reconcile(apply=False)

    assert dry["ok"] is True
    assert dry["plan"]["remove_page_count"] == 1
    assert dry["plan"]["provider_required_chunk_count"] == 0
    assert _sha256(index_dir / "wiki_index.bin") == before_index
    assert _sha256(index_dir / "wiki_meta.json") == before_meta
    backup = tmp_path / "backup"

    result = module.reconcile(
        apply=True,
        backup_dir=backup,
        reviewed_plan_hash=dry["plan"]["plan_hash"],
    )

    assert result["ok"] is True
    assert result["build"]["provider_required_chunks"] == 0
    assert result["vector_comparison"]["equal"] is True
    assert result["after_plan"]["remove_page_count"] == 0
    metadata = json.loads((index_dir / "wiki_meta.json").read_text(encoding="utf-8"))
    assert "page-00.md" not in metadata
    assert len(metadata) == 11
    manifest = json.loads(
        (backup / "wiki-search-index-reconciliation-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "committed"


def test_apply_rolls_back_both_artifacts_when_comparator_fails(
    tmp_path, patched_get_config, monkeypatch
):
    _wiki, _database, index_dir = _prepare_index(tmp_path, patched_get_config)
    monkeypatch.setattr(module, "get_config", lambda: patched_get_config)
    monkeypatch.setattr(module, "runtime_writers_are_inactive", lambda _path: True)
    dry = module.reconcile(apply=False)
    before = {
        "wiki_index.bin": _sha256(index_dir / "wiki_index.bin"),
        "wiki_meta.json": _sha256(index_dir / "wiki_meta.json"),
    }
    original_audit = EmbeddingIndexManager.audit_coverage

    def failed_audit(self):
        payload = original_audit(self)
        payload["ok"] = False
        return payload

    monkeypatch.setattr(EmbeddingIndexManager, "audit_coverage", failed_audit)
    backup = tmp_path / "rollback-backup"

    with pytest.raises(RuntimeError, match="did not converge"):
        module.reconcile(
            apply=True,
            backup_dir=backup,
            reviewed_plan_hash=dry["plan"]["plan_hash"],
        )

    assert _sha256(index_dir / "wiki_index.bin") == before["wiki_index.bin"]
    assert _sha256(index_dir / "wiki_meta.json") == before["wiki_meta.json"]
    manifest = json.loads(
        (backup / "wiki-search-index-reconciliation-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "rolled_back"


def test_memory_apply_compacts_acl_denominator_and_fresh_audit_passes(
    tmp_path, patched_get_config, monkeypatch
):
    import core.embeddings.index_manager as index_module

    monkeypatch.setattr(index_module, "HNSWLIB_AVAILABLE", False)
    wiki = tmp_path / "memory-success-wiki"
    database = tmp_path / "memory-success-database"
    index_dir = database / "embedding_index"
    wiki.mkdir()
    body = "Durable memory vector evidence. " * 10
    for index in range(2):
        (wiki / f"page-{index}.md").write_text(
            _authorized_page(f"# Page {index}\n\n{body}{index}"),
            encoding="utf-8",
        )
    client = MagicMock()
    client.health_check.return_value = {"available": True}
    client.embed.side_effect = lambda texts: [[0.01] * 1024 for _ in texts]
    initial = EmbeddingIndexManager(
        wiki_base=wiki,
        index_dir=index_dir,
        client=client,
        config=patched_get_config,
    )
    assert initial.build_index(force_full=True)["status"] == "ok"
    (wiki / "page-0.md").write_text(
        _restricted_page(f"# Page 0\n\n{body}0"),
        encoding="utf-8",
    )
    patched_get_config.wiki_dir = wiki
    patched_get_config.database_dir = database
    monkeypatch.setattr(module, "get_config", lambda: patched_get_config)
    monkeypatch.setattr(module, "runtime_writers_are_inactive", lambda _path: True)
    dry = module.reconcile(apply=False)

    result = module.reconcile(
        apply=True,
        backup_dir=tmp_path / "memory-success-backup",
        reviewed_plan_hash=dry["plan"]["plan_hash"],
    )

    assert result["ok"] is True
    assert result["audit"]["index_exists"] is True
    assert result["after_plan"]["remove_page_count"] == 0
    fresh = EmbeddingIndexManager(
        wiki_base=wiki,
        index_dir=index_dir,
        config=patched_get_config,
    )
    fresh.client = None
    assert fresh.audit_coverage()["ok"] is True


def test_memory_apply_rolls_back_when_metadata_persistence_fails(
    tmp_path, patched_get_config, monkeypatch
):
    import core.embeddings.index_manager as index_module

    monkeypatch.setattr(index_module, "HNSWLIB_AVAILABLE", False)
    wiki = tmp_path / "memory-wiki"
    database = tmp_path / "memory-database"
    index_dir = database / "embedding_index"
    wiki.mkdir()
    body = "Durable memory vector evidence. " * 10
    for index in range(2):
        (wiki / f"page-{index}.md").write_text(
            _authorized_page(f"# Page {index}\n\n{body}{index}"),
            encoding="utf-8",
        )
    client = MagicMock()
    client.health_check.return_value = {"available": True}
    client.embed.side_effect = lambda texts: [[0.01] * 1024 for _ in texts]
    initial = EmbeddingIndexManager(
        wiki_base=wiki,
        index_dir=index_dir,
        client=client,
        config=patched_get_config,
    )
    assert initial.build_index(force_full=True)["status"] == "ok"
    (wiki / "page-0.md").write_text(
        _restricted_page(f"# Page 0\n\n{body}0"),
        encoding="utf-8",
    )
    patched_get_config.wiki_dir = wiki
    patched_get_config.database_dir = database
    monkeypatch.setattr(module, "get_config", lambda: patched_get_config)
    monkeypatch.setattr(module, "runtime_writers_are_inactive", lambda _path: True)
    dry = module.reconcile(apply=False)
    before_meta = (index_dir / "wiki_meta.json").read_bytes()

    original_write_artifact = EmbeddingIndexManager._write_json_artifact

    def fail_meta_write(path, payload):
        if path.name.startswith(".wiki_meta.json."):
            raise OSError("injected metadata persistence failure")
        return original_write_artifact(path, payload)

    monkeypatch.setattr(
        EmbeddingIndexManager,
        "_write_json_artifact",
        staticmethod(fail_meta_write),
    )
    backup = tmp_path / "memory-rollback-backup"

    with pytest.raises(OSError, match="metadata persistence failure"):
        module.reconcile(
            apply=True,
            backup_dir=backup,
            reviewed_plan_hash=dry["plan"]["plan_hash"],
        )

    assert (index_dir / "wiki_meta.json").read_bytes() == before_meta
    manifest = json.loads(
        (backup / "wiki-search-index-reconciliation-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "rolled_back"


def test_apply_rejects_stale_reviewed_plan_before_backup(tmp_path, patched_get_config, monkeypatch):
    _prepare_index(tmp_path, patched_get_config)
    monkeypatch.setattr(module, "get_config", lambda: patched_get_config)
    monkeypatch.setattr(module, "runtime_writers_are_inactive", lambda _path: True)
    backup = tmp_path / "unused-backup"

    with pytest.raises(ValueError, match="reviewed plan hash"):
        module.reconcile(
            apply=True,
            backup_dir=backup,
            reviewed_plan_hash="sha256:stale",
        )

    assert not backup.exists()
