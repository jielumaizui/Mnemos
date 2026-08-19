# -*- coding: utf-8 -*-
"""
DocumentProcessor 集成测试

覆盖无需外部依赖的关键路径：
- detect_type 文件类型检测
- process_document HTML 处理（真实文件）
- validate_extraction 本地规则验证
- save_to_rejected 拒绝文档隔离
- process_document_with_validation 端到端
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
def sample_html(tmp_path):
    path = tmp_path / "article.html"
    path.write_text(
        "<html><head><title>集成测试页面</title></head>"
        "<body><h1>主标题</h1><p>这是第一段内容。</p>"
        "<p>这是第二段内容，包含更多文字以确保验证通过。</p></body></html>",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# detect_type
# ---------------------------------------------------------------------------


class TestDetectType:
    """文件类型检测集成测试。"""

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
            (".txt", DocumentType.UNKNOWN),
            ("", DocumentType.UNKNOWN),
        ],
    )
    def test_detect_type_real_files(self, processor, tmp_path, ext, expected):
        """创建真实文件并检测类型。"""
        path = tmp_path / f"test{ext}"
        path.write_text("dummy")
        assert processor.detect_type(path) == expected


# ---------------------------------------------------------------------------
# process_document
# ---------------------------------------------------------------------------


class TestProcessDocument:
    """HTML 处理集成测试（真实文件，不 mock）。"""

    def test_process_html_extracts_title_and_content(self, processor, sample_html):
        doc = processor.process_document(sample_html)
        assert doc is not None
        assert doc.title == "集成测试页面"
        assert "主标题" in doc.content
        assert "第一段内容" in doc.content
        assert doc.doc_type == DocumentType.HTML

    def test_process_html_without_title_uses_filename(self, processor, tmp_path):
        path = tmp_path / "no_title.html"
        path.write_text("<html><body><p>内容</p></body></html>", encoding="utf-8")
        doc = processor.process_document(path)
        assert doc.title == "no_title"

    def test_process_nonexistent_returns_none(self, processor):
        result = processor.process_document(Path("/nonexistent/file.html"))
        assert result is None

    def test_process_unsupported_returns_none(self, processor, tmp_path):
        path = tmp_path / "data.txt"
        path.write_text("纯文本")
        assert processor.process_document(path) is None


# ---------------------------------------------------------------------------
# validate_extraction
# ---------------------------------------------------------------------------


class TestValidateExtraction:
    """本地规则验证集成测试。"""

    def test_high_quality_document_passes(self, processor, tmp_path):
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

    def test_empty_content_fails(self, processor, tmp_path):
        doc = ExtractedDocument(
            doc_type=DocumentType.HTML,
            filename="test.html",
            title="",
            content="",
            metadata={},
            summary="",
        )
        result = processor.validate_extraction(doc, tmp_path / "dummy.html")
        assert result["is_valid"] is False
        assert result["confidence"] < 0.5

    def test_no_vision_call(self, processor, tmp_path):
        """验证不应调用 LLM vision API（vision 入口已移除）。"""
        doc = ExtractedDocument(
            doc_type=DocumentType.HTML,
            filename="test.html",
            title="测试",
            content="内容" * 50,
            metadata={},
            summary="摘要",
        )
        assert not hasattr(processor, "_call_claude_vision")
        # 不应因访问 vision 方法而抛异常
        processor.validate_extraction(doc, tmp_path / "dummy.html")


# ---------------------------------------------------------------------------
# save_to_rejected
# ---------------------------------------------------------------------------


class TestSaveToRejected:
    """拒绝文档隔离集成测试。"""

    def test_creates_json_and_copy(self, processor, tmp_path):
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
        # 原始文件副本
        copies = list((tmp_path / "rejected_documents").glob("*_orig.pdf"))
        assert len(copies) == 1


# ---------------------------------------------------------------------------
# process_document_with_validation
# ---------------------------------------------------------------------------


class TestProcessDocumentWithValidation:
    """端到端处理+验证集成测试。"""

    def test_end_to_end_with_real_html(self, processor, sample_html):
        """真实 HTML 文件经 process + validate 后返回正确状态。"""
        result = processor.process_document_with_validation(sample_html)
        assert result is not None
        # 高质量 HTML 应通过验证
        assert result.validation_status in ("validated", "review")
        assert result.title == "集成测试页面"
        assert result.confidence > 0

    def test_unsupported_file_returns_none(self, processor, tmp_path):
        path = tmp_path / "data.bin"
        path.write_bytes(b"\x00\x01\x02")
        assert processor.process_document_with_validation(path) is None
