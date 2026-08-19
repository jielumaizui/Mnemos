# -*- coding: utf-8 -*-
"""
KnowledgeInboxProcessor 单元测试

覆盖公共行为：
1. __init__ — 目录与 SQLite 状态数据库初始化
2. scan_inbox — 扫描收件箱、发现待处理文件（对应 get_pending）
3. _compute_hash / 去重 — 哈希计算与重复文件跳过（对应 _deduplicate）
4. _save_state / _load_state — SQLite 持久化（对应 mark_processed + Persistence）
5. get_status — 聚合状态统计
6. list_processed — 列出已处理文件
7. process_file — 文件类型路由（文本 / 图片 / 文档 / 电子书）
8. _process_text_file — L1 storage 保存、标签构建、文件移动
9. _process_image_file — 跳过逻辑与 .manual 目录
10. _process_document_file — 文档处理成功与失败路径
11. run — 端到端批量处理流程
12. generate_report — Markdown 报告生成

Mock 策略：
- 文件系统：tmp_path + Path.home 补丁，创建真实测试文件
- SQLite：使用真实 SQLite 文件（位于 tmp_path 下），验证实际读写
- StorageBackend：MagicMock，验证 save() 调用参数
- DocumentProcessor：MagicMock，控制 process_document_with_validation 返回值
- TaskIdParser：patch parse 方法避免实际解析
- WikiHeatTracker：patch HEAT_TRACKER_AVAILABLE 为 False 跳过可选依赖
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.kia.knowledge_inbox import (  # noqa: E402
    InboxFile,
    KnowledgeInboxProcessor,
    KnowledgeInbox,  # 兼容别名
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_config(tmp_path):
    """返回一个已配置 data_dir 的 mock Config 对象。"""
    cfg = MagicMock()
    cfg.data_dir = tmp_path / "mnemos_data"
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.database_dir = cfg.data_dir
    cfg.storage_backend = "obsidian"

    return cfg


@pytest.fixture
def processor(tmp_path, mock_config):
    """
    提供一个完全隔离的 KnowledgeInboxProcessor 实例。
    - 收件箱目录映射到 tmp_path 下的 fake home
    - 状态数据库使用 tmp_path 下的真实 SQLite 文件
    - StorageBackend / DocumentProcessor / WikiHeatTracker 全部被 mock
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir(parents=True, exist_ok=True)

    from core.sync_framework.storage_backend import StorageResult

    with (
        patch("core.kia.knowledge_inbox.get_config", return_value=mock_config),
        patch("core.kia.knowledge_inbox.Path.home", return_value=fake_home),
        patch("integrations.backends.ObsidianBackend") as mock_obs_cls,
        patch("core.kia.knowledge_inbox.DocumentProcessor") as mock_doc_cls,
        patch("core.kia.knowledge_inbox.HEAT_TRACKER_AVAILABLE", False),
    ):

        mock_backend = MagicMock()
        mock_backend.save.return_value = [
            StorageResult(
                uid="test-storage-uid",
                content="",
                tags=[],
                metadata={},
                created_at="",
                updated_at="",
            )
        ]
        mock_obs_cls.return_value = mock_backend

        mock_doc = MagicMock()
        mock_doc_cls.return_value = mock_doc

        proc = KnowledgeInboxProcessor()
        proc._mock_backend = mock_backend
        proc._mock_document_processor = mock_doc
        yield proc


@pytest.fixture
def sample_txt_file(processor):
    """在收件箱中创建一个待处理的 .txt 测试文件。"""
    path = processor.inbox_dir / "sample.txt"
    path.write_text("Hello, Knowledge Inbox!", encoding="utf-8")
    return path


@pytest.fixture
def sample_md_file(processor):
    """在收件箱中创建一个待处理的 .md 测试文件。"""
    path = processor.inbox_dir / "notes.md"
    path.write_text("# Markdown Note\n\nSome content here.", encoding="utf-8")
    return path


@pytest.fixture
def sample_py_file(processor):
    """在收件箱中创建一个待处理的 .py 测试文件。"""
    path = processor.inbox_dir / "script.py"
    path.write_text("print('hello')", encoding="utf-8")
    return path


@pytest.fixture
def sample_png_file(processor):
    """在收件箱中创建一个待处理的 .png 测试文件（仅做占位）。"""
    path = processor.inbox_dir / "diagram.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake_png_data")
    return path


@pytest.fixture
def sample_pdf_file(processor):
    """在收件箱中创建一个待处理的 .pdf 测试文件（仅做占位）。"""
    path = processor.inbox_dir / "report.pdf"
    path.write_bytes(b"%PDF-1.4 fake_pdf_data")
    return path


@pytest.fixture  # noqa
def sample_epub_file(processor):
    """在收件箱中创建一个待处理的 .epub 测试文件（仅做占位）。"""
    path = processor.inbox_dir / "book.epub"
    path.write_bytes(b"PK\x03\x04fake_epub_data")
    return path


# ---------------------------------------------------------------------------
# 1. __init__ — 初始化与目录创建
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_creates_directories(self, processor):
        """__init__ 应自动创建 inbox、processed、failed、reports 四个目录。"""
        assert processor.inbox_dir.exists()
        assert processor.processed_dir.exists()
        assert processor.failed_dir.exists()
        assert processor.report_dir.exists()

    def test_init_creates_sqlite_schema(self, processor):
        """__init__ 应初始化 SQLite 状态数据库并创建两张表。"""
        import sqlite3

        assert processor.state_db.exists()
        with sqlite3.connect(str(processor.state_db)) as conn:
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert "processed_files" in tables
        assert "scan_log" in tables

    def test_alias_exists(self):
        """KnowledgeInbox 应为 KnowledgeInboxProcessor 的兼容别名。"""
        assert KnowledgeInbox is KnowledgeInboxProcessor


# ---------------------------------------------------------------------------
# 2. scan_inbox — 扫描与过滤（对应 get_pending）
# ---------------------------------------------------------------------------


class TestScanInbox:
    def test_scan_finds_pending_files(self, processor, sample_txt_file, sample_md_file):
        """scan_inbox 应正确发现所有待处理的文件。"""
        pending = processor.scan_inbox()
        filenames = {f.filename for f in pending}
        assert "sample.txt" in filenames
        assert "notes.md" in filenames
        assert len(pending) == 2
        # 所有返回的文件状态应为 pending
        assert all(f.status == "pending" for f in pending)

    def test_scan_skips_hidden_files(self, processor):
        """scan_inbox 应跳过以点开头的隐藏文件。"""
        hidden = processor.inbox_dir / ".hidden.txt"
        hidden.write_text("secret", encoding="utf-8")
        normal = processor.inbox_dir / "visible.txt"
        normal.write_text("hello", encoding="utf-8")

        pending = processor.scan_inbox()
        filenames = {f.filename for f in pending}
        assert "visible.txt" in filenames
        assert ".hidden.txt" not in filenames

    def test_scan_skips_unsupported_extensions(self, processor):
        """scan_inbox 应跳过不在 SUPPORTED_EXTENSIONS 中的文件。"""
        bad = processor.inbox_dir / "virus.exe"
        bad.write_text("evil", encoding="utf-8")
        good = processor.inbox_dir / "readme.txt"
        good.write_text("nice", encoding="utf-8")

        pending = processor.scan_inbox()
        filenames = {f.filename for f in pending}
        assert "readme.txt" in filenames
        assert "virus.exe" not in filenames

    def test_scan_returns_inboxfile_dataclass(self, processor, sample_txt_file):
        """scan_inbox 返回的条目应为 InboxFile dataclass 且字段完整。"""
        pending = processor.scan_inbox()
        assert len(pending) == 1
        f = pending[0]
        assert isinstance(f, InboxFile)
        assert f.filename == "sample.txt"
        assert f.path == sample_txt_file
        assert f.size == sample_txt_file.stat().st_size
        assert f.status == "pending"
        assert f.hash is not None and len(f.hash) == 16  # MD5 前 16 位


# ---------------------------------------------------------------------------
# 3. 去重 — _compute_hash + 状态检查（对应 _deduplicate）
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_scan_skips_already_processed(self, processor, sample_txt_file):
        """已写入 processed_files 表（status=success）的文件应被跳过。"""
        file_hash = processor._compute_hash(sample_txt_file)
        processor._save_state(file_hash, "sample.txt", storage_uid="uid-123", status="success")

        pending = processor.scan_inbox()
        assert not any(f.filename == "sample.txt" for f in pending)

    def test_compute_hash_is_deterministic(self, processor, sample_txt_file):
        """同一文件多次计算哈希应得到相同结果。"""
        h1 = processor._compute_hash(sample_txt_file)
        h2 = processor._compute_hash(sample_txt_file)
        assert h1 == h2
        assert len(h1) == 16

    def test_different_files_different_hashes(self, processor, sample_txt_file, sample_md_file):
        """不同文件应产生不同哈希。"""
        h1 = processor._compute_hash(sample_txt_file)
        h2 = processor._compute_hash(sample_md_file)
        assert h1 != h2


# ---------------------------------------------------------------------------
# 4. 状态持久化 — _save_state / _load_state（对应 mark_processed + Persistence）
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_save_and_load_state_roundtrip(self, processor):
        """_save_state 写入后，_load_state 应能正确读出。"""
        processor._save_state("hash001", "file1.txt", storage_uid="uid-1", status="success")
        processor._save_state(
            "hash002", "file2.txt", storage_uid="uid-2", status="failed", error_msg="parse error"
        )

        state = processor._load_state()
        assert "hash001" in state["processed_files"]
        assert state["processed_files"]["hash001"] == "success"
        assert state["processed_files"]["hash002"] == "failed"

    def test_save_state_overwrites_existing(self, processor):
        """对同一 hash 再次 _save_state 应覆盖旧记录。"""
        processor._save_state("hash003", "file3.txt", storage_uid="uid-3", status="success")
        processor._save_state(
            "hash003",
            "file3.txt",
            storage_uid="uid-3-new",
            status="failed",
            error_msg="retry failed",
        )

        state = processor._load_state()
        assert state["processed_files"]["hash003"] == "failed"

    def test_persistence_survives_reinit(self, tmp_path, mock_config):
        """状态数据库应在重新初始化后被正确加载。"""
        fake_home = tmp_path / "fake_home2"
        fake_home.mkdir(parents=True, exist_ok=True)

        with (
            patch("core.kia.knowledge_inbox.get_config", return_value=mock_config),
            patch("core.kia.knowledge_inbox.Path.home", return_value=fake_home),
            patch("integrations.backends.ObsidianBackend"),
            patch("core.kia.knowledge_inbox.DocumentProcessor"),
            patch("core.kia.knowledge_inbox.HEAT_TRACKER_AVAILABLE", False),
        ):
            p1 = KnowledgeInboxProcessor()
            p1._save_state("hash999", "persistent.txt", storage_uid="uid-999", status="success")

            p2 = KnowledgeInboxProcessor()
            state = p2._load_state()
            assert "hash999" in state["processed_files"]
            assert state["processed_files"]["hash999"] == "success"


# ---------------------------------------------------------------------------
# 5. get_status — 状态聚合
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_get_status_counts(self, processor, sample_txt_file, sample_md_file):
        """get_status 应正确统计成功、失败、待处理数量。"""
        # 预置一条已处理记录
        processor._save_state("pre_h", "pre.txt", status="success")
        processor._save_state("fail_h", "fail.txt", status="failed", error_msg="err")

        status = processor.get_status()
        assert status["processed_count"] == 1
        assert status["failed_count"] == 1
        assert status["pending_count"] == 2
        assert set(status["pending_files"]) == {"sample.txt", "notes.md"}
        assert status["inbox_dir"] == str(processor.inbox_dir)

    def test_get_status_empty_inbox(self, processor):
        """空收件箱时 get_status 应返回零待处理。"""
        status = processor.get_status()
        assert status["pending_count"] == 0
        assert status["pending_files"] == []


# ---------------------------------------------------------------------------
# 6. list_processed — 已处理文件列表
# ---------------------------------------------------------------------------


class TestListProcessed:
    def test_list_processed_returns_ordered_results(self, processor):
        """list_processed 应按 processed_at DESC 返回记录。"""
        processor._save_state("h1", "a.txt", storage_uid="u1", status="success")
        processor._save_state("h2", "b.txt", storage_uid="u2", status="failed", error_msg="e")

        processed = processor.list_processed()
        assert len(processed) == 2
        assert processed[0]["filename"] == "b.txt"  # 后写入的在前
        assert processed[1]["filename"] == "a.txt"
        assert all("hash" in r and "status" in r for r in processed)

    def test_list_processed_empty(self, processor):
        """无记录时 list_processed 应返回空列表。"""
        assert processor.list_processed() == []


# ---------------------------------------------------------------------------
# 7. process_file — 文件类型路由
# ---------------------------------------------------------------------------


class TestProcessFile:
    def test_process_text_file_success(self, processor, sample_txt_file):
        """文本文件应被正确解析、保存到 backend、移入 processed 目录。"""
        with patch("core.kia.knowledge_inbox.TaskIdParser.parse", return_value="T123"):
            inbox_file = processor.scan_inbox()[0]
            result = processor.process_file(inbox_file)

        assert result["success"] is True
        assert result["storage_uid"] == "test-storage-uid"
        assert result["file"] == "sample.txt"

        # StorageBackend.save 被调用且标签包含预期内容
        mock_backend = processor._mock_backend
        assert mock_backend.save.called
        call_kwargs = mock_backend.save.call_args.kwargs
        assert "source=human" in call_kwargs["tags"]
        assert "T123" in call_kwargs["tags"]
        assert "inbox:text" in call_kwargs["tags"]
        # [P007] Inbox 文件必须有 session 标签，避免全部堆进同一个空 session
        assert any(t.startswith("session=inbox:") for t in call_kwargs["tags"])

        # 文件应被移动到 processed 目录
        assert not sample_txt_file.exists()
        assert any(processor.processed_dir.iterdir())

    def test_process_text_file_enqueues_for_distillation(self, processor, sample_txt_file):
        """L1 保存成功后应入队 amphora 触发蒸馏。"""
        with (
            patch("core.kia.knowledge_inbox.TaskIdParser.parse", return_value=None),
            patch("core.kia.amphora.enqueue_with_receipt") as mock_enqueue,
        ):
            inbox_file = processor.scan_inbox()[0]
            result = processor.process_file(inbox_file)

        assert result["success"] is True
        mock_enqueue.assert_called_once()
        kwargs = mock_enqueue.call_args.kwargs
        assert kwargs["messages"][0]["role"] == "user"
        assert "Hello, Knowledge Inbox!" in kwargs["messages"][0]["content"]
        assert kwargs["meta"]["capture_source"] == "knowledge_inbox"
        assert kwargs["meta"]["storage_uid"] == "test-storage-uid"

    def test_process_large_text_file_splits_into_chunks(self, processor, tmp_path):
        """超大文本文件应按语义边界切分为多个 amphora 任务。"""
        big_file = processor.inbox_dir / "large.txt"
        paragraphs = [f"## Section {i}\n\n{'Content. ' * 300}" for i in range(10)]
        big_file.write_text("# Big Inbox Doc\n\n" + "\n\n".join(paragraphs), encoding="utf-8")

        with (
            patch("core.kia.knowledge_inbox.TaskIdParser.parse", return_value=None),
            patch("core.kia.amphora.enqueue_with_receipt") as mock_enqueue,
        ):
            inbox_file = processor.scan_inbox()[0]
            result = processor.process_file(inbox_file)

        assert result["success"] is True
        assert mock_enqueue.call_count > 1
        session_ids = {call.kwargs["session_id"] for call in mock_enqueue.call_args_list}
        assert any("-chunk-" in sid for sid in session_ids)

        first_meta = mock_enqueue.call_args_list[0].kwargs["meta"]
        assert "chunk_index" in first_meta
        assert "total_chunks" in first_meta

    def test_process_image_file_skipped(self, processor, sample_png_file):
        """图片文件应被跳过并移入 .manual 目录。"""
        inbox_file = processor.scan_inbox()[0]
        result = processor.process_file(inbox_file)

        assert result["success"] is False
        assert "人工解析" in result["error"]

        # 状态应为 skipped
        state = processor._load_state()
        assert inbox_file.hash in state["processed_files"]
        assert state["processed_files"][inbox_file.hash] == "skipped"

        # 文件应被移动到 .manual 目录
        assert not sample_png_file.exists()
        manual_dir = processor.inbox_dir / ".manual"
        assert any(manual_dir.iterdir())

    def test_process_document_file_success(self, processor, sample_pdf_file):
        """文档文件处理成功路径：统一 trusted_user_document 服务 -> 移入 processed。"""
        service_result = {
            "success": True,
            "storage_uid": "doc-storage-uid",
            "l1_uid": "doc-storage-uid",
            "queue_id": "doc-session",
            "wiki_paths": ["03-Tech/report.md"],
            "quality_decision": "accepted",
            "routing_result": {"status": "routed"},
            "action_ledger_ref": "act-doc",
            "content_source": "external_file",
            "user_supplied": True,
            "trusted_user_document": True,
            "parse_result": {
                "doc_type": "pdf",
                "title": "Test Report",
                "validation_status": "validated",
            },
        }

        inbox_file = processor.scan_inbox()[0]
        with patch("core.application.document_import_service.DocumentImportService") as service_cls:
            service = service_cls.return_value
            service.import_document.return_value = service_result

            result = processor.process_file(inbox_file)

        assert result["success"] is True
        assert result["storage_uid"] == "doc-storage-uid"
        assert result["doc_type"] == "pdf"
        assert result["title"] == "Test Report"
        assert result["wiki_paths"] == ["03-Tech/report.md"]
        assert result["quality_decision"] == "accepted"
        assert result["action_ledger_ref"] == "act-doc"
        assert result["content_source"] == "external_file"
        assert result["user_supplied"] is True
        assert result["trusted_user_document"] is True
        service.import_document.assert_called_once_with(
            sample_pdf_file,
            mode="distill",
            title="report",
            agent_name="trusted_user_document",
        )

    def test_process_document_file_failure_empty_extraction(self, processor, sample_pdf_file):
        """统一 trusted_user_document 服务失败时应标记失败并移入 failed 目录。"""

        inbox_file = processor.scan_inbox()[0]
        with patch("core.application.document_import_service.DocumentImportService") as service_cls:
            service = service_cls.return_value
            service.import_document.return_value = {
                "success": False,
                "message": "API 蒸馏未配置",
                "content_source": "external_file",
                "user_supplied": True,
                "trusted_user_document": True,
            }

            result = processor.process_file(inbox_file)

        assert result["success"] is False
        assert "API 蒸馏未配置" in result["error"]
        assert any(processor.failed_dir.iterdir())

    def test_process_ebook_fallback_as_text(self, processor, sample_epub_file):  # noqa
        """EBOOKLIB 不可用时 .epub 应回退到纯文本处理。"""
        with patch("core.kia.knowledge_inbox.EBOOKLIB_AVAILABLE", False):
            inbox_file = processor.scan_inbox()[0]
            result = processor.process_file(inbox_file)

        assert result["success"] is True
        assert result["storage_uid"] == "test-storage-uid"
        mock_backend = processor._mock_backend
        call_kwargs = mock_backend.save.call_args.kwargs
        assert "inbox:ebook" in call_kwargs["tags"]
        assert "ebook:fallback=text" in call_kwargs["tags"]


# ---------------------------------------------------------------------------
# 8. run — 端到端批量处理
# ---------------------------------------------------------------------------


class TestRun:
    def test_run_processes_all_pending(self, processor, sample_txt_file, sample_md_file):
        """run 应处理所有待处理文件并返回结果列表。"""
        with patch("core.kia.knowledge_inbox.TaskIdParser.parse", return_value=None):
            results = processor.run()

        assert len(results) == 2
        assert all(r["success"] for r in results)
        assert processor.scan_inbox() == []  # 全部处理完毕

    def test_run_empty_inbox(self, processor):
        """收件箱为空时 run 应返回空列表。"""
        assert processor.run() == []

    def test_run_counts_and_logs_scan(self, processor, sample_txt_file, sample_png_file):
        """run 应正确分类 success / failed / skipped 并写入 scan_log。"""
        results = processor.run()
        assert len(results) == 2
        assert sum(1 for r in results if r["success"]) == 1
        assert sum(1 for r in results if r.get("error")) == 1

        import sqlite3

        with sqlite3.connect(str(processor.state_db)) as conn:
            row = conn.execute(
                "SELECT files_found, files_processed, files_failed FROM scan_log ORDER BY id DESC LIMIT 1"  # noqa: E501
            ).fetchone()
        assert row[0] == 2
        assert row[1] == 1
        assert row[2] == 1


# ---------------------------------------------------------------------------
# 9. generate_report — 报告生成
# ---------------------------------------------------------------------------


class TestGenerateReport:
    def test_generate_report_creates_markdown(self, processor):
        """generate_report 应生成包含正确统计信息的 Markdown 文件。"""
        results = [
            {"success": True, "file": "a.txt", "storage_uid": "u1"},
            {"success": False, "file": "b.png", "error": "需人工解析"},
        ]
        path = processor.generate_report(results, success=1, failed=1, skipped=0)
        assert path is not None
        report_path = Path(path)
        assert report_path.exists()
        content = report_path.read_text(encoding="utf-8")
        assert "Knowledge Inbox 处理报告" in content
        assert "总文件数**: 2" in content
        assert "a.txt" in content
        assert "b.png" in content
        assert "u1" in content
        assert "需人工解析" in content

    def test_generate_report_returns_none_for_empty(self, processor):
        """空结果时 generate_report 应返回 None。"""
        assert processor.generate_report([], 0, 0, 0) is None


# ---------------------------------------------------------------------------
# 10. _build_storage_content / _extract_content — 内容构建
# ---------------------------------------------------------------------------


class TestContentBuilding:
    def test_extract_content_detects_markdown(self, processor, sample_md_file):
        """_extract_content 应正确识别 .md 文件为 markdown 类型。"""
        content, content_type = processor._extract_content(sample_md_file)
        assert content_type == "markdown"
        assert "# Markdown Note" in content

    def test_extract_content_detects_code(self, processor, sample_py_file):
        """_extract_content 应正确识别 .py 文件为 code 类型。"""
        content, content_type = processor._extract_content(sample_py_file)
        assert content_type == "code"
        assert "print" in content

    def test_build_storage_content_structure(self, processor, sample_txt_file):
        """_build_storage_content 应包含文件元数据和内容。"""
        inbox_file = InboxFile(
            path=sample_txt_file,
            filename="sample.txt",
            size=sample_txt_file.stat().st_size,
            mtime=sample_txt_file.stat().st_mtime,
            hash="abc123",
            status="pending",
        )
        content = processor._build_storage_content(inbox_file, "Hello", "text")
        assert "# Inbox Import: sample.txt" in content
        assert "Source**: human-local" in content
        assert "Hash**: abc123" in content
        assert "Hello" in content
