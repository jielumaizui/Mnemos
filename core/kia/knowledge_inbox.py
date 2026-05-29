from __future__ import annotations
#!/usr/bin/env python3
import logging
"""
Knowledge Inbox Processor - Knowledge Inbox 文件处理模块
监控桌面文件夹，处理用户导入的文件
"""

import os
import sys
import json
import sqlite3
import time
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from core.config import get_config
from integrations.styx import MemosClient
from core.task_id_parser import TaskIdParser, TagBuilder
from core.hephaestus.document_processor import DocumentProcessor

# Ebook 处理（软依赖）
logger = logging.getLogger(__name__)
try:
    import ebooklib
    from ebooklib import epub
    EBOOKLIB_AVAILABLE = True
except ImportError:
    EBOOKLIB_AVAILABLE = False

# 热力追踪器（软依赖）
try:
    from core.wiki_metrics import WikiHeatTracker
    HEAT_TRACKER_AVAILABLE = True
except ImportError:
    HEAT_TRACKER_AVAILABLE = False


@dataclass
class InboxFile:
    """收件箱文件记录"""
    path: Path
    filename: str
    size: int
    mtime: float
    hash: str
    status: str  # pending, processing, done, error
    processed_at: Optional[str] = None
    memos_uid: Optional[str] = None
    error_msg: Optional[str] = None


class KnowledgeInboxProcessor:
    """Knowledge Inbox 处理器"""

    # 支持的文件类型
    SUPPORTED_EXTENSIONS = {
        # 文本文件
        '.txt', '.md', '.markdown',
        '.json', '.yaml', '.yml',
        '.py', '.js', '.ts', '.sh',
        '.sql', '.log',
        # 结构化图片（需要特殊处理）
        '.png', '.jpg', '.jpeg', '.gif', '.webp',
        # 文档文件（需要特殊处理）
        '.xlsx', '.xls', '.pptx', '.ppt',
        '.pdf', '.docx', '.doc',
        '.html', '.htm',
        # 电子书格式
        '.epub', '.mobi', '.azw3',
    }

    # 结构化图片扩展名
    IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}

    # 文档扩展名
    DOCUMENT_EXTENSIONS = {
        '.xlsx', '.xls', '.pptx', '.ppt',
        '.pdf', '.docx', '.doc',
        '.html', '.htm'
    }

    # 电子书扩展名
    EBOOK_EXTENSIONS = {'.epub', '.mobi', '.azw3'}

    def __init__(self):
        self.inbox_dir = Path.home() / "Desktop" / "到家" / "ai" / "knowledge_inbox"
        self.state_db = get_config().data_dir / "inbox_state.db"
        self.processed_dir = self.inbox_dir / ".processed"
        self.failed_dir = self.inbox_dir / ".failed"
        self.report_dir = self.inbox_dir / ".reports"

        # 确保目录存在
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(exist_ok=True)
        self.failed_dir.mkdir(exist_ok=True)
        self.report_dir.mkdir(exist_ok=True)

        # 初始化 SQLite 状态数据库
        self._init_state_db()

        # Memos客户端
        cfg = get_config()
        self.client = MemosClient(
            token=cfg.memos_token,
            base_url=cfg.memos_api_url,
            agent="inbox-processor"
        )

        # 文档处理器
        self.document_processor = DocumentProcessor()

        # 热力追踪器（可选）
        self.heat_tracker = None
        if HEAT_TRACKER_AVAILABLE:
            try:
                self.heat_tracker = WikiHeatTracker()
            except Exception as e:
                logger.warning(f"[KnowledgeInbox] 热力追踪器初始化失败: {e}")

        # TODO: 来源追踪功能原由 IngestEngine 提供，该模块已移除。
        # 如需恢复，请使用 SyncEngine 或扩展 _save_state 记录来源详情。

    # ... 类定义继续 ...

    def _init_state_db(self):
        """初始化 SQLite 状态数据库"""
        with sqlite3.connect(str(self.state_db), timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_files (
                    file_hash TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    memos_uid TEXT,
                    status TEXT DEFAULT 'success',  -- success/failed/skipped
                    error_msg TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scan_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    files_found INTEGER DEFAULT 0,
                    files_processed INTEGER DEFAULT 0,
                    files_failed INTEGER DEFAULT 0,
                    report_path TEXT
                )
            """)
            conn.commit()

    def _load_state(self) -> Dict:
        """从 SQLite 加载处理状态（兼容旧 JSON）"""
        # 如果旧 JSON 存在，迁移到 SQLite
        old_state_file = get_config().data_dir / "inbox_state.json"
        if old_state_file.exists():
            try:
                old = json.loads(old_state_file.read_text(encoding="utf-8"))
                with sqlite3.connect(str(self.state_db), timeout=10) as conn:
                    for h, info in old.get("processed_files", {}).items():
                        conn.execute("""
                            INSERT OR IGNORE INTO processed_files
                            (file_hash, filename, processed_at, memos_uid, status)
                            VALUES (?, ?, ?, ?, 'success')
                        """, (h, info.get("filename", ""), info.get("processed_at"), info.get("memos_uid")))
                    conn.commit()
                import shutil
                shutil.move(str(old_state_file), str(old_state_file.with_suffix(".json.bak")))
                logger.info("[Inbox] 状态已迁移到 SQLite")
            except Exception as e:
                logger.warning(f"[Inbox] 状态迁移失败: {e}")

        with sqlite3.connect(str(self.state_db), timeout=10) as conn:
            cursor = conn.execute("SELECT file_hash, status FROM processed_files")
            files = {row[0]: row[1] for row in cursor.fetchall()}
            cursor = conn.execute("SELECT MAX(scan_time) FROM scan_log")
            last_scan = cursor.fetchone()[0]
        return {"processed_files": files, "last_scan": last_scan}

    def _save_state(self, file_hash: str, filename: str, memos_uid: str = None,
                    status: str = "success", error_msg: str = None):
        """保存单个文件处理状态到 SQLite"""
        with sqlite3.connect(str(self.state_db), timeout=10) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO processed_files
                (file_hash, filename, processed_at, memos_uid, status, error_msg)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (file_hash, filename, datetime.now().isoformat(), memos_uid, status, error_msg))
            conn.commit()

    def _log_scan(self, files_found: int, files_processed: int,
                  files_failed: int, report_path: str = None) -> int:
        """记录扫描日志，返回 scan_id"""
        with sqlite3.connect(str(self.state_db), timeout=10) as conn:
            cursor = conn.execute("""
                INSERT INTO scan_log (files_found, files_processed, files_failed, report_path)
                VALUES (?, ?, ?, ?)
            """, (files_found, files_processed, files_failed, report_path))
            conn.commit()
            return cursor.lastrowid

    def _compute_hash(self, file_path: Path) -> str:
        """计算文件哈希"""
        hasher = hashlib.md5()
        hasher.update(file_path.read_bytes())
        return hasher.hexdigest()[:16]

    def scan_inbox(self) -> List[InboxFile]:
        """扫描收件箱，返回待处理文件列表"""
        state = self._load_state()
        pending_files = []

        # 遍历inbox目录
        for file_path in self.inbox_dir.iterdir():
            if not file_path.is_file():
                continue

            # 跳过隐藏文件和已处理标记
            if file_path.name.startswith('.'):
                continue

            # 检查扩展名
            if file_path.suffix.lower() not in self.SUPPORTED_EXTENSIONS:
                continue

            # 计算哈希
            file_hash = self._compute_hash(file_path)

            # 检查是否已处理（SQLite 中 status != pending）
            if file_hash in state["processed_files"]:
                continue

            stat = file_path.stat()
            inbox_file = InboxFile(
                path=file_path,
                filename=file_path.name,
                size=stat.st_size,
                mtime=stat.st_mtime,
                hash=file_hash,
                status="pending"
            )
            pending_files.append(inbox_file)

        return pending_files

    def _extract_content(self, file_path: Path) -> Tuple[str, str]:
        """
        提取文件内容

        Returns:
            (content, content_type)
        """
        suffix = file_path.suffix.lower()

        try:
            content = file_path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            # 尝试其他编码
            content = file_path.read_text(encoding='gbk', errors='replace')

        # 判断内容类型
        content_type = "text"
        if suffix in ['.md', '.markdown']:
            content_type = "markdown"
        elif suffix in ['.json']:
            content_type = "json"
        elif suffix in ['.yaml', '.yml']:
            content_type = "yaml"
        elif suffix in ['.py', '.js', '.ts', '.sh', '.sql']:
            content_type = "code"

        return content, content_type

    def _build_memos_content(self, inbox_file: InboxFile, content: str, content_type: str) -> str:
        """构建Memos内容"""
        lines = [
            f"# Inbox Import: {inbox_file.filename}",
            f"",
            f"**Source**: human-local",
            f"**Size**: {inbox_file.size} bytes",
            f"**Type**: {content_type}",
            f"**Imported**: {datetime.now().isoformat()}",
            f"**Hash**: {inbox_file.hash}",
            f"",
            "---",
            f"",
        ]

        if content_type == "code":
            lines.append(f"```{inbox_file.filename.split('.')[-1]}")
            lines.append(content)
            lines.append("```")
        elif content_type in ["json", "yaml"]:
            lines.append("```yaml")
            lines.append(content[:2000])  # 限制长度
            if len(content) > 2000:
                lines.append(f"\n... (truncated, total {len(content)} chars)")
            lines.append("```")
        else:
            lines.append(content[:3000])
            if len(content) > 3000:
                lines.append(f"\n\n... (truncated, total {len(content)} chars)")

        return "\n".join(lines)

    def process_file(self, inbox_file: InboxFile) -> Dict:
        """处理单个文件"""
        result = {
            "success": False,
            "file": inbox_file.filename,
            "hash": inbox_file.hash,
            "memos_uid": None,
            "error": None
        }

        try:
            suffix = inbox_file.path.suffix.lower()

            # 检查是否为结构化图片
            if suffix in self.IMAGE_EXTENSIONS:
                return self._process_image_file(inbox_file, result)

            # 检查是否为文档文件
            if suffix in self.DOCUMENT_EXTENSIONS:
                return self._process_document_file(inbox_file, result)

            # 检查是否为电子书
            if suffix in self.EBOOK_EXTENSIONS:
                return self._process_ebook_file(inbox_file, result)

            # 处理普通文本文件
            return self._process_text_file(inbox_file, result)

        except Exception as e:
            inbox_file.status = "error"
            inbox_file.error_msg = str(e)
            result["error"] = str(e)
            self._move_to_failed(inbox_file, str(e))
            self._save_state(inbox_file.hash, inbox_file.filename, status="failed", error_msg=str(e))
            # TODO: 来源追踪功能原由 IngestEngine 提供，该模块已移除。
            return result

    def _move_to_failed(self, inbox_file: InboxFile, error_msg: str):
        """将失败的文件移动到失败目录"""
        try:
            failed_path = self.failed_dir / f"{inbox_file.hash}_{inbox_file.filename}"
            shutil.move(str(inbox_file.path), str(failed_path))
            # 写入错误信息
            error_file = self.failed_dir / f"{inbox_file.hash}_{inbox_file.filename}.error.txt"
            error_file.write_text(f"处理时间: {datetime.now().isoformat()}\n错误: {error_msg}\n", encoding="utf-8")
        except Exception as e:
            logger.warning(f"[Inbox] 移动失败文件失败: {e}")

    def _process_text_file(self, inbox_file: InboxFile, result: Dict) -> Dict:
        """处理文本文件"""
        # 提取内容
        content, content_type = self._extract_content(inbox_file.path)

        # 构建Memos内容
        memos_content = self._build_memos_content(inbox_file, content, content_type)

        # 解析Task ID（从文件名或内容中）
        task_id = TaskIdParser.parse(inbox_file.filename + " " + content[:200])

        # 构建标签（人工导入使用 source=human，不使用 model 标签）
        tags = [
            "source=human",
            f"time={datetime.now().strftime('%Y%m%d')}",
            "scope=public",
        ]
        if task_id:
            tags.append(task_id)
        # 添加额外标签
        tags.extend([
            f"inbox:{content_type}",
            f"file:{inbox_file.filename}",
            f"size:{inbox_file.size}"
        ])

        # 保存到Memos
        memos_result = self.client.save(content=memos_content, tags=tags)
        memos_uid = memos_result.uid if hasattr(memos_result, 'uid') else str(memos_result)

        # 标记为已处理
        inbox_file.status = "done"
        inbox_file.processed_at = datetime.now().isoformat()
        inbox_file.memos_uid = memos_uid

        # 移动到已处理目录
        processed_path = self.processed_dir / f"{inbox_file.hash}_{inbox_file.filename}"
        shutil.move(str(inbox_file.path), str(processed_path))

        # 保存状态到SQLite
        self._save_state(inbox_file.hash, inbox_file.filename, memos_uid, status="success")

        # 记录文件监控事件
        # TODO: 来源追踪功能原由 IngestEngine 提供，该模块已移除。

        # 初始化热力追踪
        self._init_heat_tracking(inbox_file.filename, memos_uid, "text")

        result["success"] = True
        result["memos_uid"] = memos_uid
        return result

    def _init_heat_tracking(self, filename: str, memos_uid: str, content_type: str):
        """初始化热力追踪"""
        if self.heat_tracker and memos_uid:
            try:
                page_id = f"inbox/{filename}"
                self.heat_tracker.init_page(page_id, initial_level="L1")
                logger.info(f"[Inbox] 热力追踪已初始化: {page_id}")
            except Exception as e:
                logger.warning(f"[Inbox] 热力追踪初始化失败: {e}")

    def _process_image_file(self, inbox_file: InboxFile, result: Dict) -> Dict:
        """处理图片文件——人工解析流程"""
        logger.info(f"[Inbox] 检测到图片文件: {inbox_file.filename}")
        logger.info("[Inbox] 图片处理已改为人工流程：请用 Kimi/豆包等工具解析图片内容，")
        logger.info("        将解析结果保存为 .md 文件后重新放入 inbox，系统将自动入库。")

        # 将原图片移动到待人工处理目录
        manual_dir = self.inbox_dir / ".manual"
        manual_dir.mkdir(exist_ok=True)
        manual_path = manual_dir / f"{inbox_file.hash}_{inbox_file.filename}"
        try:
            shutil.move(str(inbox_file.path), str(manual_path))
        except Exception as e:
            logger.warning(f"[Inbox] 移动文件到 .manual 失败: {e}")

        result["success"] = False
        result["error"] = "图片需人工解析：请用 Kimi/豆包解析后保存为 .md 重新放入 inbox"
        self._save_state(inbox_file.hash, inbox_file.filename, status="skipped")
        return result

    def _process_document_file(self, inbox_file: InboxFile, result: Dict) -> Dict:
        """处理文档文件（Excel/PPT/PDF/Word/HTML）"""
        logger.info(f"[Inbox] 检测到文档文件: {inbox_file.filename}")

        # 使用文档处理器（带验证流程）
        extraction = self.document_processor.process_document_with_validation(inbox_file.path)

        if not extraction:
            result["error"] = "文档处理失败或内容为空"
            self._move_to_failed(inbox_file, "文档处理失败或内容为空")
            self._save_state(inbox_file.hash, inbox_file.filename, status="failed", error_msg="文档处理失败或内容为空")
            # TODO: 来源追踪功能原由 IngestEngine 提供，该模块已移除。
            return result

        # 根据验证状态决定后续操作
        if extraction.validation_status == "review":
            logger.info(f"[Inbox] 文档需要人工审核: {inbox_file.filename}")
            # 保存到Memos但标记为待审核
            memos_uid = self.document_processor.save_to_memos(extraction)
        elif extraction.validation_status == "failed":
            logger.warning(f"[Inbox] 文档验证失败: {inbox_file.filename}")
            # 即使失败也保存，但标记为待审核
            extraction.needs_review = True
            extraction.review_reason = "验证失败，需要人工核对"
            memos_uid = self.document_processor.save_to_memos(extraction)
        else:
            # 验证通过，正常保存
            memos_uid = self.document_processor.save_to_memos(extraction)

        if memos_uid:
            # 标记为已处理
            inbox_file.status = "done"
            inbox_file.processed_at = datetime.now().isoformat()
            inbox_file.memos_uid = memos_uid

            # 移动到已处理目录
            processed_path = self.processed_dir / f"{inbox_file.hash}_{inbox_file.filename}"
            shutil.move(str(inbox_file.path), str(processed_path))

            # 保存状态
            self._save_state(inbox_file.hash, inbox_file.filename, memos_uid, status="success")
            # TODO: 来源追踪功能原由 IngestEngine 提供，该模块已移除。

            # 初始化热力追踪
            doc_type = extraction.doc_type.value if hasattr(extraction, 'doc_type') else 'document'
            self._init_heat_tracking(inbox_file.filename, memos_uid, f"doc:{doc_type}")

            result["success"] = True
            result["memos_uid"] = memos_uid
            result["doc_type"] = extraction.doc_type.value
            result["validation_status"] = extraction.validation_status
            result["title"] = extraction.title
            if extraction.needs_review:
                result["review_reason"] = extraction.review_reason
        else:
            result["error"] = "保存到Memos失败"
            self._move_to_failed(inbox_file, "保存到Memos失败")
            self._save_state(inbox_file.hash, inbox_file.filename, status="failed", error_msg="保存到Memos失败")
            # TODO: 来源追踪功能原由 IngestEngine 提供，该模块已移除。

        return result

    def _process_ebook_file(self, inbox_file: InboxFile, result: Dict) -> Dict:
        """处理电子书文件（epub/mobi/azw3）"""
        logger.info(f"[Inbox] 检测到电子书文件: {inbox_file.filename}")

        if not EBOOKLIB_AVAILABLE and inbox_file.path.suffix.lower() == '.epub':
            # ebooklib 不可用，回退到文本提取
            logger.info(f"[Inbox] ebooklib 不可用，回退到文本提取")
            try:
                content = inbox_file.path.read_text(encoding='utf-8', errors='ignore')[:50000]
                return self._process_ebook_as_text(inbox_file, result, content)
            except Exception as e:
                result["error"] = f"电子书处理失败: {e}"
                self._move_to_failed(inbox_file, str(e))
                self._save_state(inbox_file.hash, inbox_file.filename, status="failed", error_msg=str(e))
                return result

        # 使用 ebooklib 处理 epub
        if inbox_file.path.suffix.lower() == '.epub' and EBOOKLIB_AVAILABLE:
            try:
                book = epub.read_epub(str(inbox_file.path))
                # 提取元数据
                title = book.get_metadata('DC', 'Title')
                title = title[0][0] if title else inbox_file.filename
                author = book.get_metadata('DC', 'Creator')
                author = author[0][0] if author else "Unknown"

                # 提取文本内容
                content_parts = []
                for item in book.get_items():
                    if item.get_type() == ebooklib.ITEM_DOCUMENT:
                        try:
                            from bs4 import BeautifulSoup
                            soup = BeautifulSoup(item.get_content(), 'html.parser')
                            text = soup.get_text(separator='\n', strip=True)
                            if text:
                                content_parts.append(text)
                        except Exception:
                            logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
                            pass
                full_content = "\n\n".join(content_parts)
                if len(full_content) > 100000:
                    full_content = full_content[:100000] + "\n\n... (内容截断)"

                # 构建 Memos 内容
                memos_content = f"""# Ebook: {title}

**作者**: {author}
**文件**: {inbox_file.filename}
**导入时间**: {datetime.now().isoformat()}

---

{full_content[:50000]}
"""
                if len(full_content) > 50000:
                    memos_content += f"\n\n... (共 {len(full_content)} 字符，已截断)"

                tags = [
                    "source=human",
                    f"time={datetime.now().strftime('%Y%m%d')}",
                    "scope=public",
                    "inbox:ebook",
                    f"file:{inbox_file.filename}",
                    f"ebook:title={title[:50]}",
                ]

                memos_result = self.client.save(content=memos_content, tags=tags)
                memos_uid = memos_result.uid if hasattr(memos_result, 'uid') else str(memos_result)

                if memos_uid:
                    inbox_file.status = "done"
                    inbox_file.processed_at = datetime.now().isoformat()
                    inbox_file.memos_uid = memos_uid

                    processed_path = self.processed_dir / f"{inbox_file.hash}_{inbox_file.filename}"
                    shutil.move(str(inbox_file.path), str(processed_path))

                    self._save_state(inbox_file.hash, inbox_file.filename, memos_uid, status="success")
                    self._init_heat_tracking(inbox_file.filename, memos_uid, "ebook")

                    result["success"] = True
                    result["memos_uid"] = memos_uid
                    result["title"] = title
                    return result
                else:
                    raise Exception("保存到Memos失败")

            except Exception as e:
                result["error"] = f"EPUB 处理失败: {e}"
                self._move_to_failed(inbox_file, str(e))
                self._save_state(inbox_file.hash, inbox_file.filename, status="failed", error_msg=str(e))
                return result
        else:
            # mobi/azw3 暂不支持 ebooklib，回退到文本提取
            try:
                content = inbox_file.path.read_text(encoding='utf-8', errors='ignore')[:50000]
                return self._process_ebook_as_text(inbox_file, result, content)
            except Exception as e:
                result["error"] = f"电子书处理失败: {e}"
                self._move_to_failed(inbox_file, str(e))
                self._save_state(inbox_file.hash, inbox_file.filename, status="failed", error_msg=str(e))
                return result

    def _process_ebook_as_text(self, inbox_file: InboxFile, result: Dict, content: str) -> Dict:
        """将电子书作为纯文本处理（回退方案）"""
        memos_content = f"""# Ebook: {inbox_file.filename}

**文件**: {inbox_file.filename}
**导入时间**: {datetime.now().isoformat()}
**注意**: 回退到纯文本提取，格式可能丢失

---

{content[:30000]}
"""
        if len(content) > 30000:
            memos_content += f"\n\n... (共 {len(content)} 字符，已截断)"

        tags = [
            "source=human",
            f"time={datetime.now().strftime('%Y%m%d')}",
            "scope=public",
            "inbox:ebook",
            f"file:{inbox_file.filename}",
            "ebook:fallback=text",
        ]

        memos_result = self.client.save(content=memos_content, tags=tags)
        memos_uid = memos_result.uid if hasattr(memos_result, 'uid') else str(memos_result)

        if memos_uid:
            inbox_file.status = "done"
            inbox_file.processed_at = datetime.now().isoformat()
            inbox_file.memos_uid = memos_uid

            processed_path = self.processed_dir / f"{inbox_file.hash}_{inbox_file.filename}"
            shutil.move(str(inbox_file.path), str(processed_path))

            self._save_state(inbox_file.hash, inbox_file.filename, memos_uid, status="success")
            # TODO: 来源追踪功能原由 IngestEngine 提供，该模块已移除。
            self._init_heat_tracking(inbox_file.filename, memos_uid, "ebook")

            result["success"] = True
            result["memos_uid"] = memos_uid
        else:
            result["error"] = "保存到Memos失败"
            self._move_to_failed(inbox_file, "保存到Memos失败")
            self._save_state(inbox_file.hash, inbox_file.filename, status="failed", error_msg="保存到Memos失败")
            # TODO: 来源追踪功能原由 IngestEngine 提供，该模块已移除。

        return result

    def run(self) -> List[Dict]:
        """运行处理器，返回处理结果列表，并生成报告"""
        pending_files = self.scan_inbox()

        if not pending_files:
            logger.info("[Inbox] 没有待处理文件")
            return []

        results = []
        success_count = 0
        failed_count = 0
        skipped_count = 0

        for inbox_file in pending_files:
            logger.info(f"Processing: {inbox_file.filename}...")
            result = self.process_file(inbox_file)
            results.append(result)

            if result["success"]:
                success_count += 1
            elif result.get("error"):
                failed_count += 1
            else:
                skipped_count += 1

        # 生成报告
        report_path = self.generate_report(results, success_count, failed_count, skipped_count)

        # 记录扫描日志
        self._log_scan(len(pending_files), success_count, failed_count, report_path)

        logger.warning(f"\n[Inbox] 处理完成: {success_count} 成功, {failed_count} 失败, {skipped_count} 跳过")
        if report_path:
            logger.info(f"[Inbox] 报告已生成: {report_path}")

        return results

    def generate_report(self, results: List[Dict], success: int, failed: int,
                        skipped: int) -> Optional[str]:
        """生成处理报告"""
        if not results:
            return None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = self.report_dir / f"report_{timestamp}.md"

        lines = [
            f"# 📥 Knowledge Inbox 处理报告",
            f"",
            f"**时间**: {datetime.now().isoformat()}",
            f"**总文件数**: {len(results)}",
            f"**成功**: {success} | **失败**: {failed} | **跳过**: {skipped}",
            f"",
            f"---",
            f"",
            f"## 处理详情",
            f"",
        ]

        for r in results:
            status = "OK" if r["success"] else ("❌" if r.get("error") else "SKIP")
            lines.append(f"### {status} {r['file']}")
            if r["success"]:
                lines.append(f"- Memos UID: {r.get('memos_uid', 'N/A')}")
                if "doc_type" in r:
                    lines.append(f"- 文档类型: {r['doc_type']}")
                if "title" in r:
                    lines.append(f"- 标题: {r['title']}")
            elif r.get("error"):
                lines.append(f"- 错误: {r['error']}")
            else:
                lines.append(f"- 状态: 跳过")
            lines.append("")

        # 失败文件列表
        failed_files = [r for r in results if r.get("error")]
        if failed_files:
            lines.extend([
                f"---",
                f"",
                f"## 失败文件（位于 `.failed/` 目录）",
                f"",
            ])
            for r in failed_files:
                lines.append(f"- {r['file']}: {r['error']}")
            lines.append("")

        report_path.write_text("\n".join(lines), encoding="utf-8")
        return str(report_path)

    def get_status(self) -> Dict:
        """获取处理状态（从 SQLite）"""
        pending = self.scan_inbox()
        pending_count = len(pending)

        with sqlite3.connect(str(self.state_db), timeout=10) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM processed_files WHERE status = 'success'")
            success_count = cursor.fetchone()[0]
            cursor = conn.execute("SELECT COUNT(*) FROM processed_files WHERE status = 'failed'")
            failed_count = cursor.fetchone()[0]
            cursor = conn.execute("SELECT MAX(scan_time) FROM scan_log")
            last_scan = cursor.fetchone()[0]

        return {
            "inbox_dir": str(self.inbox_dir),
            "processed_count": success_count,
            "failed_count": failed_count,
            "pending_count": pending_count,
            "last_scan": last_scan,
            "pending_files": [f.filename for f in pending]
        }

    def list_processed(self) -> List[Dict]:
        """列出已处理的文件（从 SQLite）"""
        with sqlite3.connect(str(self.state_db), timeout=10) as conn:
            cursor = conn.execute("""
                SELECT file_hash, filename, processed_at, memos_uid, status
                FROM processed_files ORDER BY processed_at DESC
            """)
            return [
                {"hash": row[0], "filename": row[1], "processed_at": row[2],
                 "memos_uid": row[3], "status": row[4]}
                for row in cursor.fetchall()
            ]


def main():
    """CLI入口"""
    import argparse
    parser = argparse.ArgumentParser(description="Knowledge Inbox Processor")
    parser.add_argument("--run", action="store_true", help="运行处理器")
    parser.add_argument("--status", action="store_true", help="查看状态")
    parser.add_argument("--list", action="store_true", help="列出已处理文件")

    args = parser.parse_args()

    processor = KnowledgeInboxProcessor()

    if args.run:
        results = processor.run()
        logger.info(f"\n处理完成: {len(results)} 个文件")
        for r in results:
            status = "OK" if r["success"] else "❌"
            logger.warning(f"  {status} {r['file']}: {r.get('memos_uid', r.get('error'))}")

    elif args.list:
        processed = processor.list_processed()
        logger.info(f"已处理文件: {len(processed)}")
        for p in processed:
            logger.info(f"  - {p['filename']} ({p['processed_at'][:10]})")

    else:
        status = processor.get_status()
        logger.info(json.dumps(status, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()


# 提供兼容别名
KnowledgeInbox = KnowledgeInboxProcessor
