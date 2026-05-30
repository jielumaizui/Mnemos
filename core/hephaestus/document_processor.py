#!/usr/bin/env python3
import logging
"""
Document Processor - 文档处理器

支持格式：
- Excel (.xlsx, .xls) → Markdown表格
- PPT (.pptx, .ppt) → Markdown幻灯片列表
- PDF (.pdf) → Markdown文本（带页码）
- Word (.docx) → Markdown
- HTML (.html, .htm) → Markdown

处理流程：
1. 检测文件类型
2. 提取内容并转为 Markdown
3. 保存到 Memos (source=human-local)
4. 进入 Ingest 流程 → Wiki
"""

# Document Processor - 文档处理器（开源版 · 同源复用改造）
# 原模块: memos-client/document_processor.py
#
# 改造说明：
# - 去掉直接调用 ANTHROPIC_API_KEY 的 Claude Vision 验证
# - 改为通过 AgentDelegate 委托本地 Agent 进行验证和摘要
# - Memos 写入改为可选（开源版优先写入 Wiki 00-Inbox）

import os
import sys
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
from dataclasses import dataclass
from enum import Enum

# 使用当前项目的 MemosClient
logger = logging.getLogger(__name__)
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from integrations.styx import MemosClient
from core.config import get_config

# 延迟导入 AgentDelegate（避免循环依赖）
def _get_agent_delegate():
    from core.prometheus_fire import AgentDelegate
    return AgentDelegate()


class DocumentType(Enum):
    """文档类型"""
    EXCEL = "excel"
    PPT = "ppt"
    PDF = "pdf"
    WORD = "word"
    HTML = "html"
    EBOOK = "ebook"
    UNKNOWN = "unknown"


@dataclass
class ExtractedDocument:
    """提取的文档内容"""
    doc_type: DocumentType
    filename: str
    title: str
    content: str          # Markdown 格式
    metadata: Dict        # 文档元数据
    summary: str          # 内容摘要
    # 验证相关字段
    validation_status: str = "pending"  # pending, validated, review, failed
    needs_review: bool = False
    review_reason: str = ""
    confidence: float = 0.0             # 提取置信度
    processing_method: str = "local"    # local, cloud, fallback


class DocumentProcessor:
    """
    文档处理器 v2.0

    【使用Claude Vision API进行验证和处理】
    【支持人工核对机制】

    自动检测文档类型并提取内容为 Markdown
    """

    SUPPORTED_EXTENSIONS = {
        '.xlsx', '.xls',           # Excel
        '.pptx', '.ppt',            # PowerPoint
        '.pdf',                     # PDF
        '.docx',                    # Word (doc不支持，需另存为docx)
        '.html', '.htm',            # HTML
        '.epub', '.mobi', '.azw3',  # Ebook
    }

    # 验证阈值（与ImageProcessor保持一致）
    VALIDATION_CONFIDENCE_THRESHOLD = 0.85    # 验证通过阈值
    REVIEW_CONFIDENCE_THRESHOLD = 0.60        # 人工核对阈值

    def __init__(self, memos_client: MemosClient = None):
        # Memos 客户端（可选，开源版优先写入 Wiki）
        self.client = memos_client
        if self.client is None:
            try:
                config = get_config()
                if config.memos_enabled and config.memos_token:
                    self.client = MemosClient(
                        base_url=config.memos_api_url,
                        token=config.memos_token,
                    )
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at document_processor.py", exc_info=True)
                pass

        # 检查依赖
        self._check_dependencies()

    def _check_dependencies(self):
        """检查并报告依赖可用性"""
        self.deps = {
            'pandas': False,
            'openpyxl': False,
            'pptx': False,
            'PyPDF2': False,
            'docx': False,
            'beautifulsoup4': False,
            'markdownify': False,
            'ebooklib': False,
        }

        try:
            import pandas
            self.deps['pandas'] = True
        except ImportError:
            pass

        try:
            import openpyxl
            self.deps['openpyxl'] = True
        except ImportError:
            pass

        try:
            import pptx
            self.deps['pptx'] = True
        except ImportError:
            pass

        try:
            import PyPDF2
            self.deps['PyPDF2'] = True
        except ImportError:
            pass

        try:
            import docx
            self.deps['docx'] = True
        except ImportError:
            pass

        try:
            from bs4 import BeautifulSoup
            self.deps['beautifulsoup4'] = True
        except ImportError:
            pass

        try:
            import markdownify
            self.deps['markdownify'] = True
        except ImportError:
            pass

        try:
            import ebooklib
            self.deps['ebooklib'] = True
        except ImportError:
            pass

        # 打印依赖状态（首次初始化时检查，缺失时仅 INFO 提示避免刷屏）
        missing = [k for k, v in self.deps.items() if not v]
        if missing:
            logger.info(f"[DocumentProcessor] 可选依赖未安装: {', '.join(missing)} — 对应文档格式处理将不可用")

    def _call_claude_vision(self, file_content: str, doc_type: DocumentType, prompt: str) -> Optional[str]:
        """
        【同源复用】委托本地 Agent 进行验证

        开源版不直接调用 LLM API，而是通过 AgentDelegate 将验证任务
        委托给用户本地的 AI Agent（Claude Code / Codex / Hermes 等）。

        委托方式：写入任务文件到 ~/.mnemos/distill_tasks/，由 Agent 后台处理。
        如果无可用的 Agent，回退到本地规则验证（不依赖 LLM）。
        """
        try:
            delegate = _get_agent_delegate()
            from core.prometheus_fire import DistillTask

            full_prompt = prompt + f"\n\n【文档类型】: {doc_type.value}\n【内容预览】: {file_content[:5000]}"
            task = DistillTask(
                session_id=f"doc-verify-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
                messages=[{"role": "user", "content": full_prompt}],
                meta={"source": "document-processor", "task_type": "document_validation"},
            )
            # 生成输出路径
            output_path = Path.home() / ".mnemos" / "distill_output" / f"{task.session_id}.md"
            ok = delegate.delegate(task, output_path)
            if not ok:
                logger.warning(f"[DocumentProcessor] ⚠️ 无可用的 Agent 执行验证，回退到本地规则")
                return None

            # 等待结果（短时间轮询）
            result = delegate.wait_for_result(output_path, timeout=60)
            if result:
                return result
            logger.warning(f"[DocumentProcessor] ⚠️ Agent 验证超时，回退到本地规则")
            return None

        except Exception as e:
            logger.warning(f"[DocumentProcessor] ⚠️ Agent 委托验证失败: {e}")
            return None

    def validate_extraction(self, doc: ExtractedDocument, file_path: Path) -> Dict:
        """
        使用 Claude Vision 验证文档提取结果

        Args:
            doc: 提取的文档内容
            file_path: 文件路径

        Returns:
            验证结果字典
        """
        # 读取原始文件内容用于验证
        try:
            raw_content = self._get_raw_content_for_validation(file_path, doc.doc_type)
        except Exception as e:
            logger.warning(f"[DocumentProcessor] ⚠️ 无法读取原始内容进行验证: {e}")
            return {
                "is_valid": True,
                "confidence": 0.7,
                "issues": ["无法验证（依赖缺失）"],
                "suggested_action": "accept"
            }

        prompt = f"""
请验证以下从文档中提取的Markdown内容是否准确。

【提取的Markdown内容】:
{doc.content[:3000]}

【原始文档的部分内容】:
{raw_content[:3000]}

请对比检查：
1. 内容是否完整？有没有遗漏重要信息？
2. 表格/列表结构是否正确？
3. 文字是否有误读、错漏？
4. 文档类型判断是否正确？

返回JSON格式：
{{
    "is_valid": true/false,
    "confidence": 0.85,
    "completeness": 0.9,
    "accuracy": 0.85,
    "issues": ["问题1", "问题2"],
    "suggested_action": "accept|reprocess|review|reject"
}}

判断标准：
- is_valid=true: 内容准确完整，可直接使用
- is_valid=false + suggested_action="reprocess": 错误较多，需要重新提取
- is_valid=false + suggested_action="review": 置信度低，需要人工核对
- is_valid=false + suggested_action="reject": 内容严重不可信（如乱码、大面积错漏），不应入库

只返回JSON，不要其他文字。
"""

        response = self._call_claude_vision(doc.content, doc.doc_type, prompt)
        if not response:
            # API 不可用，直接通过但标记待审
            return {
                "is_valid": True,
                "confidence": 0.5,
                "issues": ["Claude Vision API 不可用，跳过验证"],
                "suggested_action": "review"
            }

        try:
            # 提取JSON
            json_match = response.strip()
            if '```json' in json_match:
                json_match = json_match.split('```json')[1].split('```')[0].strip()
            elif '```' in json_match:
                json_match = json_match.split('```')[1].split('```')[0].strip()

            result = json.loads(json_match)
            return {
                "is_valid": result.get("is_valid", False),
                "confidence": result.get("confidence", 0.0),
                "issues": result.get("issues", []),
                "suggested_action": result.get("suggested_action", "review")
            }
        except Exception as e:
            logger.warning(f"[DocumentProcessor] ⚠️ 验证结果解析失败: {e}")
            return {
                "is_valid": False,
                "confidence": 0.0,
                "issues": [f"验证结果解析失败: {e}"],
                "suggested_action": "review"
            }

    def _get_raw_content_for_validation(self, file_path: Path, doc_type: DocumentType) -> str:
        """获取原始内容用于验证"""
        # 根据文档类型读取原始文本
        if doc_type == DocumentType.PDF:
            # 尝试读取PDF文本
            try:
                import PyPDF2
                with open(file_path, 'rb') as f:
                    reader = PyPDF2.PdfReader(f)
                    text_parts = []
                    for i, page in enumerate(reader.pages[:3]):  # 前3页
                        try:
                            text = page.extract_text()
                            if text:
                                text_parts.append(f"--- Page {i+1} ---\n{text}")
                        except Exception:
                            logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
                            pass
                    return "\n".join(text_parts) if text_parts else ""
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at document_processor.py", exc_info=True)
                return ""
        elif doc_type == DocumentType.WORD:
            try:
                import docx
                doc = docx.Document(file_path)
                texts = []
                for para in doc.paragraphs[:50]:  # 前50段
                    if para.text.strip():
                        texts.append(para.text)
                return "\n".join(texts)
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at document_processor.py", exc_info=True)
                return ""
        elif doc_type == DocumentType.HTML:
            content = file_path.read_text(encoding='utf-8', errors='ignore')
            # 简单去除标签
            return re.sub(r'<[^>]+>', ' ', content)[:10000]
        else:
            # Excel, PPT等返回摘要
            return f"文档类型: {doc_type.value}, 大小: {file_path.stat().st_size} bytes"

    def process_document_with_validation(self, file_path: Path) -> Optional[ExtractedDocument]:
        """
        处理文档（带验证流程）

        完整流程：
        1. 本地提取文档内容
        2. Claude Vision 验证提取准确性
        3. 根据验证结果决定：通过/重处理/标记人工审核
        """
        logger.info(f"[DocumentProcessor] 📄 开始处理（带验证）: {file_path.name}")

        # Step 1: 本地提取
        doc = self.process_document(file_path)
        if not doc:
            return None

        # Step 2: 验证提取结果
        logger.info(f"[DocumentProcessor] 🔎 使用Claude Vision验证提取结果...")
        validation = self.validate_extraction(doc, file_path)

        confidence = validation.get("confidence", 0.0)
        is_valid = validation.get("is_valid", False)
        suggested_action = validation.get("suggested_action", "review")
        issues = validation.get("issues", [])

        if suggested_action == "reject":
            # 严重不可信，拒绝入库
            logger.info(f"[DocumentProcessor] ❌ 验证拒绝 (置信度: {confidence:.2f})")
            logger.info(f"[DocumentProcessor] 原因: {', '.join(issues) if issues else '内容严重不可信'}")
            doc.validation_status = "rejected"
            doc.needs_review = False
            doc.confidence = confidence
            doc.review_reason = f"验证拒绝 ({confidence:.2f}): {', '.join(issues) if issues else '内容严重不可信'}"

        elif is_valid and confidence >= self.VALIDATION_CONFIDENCE_THRESHOLD:
            # 验证通过
            logger.info(f"[DocumentProcessor] ✅ 验证通过 (置信度: {confidence:.2f})")
            doc.validation_status = "validated"
            doc.confidence = confidence
            doc.needs_review = False

        elif suggested_action == "reprocess" or confidence < self.REVIEW_CONFIDENCE_THRESHOLD:
            # 需要重新处理（使用云端）
            logger.warning(f"[DocumentProcessor] ⚠️ 验证失败/置信度低，需要人工核对...")
            logger.info(f"[DocumentProcessor] 原因: {', '.join(issues) if issues else '未知'}")

            # 对于文档，我们无法像图片那样云端重处理
            # 标记为需要人工审核
            doc.validation_status = "review"
            doc.needs_review = True
            doc.confidence = confidence
            doc.review_reason = f"验证置信度低 ({confidence:.2f}): {', '.join(issues) if issues else '结构可能不准确'}"

        else:
            # 标记人工核对
            logger.warning(f"[DocumentProcessor] ⚠️ 标记待人工核对 (置信度: {confidence:.2f})")
            doc.validation_status = "review"
            doc.needs_review = True
            doc.confidence = confidence
            doc.review_reason = f"置信度较低: {', '.join(issues) if issues else '建议人工核对'}"

        return doc

    def detect_type(self, file_path: Path) -> DocumentType:
        """检测文档类型"""
        ext = file_path.suffix.lower()

        if ext in ['.xlsx', '.xls']:
            return DocumentType.EXCEL
        elif ext in ['.pptx', '.ppt']:
            return DocumentType.PPT
        elif ext == '.pdf':
            return DocumentType.PDF
        elif ext == '.docx':
            return DocumentType.WORD
        elif ext in ['.html', '.htm']:
            return DocumentType.HTML
        elif ext in ['.epub', '.mobi', '.azw3']:
            return DocumentType.EBOOK
        else:
            return DocumentType.UNKNOWN

    def process_document(self, file_path: Path) -> Optional[ExtractedDocument]:
        """
        处理文档

        自动检测类型并提取内容
        """
        if not file_path.exists():
            logger.info(f"[DocumentProcessor] ❌ 文件不存在: {file_path}")
            return None

        doc_type = self.detect_type(file_path)

        if doc_type == DocumentType.UNKNOWN:
            logger.warning(f"[DocumentProcessor] ❌ 不支持的文件类型: {file_path.suffix}")
            return None

        logger.info(f"[DocumentProcessor] 📄 处理 {doc_type.value}: {file_path.name}")

        # 根据类型处理
        processors = {
            DocumentType.EXCEL: self._process_excel,
            DocumentType.PPT: self._process_ppt,
            DocumentType.PDF: self._process_pdf,
            DocumentType.WORD: self._process_word,
            DocumentType.HTML: self._process_html,
        }

        processor = processors.get(doc_type)
        if not processor:
            return None

        try:
            return processor(file_path)
        except Exception as e:
            logger.warning(f"[DocumentProcessor] ❌ 处理失败: {e}")
            return None

    def _process_excel(self, file_path: Path) -> Optional[ExtractedDocument]:
        """
        处理 Excel 文件

        提取所有工作表为 Markdown 表格
        """
        if not self.deps['pandas'] or not self.deps['openpyxl']:
            # 回退：使用系统命令转换为 CSV 再处理
            return self._process_excel_fallback(file_path)

        import pandas as pd

        # 读取所有工作表
        xl_file = pd.ExcelFile(file_path)
        sheet_names = xl_file.sheet_names

        content_lines = [f"# 📊 Excel: {file_path.stem}", ""]
        metadata = {
            "sheets": len(sheet_names),
            "sheet_names": sheet_names,
            "total_rows": 0,
            "total_cells": 0
        }

        for sheet_name in sheet_names:
            df = pd.read_excel(file_path, sheet_name=sheet_name)

            # 跳过空表
            if df.empty:
                continue

            content_lines.append(f"## 工作表: {sheet_name}")
            content_lines.append("")

            # 转换为 Markdown 表格
            # 限制显示前100行，避免过大
            display_df = df.head(100)

            # 生成表头
            headers = [str(col) for col in display_df.columns]
            content_lines.append("| " + " | ".join(headers) + " |")
            content_lines.append("| " + " | ".join(["---"] * len(headers)) + " |")

            # 生成数据行
            for _, row in display_df.iterrows():
                cells = [str(cell) if pd.notna(cell) else "" for cell in row]
                # 截断过长的单元格
                cells = [c[:100] + "..." if len(c) > 100 else c for c in cells]
                content_lines.append("| " + " | ".join(cells) + " |")

            if len(df) > 100:
                content_lines.append("")
                content_lines.append(f"*... 共 {len(df)} 行，显示前 100 行*")

            content_lines.append("")

            metadata["total_rows"] += len(df)
            metadata["total_cells"] += len(df) * len(df.columns)

        summary = f"Excel文件：{len(sheet_names)}个工作表，共{metadata['total_rows']}行"

        return ExtractedDocument(
            doc_type=DocumentType.EXCEL,
            filename=file_path.name,
            title=file_path.stem,
            content="\n".join(content_lines),
            metadata=metadata,
            summary=summary,
            validation_status="pending",
            needs_review=False,
            review_reason="",
            confidence=0.8,  # Excel提取通常较准确
            processing_method="local"
        )

    def _process_excel_fallback(self, file_path: Path) -> Optional[ExtractedDocument]:
        """Excel 处理回退方案（使用系统命令）"""
        logger.warning(f"[DocumentProcessor] ⚠️ 使用回退方案处理 Excel...")

        import tempfile
        tmp_dir = Path(tempfile.gettempdir())

        # 尝试使用 ssconvert (Gnumeric) 或 LibreOffice 转换
        temp_csv = tmp_dir / f"excel_export_{datetime.now().strftime('%Y%m%d%H%M%S')}.csv"

        try:
            # 检查 LibreOffice 是否可用
            if not shutil.which("libreoffice"):
                raise FileNotFoundError("libreoffice 未安装")

            # 尝试使用 LibreOffice 转换
            result = subprocess.run(
                ['libreoffice', '--headless', '--convert-to', 'csv', '--outdir', str(tmp_dir), str(file_path)],
                capture_output=True,
                timeout=30
            )

            if result.returncode == 0:
                # 查找生成的 CSV 文件
                csv_files = list(tmp_dir.glob(f"{file_path.stem}*.csv"))
                if csv_files:
                    # 读取 CSV 并转为 Markdown
                    content_lines = [f"# 📊 Excel: {file_path.stem}", ""]

                    with open(csv_files[0], 'r', encoding='utf-8', errors='ignore') as f:
                        lines = f.readlines()

                    if lines:
                        # 第一行作为表头
                        headers = lines[0].strip().split(',')
                        content_lines.append("| " + " | ".join(headers) + " |")
                        content_lines.append("| " + " | ".join(["---"] * len(headers)) + " |")

                        # 数据行（限制前100行）
                        for line in lines[1:101]:
                            cells = line.strip().split(',')
                            content_lines.append("| " + " | ".join(cells) + " |")

                        if len(lines) > 101:
                            content_lines.append(f"\n*... 共 {len(lines)-1} 行，显示前 100 行*")

                    # 清理临时文件
                    for cf in csv_files:
                        cf.unlink()

                    return ExtractedDocument(
                        doc_type=DocumentType.EXCEL,
                        filename=file_path.name,
                        title=file_path.stem,
                        content="\n".join(content_lines),
                        metadata={"method": "libreoffice_fallback"},
                        summary=f"Excel文件（LibreOffice转换）",
                        validation_status="pending",
                        needs_review=False,
                        review_reason="",
                        confidence=0.6,  # 回退方案置信度较低
                        processing_method="fallback"
                    )

        except Exception as e:
            logger.warning(f"[DocumentProcessor] ❌ 回退处理失败: {e}")

        return None

    def _process_ppt(self, file_path: Path) -> Optional[ExtractedDocument]:
        """
        处理 PowerPoint 文件

        提取每页的标题和内容
        """
        if not self.deps['pptx']:
            logger.info(f"[DocumentProcessor] ❌ 缺少 python-pptx，无法处理 PPT")
            logger.info(f"[DocumentProcessor] 安装: pip install python-pptx")
            return None

        from pptx import Presentation

        prs = Presentation(file_path)

        content_lines = [f"# 📽️ PowerPoint: {file_path.stem}", ""]
        content_lines.append(f"**幻灯片数量**: {len(prs.slides)}")
        content_lines.append("")

        slides_content = []
        total_text_chars = 0

        for i, slide in enumerate(prs.slides, 1):
            slide_lines = [f"## 幻灯片 {i}", ""]

            # 提取所有文本
            slide_texts = []
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text.strip():
                    slide_texts.append(shape.text.strip())
                    total_text_chars += len(shape.text)

            if slide_texts:
                # 第一行通常作为标题
                slide_lines.append(f"### {slide_texts[0]}")
                slide_lines.append("")

                # 其余作为内容
                for text in slide_texts[1:]:
                    # 分段处理
                    paragraphs = text.split('\n')
                    for para in paragraphs:
                        if para.strip():
                            slide_lines.append(para.strip())

            slides_content.append("\n".join(slide_lines))

        # 合并所有幻灯片内容
        content_lines.extend(slides_content)

        metadata = {
            "slides": len(prs.slides),
            "total_text_chars": total_text_chars
        }

        summary = f"PPT文件：{len(prs.slides)}页幻灯片"

        return ExtractedDocument(
            doc_type=DocumentType.PPT,
            filename=file_path.name,
            title=file_path.stem,
            content="\n\n".join(content_lines),
            metadata=metadata,
            summary=summary,
            validation_status="pending",
            needs_review=False,
            review_reason="",
            confidence=0.75,
            processing_method="local"
        )

    def _process_pdf(self, file_path: Path) -> Optional[ExtractedDocument]:
        """
        处理 PDF 文件

        提取文本并保留页码信息
        """
        if not self.deps['PyPDF2']:
            # 回退到 pdftotext (poppler)
            return self._process_pdf_fallback(file_path)

        import PyPDF2

        content_lines = [f"# 📄 PDF: {file_path.stem}", ""]
        metadata = {
            "pages": 0,
            "extracted_pages": 0
        }

        try:
            with open(file_path, 'rb') as f:
                pdf_reader = PyPDF2.PdfReader(f)
                num_pages = len(pdf_reader.pages)
                metadata["pages"] = num_pages

                content_lines.append(f"**总页数**: {num_pages}")
                content_lines.append("")

                # 提取每页内容
                for i, page in enumerate(pdf_reader.pages, 1):
                    try:
                        text = page.extract_text()
                        if text and text.strip():
                            content_lines.append(f"## 第 {i} 页")
                            content_lines.append("")
                            content_lines.append(text.strip())
                            content_lines.append("")
                            metadata["extracted_pages"] += 1
                    except Exception as e:
                        content_lines.append(f"*第 {i} 页提取失败: {e}*")
                        content_lines.append("")

        except Exception as e:
            logger.warning(f"[DocumentProcessor] ❌ PDF 处理失败: {e}")
            return self._process_pdf_fallback(file_path)

        summary = f"PDF文件：{metadata['pages']}页，成功提取{metadata['extracted_pages']}页"

        return ExtractedDocument(
            doc_type=DocumentType.PDF,
            filename=file_path.name,
            title=file_path.stem,
            content="\n".join(content_lines),
            metadata=metadata,
            summary=summary,
            validation_status="pending",
            needs_review=False,
            review_reason="",
            confidence=0.7,
            processing_method="local"
        )

    def _process_pdf_fallback(self, file_path: Path) -> Optional[ExtractedDocument]:
        """PDF 处理回退方案（使用 pdftotext）"""
        logger.warning(f"[DocumentProcessor] ⚠️ 使用回退方案处理 PDF...")

        # 检查 pdftotext 是否可用
        if not shutil.which("pdftotext"):
            logger.warning(f"[DocumentProcessor] ⚠️ pdftotext 未安装，跳过 PDF 回退处理")
            return None

        try:
            result = subprocess.run(
                ['pdftotext', '-layout', str(file_path), '-'],
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0 and result.stdout:
                text = result.stdout

                # 尝试按页分割（如果 pdftotext 支持 -f -l 参数）
                content_lines = [f"# 📄 PDF: {file_path.stem}", ""]
                content_lines.append(text[:50000])  # 限制大小

                return ExtractedDocument(
                    doc_type=DocumentType.PDF,
                    filename=file_path.name,
                    title=file_path.stem,
                    content="\n".join(content_lines),
                    metadata={"method": "pdftotext_fallback"},
                    summary=f"PDF文件（pdftotext提取）",
                    validation_status="pending",
                    needs_review=False,
                    review_reason="",
                    confidence=0.5,  # 回退方案置信度低
                    processing_method="fallback"
                )

        except Exception as e:
            logger.warning(f"[DocumentProcessor] ❌ 回退处理失败: {e}")

        return None

    def _process_word(self, file_path: Path) -> Optional[ExtractedDocument]:
        """
        处理 Word 文件

        提取段落和表格
        """
        if not self.deps['docx']:
            logger.info(f"[DocumentProcessor] ❌ 缺少 python-docx，无法处理 Word")
            logger.info(f"[DocumentProcessor] 安装: pip install python-docx")
            return None

        from docx import Document

        doc = Document(file_path)

        content_lines = [f"# 📝 Word: {file_path.stem}", ""]
        metadata = {
            "paragraphs": len(doc.paragraphs),
            "tables": len(doc.tables)
        }

        # 提取段落
        for para in doc.paragraphs:
            text = para.text.strip()
            if text:
                # 根据样式判断标题级别
                if para.style.name.startswith('Heading'):
                    level = para.style.name.replace('Heading ', '')
                    try:
                        level_num = int(level)
                        content_lines.append(f"{'#' * level_num} {text}")
                    except ValueError:
                        content_lines.append(f"## {text}")
                else:
                    content_lines.append(text)

        # 提取表格
        if doc.tables:
            content_lines.append("")
            content_lines.append("## 表格")
            content_lines.append("")

            for i, table in enumerate(doc.tables, 1):
                content_lines.append(f"### 表格 {i}")
                content_lines.append("")

                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    content_lines.append("| " + " | ".join(cells) + " |")

                content_lines.append("")

        summary = f"Word文件：{metadata['paragraphs']}段落，{metadata['tables']}表格"

        return ExtractedDocument(
            doc_type=DocumentType.WORD,
            filename=file_path.name,
            title=file_path.stem,
            content="\n\n".join(content_lines),
            metadata=metadata,
            summary=summary,
            validation_status="pending",
            needs_review=False,
            review_reason="",
            confidence=0.75,
            processing_method="local"
        )

    def _process_html(self, file_path: Path) -> Optional[ExtractedDocument]:
        """
        处理 HTML 文件

        转换为 Markdown
        """
        content = file_path.read_text(encoding='utf-8', errors='ignore')

        if self.deps['markdownify']:
            import markdownify
            markdown_content = markdownify.markdownify(content, heading_style="ATX")
        elif self.deps['beautifulsoup4']:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(content, 'html.parser')
            # 移除 script 和 style
            for script in soup(["script", "style"]):
                script.decompose()
            markdown_content = soup.get_text(separator='\n', strip=True)
        else:
            # 纯文本提取
            # 移除 HTML 标签的简单实现
            markdown_content = re.sub(r'<[^>]+>', '', content)
            markdown_content = re.sub(r'\n\s*\n', '\n\n', markdown_content)

        # 添加标题
        title = file_path.stem
        # 尝试从 HTML 中提取 title
        title_match = re.search(r'<title[^>]*>([^<]+)</title>', content, re.IGNORECASE)
        if title_match:
            title = title_match.group(1).strip()

        content_lines = [
            f"# 🌐 HTML: {title}",
            "",
            f"**源文件**: {file_path.name}",
            "",
            markdown_content[:30000]  # 限制大小
        ]

        metadata = {
            "original_size": len(content),
            "extracted_size": len(markdown_content)
        }

        summary = f"HTML文件：原始{len(content)}字符，提取{len(markdown_content)}字符"

        return ExtractedDocument(
            doc_type=DocumentType.HTML,
            filename=file_path.name,
            title=title,
            content="\n".join(content_lines),
            metadata=metadata,
            summary=summary,
            validation_status="pending",
            needs_review=False,
            review_reason="",
            confidence=0.7,
            processing_method="local"
        )

    def save_to_memos(self, doc: ExtractedDocument) -> Optional[str]:
        """保存提取的内容到 Memos"""
        # 使用正确的标签：人工导入使用 source=human，不使用 model 标签
        tags = [
            "source=human",
            f"time={datetime.now().strftime('%Y%m%d')}",
            "scope=public",
            f"doc:type={doc.doc_type.value}",
            f"doc:file={doc.filename}",
            "inbox:document"
        ]

        # 添加验证状态标签
        if doc.validation_status == "validated":
            tags.append("validation:passed")
        elif doc.validation_status == "review":
            tags.append("validation:needs-review")
            tags.append("scope:private")  # 待审核内容设为私有

        # 构建完整内容
        full_content = f"""{doc.content}

---

## 元数据

- **原始文件**: {doc.filename}
- **文档类型**: {doc.doc_type.value}
- **提取时间**: {datetime.now().isoformat()}
- **内容摘要**: {doc.summary}
- **验证状态**: {doc.validation_status}
- **置信度**: {doc.confidence:.2f}

## 详细信息

```json
{json.dumps(doc.metadata, ensure_ascii=False, indent=2)}
```
"""

        if doc.needs_review:
            full_content += f"""

---

## ⚠️ 人工核对提醒

此文档由系统自动提取，验证置信度较低（{doc.confidence:.2f}），需要人工核对。

**核对要点**:
1. 内容是否完整？
2. 表格/结构是否正确？
3. 文字是否有误？

**需要核对的原因**: {doc.review_reason}

核对后请删除此提醒，并将 scope:private 标签移除。
"""

        try:
            result = self.client.save(content=full_content, tags=tags)
            memos_uid = result.uid if hasattr(result, 'uid') else str(result)
            logger.info(f"[DocumentProcessor] ✅ 已保存: {memos_uid[:16]}...")
            return memos_uid
        except Exception as e:
            logger.warning(f"[DocumentProcessor] ❌ 保存失败: {e}")
            return None

    def save_to_memos_with_review(self, file_path: Path, doc: ExtractedDocument) -> Optional[str]:
        """
        保存需要人工审核的文档到 Memos

        与 save_to_memos 的区别：
        1. 强制标记为需要审核
        2. 设为私有可见性
        3. 添加审核提醒
        """
        # 强制设置审核标记
        doc.needs_review = True
        doc.validation_status = "review"

        if not doc.review_reason:
            doc.review_reason = "验证置信度低，需要人工核对"

        return self.save_to_memos(doc)

    def save_to_rejected(self, doc: ExtractedDocument, file_path: Path) -> Path:
        """
        将验证拒绝的文档保存到隔离目录，不入主库
        返回保存路径
        """
        from core.config import get_config
        rejected_dir = get_config().data_dir / "rejected_documents"
        rejected_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"{timestamp}_{doc.filename}"

        # 保存元数据
        meta = {
            "filename": doc.filename,
            "doc_type": doc.doc_type.value,
            "validation_status": doc.validation_status,
            "confidence": doc.confidence,
            "review_reason": doc.review_reason,
            "original_path": str(file_path),
            "rejected_at": datetime.now().isoformat(),
            "content_preview": doc.content[:2000],
        }
        meta_path = rejected_dir / f"{base_name}.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        # 保存原始文件副本
        if file_path.exists():
            import shutil
            dest = rejected_dir / f"{base_name}_orig{file_path.suffix}"
            shutil.copy2(file_path, dest)

        logger.info(f"[DocumentProcessor] 🚫 已拒绝并隔离: {meta_path}")
        return meta_path


def main():
    """CLI入口"""
    import argparse
    parser = argparse.ArgumentParser(description="Document Processor")
    parser.add_argument("file", nargs="?", help="文档文件路径")
    parser.add_argument("--check-deps", action="store_true", help="检查依赖")
    parser.add_argument("--save", action="store_true", help="保存到Memos")

    args = parser.parse_args()

    processor = DocumentProcessor()

    if args.check_deps:
        logger.info("📦 依赖状态:")
        for dep, available in processor.deps.items():
            status = "✅" if available else "❌"
            logger.info(f"  {status} {dep}")
        return

    if not args.file:
        parser.print_help()
        return

    file_path = Path(args.file)
    doc = processor.process_document(file_path)

    if doc:
        logger.info(f"\n{'='*50}")
        logger.info(f"处理结果:")
        logger.info(f"  类型: {doc.doc_type.value}")
        logger.info(f"  标题: {doc.title}")
        logger.info(f"  摘要: {doc.summary}")
        logger.info(f"{'='*50}")
        logger.info(f"\n内容预览（前500字符）:")
        logger.info(doc.content[:500])
        logger.info(f"\n... ({len(doc.content)} 字符)")

        if args.save:
            memos_uid = processor.save_to_memos(doc)
            if memos_uid:
                logger.info(f"\n✅ 已保存到Memos: {memos_uid}")
    else:
        logger.warning("❌ 处理失败")


if __name__ == "__main__":
    main()
