# -*- coding: utf-8 -*-
"""
MCP document_process 工具单测

覆盖 HTML/PDF/DOCX/XLSX/PPTX 各格式的处理流程，
验证 P0-1 修复后 MCP 入口不再访问不存在的 doc.pages/doc.toc。
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from integrations.agora import MCPServer


class FakeExtractedDocument:
    """模拟 ExtractedDocument，不依赖真实 DocumentProcessor"""
    def __init__(self, doc_type, filename, title, content, metadata=None,
                 summary="", validation_status="pending"):
        self.doc_type = FakeDocType(doc_type)
        self.filename = filename
        self.title = title
        self.content = content
        self.metadata = metadata or {"pages": 5}
        self.summary = summary or f"{doc_type} 文档摘要"
        self.validation_status = validation_status


class FakeDocType:
    def __init__(self, name):
        self.value = name


@pytest.fixture
def mcp_server():
    return MCPServer()


def _make_fake_doc(ext: str, title: str, content: str):
    return FakeExtractedDocument(
        doc_type=ext.upper(),
        filename=f"test.{ext}",
        title=title,
        content=content,
        metadata={"pages": 3, "words": len(content)},
        summary=f"这是一个 {ext.upper()} 测试文档",
        validation_status="validated",
    )


def _create_temp_file(tmp_path: Path, ext: str, content: str) -> Path:
    path = tmp_path / f"test.{ext}"
    path.write_text(content, encoding="utf-8")
    return path


class TestMCPDocumentProcess:
    """MCP document_process 各格式测试"""

    def test_html_success(self, tmp_path, mcp_server):
        """HTML 文件处理成功"""
        file_path = _create_temp_file(tmp_path, "html",
            "<html><body><h1>测试标题</h1><p>这是内容。</p></body></html>")
        fake_doc = _make_fake_doc("html", "测试HTML", "# 测试标题\n\n这是内容。")

        with patch("core.hephaestus.document_processor.DocumentProcessor") as mock_proc_cls:
            mock_proc_cls.return_value.process_document.return_value = fake_doc
            result = mcp_server._tool_document_process(str(file_path), save_to_memos=False)

        assert result["success"] is True
        assert result["title"] == "测试HTML"
        assert result["validation_status"] == "validated"
        assert "content_preview" in result
        assert "metadata" in result
        assert "summary" in result
        assert "wiki_paths" not in result  # save_to_memos=false 不写入

    def test_pdf_success(self, tmp_path, mcp_server):
        """PDF 文件处理成功"""
        file_path = _create_temp_file(tmp_path, "pdf", "%PDF-1.4 fake")
        fake_doc = _make_fake_doc("pdf", "测试PDF", "# PDF 内容\n\n测试。")

        with patch("core.hephaestus.document_processor.DocumentProcessor") as mock_proc_cls:
            mock_proc_cls.return_value.process_document.return_value = fake_doc
            result = mcp_server._tool_document_process(str(file_path), save_to_memos=False)

        assert result["success"] is True
        assert result["doc_type"] == "PDF"
        assert result["pages"] == 3
        assert "toc" in result

    def test_docx_success(self, tmp_path, mcp_server):
        """DOCX 文件处理成功"""
        file_path = _create_temp_file(tmp_path, "docx", "fake docx bytes")
        fake_doc = _make_fake_doc("word", "测试Word", "# Word 内容\n\n段落。")

        with patch("core.hephaestus.document_processor.DocumentProcessor") as mock_proc_cls:
            mock_proc_cls.return_value.process_document.return_value = fake_doc
            result = mcp_server._tool_document_process(str(file_path), save_to_memos=False)

        assert result["success"] is True
        assert result["doc_type"] == "WORD"
        assert result["word_count"] > 0

    def test_xlsx_success(self, tmp_path, mcp_server):
        """XLSX 文件处理成功"""
        file_path = _create_temp_file(tmp_path, "xlsx", "fake xlsx bytes")
        fake_doc = _make_fake_doc("excel", "测试Excel", "| A | B |\n|---|---|\n| 1 | 2 |")

        with patch("core.hephaestus.document_processor.DocumentProcessor") as mock_proc_cls:
            mock_proc_cls.return_value.process_document.return_value = fake_doc
            result = mcp_server._tool_document_process(str(file_path), save_to_memos=False)

        assert result["success"] is True
        assert result["doc_type"] == "EXCEL"
        assert result["validation_status"] == "validated"

    def test_pptx_success(self, tmp_path, mcp_server):
        """PPTX 文件处理成功"""
        file_path = _create_temp_file(tmp_path, "pptx", "fake pptx bytes")
        fake_doc = _make_fake_doc("ppt", "测试PPT", "# 幻灯片 1\n\n内容。")

        with patch("core.hephaestus.document_processor.DocumentProcessor") as mock_proc_cls:
            mock_proc_cls.return_value.process_document.return_value = fake_doc
            result = mcp_server._tool_document_process(str(file_path), save_to_memos=False)

        assert result["success"] is True
        assert result["doc_type"] == "PPT"
        assert "summary" in result

    def test_save_to_wiki_true(self, tmp_path, mcp_server):
        """save_to_memos=true 时触发 Wiki 蒸馏管道"""
        file_path = _create_temp_file(tmp_path, "html",
            "<html><body><h1>Wiki测试</h1></body></html>")
        fake_doc = _make_fake_doc("html", "Wiki测试", "# Wiki测试\n\n内容。")

        with patch("core.hephaestus.document_processor.DocumentProcessor") as mock_proc_cls, \
             patch("core.hephaestus.document_pipeline.DocumentDistillationPipeline") as mock_pipe_cls:
            mock_proc_cls.return_value.process_document.return_value = fake_doc
            mock_pipe = MagicMock()
            mock_pipe.process.return_value = MagicMock(fragments=[])
            mock_pipe.write_to_wiki.return_value = [Path("00-Inbox/wiki_test.md")]
            mock_pipe_cls.return_value = mock_pipe

            result = mcp_server._tool_document_process(str(file_path), save_to_memos=True)

        assert result["success"] is True
        assert "wiki_paths" in result
        assert result["pipeline"] == "文档 → Wiki 蒸馏 → 00-Inbox"
        assert "session_id" in result
        mock_pipe.write_to_wiki.assert_called_once()

    def test_file_not_found(self, mcp_server):
        """文件不存在时返回明确错误"""
        result = mcp_server._tool_document_process("/nonexistent/file.pdf")
        assert result["success"] is False
        assert "文件不存在" in result["message"]

    def test_no_pages_toc_attr_access(self, tmp_path, mcp_server):
        """验证修复：不访问 doc.pages/doc.toc，只从 metadata 读取"""
        file_path = _create_temp_file(tmp_path, "html", "<h1>标题</h1>")
        fake_doc = _make_fake_doc("html", "无结构", "# 标题")
        # 确保 FakeExtractedDocument 没有 pages/toc 属性（模拟真实 dataclass）
        assert not hasattr(fake_doc, "pages")
        assert not hasattr(fake_doc, "toc")

        with patch("core.hephaestus.document_processor.DocumentProcessor") as mock_proc_cls:
            mock_proc_cls.return_value.process_document.return_value = fake_doc
            # 如果代码仍访问 doc.pages/doc.toc，这里会抛 AttributeError
            result = mcp_server._tool_document_process(str(file_path), save_to_memos=False)

        assert result["success"] is True
        assert result["pages"] == 3  # 从 metadata 读取
