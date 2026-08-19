# -*- coding: utf-8 -*-
"""
index_manager.py 单元测试

覆盖范围：
  - EmbeddingIndexManager 初始化
  - _load_meta / _save_meta
  - _extract_page_text / _extract_chunks
  - _scan_wiki_pages
  - _client_available
  - build_index（含 fallback 路径）
  - _update_hnsw_index / _update_memory_fallback
  - search（含 rerank）
  - get_stats

测试策略：
  - tmp_path 构建 wiki 目录结构
  - monkeypatch get_config
  - monkeypatch hnswlib / numpy / sentence-transformers 等依赖
  - 测试 memory fallback 路径（不依赖 hnswlib）
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _authorized_page(body: str, *, scope: str = "public") -> str:
    """Wrap semantic test content in the production ACL envelope."""

    return (
        "---\n"
        f"scope: {scope}\n"
        "source_agent: human\n"
        "acl_schema_version: 1\n"
        "acl_metadata_complete: true\n"
        "acl_reconciliation_status: server_principal\n"
        "---\n\n"
        f"{body}"
    )


def _restricted_page(body: str) -> str:
    """Render an unresolved legacy page that must never enter the ANN corpus."""

    return (
        "---\n"
        "scope: restricted\n"
        "acl_schema_version: 1\n"
        "acl_metadata_complete: true\n"
        "acl_reconciliation_status: restricted_unknown\n"
        "---\n\n"
        f"{body}"
    )


@pytest.fixture(autouse=True)  # noqa
def _patch_im_get_config(monkeypatch, patched_get_config):
    import core.config as _cfg_mod

    monkeypatch.setattr(_cfg_mod, "get_config", lambda: patched_get_config)


@pytest.fixture
def manager(tmp_path, patched_get_config):
    from core.embeddings.index_manager import EmbeddingIndexManager

    patched_get_config.wiki_dir = tmp_path / "wiki"
    patched_get_config.wiki_dir.mkdir(parents=True, exist_ok=True)
    return EmbeddingIndexManager(config=patched_get_config)


@pytest.fixture
def wiki_pages(tmp_path):
    """创建测试 wiki 页面。"""
    wiki_dir = tmp_path / "wiki"
    concepts = wiki_dir / "concepts"
    concepts.mkdir(parents=True)
    (concepts / "python.md").write_text(
        _authorized_page("# Python\n\nPython is a programming language.\n")
    )
    (concepts / "rust.md").write_text(_authorized_page("# Rust\n\nRust is a systems language.\n"))
    return wiki_dir


# =============================================================================
# EmbeddingIndexManager 初始化
# =============================================================================


class TestIndexManagerInit:
    def test_init_default(self, manager):
        assert manager is not None
        assert hasattr(manager, "_index")
        assert hasattr(manager, "_meta")

    def test_init_creates_meta(self, manager):
        assert isinstance(manager._meta, dict)

    def test_custom_wiki_uses_local_index_directory(self, tmp_path, patched_get_config):
        from core.embeddings.index_manager import EmbeddingIndexManager

        custom_wiki = tmp_path / "custom-wiki"
        custom_wiki.mkdir()
        index = EmbeddingIndexManager(
            wiki_base=custom_wiki,
            config=patched_get_config,
        )

        assert index.index_dir == custom_wiki / ".kg" / "embedding_index"


# =============================================================================
# _load_meta / _save_meta
# =============================================================================


class TestMetaLifecycle:
    def test_save_and_load_meta(self, manager, tmp_path):
        manager._meta = {"version": 1, "page_count": 5, "last_build": "2024-06-01"}
        manager._save_meta()
        assert manager._meta_path.exists()

        manager._meta = {}
        manager._load_meta()
        assert manager._meta.get("version") == 1
        assert manager._meta.get("page_count") == 5

    def test_load_meta_missing_file(self, manager):
        # 删除 meta 文件
        if manager._meta_path.exists():
            manager._meta_path.unlink()
        manager._load_meta()
        assert isinstance(manager._meta, dict)

    def test_failed_meta_replace_preserves_previous_generation(
        self,
        manager,
        monkeypatch,
    ):
        import core.embeddings.index_manager as index_module

        manager._meta = {"page.md": {"chunks": [{"id": 0, "chunk_idx": 0}]}}
        manager._save_meta()
        previous = manager._meta_path.read_bytes()
        manager._meta = {"page.md": {"chunks": [{"id": 99, "chunk_idx": 0}]}}
        real_replace = index_module.os.replace

        def fail_meta_replace(source, destination):
            if Path(destination) == manager._meta_path:
                raise OSError("injected metadata replace failure")
            return real_replace(source, destination)

        monkeypatch.setattr(index_module.os, "replace", fail_meta_replace)

        with pytest.raises(OSError, match="injected metadata replace failure"):
            manager._save_meta()

        assert manager._meta_path.read_bytes() == previous
        assert not list(manager.index_dir.glob(".wiki_meta.json.*.tmp"))

    def test_restart_fails_closed_then_recovers_prepared_generation(
        self,
        manager,
        patched_get_config,
    ):
        from core.embeddings.index_manager import EmbeddingIndexManager

        manager._meta = {"page.md": {"chunks": [{"id": 0, "chunk_idx": 0}]}}
        manager._save_meta()
        previous = manager._meta_path.read_bytes()
        backup = manager.index_dir / ".wiki_meta.json.crash.bak"
        backup.write_bytes(previous)
        manager._write_generation_manifest(
            {
                "schema_version": "mnemos.wiki_index_generation.v1",
                "status": "prepared",
                "backend": "memory",
                "transaction_id": "crash",
                "artifacts": [
                    {
                        "target": "wiki_index.bin",
                        "stage": "",
                        "backup": "",
                        "backup_sha256": "",
                        "existed": False,
                    },
                    {
                        "target": "wiki_meta.json",
                        "stage": "",
                        "backup": backup.name,
                        "backup_sha256": manager._artifact_digest(backup),
                        "existed": True,
                    },
                ],
            }
        )
        manager._meta_path.write_text("{", encoding="utf-8")

        restarted = EmbeddingIndexManager(
            wiki_base=manager.wiki_base,
            index_dir=manager.index_dir,
            client=MagicMock(),
            config=patched_get_config,
        )

        assert restarted._generation_recovery_required is True
        assert restarted._meta == {}
        assert restarted._load_persisted_index_for_search() is False
        assert restarted._recover_persisted_generation() is True
        restarted._load_meta()
        assert restarted._meta_path.read_bytes() == previous
        assert restarted._meta["page.md"]["chunks"][0]["id"] == 0


# =============================================================================
# _extract_page_text
# =============================================================================


class TestExtractPageText:
    def test_extract_with_frontmatter(self, manager, tmp_path):
        page = tmp_path / "test.md"
        page.write_text("---\ntitle: Test\n---\n\n# Heading\n\nBody text.\n")
        text = manager._extract_page_text(page)
        assert "Body text" in text
        assert "---" not in text

    def test_extract_without_frontmatter(self, manager, tmp_path):
        page = tmp_path / "test.md"
        page.write_text("# Heading\n\nBody text.\n")
        text = manager._extract_page_text(page)
        assert "Body text" in text
        assert "# Heading" in text

    def test_extract_empty_file(self, manager, tmp_path):
        page = tmp_path / "empty.md"
        page.write_text("")
        text = manager._extract_page_text(page)
        assert text == ""


# =============================================================================
# _extract_chunks
# =============================================================================


class TestExtractChunks:
    def test_chunks_from_file(self, manager, tmp_path):
        page = tmp_path / "test.md"
        page.write_text("# Heading 1\n\nParagraph one.\n\n# Heading 2\n\nParagraph two.\n")
        chunks = manager._extract_chunks(page)
        assert isinstance(chunks, list)
        assert len(chunks) > 0
        # 每个 chunk 应包含 heading 和 text

    def test_chunks_empty_file(self, manager, tmp_path):
        page = tmp_path / "empty.md"
        page.write_text("")
        chunks = manager._extract_chunks(page)
        assert chunks == []

    def test_chunks_single_paragraph(self, manager, tmp_path):
        page = tmp_path / "test.md"
        page.write_text("Just one paragraph.")
        chunks = manager._extract_chunks(page)
        assert isinstance(chunks, list)
        assert len(chunks) >= 1


# =============================================================================
# _scan_wiki_pages
# =============================================================================


class TestScanWikiPages:
    def test_scan_empty_wiki(self, manager):
        pages = manager._scan_wiki_pages()
        assert isinstance(pages, list)
        assert len(pages) == 0

    def test_scan_with_pages(self, manager, wiki_pages):
        manager.wiki_base = wiki_pages
        pages = manager._scan_wiki_pages()
        assert len(pages) >= 2
        names = [p.name for p in pages]
        assert "python.md" in names
        assert "rust.md" in names


# =============================================================================
# _client_available
# =============================================================================


class TestClientAvailable:
    def test_client_available_no_client(self, manager):
        manager.client = None
        assert manager._client_available() is False

    def test_client_available_with_mock_client(self, manager, monkeypatch):
        mock_client = MagicMock()
        mock_client.health_check.return_value = {"available": True}
        manager.client = mock_client
        assert manager._client_available() is True

    def test_client_available_health_check_fails(self, manager, monkeypatch):
        mock_client = MagicMock()
        mock_client.health_check.side_effect = RuntimeError("provider unavailable")
        manager.client = mock_client
        assert manager._client_available() is False

    def test_client_available_does_not_hide_programming_errors(self, manager):
        mock_client = MagicMock()
        mock_client.health_check.side_effect = AssertionError("broken health-check contract")
        manager.client = mock_client

        with pytest.raises(AssertionError, match="broken health-check contract"):
            manager._client_available()


# =============================================================================
# build_index
# =============================================================================


class TestBuildIndex:
    def test_build_empty_wiki(self, manager):
        result = manager.build_index()
        assert result is not None

    def test_build_with_pages(self, manager, wiki_pages):
        manager.wiki_base = wiki_pages
        # mock 嵌入以避免真实模型调用
        manager._embeddings = {"python.md": [[0.1, 0.2, 0.3]]}  # noqa
        result = manager.build_index()
        assert result is not None

    def test_build_updates_meta(self, manager, wiki_pages):
        manager.wiki_base = wiki_pages
        client = MagicMock()
        client.health_check.return_value = {"available": True}
        client.embed.side_effect = lambda texts: [[0.01] * 1024 for _ in texts]
        manager.client = client

        result = manager.build_index()

        assert result["status"] == "ok"
        client.embed.assert_called_once()
        # _meta 以页面路径为 key，存储 chunks/hash/mtime
        assert len(manager._meta) >= 2
        for key in manager._meta:
            assert "hash" in manager._meta[key]
            assert "mtime" in manager._meta[key]

    def test_build_excludes_unresolved_acl_and_system_projections_before_provider(
        self, tmp_path, patched_get_config, monkeypatch
    ):
        import core.embeddings.index_manager as index_module

        monkeypatch.setattr(index_module, "HNSWLIB_AVAILABLE", False)
        wiki = tmp_path / "wiki-acl-denominator"
        wiki.mkdir()
        (wiki / "allowed.md").write_text(
            _authorized_page("# Allowed\n\n" + "authorized semantic body " * 12),
            encoding="utf-8",
        )
        (wiki / "restricted.md").write_text(
            _restricted_page("# Restricted\n\n" + "secret legacy body " * 12),
            encoding="utf-8",
        )
        (wiki / "missing-acl.md").write_text(
            "# Missing ACL\n\n" + "unclassified body " * 12,
            encoding="utf-8",
        )
        system_dir = wiki / "L3-Observations"
        system_dir.mkdir()
        (system_dir / "attention.md").write_text(
            _authorized_page("# Generated projection\n\n" + "duplicate body " * 12),
            encoding="utf-8",
        )
        client = MagicMock()
        client.health_check.return_value = {"available": True}
        client.embed.side_effect = lambda texts: [[0.01] * 1024 for _ in texts]
        manager = index_module.EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=tmp_path / "embedding-index-acl-denominator",
            client=client,
            config=patched_get_config,
        )

        result = manager.build_index()

        assert result["status"] == "ok"
        assert set(manager._meta) == {"allowed.md"}
        embedded_text = "\n".join(client.embed.call_args.args[0])
        assert "authorized semantic body" in embedded_text
        assert "secret legacy body" not in embedded_text
        assert "unclassified body" not in embedded_text
        assert "duplicate body" not in embedded_text
        audit = manager.audit_coverage()
        assert audit["ok"] is True
        assert audit["excluded_pages_by_reason"] == {
            "acl_metadata_missing": 1,
            "acl_reconciliation_required": 1,
            "system_generated_projection": 1,
        }

    def test_hnsw_acl_compaction_reuses_vectors_without_provider_or_model_run(
        self, tmp_path, patched_get_config, monkeypatch
    ):
        from core.embeddings.index_manager import (
            HNSWLIB_AVAILABLE,
            EmbeddingIndexManager,
        )

        if not HNSWLIB_AVAILABLE:
            pytest.skip("hnswlib is unavailable")

        wiki = tmp_path / "wiki-local-compaction"
        wiki.mkdir()
        body = "Vector-preserving ACL compaction evidence. " * 10
        for index in range(12):
            (wiki / f"page-{index:02d}.md").write_text(
                _authorized_page(f"# Page {index}\n\n{body}{index}"),
                encoding="utf-8",
            )
        online = MagicMock()
        online.health_check.return_value = {"available": True}
        online.embed.side_effect = lambda texts: [[0.01] * 1024 for _ in texts]
        index_dir = tmp_path / "embedding-index-local-compaction"
        initial = EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=index_dir,
            client=online,
            config=patched_get_config,
        )
        assert initial.build_index(force_full=True)["status"] == "ok"

        denied = wiki / "page-00.md"
        denied.write_text(
            _restricted_page(f"# Page 0\n\n{body}0"),
            encoding="utf-8",
        )
        offline = MagicMock()
        offline.health_check.return_value = {"available": False}
        offline.embed.side_effect = AssertionError("local compaction must not call provider")
        offline.embed_single.side_effect = AssertionError("local compaction must not call provider")
        compacted = EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=index_dir,
            client=offline,
            config=patched_get_config,
        )

        import core.telemetry.prompt_call_log as prompt_call_log

        def forbidden_model_call_run(*_args, **_kwargs):
            raise AssertionError("local compaction must not create a model-call run")

        monkeypatch.setattr(
            prompt_call_log,
            "model_call_run_scope",
            forbidden_model_call_run,
        )

        result = compacted.build_index(force_full=False)

        assert result["status"] == "ok"
        assert result["removed"] == 1
        assert result["provider_required_chunks"] == 0
        offline.health_check.assert_not_called()
        offline.embed.assert_not_called()
        offline.embed_single.assert_not_called()
        assert "page-00.md" not in compacted._meta
        assert compacted.audit_coverage()["ok"] is True

    def test_hnsw_and_metadata_commit_rolls_back_as_one_generation(
        self,
        tmp_path,
        patched_get_config,
        monkeypatch,
    ):
        import core.embeddings.index_manager as index_module

        if not index_module.HNSWLIB_AVAILABLE:
            pytest.skip("hnswlib is unavailable")
        wiki = tmp_path / "wiki-atomic-generation"
        wiki.mkdir()
        for index in range(10):
            (wiki / f"page-{index:02d}.md").write_text(
                _authorized_page(f"# Atomic page {index}\n\n" + "generation evidence " * 20),
                encoding="utf-8",
            )
        client = MagicMock()
        client.health_check.return_value = {"available": True}
        client.embed.side_effect = lambda texts: [[0.01] * 1024 for _ in texts]
        index_dir = tmp_path / "embedding-index-atomic-generation"
        manager = index_module.EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=index_dir,
            client=client,
            config=patched_get_config,
        )
        assert manager.build_index(force_full=True)["status"] == "ok"
        previous_index = manager._index_path.read_bytes()
        previous_meta = manager._meta_path.read_bytes()
        (wiki / "page-00.md").write_text(
            _authorized_page("# Atomic page 0\n\n" + "changed generation " * 20),
            encoding="utf-8",
        )
        real_replace = index_module.os.replace
        failed = False

        def corrupt_then_fail_meta_commit(source, destination):
            nonlocal failed
            if Path(destination) == manager._meta_path and not failed:
                failed = True
                manager._meta_path.write_text("{", encoding="utf-8")
                raise OSError("injected generation metadata failure")
            return real_replace(source, destination)

        monkeypatch.setattr(
            index_module.os,
            "replace",
            corrupt_then_fail_meta_commit,
        )

        with pytest.raises(OSError, match="injected generation metadata failure"):
            manager.build_index(force_full=False)

        assert manager._index_path.read_bytes() == previous_index
        assert manager._meta_path.read_bytes() == previous_meta
        assert not manager._generation_manifest_path.exists()
        assert not list(index_dir.glob("*.bak"))

    def test_provider_unavailable_does_not_create_zero_call_model_run(
        self, tmp_path, patched_get_config, monkeypatch
    ):
        from core.embeddings.index_manager import EmbeddingIndexManager
        import core.telemetry.prompt_call_log as prompt_call_log

        wiki = tmp_path / "wiki-provider-unavailable"
        wiki.mkdir()
        (wiki / "page.md").write_text(
            _authorized_page("# Provider unavailable\n\nSemantic content needs an embedding."),
            encoding="utf-8",
        )
        offline = MagicMock()
        offline.health_check.return_value = {"available": False}
        offline.embed.side_effect = AssertionError("unavailable provider must not be called")
        manager = EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=tmp_path / "embedding-index-provider-unavailable",
            client=offline,
            config=patched_get_config,
        )

        def forbidden_model_call_run(*_args, **_kwargs):
            raise AssertionError("unavailable provider must not create a model-call run")

        monkeypatch.setattr(
            prompt_call_log,
            "model_call_run_scope",
            forbidden_model_call_run,
        )

        result = manager.build_index()

        assert result["status"] == "no_client"
        assert result["provider_required_chunks"] > 0
        offline.health_check.assert_called_once()
        offline.embed.assert_not_called()

    def test_local_only_rescan_fails_closed_on_source_drift_without_model_run(
        self, tmp_path, patched_get_config, monkeypatch
    ):
        from core.embeddings.index_manager import EmbeddingIndexManager
        import core.telemetry.prompt_call_log as prompt_call_log

        wiki = tmp_path / "wiki-local-only-drift"
        wiki.mkdir()
        (wiki / "page.md").write_text(
            _authorized_page("# Drifted source\n\nThis page appeared after the reviewed plan."),
            encoding="utf-8",
        )
        client = MagicMock()
        client.embed.side_effect = AssertionError("drifted local-only plan must not call provider")
        manager = EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=tmp_path / "embedding-index-local-only-drift",
            client=client,
            config=patched_get_config,
        )
        monkeypatch.setattr(
            manager,
            "reconciliation_plan",
            lambda **_kwargs: {"provider_required_chunk_count": 0},
        )

        def forbidden_model_call_run(*_args, **_kwargs):
            raise AssertionError("drifted local-only plan must not create a model-call run")

        monkeypatch.setattr(
            prompt_call_log,
            "model_call_run_scope",
            forbidden_model_call_run,
        )

        result = manager.build_index()

        assert result == {
            "added": 0,
            "updated": 0,
            "removed": 0,
            "total": 0,
            "status": "provider_required",
            "reason": "source_changed_after_local_only_plan",
            "provider_required_chunks": 1,
            "excluded_pages_by_reason": {},
        }
        client.health_check.assert_not_called()
        client.embed.assert_not_called()

    def test_memory_fallback_removes_deleted_page_from_durable_metadata(
        self, tmp_path, patched_get_config, monkeypatch
    ):
        """A Wiki delete event must remove the page after a restart too."""

        import core.embeddings.index_manager as index_module

        monkeypatch.setattr(index_module, "HNSWLIB_AVAILABLE", False)
        wiki = tmp_path / "wiki-delete"
        wiki.mkdir()
        page = wiki / "subject.md"
        page.write_text(
            _authorized_page("# Subject\n\n" + ("sensitive derived subject content " * 12)),
            encoding="utf-8",
        )
        client = MagicMock()
        client.health_check.return_value = {"available": True}
        client.embed.side_effect = lambda texts: [[0.01] * 1024 for _ in texts]
        client.embed_single.side_effect = lambda _text: [0.01] * 1024
        index_dir = tmp_path / "embedding-index"
        manager = index_module.EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=index_dir,
            client=client,
            config=patched_get_config,
        )

        assert manager.build_index()["status"] == "ok"
        assert "subject.md" in manager._meta
        assert manager._memory_fallback

        page.unlink()
        result = manager.build_index()

        assert result["removed"] == 1
        assert manager._meta == {}
        assert manager._memory_fallback == []
        assert manager.audit_coverage()["ok"] is True

        restarted = index_module.EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=index_dir,
            client=client,
            config=patched_get_config,
        )
        assert restarted._meta == {}
        assert restarted.build_index()["status"] == "no_change"
        assert restarted.audit_coverage()["ok"] is True

    def test_incremental_hnsw_reuses_unchanged_vectors(self, tmp_path, patched_get_config):
        from core.embeddings.index_manager import (
            HNSWLIB_AVAILABLE,
            EmbeddingIndexManager,
        )

        if not HNSWLIB_AVAILABLE:
            pytest.skip("hnswlib is unavailable")

        wiki = tmp_path / "wiki-incremental"
        wiki.mkdir()
        body = "Evidence-backed projection content. " * 10
        for index in range(10):
            (wiki / f"page-{index}.md").write_text(
                _authorized_page(f"# Page {index}\n\n{body}{index}\n"),
                encoding="utf-8",
            )

        client = MagicMock()
        client.health_check.return_value = {"available": True}
        client.embed.side_effect = lambda texts: [[0.01] * 1024 for _ in texts]
        client.embed_single.side_effect = AssertionError(
            "unchanged chunks must reuse persisted HNSW vectors"
        )
        index = EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=tmp_path / "embedding-index",
            client=client,
            config=patched_get_config,
        )
        assert index.build_index(force_full=True)["status"] == "ok"

        unchanged = wiki / "page-9.md"
        unchanged.touch()
        assert index.build_index(force_full=False)["status"] == "no_change"
        assert [len(call.args[0]) for call in client.embed.call_args_list] == [10]

        changed = wiki / "page-0.md"
        changed.write_text(
            _authorized_page("# Page 0\n\n" + body + "changed\n"),
            encoding="utf-8",
        )
        assert index.build_index(force_full=False)["status"] == "ok"

        assert [len(call.args[0]) for call in client.embed.call_args_list] == [10, 1]
        client.embed_single.assert_not_called()
        assert index.audit_coverage()["ok"] is True
        index._meta.pop("page-9.md")
        audit = index.audit_coverage()
        assert audit["ok"] is False
        assert audit["missing_pages"] == ["page-9.md"]

    def test_audit_rejects_legacy_chunk_without_durable_label(self, manager, wiki_pages):
        manager.wiki_base = wiki_pages
        manager._meta = {
            str(page.relative_to(wiki_pages)): {"chunks": [{"chunk_idx": 0, "heading": ""}]}
            for page in wiki_pages.rglob("*.md")
        }
        manager.index_dir.mkdir(parents=True, exist_ok=True)
        manager._index_path.write_bytes(b"legacy-index")

        audit = manager.audit_coverage()

        assert audit["ok"] is False
        assert audit["invalid_chunks"]

        manager._rebuild_id_to_chunk_from_meta()
        assert manager._id_to_chunk == {}

    def test_audit_rejects_unique_but_wrong_label_order(self, manager, wiki_pages):
        manager.wiki_base = wiki_pages
        pages = manager._scan_wiki_pages()
        current, _add, _update, _remove, chunks = manager._classify_pages(pages, False)
        manager._meta = {}
        label = 0
        for rel_path, page in current.items():
            page_chunks = chunks[rel_path]
            manager._meta[rel_path] = {
                "hash": manager._compute_chunk_hash(page_chunks),
                "mtime": page.stat().st_mtime,
                "chunks": [
                    {"id": label + idx, "chunk_idx": idx, "heading": ""}
                    for idx, _chunk in enumerate(page_chunks)
                ],
            }
            label += len(page_chunks)
        labels = [chunk for meta in manager._meta.values() for chunk in meta["chunks"]]
        labels[0]["id"], labels[-1]["id"] = labels[-1]["id"], labels[0]["id"]
        manager.index_dir.mkdir(parents=True, exist_ok=True)
        manager._index_path.write_bytes(b"synthetic-index")

        audit = manager.audit_coverage()

        assert audit["ok"] is False
        assert {item["reason"] for item in audit["invalid_chunks"]} == {"wrong_label_order"}

    def test_incremental_build_repairs_legacy_chunk_without_durable_label(
        self, tmp_path, patched_get_config
    ):
        from core.embeddings.index_manager import (
            HNSWLIB_AVAILABLE,
            EmbeddingIndexManager,
        )

        if not HNSWLIB_AVAILABLE:
            pytest.skip("hnswlib is unavailable")

        wiki = tmp_path / "wiki-legacy-label"
        wiki.mkdir()
        body = "Durable Wiki projection evidence. " * 10
        for index in range(10):
            (wiki / f"page-{index}.md").write_text(
                _authorized_page(f"# Page {index}\n\n{body}{index}\n"),
                encoding="utf-8",
            )

        client = MagicMock()
        client.health_check.return_value = {"available": True}
        client.embed.side_effect = lambda texts: [[0.01] * 1024 for _ in texts]
        index_dir = tmp_path / "embedding-index"
        first = EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=index_dir,
            client=client,
            config=patched_get_config,
        )
        assert first.build_index(force_full=True)["status"] == "ok"

        metadata = first._meta
        del metadata["page-9.md"]["chunks"][0]["id"]
        first._save_meta()

        recovered = EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=index_dir,
            client=client,
            config=patched_get_config,
        )
        result = recovered.build_index(force_full=False)

        assert result["status"] == "ok"
        assert result["updated"] == 1
        assert [len(call.args[0]) for call in client.embed.call_args_list] == [10, 1]
        assert recovered.audit_coverage()["ok"] is True
        assert all(
            "id" in chunk for page_meta in recovered._meta.values() for chunk in page_meta["chunks"]
        )

    def test_duplicate_labels_reembed_all_owners_without_vector_aliasing(
        self, tmp_path, patched_get_config
    ):
        from core.embeddings.index_manager import (
            HNSWLIB_AVAILABLE,
            EmbeddingIndexManager,
        )

        if not HNSWLIB_AVAILABLE:
            pytest.skip("hnswlib is unavailable")
        patched_get_config._values["embedding.use_rerank"] = False

        wiki = tmp_path / "wiki-duplicate-label"
        wiki.mkdir()
        for index in range(10):
            (wiki / f"page-{index}.md").write_text(
                _authorized_page(f"# Page {index}\n\nTOKEN_{index} " + "projection evidence " * 20),
                encoding="utf-8",
            )

        def vector(text):
            result = [0.0] * 1024
            token = next(index for index in range(10) if f"TOKEN_{index}" in text)
            result[token] = 1.0
            return result

        client = MagicMock()
        client.health_check.return_value = {"available": True}
        client.embed.side_effect = lambda texts: [vector(text) for text in texts]
        client.embed_single.side_effect = vector
        index_dir = tmp_path / "embedding-index"
        first = EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=index_dir,
            client=client,
            config=patched_get_config,
        )
        assert first.build_index(force_full=True)["status"] == "ok"
        first._meta["page-0.md"]["chunks"][0]["id"] = 1
        first._save_meta()

        recovered = EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=index_dir,
            client=client,
            config=patched_get_config,
        )
        result = recovered.build_index(force_full=False)

        assert result["updated"] == 2
        assert [len(call.args[0]) for call in client.embed.call_args_list] == [10, 2]
        assert recovered.audit_coverage()["ok"] is True
        assert recovered.search("TOKEN_0", top_k=1)[0][0] == "page-0.md"
        assert recovered.search("TOKEN_1", top_k=1)[0][0] == "page-1.md"

    def test_memory_fallback_persists_and_restores_search_projection(
        self, tmp_path, patched_get_config, monkeypatch
    ):
        import core.embeddings.index_manager as index_module

        monkeypatch.setattr(index_module, "HNSWLIB_AVAILABLE", False)
        patched_get_config._values["embedding.use_rerank"] = False
        wiki = tmp_path / "wiki-memory-fallback"
        wiki.mkdir()
        (wiki / "page.md").write_text(
            _authorized_page("# Durable fallback\n\n" + "Persistent semantic evidence. " * 10),
            encoding="utf-8",
        )
        client = MagicMock()
        client.health_check.return_value = {"available": True}
        client.embed.return_value = [[0.01] * 1024]
        client.embed_single.return_value = [0.01] * 1024
        index_dir = tmp_path / "embedding-index"

        first = index_module.EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=index_dir,
            client=client,
            config=patched_get_config,
        )
        assert first.build_index(force_full=True)["status"] == "ok"
        assert first.audit_coverage()["ok"] is True

        restored = index_module.EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=index_dir,
            client=client,
            config=patched_get_config,
        )
        assert restored.build_index(force_full=False)["status"] == "no_change"
        assert restored.audit_coverage()["ok"] is True
        assert restored.search("persistent evidence", top_k=1)[0][0] == "page.md"
        assert len(client.embed.call_args_list) == 1

    def test_model_call_ledger_error_blocks_build_without_chunk_fallback(
        self, tmp_path, patched_get_config, caplog
    ):
        from core.embeddings.index_manager import EmbeddingIndexManager
        from core.telemetry.prompt_call_log import ModelCallLedgerInvariantError

        wiki = tmp_path / "wiki-ledger-blocked"
        wiki.mkdir()
        for index in range(12):
            (wiki / f"page-{index}.md").write_text(
                _authorized_page(
                    f"# Ledger circuit breaker {index}\n\n" + "Provider-bound content. " * 10
                ),
                encoding="utf-8",
            )
        ledger_error = ModelCallLedgerInvariantError("synthetic ledger failure")
        client = MagicMock()
        client.health_check.return_value = {"available": True}
        client.embed.side_effect = ledger_error
        client.embed_single.side_effect = ledger_error
        manager = EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=tmp_path / "embedding-index-ledger-blocked",
            client=client,
            config=patched_get_config,
        )
        manager._meta = {
            "existing.md": {
                "mtime": 1.0,
                "hash": "existing-hash",
                "chunks": [],
            }
        }
        manager._save_meta()
        manager._index_path.write_bytes(b"existing-index")
        original_meta = dict(manager._meta)
        original_meta_bytes = manager._meta_path.read_bytes()
        original_index_bytes = manager._index_path.read_bytes()
        caplog.set_level("WARNING")

        result = manager.build_index(force_full=True)

        assert result == {
            "status": "blocked",
            "reason": "model_call_ledger",
            "added": 0,
            "updated": 0,
            "removed": 0,
            "total": 1,
        }
        client.embed.assert_called_once()
        client.embed_single.assert_not_called()
        assert manager._meta == original_meta
        assert manager._meta_path.read_bytes() == original_meta_bytes
        assert manager._index_path.read_bytes() == original_index_bytes
        circuit_logs = [
            record
            for record in caplog.records
            if "blocked by model-call ledger" in record.getMessage()
        ]
        assert len(circuit_logs) == 1
        assert circuit_logs[0].exc_info is None

    def test_late_memory_fallback_ledger_error_restores_prebuild_state(
        self, tmp_path, patched_get_config
    ):
        from core.embeddings.index_manager import EmbeddingIndexManager
        from core.telemetry.prompt_call_log import ModelCallLedgerInvariantError

        wiki = tmp_path / "wiki-late-ledger-error"
        wiki.mkdir()
        for name in ("a", "b"):
            (wiki / f"{name}.md").write_text(
                _authorized_page(f"# {name}\n\nMemory fallback content for {name}."),
                encoding="utf-8",
            )
        vector = [0.01] * 1024
        client = MagicMock()
        client.health_check.return_value = {"available": True}
        client.embed.return_value = [vector, None]
        client.embed_single.side_effect = ModelCallLedgerInvariantError(
            "synthetic late ledger failure"
        )
        manager = EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=tmp_path / "embedding-index-late-ledger-error",
            client=client,
            config=patched_get_config,
        )
        manager._meta = {
            "existing.md": {
                "mtime": 1.0,
                "hash": "existing-hash",
                "chunks": [],
            }
        }
        manager._memory_fallback = [("existing.md", 0, vector)]
        manager._id_to_chunk = {0: ("existing.md", 0)}
        manager._save_meta()
        original_meta = dict(manager._meta)
        original_meta_bytes = manager._meta_path.read_bytes()
        original_fallback = list(manager._memory_fallback)
        original_id_map = dict(manager._id_to_chunk)

        result = manager.build_index(force_full=True)

        assert result["status"] == "blocked"
        assert result["reason"] == "model_call_ledger"
        client.embed.assert_called_once()
        client.embed_single.assert_called_once()
        assert manager._meta == original_meta
        assert manager._meta_path.read_bytes() == original_meta_bytes
        assert manager._memory_fallback == original_fallback
        assert manager._id_to_chunk == original_id_map

    def test_individual_embedding_fallback_propagates_model_call_ledger_error(self, manager):
        from core.telemetry.prompt_call_log import ModelCallLedgerInvariantError

        client = MagicMock()
        client.embed.side_effect = OSError("synthetic provider failure")
        client.embed_single.side_effect = ModelCallLedgerInvariantError("synthetic ledger failure")
        manager.client = client

        with pytest.raises(ModelCallLedgerInvariantError):
            manager._embed_texts(
                ["content"],
                subject_scopes=[(("path", "/isolated/page.md"),)],
            )

        client.embed.assert_called_once()
        client.embed_single.assert_called_once()

    def test_hnsw_chunk_fallback_propagates_model_call_ledger_error(self, manager):
        from core.telemetry.prompt_call_log import ModelCallLedgerInvariantError

        manager.client = MagicMock()
        manager.client.embed_single.side_effect = ModelCallLedgerInvariantError(
            "synthetic ledger failure"
        )

        with pytest.raises(ModelCallLedgerInvariantError):
            manager._resolve_chunk_embedding(
                "page.md",
                0,
                {"text": "content"},
                {},
                [],
                {},
            )

    def test_memory_chunk_fallback_propagates_model_call_ledger_error_before_mutation(
        self, manager, tmp_path
    ):
        from core.telemetry.prompt_call_log import ModelCallLedgerInvariantError

        page = tmp_path / "memory-ledger-page.md"
        page.write_text("# Page\n\nMemory fallback content.", encoding="utf-8")
        manager.client = MagicMock()
        manager.client.embed_single.side_effect = ModelCallLedgerInvariantError(
            "synthetic ledger failure"
        )
        original_meta = dict(manager._meta)

        with pytest.raises(ModelCallLedgerInvariantError):
            manager._update_memory_fallback(
                [],
                [],
                [],
                [],
                [],
                {"page.md": page},
            )

        assert manager._meta == original_meta


# =============================================================================
# search
# =============================================================================


class TestSearch:
    def test_search_without_persisted_index_never_builds_or_reads_wiki_bodies(
        self,
        tmp_path,
        patched_get_config,
        monkeypatch,
    ):
        from core.embeddings.index_manager import EmbeddingIndexManager

        wiki = tmp_path / "restricted-wiki"
        wiki.mkdir()
        (wiki / "secret.md").write_text(
            "# Secret\nUNAUTHORIZED-WIKI-BODY-MUST-NOT-BE-EMBEDDED",
            encoding="utf-8",
        )
        index_dir = tmp_path / "missing-index"
        client = MagicMock()
        searcher = EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=index_dir,
            client=client,
            config=patched_get_config,
        )
        monkeypatch.setattr(
            searcher,
            "build_index",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("search must not build an index")
            ),
        )
        monkeypatch.setattr(
            searcher,
            "_scan_wiki_pages",
            lambda: (_ for _ in ()).throw(AssertionError("search must not enumerate Wiki bodies")),
        )

        assert searcher.search("secret", allowed_page_paths=set()) == []
        assert client.method_calls == []
        assert not index_dir.exists()
        assert not (Path(patched_get_config.database_dir) / "model_call_ledger.db").exists()

    def test_search_no_client_returns_empty(self, manager):
        manager.client = None
        results = manager.search("python")
        assert results == []

    def test_search_no_client_with_persisted_index_creates_no_model_run(
        self,
        manager,
        monkeypatch,
    ):
        import core.telemetry.prompt_call_log as prompt_call_log

        manager._meta = {
            "page.md": {
                "chunks": [
                    {
                        "id": 0,
                        "chunk_idx": 0,
                        "embedding": [0.01] * manager.DIM,
                    }
                ]
            }
        }
        manager._restore_memory_fallback_from_meta()
        manager.client = None

        def forbidden_model_call_run(*_args, **_kwargs):
            raise AssertionError("no provider means no model-call run")

        monkeypatch.setattr(
            prompt_call_log,
            "model_call_run_scope",
            forbidden_model_call_run,
        )

        assert manager.search("python") == []

    def test_search_with_mock_client(self, manager):
        mock_client = MagicMock()
        mock_client.embed_single.return_value = [0.5] * 384
        manager.client = mock_client
        # 无索引时仍返回空
        results = manager.search("python")
        assert isinstance(results, list)

    def test_search_top_k_param(self, manager):
        mock_client = MagicMock()
        mock_client.embed_single.return_value = [0.5] * 384
        manager.client = mock_client
        results = manager.search("test", top_k=3)
        assert isinstance(results, list)


# =============================================================================
# _rerank_results
# =============================================================================


class TestRerankResults:
    def test_rerank_empty(self, manager):
        results = manager._rerank_results("query", [], 5)
        assert results == []

    def test_rerank_results(self, manager):
        candidates = [("python.md", 0.5), ("rust.md", 0.8)]
        reranked = manager._rerank_results("python tutorial", candidates, 5)
        assert isinstance(reranked, list)


# =============================================================================
# get_stats
# =============================================================================


class TestGetStats:
    def test_stats_empty(self, manager):
        stats = manager.get_stats()
        assert isinstance(stats, dict)
        assert "total_pages" in stats
        assert "hnswlib_available" in stats
        assert "client_available" in stats
        assert stats["total_pages"] == 0

    def test_stats_with_meta(self, manager, wiki_pages):
        manager.wiki_base = wiki_pages
        manager._meta = {"page1": {}, "page2": {}}
        stats = manager.get_stats()
        assert stats["total_pages"] == 2


# =============================================================================
# hnswlib 路径
# =============================================================================


class TestHNSWPath:
    def test_hnswlib_flag_exists(self):
        from core.embeddings.index_manager import HNSWLIB_AVAILABLE

        assert isinstance(HNSWLIB_AVAILABLE, bool)
