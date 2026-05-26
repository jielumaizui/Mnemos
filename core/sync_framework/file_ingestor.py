# -*- coding: utf-8 -*-
"""
FileIngestor — 用户文件摄入器

将用户文件（PDF/Word/PPT/Excel/HTML/epub/txt/md）内容提取为文本，
存入 Memos 作为 L1 原始资料，触发后续蒸馏。

设计原则：
  - 文本提取：纯工具提取，零 LLM 成本
  - 大文件处理：>10MB 分块，>100KB 截断前50页
  - 编码回退：utf-8 → gbk → latin-1
  - 分片保存：复用 MemosClient.save_long_content()
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from integrations.styx import MemosClient
from core.config import get_config

logger = logging.getLogger(__name__)

# 文件扩展名到提取器的映射
_TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".toml", ".ini", ".log"}
_PDF_EXTENSIONS = {".pdf"}
_DOCX_EXTENSIONS = {".docx", ".doc"}
_PPTX_EXTENSIONS = {".pptx", ".ppt"}
_XLSX_EXTENSIONS = {".xlsx", ".xls"}
_HTML_EXTENSIONS = {".html", ".htm"}
_EPUB_EXTENSIONS = {".epub"}

_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
_MAX_TRUNCATE_CHARS = 50000  # 截断前 ~50 页


class FileIngestor:
    """用户文件摄入器"""

    def __init__(self, client: Optional[MemosClient] = None):
        self.config = get_config()
        self.client = client or MemosClient(
            token=self.config.memos_token,
            base_url=self.config.memos_api_url,
            agent="file-ingestor",
        )

    def ingest_file(self, file_path: Path, agent_name: str = "file") -> Optional[List]:
        """
        摄入单个文件：提取文本 → 构建 Markdown → 存入 Memos

        Args:
            file_path: 文件路径
            agent_name: 来源 Agent 名

        Returns:
            保存的 Memory 列表，失败返回 None
        """
        if not file_path.exists():
            logger.warning(f"[FileIngestor] 文件不存在: {file_path}")
            return None

        # 大文件检查
        file_size = file_path.stat().st_size
        if file_size > _MAX_FILE_SIZE:
            logger.warning(f"[FileIngestor] 文件过大 ({file_size} bytes): {file_path}")
            return None

        # 文本提取
        text = self._extract_text(file_path)
        if not text:
            logger.warning(f"[FileIngestor] 无法提取文本: {file_path}")
            return None

        # 截断过长内容
        if len(text) > _MAX_TRUNCATE_CHARS:
            text = text[:_MAX_TRUNCATE_CHARS] + "\n\n[... 文件内容已截断 ...]"

        # 构建 Markdown 内容
        content = self._build_file_markdown(file_path, text)

        # 标签
        tags = [
            f"source={agent_name}",
            f"time={self._now_date()}",
            f"model=file-ingestor",
            "scope=public",
            "status=raw",
            "content_type=file-extract",
            "layer=L1",
            f"original-path={file_path.name}",
            f"file-ext={file_path.suffix.lstrip('.')}",
        ]

        # 存入 Memos
        title = f"file-{file_path.stem}"
        try:
            return self.client.save_long_content(
                content=content,
                tags=tags,
                visibility="PUBLIC",
                title=title,
            )
        except Exception as e:
            logger.error(f"[FileIngestor] 保存失败 {file_path}: {e}")
            return None

    def ingest_directory(self, dir_path: Path, agent_name: str = "file", recursive: bool = True) -> int:
        """
        批量摄入目录中的文件

        Returns:
            成功摄入的文件数量
        """
        if not dir_path.exists() or not dir_path.is_dir():
            return 0

        count = 0
        pattern = "**/*" if recursive else "*"
        for f in dir_path.glob(pattern):
            if not f.is_file():
                continue
            if self._is_supported(f):
                result = self.ingest_file(f, agent_name)
                if result:
                    count += 1
        return count

    def _extract_text(self, file_path: Path) -> Optional[str]:
        """根据文件类型提取文本"""
        ext = file_path.suffix.lower()

        if ext in _TEXT_EXTENSIONS:
            return self._extract_plain(file_path)
        elif ext in _PDF_EXTENSIONS:
            return self._extract_pdf(file_path)
        elif ext in _DOCX_EXTENSIONS:
            return self._extract_docx(file_path)
        elif ext in _PPTX_EXTENSIONS:
            return self._extract_pptx(file_path)
        elif ext in _XLSX_EXTENSIONS:
            return self._extract_xlsx(file_path)
        elif ext in _HTML_EXTENSIONS:
            return self._extract_html(file_path)
        elif ext in _EPUB_EXTENSIONS:
            return self._extract_epub(file_path)
        else:
            logger.debug(f"[FileIngestor] 不支持的文件类型: {ext}")
            return None

    def _extract_plain(self, file_path: Path) -> Optional[str]:
        """提取纯文本文件（编码回退）"""
        for encoding in ("utf-8", "gbk", "latin-1"):
            try:
                return file_path.read_text(encoding=encoding)
            except (UnicodeDecodeError, UnicodeError):
                continue
            except Exception:
                return None
        return None

    def _extract_pdf(self, file_path: Path) -> Optional[str]:
        """提取 PDF 文本"""
        try:
            import pdfplumber
            with pdfplumber.open(str(file_path)) as pdf:
                texts = []
                for i, page in enumerate(pdf.pages):
                    if i >= 50:
                        texts.append("[... 仅提取前 50 页 ...]")
                        break
                    text = page.extract_text()
                    if text:
                        texts.append(text)
                return "\n\n".join(texts)
        except ImportError:
            logger.debug("[FileIngestor] pdfplumber 未安装，尝试 pdftotext")
            return self._extract_pdf_fallback(file_path)
        except Exception as e:
            logger.warning(f"[FileIngestor] PDF 提取失败: {e}")
            return None

    def _extract_pdf_fallback(self, file_path: Path) -> Optional[str]:
        """pdftotext 回退"""
        import subprocess
        try:
            result = subprocess.run(
                ["pdftotext", str(file_path), "-"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return None

    def _extract_docx(self, file_path: Path) -> Optional[str]:
        """提取 Word 文档文本"""
        try:
            from docx import Document
            doc = Document(str(file_path))
            return "\n\n".join(p.text for p in doc.paragraphs if p.text)
        except ImportError:
            logger.debug("[FileIngestor] python-docx 未安装")
            return None
        except Exception as e:
            logger.warning(f"[FileIngestor] DOCX 提取失败: {e}")
            return None

    def _extract_pptx(self, file_path: Path) -> Optional[str]:
        """提取 PPT 文本"""
        try:
            from pptx import Presentation
            prs = Presentation(str(file_path))
            slides = []
            for i, slide in enumerate(prs.slides):
                texts = []
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        for para in shape.text_frame.paragraphs:
                            if para.text:
                                texts.append(para.text)
                if texts:
                    slides.append(f"## Slide {i+1}\n\n" + "\n".join(texts))
            return "\n\n".join(slides)
        except ImportError:
            logger.debug("[FileIngestor] python-pptx 未安装")
            return None
        except Exception as e:
            logger.warning(f"[FileIngestor] PPTX 提取失败: {e}")
            return None

    def _extract_xlsx(self, file_path: Path) -> Optional[str]:
        """提取 Excel 为 Markdown 表格"""
        try:
            from openpyxl import load_workbook
            wb = load_workbook(str(file_path), read_only=True)
            sheets = []
            for ws in wb.worksheets:
                rows = []
                for row in ws.iter_rows(values_only=True):
                    cells = [str(c) if c is not None else "" for c in row]
                    rows.append("| " + " | ".join(cells) + " |")
                if rows:
                    header = rows[0]
                    separator = "|" + "|".join(["---"] * (len(rows[0].split("|")) - 2)) + "|"
                    sheets.append(f"## {ws.title}\n\n{header}\n{separator}\n" + "\n".join(rows[1:]))
            return "\n\n".join(sheets)
        except ImportError:
            logger.debug("[FileIngestor] openpyxl 未安装")
            return None
        except Exception as e:
            logger.warning(f"[FileIngestor] XLSX 提取失败: {e}")
            return None

    def _extract_html(self, file_path: Path) -> Optional[str]:
        """提取 HTML 文本"""
        try:
            from bs4 import BeautifulSoup
            content = file_path.read_text(encoding="utf-8", errors="replace")
            soup = BeautifulSoup(content, "html.parser")
            # 移除 script 和 style
            for tag in soup(["script", "style"]):
                tag.decompose()
            return soup.get_text(separator="\n", strip=True)
        except ImportError:
            logger.debug("[FileIngestor] beautifulsoup4 未安装")
            return None
        except Exception as e:
            logger.warning(f"[FileIngestor] HTML 提取失败: {e}")
            return None

    def _extract_epub(self, file_path: Path) -> Optional[str]:
        """提取 epub 文本"""
        try:
            import ebooklib
            from ebooklib import epub
            from bs4 import BeautifulSoup

            book = epub.read_epub(str(file_path))
            chapters = []
            for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
                soup = BeautifulSoup(item.get_content(), "html.parser")
                text = soup.get_text(separator="\n", strip=True)
                if text:
                    chapters.append(text)
            return "\n\n".join(chapters)
        except ImportError:
            logger.debug("[FileIngestor] ebooklib 未安装")
            return None
        except Exception as e:
            logger.warning(f"[FileIngestor] EPUB 提取失败: {e}")
            return None

    def _build_file_markdown(self, file_path: Path, text: str) -> str:
        """构建文件 Markdown 内容"""
        from datetime import datetime
        return (
            f"# File: {file_path.name}\n\n"
            f"**路径**: `{file_path}`\n"
            f"**类型**: {file_path.suffix.lstrip('.')}\n"
            f"**大小**: {file_path.stat().st_size} bytes\n"
            f"**提取时间**: {datetime.now().isoformat()}\n\n"
            f"---\n\n{text}"
        )

    def _is_supported(self, file_path: Path) -> bool:
        """检查文件类型是否支持"""
        ext = file_path.suffix.lower()
        return (
            ext in _TEXT_EXTENSIONS
            or ext in _PDF_EXTENSIONS
            or ext in _DOCX_EXTENSIONS
            or ext in _PPTX_EXTENSIONS
            or ext in _XLSX_EXTENSIONS
            or ext in _HTML_EXTENSIONS
            or ext in _EPUB_EXTENSIONS
        )

    @staticmethod
    def _now_date() -> str:
        from datetime import datetime
        return datetime.now().strftime("%Y%m%d")
