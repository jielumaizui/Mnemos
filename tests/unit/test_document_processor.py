# -*- coding: utf-8 -*-
"""
DocumentProcessor 单元测试

覆盖公共行为：
1. __init__ — 依赖检查
2. detect_type — 文件类型检测
3. process_document — 主处理入口（含错误处理）
4. process_and_distill — 蒸馏入 Wiki 流程
5. save_to_rejected — 拒绝文档隔离
6. validate_extraction — 验证结果解析

Mock 策略：
- 文件读取：使用 tmp_path 创建真实小文件
- LLM 调用：mock AgentDelegate
- 子进程：mock subprocess.run / shutil.which
- 外部模块：mock pandas / pypdf / docx 等导入
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 确保项目根目录在 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.hephaestus.document_processor import (  # noqa: E402
    DocumentProcessor,
    DocumentType,
    ExtractedDocument,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def processor():
    """提供一个不触发真实可选依赖导入的 DocumentProcessor。"""
    with patch.object(DocumentProcessor, "_check_dependencies"):
        proc = DocumentProcessor()
        # 默认所有依赖都"可用"，测试按需覆盖
        proc.deps = {
            k: True
            for k in [
                "pandas",
                "openpyxl",
                "pptx",
                "pypdf",
                "docx",
                "beautifulsoup4",
                "markdownify",
                "ebooklib",
            ]
        }
    return proc


@pytest.fixture
def sample_html_file(tmp_path):
    """创建一个简单的 HTML 测试文件。"""
    path = tmp_path / "sample.html"
    path.write_text(
        "<html><head><title>测试页面</title></head>"
        "<body><h1>标题</h1><p>段落内容。</p></body></html>",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def sample_txt_file(tmp_path):
    """创建一个不支持的 .txt 测试文件。"""
    path = tmp_path / "sample.txt"
    path.write_text("纯文本内容", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. __init__ — 初始化与依赖检查
# ---------------------------------------------------------------------------


class TestInit:
    """测试 DocumentProcessor 初始化行为。"""

    def test_init_checks_dependencies_without_initializing_storage(self):
        with patch.object(DocumentProcessor, "_check_dependencies") as check:
            proc = DocumentProcessor()

        check.assert_called_once_with()
        assert not hasattr(proc, "backend")

    def test_check_dependencies_flags(self):
        """_check_dependencies 正确标记各依赖可用性。"""
        proc = DocumentProcessor.__new__(DocumentProcessor)
        # 模拟所有导入失败
        with patch("builtins.__import__", side_effect=ImportError):
            proc._check_dependencies()
        assert all(v is False for v in proc.deps.values())


# ---------------------------------------------------------------------------
# 2. detect_type — 文件类型检测
# ---------------------------------------------------------------------------


class TestDetectType:
    """测试 detect_type 对各种扩展名的映射。"""

    @pytest.mark.parametrize(
        "ext,expected",
        [
            (".xlsx", DocumentType.EXCEL),
            (".xls", DocumentType.EXCEL),
            (".pptx", DocumentType.PPT),
            (".ppt", DocumentType.UNKNOWN),  # 老格式不直接支持
            (".pdf", DocumentType.PDF),
            (".docx", DocumentType.WORD),
            (".doc", DocumentType.UNKNOWN),  # 老格式不直接支持
            (".html", DocumentType.HTML),
            (".htm", DocumentType.HTML),
            (".epub", DocumentType.EBOOK),
            (".mobi", DocumentType.EBOOK),
            (".azw3", DocumentType.EBOOK),
            (".unknown", DocumentType.UNKNOWN),
            ("", DocumentType.UNKNOWN),
        ],
    )
    def test_detect_type_mapping(self, processor, ext, expected):
        """各扩展名正确映射到 DocumentType。"""
        path = Path(f"/tmp/test{ext}")
        assert processor.detect_type(path) == expected


# ---------------------------------------------------------------------------
# 3. process_document — 主处理入口与错误处理
# ---------------------------------------------------------------------------


class TestProcessDocument:
    """测试 process_document 对存在/不存在、支持/不支持文件的处理。"""

    def test_process_nonexistent_file_returns_none(self, processor):
        """文件不存在时返回 None。"""
        result = processor.process_document(Path("/nonexistent/file.pdf"))
        assert result is None

    def test_process_unsupported_extension_returns_none(self, processor, sample_txt_file):
        """不支持的扩展名返回 None。"""
        result = processor.process_document(sample_txt_file)
        assert result is None

    def test_process_html_success(self, processor, sample_html_file):
        """HTML 文件成功提取，验证标题、内容、元数据。"""
        doc = processor.process_document(sample_html_file)
        assert doc is not None
        assert doc.doc_type == DocumentType.HTML
        assert doc.title == "测试页面"
        assert "标题" in doc.content
        assert "段落内容" in doc.content
        assert doc.metadata["original_size"] > 0
        assert doc.metadata["extracted_size"] > 0

    def test_process_html_without_title(self, processor, tmp_path):
        """HTML 无 <title> 时回退到文件名作为标题。"""
        path = tmp_path / "untitled.html"
        path.write_text("<html><body><p>内容</p></body></html>", encoding="utf-8")
        doc = processor.process_document(path)
        assert doc.title == "untitled"

    def test_process_html_without_deps_uses_regex(self, processor, sample_html_file):
        """无 markdownify / beautifulsoup4 时回退到正则提取。"""
        processor.deps["markdownify"] = False
        processor.deps["beautifulsoup4"] = False
        doc = processor.process_document(sample_html_file)
        assert doc is not None
        assert "标题" in doc.content
        assert "段落内容" in doc.content

    def test_process_document_exception_returns_none(self, processor, sample_html_file):
        """处理器内部抛异常时返回 None 不崩溃。"""
        with patch.object(processor, "_process_html", side_effect=RuntimeError("boom")):
            result = processor.process_document(sample_html_file)
        assert result is None

    def test_process_document_programming_error_propagates(self, processor, sample_html_file):
        """未知编程错误必须暴露，不能被公共入口误报为文档损坏。"""
        with (
            patch.object(processor, "_process_html", side_effect=AssertionError("bug")),
            pytest.raises(AssertionError, match="bug"),
        ):
            processor.process_document(sample_html_file)


# ---------------------------------------------------------------------------
# 4. process_and_distill — 蒸馏入 Wiki
# ---------------------------------------------------------------------------


class TestProcessAndDistill:
    """测试 process_and_distill 主入口。"""

    @staticmethod
    def _receipt():
        return {
            "success": True,
            "source_event_id": "raw-event-1",
            "raw_event_id": "raw-event-1",
            "provenance_id": "raw-event-1",
            "capture_result": {"status": "queued"},
        }

    def test_process_and_distill_returns_zero_when_process_fails(self, processor, sample_txt_file):
        """process_document 返回 None 时整体返回 0。"""
        result = processor.process_and_distill(sample_txt_file)
        assert result == 0

    def test_process_and_distill_skipped_document(self, processor, sample_html_file):
        """pipeline 判定 skip 时返回 0。"""
        fake_doc = MagicMock()
        fake_doc.content = "提取内容"
        fake_doc.filename = "sample.html"
        fake_doc.doc_type = MagicMock(value="html")
        fake_doc.title = "测试"
        fake_doc.metadata = {"pages": 1}

        fake_result = MagicMock()
        fake_result.judgment = "skip"
        fake_result.fragments = []

        with (
            patch.object(processor, "process_document", return_value=fake_doc),
            patch(
                "core.sync_framework.ingestion_receipt.create_ingestion_receipt",
                return_value=self._receipt(),
            ),
            patch(
                "core.hephaestus.document_pipeline.DocumentDistillationPipeline"
            ) as mock_pipe_cls,
            patch("core.hephaestus.distillation_engine.HttpApiHostAgentCaller"),
        ):
            mock_pipe = MagicMock()
            mock_pipe.process.return_value = fake_result
            mock_pipe_cls.return_value = mock_pipe

            count = processor.process_and_distill(sample_html_file)
        assert count == 0

    def test_process_and_distill_success(self, processor, sample_html_file):
        """正常流程返回写入 Wiki 的页数，并先写入 L1。"""
        fake_doc = MagicMock()
        fake_doc.content = "提取内容"
        fake_doc.filename = "sample.html"
        fake_doc.doc_type = MagicMock(value="html")
        fake_doc.title = "测试"
        fake_doc.metadata = {"pages": 1}

        fake_result = MagicMock()
        fake_result.judgment = "distill"
        fake_result.fragments = [MagicMock(keywords=["k1"], frontmatter={"类型": "note"})]
        fake_result.doc_category = "test"

        with (
            patch.object(processor, "process_document", return_value=fake_doc),
            patch(
                "core.sync_framework.ingestion_receipt.create_ingestion_receipt",
                return_value=self._receipt(),
            ),
            patch(
                "core.hephaestus.document_pipeline.DocumentDistillationPipeline"
            ) as mock_pipe_cls,
            patch("core.hephaestus.distillation_engine.HttpApiHostAgentCaller"),
            patch("core.persona.psyche.get_signal_store") as _,
        ):
            mock_pipe = MagicMock()
            mock_pipe.process.return_value = fake_result
            mock_pipe.write_to_wiki.return_value = [
                Path("00-Inbox/page1.md"),
                Path("00-Inbox/page2.md"),
            ]
            mock_pipe_cls.return_value = mock_pipe

            count = processor.process_and_distill(sample_html_file)
        assert count == 2
        mock_pipe.write_to_wiki.assert_called_once()
        # session_id 使用 canonical raw revision（process 以位置参数调用）
        processed_session_id = mock_pipe.process.call_args.args[0]
        assert processed_session_id == "raw-event-1"

    def test_process_and_distill_blocks_when_raw_revision_missing(
        self, processor, sample_html_file
    ):
        """capture receipt 未给出 canonical raw revision 时不得继续正式写 Wiki。"""
        fake_doc = MagicMock()
        fake_doc.content = "提取内容"
        fake_doc.filename = "sample.html"
        fake_doc.doc_type = MagicMock(value="html")
        fake_doc.title = "测试"
        fake_doc.metadata = {"pages": 1}

        with (
            patch.object(processor, "process_document", return_value=fake_doc),
            patch(
                "core.sync_framework.ingestion_receipt.create_ingestion_receipt",
                return_value={
                    "success": True,
                    "source_event_id": "capture-only",
                    "raw_event_id": "",
                    "provenance_id": "capture-only",
                },
            ),
            patch(
                "core.hephaestus.document_pipeline.DocumentDistillationPipeline"
            ) as mock_pipe_cls,
        ):
            details = processor.process_and_distill(sample_html_file, return_details=True)

        assert details["page_count"] == 0
        assert details["judgment"] == "capture_failed_recoverable"
        assert details["ingestion_receipt"]["raw_event_id"] == ""
        mock_pipe_cls.assert_not_called()

    def test_process_and_distill_force_provider_passed(self, processor, sample_html_file):
        """force_provider 参数正确传递给 HttpApiHostAgentCaller。"""
        fake_doc = MagicMock()
        fake_doc.content = "内容"
        fake_doc.filename = "sample.html"
        fake_doc.doc_type = MagicMock(value="html")
        fake_doc.title = "测试"
        fake_doc.metadata = {}

        fake_result = MagicMock()
        fake_result.judgment = "skip"
        fake_result.fragments = []

        with (
            patch.object(processor, "process_document", return_value=fake_doc),
            patch(
                "core.sync_framework.ingestion_receipt.create_ingestion_receipt",
                return_value=self._receipt(),
            ),
            patch(
                "core.hephaestus.document_pipeline.DocumentDistillationPipeline"
            ) as mock_pipe_cls,
            patch("core.hephaestus.distillation_engine.HttpApiHostAgentCaller") as mock_caller_cls,
        ):
            mock_pipe = MagicMock()
            mock_pipe.process.return_value = fake_result
            mock_pipe_cls.return_value = mock_pipe

            processor.process_and_distill(sample_html_file, force_provider="cli")
            mock_caller_cls.assert_called_once_with(force_provider="cli")


# ---------------------------------------------------------------------------
# 7. save_to_rejected — 拒绝文档隔离
# ---------------------------------------------------------------------------


class TestSaveToRejected:
    """测试 save_to_rejected 将拒绝文档写入隔离目录。"""

    def test_save_to_rejected_creates_json_and_copy(self, processor, tmp_path):
        """生成 JSON 元数据文件并复制原始文件。"""
        fake_config = MagicMock()
        fake_config.data_dir = tmp_path
        fake_config.database_dir = tmp_path
        with patch("core.config.get_config", return_value=fake_config):
            doc = ExtractedDocument(
                doc_type=DocumentType.PDF,
                filename="bad.pdf",
                title="Bad",
                content="内容预览",
                metadata={},
                summary="摘要",
                validation_status="rejected",
                confidence=0.2,
                review_reason="严重错误",
            )
            original = tmp_path / "bad.pdf"
            original.write_text("fake pdf", encoding="utf-8")

            meta_path = processor.save_to_rejected(doc, original)

        assert meta_path.exists()
        assert meta_path.suffix == ".json"
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        assert data["filename"] == "bad.pdf"
        assert data["validation_status"] == "rejected"
        assert data["confidence"] == 0.2
        assert "content_preview" in data
        # 原始文件副本（文件名带时间戳前缀）
        copies = list((tmp_path / "rejected_documents").glob("*_orig.pdf"))
        assert len(copies) == 1


# ---------------------------------------------------------------------------
# 8. validate_extraction — 验证结果解析
# ---------------------------------------------------------------------------


class TestValidateExtraction:
    """测试 validate_extraction（已废弃 Agent 委托模式，回退到本地规则）。"""

    def test_validate_returns_local_rule_result(self, processor, tmp_path):
        """validate_extraction 使用本地规则验证内容质量。"""
        # 高质量文档应通过验证
        doc = ExtractedDocument(
            doc_type=DocumentType.HTML,
            filename="test.html",
            title="测试文档",
            content="# 标题\n\n这是正文内容，包含足够的字符长度来通过验证。\n\n"
            "- 列表项1\n- 列表项2\n\n"
            "段落内容继续，确保总长度超过100个字符。",
            metadata={"pages": 1},
            summary="摘要",
        )
        dummy_file = tmp_path / "dummy.html"
        result = processor.validate_extraction(doc, dummy_file)
        assert result["is_valid"] is True
        assert result["confidence"] >= 0.6


# ---------------------------------------------------------------------------
# 9. process_document_with_validation — 完整验证流程
# ---------------------------------------------------------------------------


class TestProcessDocumentWithValidation:
    """测试 process_document_with_validation 对验证结果的分支处理。"""

    def test_validation_reject(self, processor, sample_html_file):
        """验证建议 reject 时标记 rejected。"""
        fake_doc = ExtractedDocument(
            doc_type=DocumentType.HTML,
            filename="test.html",
            title="测试",
            content="内容",
            metadata={},
            summary="摘要",
        )
        with (
            patch.object(processor, "process_document", return_value=fake_doc),
            patch.object(
                processor,
                "validate_extraction",
                return_value={
                    "is_valid": False,
                    "confidence": 0.2,
                    "issues": ["乱码"],
                    "suggested_action": "reject",
                },
            ),
        ):
            result = processor.process_document_with_validation(sample_html_file)
        assert result.validation_status == "rejected"
        assert result.needs_review is False
        assert "乱码" in result.review_reason

    def test_validation_pass(self, processor, sample_html_file):
        """高置信度通过时标记 validated。"""
        fake_doc = ExtractedDocument(
            doc_type=DocumentType.HTML,
            filename="test.html",
            title="测试",
            content="内容",
            metadata={},
            summary="摘要",
        )
        with (
            patch.object(processor, "process_document", return_value=fake_doc),
            patch.object(
                processor,
                "validate_extraction",
                return_value={
                    "is_valid": True,
                    "confidence": 0.95,
                    "issues": [],
                    "suggested_action": "accept",
                },
            ),
        ):
            result = processor.process_document_with_validation(sample_html_file)
        assert result.validation_status == "validated"
        assert result.needs_review is False
        assert result.confidence == 0.95

    def test_validation_low_confidence_review(self, processor, sample_html_file):
        """置信度低于阈值时标记 review。"""
        fake_doc = ExtractedDocument(
            doc_type=DocumentType.HTML,
            filename="test.html",
            title="测试",
            content="内容",
            metadata={},
            summary="摘要",
        )
        with (
            patch.object(processor, "process_document", return_value=fake_doc),
            patch.object(
                processor,
                "validate_extraction",
                return_value={
                    "is_valid": True,
                    "confidence": 0.5,
                    "issues": ["结构可能不准"],
                    "suggested_action": "review",
                },
            ),
        ):
            result = processor.process_document_with_validation(sample_html_file)
        assert result.validation_status == "review"
        assert result.needs_review is True

    def test_process_document_returns_none(self, processor, sample_html_file):
        """process_document 返回 None 时直接返回 None。"""
        with patch.object(processor, "process_document", return_value=None):
            result = processor.process_document_with_validation(sample_html_file)
        assert result is None
