#!/usr/bin/env python3
from __future__ import annotations

import logging

"""
Knowledge Inbox Processor - Knowledge Inbox 文件处理模块
监控桌面文件夹，处理用户导入的文件
"""

import base64
import json
import mimetypes
import sqlite3
import hashlib
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass

from core.config import get_config
from core.sync_framework.storage_backend import StorageBackend, create_storage_backend
from core.task_id_parser import TaskIdParser
from core.hephaestus.document_processor import DocumentProcessor
from core.ingest_chunking import SemanticChunker
from core.telemetry.prompt_call_log import (
    ModelCallLedger,
    ModelCallReservation,
    metered_provider_usage,
)
from core.telemetry.provider_request import (
    ProviderRequestError,
    canonical_provider_input,
    safe_provider_error_category,
)

# Constants extracted from magic numbers
# [P115] 与 CaptureService.MAX_PAYLOAD_BYTES=200000 保持一致，避免 Inbox 内容被过度截断
KNOWLEDGE_INBOX_PROCESSOR__BUILD_STORAGE_CONTENT_CONTENT = 200000
CONTENT = 50000
KNOWLEDGE_INBOX_PROCESSOR__PROCESS_EBOOK_FILE_FULL_CONTENT = 100000
FULL_CONTENT = 100000
STORAGE_CONTENT = 50000
KNOWLEDGE_INBOX_PROCESSOR__PROCESS_EBOOK_FILE_FULL_CONTENT_2 = 50000
STORAGE_RESULT = 30
STORAGE_CONTENT_2 = 30000
KNOWLEDGE_INBOX_PROCESSOR__PROCESS_EBOOK_AS_TEXT_CONTENT = 30000
MULTIMODAL_EXTRACT_MAX_TOKENS = 2000
MULTIMODAL_API_TEMPERATURE = 0.1


def _configured_multimodal_input_token_reservation(config: Any) -> int:
    """Return the explicit worst-case vision-input allowance before dispatch.

    Image bytes are not a safe proxy for vision tokens: a small compressed
    image can expand into many provider-side patches.  The caller must use the
    configured hard allowance instead of silently under-reserving a request.
    """
    value = config.get("multimodal.max_input_tokens")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError(
            "multimodal.max_input_tokens must be configured as a positive integer "
            "before a vision provider request"
        )
    return value


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
    from core.wiki_metrics import WikiHeatTracker  # type: ignore[attr-defined]

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
    storage_uid: Optional[str] = None
    error_msg: Optional[str] = None


class KnowledgeInboxProcessor:
    """Knowledge Inbox 处理器"""

    # 支持的文件类型
    SUPPORTED_EXTENSIONS = {
        # 文本文件
        ".txt",
        ".md",
        ".markdown",
        ".json",
        ".yaml",
        ".yml",
        ".py",
        ".js",
        ".ts",
        ".sh",
        ".sql",
        ".log",
        # 结构化图片（需要特殊处理）
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        # 文档文件（需要特殊处理）
        ".xlsx",
        ".xls",
        ".pptx",
        ".ppt",
        ".pdf",
        ".docx",
        ".doc",
        ".html",
        ".htm",
        # 电子书格式
        ".epub",
        ".mobi",
        ".azw3",
    }

    # 结构化图片扩展名
    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

    # 文档扩展名
    DOCUMENT_EXTENSIONS = {
        ".xlsx",
        ".xls",
        ".pptx",
        ".ppt",
        ".pdf",
        ".docx",
        ".doc",
        ".html",
        ".htm",
    }

    # 电子书扩展名
    EBOOK_EXTENSIONS = {".epub", ".mobi", ".azw3"}

    def __init__(self, backend: StorageBackend | None = None):
        self.inbox_dir = get_config().database_dir / "knowledge_inbox"
        self.state_db = get_config().database_dir / "inbox_state.db"
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

        # StorageBackend（根据配置自动创建或注入）
        self.backend = backend if backend is not None else create_storage_backend()

        # 文档处理器
        self.document_processor = DocumentProcessor()

        # 热力追踪器（可选）
        self.heat_tracker = None
        if HEAT_TRACKER_AVAILABLE:
            try:
                self.heat_tracker = WikiHeatTracker()
            except (ImportError, OSError, RuntimeError, ValueError, TypeError, sqlite3.Error) as e:
                logger.warning("[KnowledgeInbox] 热力追踪器初始化失败: %s", e)

        # 语义分片器：大文件按标题/段落切分后逐个入队蒸馏
        self._chunker = SemanticChunker()

        # NOTE: 来源追踪功能原由 IngestEngine 提供，该模块已移除。
        # 当前来源信息由 SyncEngine 在采集阶段记录，此处无需重复追踪。

    # ... 类定义继续 ...

    def _init_state_db(self):
        """初始化 SQLite 状态数据库"""
        with sqlite3.connect(str(self.state_db), timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_files (
                    file_hash TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    storage_uid TEXT,
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
        """从 canonical SQLite owner 加载处理状态。"""
        with sqlite3.connect(str(self.state_db), timeout=10) as conn:
            cursor = conn.execute("SELECT file_hash, status FROM processed_files")
            files = {row[0]: row[1] for row in cursor.fetchall()}
            cursor = conn.execute("SELECT MAX(scan_time) FROM scan_log")
            last_scan = cursor.fetchone()[0]
        return {"processed_files": files, "last_scan": last_scan}

    def _save_state(
        self,
        file_hash: str,
        filename: str,
        storage_uid: str | None = None,
        status: str = "success",
        error_msg: str | None = None,
    ):
        """保存单个文件处理状态到 SQLite"""
        with sqlite3.connect(str(self.state_db), timeout=10) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO processed_files
                (file_hash, filename, processed_at, storage_uid, status, error_msg)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (file_hash, filename, datetime.now().isoformat(), storage_uid, status, error_msg),
            )
            conn.commit()

    def _log_scan(
        self,
        files_found: int,
        files_processed: int,
        files_failed: int,
        report_path: str | None = None,
    ) -> int:
        """记录扫描日志，返回 scan_id"""
        with sqlite3.connect(str(self.state_db), timeout=10) as conn:
            cursor = conn.execute(
                """
                INSERT INTO scan_log (files_found, files_processed, files_failed, report_path)
                VALUES (?, ?, ?, ?)
            """,
                (files_found, files_processed, files_failed, report_path),
            )
            conn.commit()
            return cursor.lastrowid  # type: ignore[return-value]

    def _compute_hash(self, file_path: Path) -> str:
        """计算文件哈希"""
        hasher = hashlib.md5(usedforsecurity=False)
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
            if file_path.name.startswith("."):
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
                status="pending",
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
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # 尝试其他编码
            content = file_path.read_text(encoding="gbk", errors="replace")

        # 判断内容类型
        content_type = "text"
        if suffix in [".md", ".markdown"]:
            content_type = "markdown"
        elif suffix in [".json"]:
            content_type = "json"
        elif suffix in [".yaml", ".yml"]:
            content_type = "yaml"
        elif suffix in [".py", ".js", ".ts", ".sh", ".sql"]:
            content_type = "code"

        return content, content_type

    def _build_storage_content(self, inbox_file: InboxFile, content: str, content_type: str) -> str:
        """构建存储内容"""
        lines = [
            f"# Inbox Import: {inbox_file.filename}",
            "",
            "**Source**: human-local",
            f"**Size**: {inbox_file.size} bytes",
            f"**Type**: {content_type}",
            f"**Imported**: {datetime.now().isoformat()}",
            f"**Hash**: {inbox_file.hash}",
            "",
            "---",
            "",
        ]

        if content_type == "code":
            lines.append(f"```{inbox_file.filename.split('.')[-1]}")
            lines.append(content)
            lines.append("```")
        elif content_type in ["json", "yaml"]:
            lines.append("```yaml")
            lines.append(content[:KNOWLEDGE_INBOX_PROCESSOR__BUILD_STORAGE_CONTENT_CONTENT])
            if len(content) > KNOWLEDGE_INBOX_PROCESSOR__BUILD_STORAGE_CONTENT_CONTENT:
                lines.append(f"\n... (truncated, total {len(content)} chars)")
            lines.append("```")
        else:
            lines.append(content[:KNOWLEDGE_INBOX_PROCESSOR__BUILD_STORAGE_CONTENT_CONTENT])
            if len(content) > KNOWLEDGE_INBOX_PROCESSOR__BUILD_STORAGE_CONTENT_CONTENT:
                lines.append(f"\n\n... (truncated, total {len(content)} chars)")

        return "\n".join(lines)

    def process_file(self, inbox_file: InboxFile) -> Dict:
        """处理单个文件"""
        result = {
            "success": False,
            "file": inbox_file.filename,
            "hash": inbox_file.hash,
            "storage_uid": None,
            "error": None,
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

        except (
            ImportError,
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
            sqlite3.Error,
        ) as e:
            inbox_file.status = "error"
            inbox_file.error_msg = str(e)
            result["error"] = str(e)
            self._move_to_failed(inbox_file, str(e))
            self._save_state(
                inbox_file.hash, inbox_file.filename, status="failed", error_msg=str(e)
            )
            # NOTE: 来源追踪功能原由 IngestEngine 提供，该模块已移除。
            return result

    def _move_to_failed(self, inbox_file: InboxFile, error_msg: str):
        """将失败的文件移动到失败目录"""
        try:
            failed_path = self.failed_dir / f"{inbox_file.hash}_{inbox_file.filename}"
            shutil.move(str(inbox_file.path), str(failed_path))
            # 写入错误信息
            error_file = self.failed_dir / f"{inbox_file.hash}_{inbox_file.filename}.error.txt"
            error_file.write_text(
                f"处理时间: {datetime.now().isoformat()}\n错误: {error_msg}\n", encoding="utf-8"
            )
        except OSError as e:
            logger.warning("[Inbox] 移动失败文件失败: %s", e)

    def _process_text_file(self, inbox_file: InboxFile, result: Dict) -> Dict:
        """处理文本文件"""
        # 提取内容
        content, content_type = self._extract_content(inbox_file.path)

        # 构建存储内容
        storage_content = self._build_storage_content(inbox_file, content, content_type)

        # 解析Task ID（从文件名或内容中）
        task_id = TaskIdParser.parse(inbox_file.filename + " " + content[:200])

        # 为每个 Inbox 文件生成唯一 session_id，避免全部堆进同一个空 session
        inbox_session_id = f"inbox:{inbox_file.hash}:{datetime.now().strftime('%Y%m%d%H%M%S')}"

        # 构建标签（人工导入使用 source=human，不使用 model 标签）
        tags = [
            "source=human",
            f"session={inbox_session_id}",
            f"time={datetime.now().strftime('%Y%m%d')}",
            "scope=restricted",
        ]
        if task_id:
            tags.append(task_id)
        # 添加额外标签
        tags.extend(
            [f"inbox:{content_type}", f"file:{inbox_file.filename}", f"size:{inbox_file.size}"]
        )

        # 保存到 StorageBackend
        storage_result = self.backend.save(
            content=storage_content, tags=tags, title=f"inbox-{inbox_file.filename}"
        )
        storage_uid = storage_result[0].uid if storage_result else None

        # 标记为已处理
        inbox_file.status = "done"
        inbox_file.processed_at = datetime.now().isoformat()
        inbox_file.storage_uid = storage_uid

        # 移动到已处理目录
        processed_path = self.processed_dir / f"{inbox_file.hash}_{inbox_file.filename}"
        shutil.move(str(inbox_file.path), str(processed_path))

        # 保存状态到SQLite
        self._save_state(inbox_file.hash, inbox_file.filename, storage_uid, status="success")

        # 记录文件监控事件
        # NOTE: 来源追踪功能原由 IngestEngine 提供，该模块已移除。

        # 初始化热力追踪
        self._init_heat_tracking(inbox_file.filename, storage_uid, "text")  # type: ignore[arg-type]

        # [P0-5] L1 写入成功后入队 amphora 触发蒸馏
        # 使用原始完整内容，避免 _build_storage_content 中的展示截断丢失信息
        self._enqueue_for_distillation(inbox_file, content, storage_uid, "text", inbox_session_id)

        result["success"] = True
        result["storage_uid"] = storage_uid
        return result

    def _init_heat_tracking(self, filename: str, storage_uid: str, content_type: str):
        """初始化热力追踪"""
        if self.heat_tracker and storage_uid:
            try:
                page_id = f"inbox/{filename}"
                self.heat_tracker.init_page(page_id, initial_level="L1")
                logger.info("[Inbox] 热力追踪已初始化: %s", page_id)
            except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error) as e:
                logger.warning("[Inbox] 热力追踪初始化失败: %s", e)

    def _enqueue_for_distillation(
        self,
        inbox_file: InboxFile,
        content: str,
        storage_uid: Optional[str],
        content_type: str,
        inbox_session_id: Optional[str] = None,
    ) -> None:
        """L1 写入成功后，按语义边界切分并逐个 chunk 入队 amphora 触发蒸馏。"""
        try:
            from core.kia.amphora import enqueue_with_receipt

            base_session_id = inbox_session_id or storage_uid or f"inbox-{inbox_file.hash}"
            chunks = self._chunker.chunk_text(content, source_name=inbox_file.filename)

            for idx, chunk in enumerate(chunks):
                session_id = (
                    f"{base_session_id}-chunk-{idx}" if len(chunks) > 1 else base_session_id
                )
                messages = [
                    {
                        "role": "user",
                        "content": chunk,
                        "content_source": "external_file",
                        "source_authority": "external_content",
                        "source_authority_purpose": "searchable_reference_or_pending_hypothesis",
                    }
                ]
                meta = {
                    "source": "human",
                    "capture_source": "knowledge_inbox",
                    "file_path": str(inbox_file.path),
                    "file_name": inbox_file.filename,
                    "storage_uid": storage_uid,
                    "content_type": content_type,
                    "chunk_index": str(idx),
                    "total_chunks": str(len(chunks)),
                    "original_session_id": base_session_id,
                    "content_source": "external_file",
                    "source_authority": "external_content",
                    "source_authority_purpose": "searchable_reference_or_pending_hypothesis",
                }
                enqueue_with_receipt(session_id=session_id, messages=messages, meta=meta)

            logger.info(
                "[KnowledgeInbox] 蒸馏入队成功: %s (%s chunks)",
                inbox_file.filename,
                len(chunks),
            )
        except ImportError:
            logger.debug("[KnowledgeInbox] amphora 不可用，跳过蒸馏入队")
        except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as e:
            logger.warning(
                "[KnowledgeInbox] 蒸馏入队失败 %s: %s",
                inbox_file.filename,
                e,
                exc_info=True,
            )

    def _write_multimodal_task(
        self,
        *,
        inbox_file: InboxFile,
        image_path: Path,
        task_dir: Path,
        status: str,
        api_cfg: object | None,
        error_msg: str | None = None,
    ) -> Path:
        task = {
            "schema_version": "mnemos.multimodal_image_task.v1",
            "status": status,
            "created_at": datetime.now().isoformat(),
            "file": inbox_file.filename,
            "hash": inbox_file.hash,
            "image_path": str(image_path),
            "recoverable": True,
            "provider": str(getattr(api_cfg, "provider", "") or ""),
            "model": str(getattr(api_cfg, "model", "") or ""),
            "base_url": str(getattr(api_cfg, "base_url", "") or ""),
            "key_source": str(getattr(api_cfg, "source", "") or "missing"),
            "repair_actions": [
                "Set MNEMOS_MULTIMODAL_API_KEY, MNEMOS_MULTIMODAL_BASE_URL, and MNEMOS_MULTIMODAL_MODEL.",
                "Check provider reachability and model vision capability if this task failed after configuration.",
                "Or parse the image manually and save the result as Markdown back into inbox.",
            ],
        }
        if error_msg:
            task["error"] = error_msg
        task_path = task_dir / f"{inbox_file.hash}_{inbox_file.filename}.multimodal_task.json"
        task_path.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return task_path

    def _image_data_url(self, image_path: Path) -> str:
        mime_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
        with image_path.open("rb") as image_file:
            encoded = base64.b64encode(image_file.read()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def _call_multimodal_api(self, api_cfg: object, image_path: Path) -> str:
        try:
            import requests
        except ImportError as e:
            raise RuntimeError("requests package required for multimodal image parsing") from e

        base_url = str(getattr(api_cfg, "base_url", "") or "").rstrip("/")
        api_key = str(getattr(api_cfg, "api_key", "") or "")
        model = str(getattr(api_cfg, "model", "") or "")
        timeout = float(getattr(api_cfg, "timeout", 60) or 60)
        if not base_url or not api_key or not model:
            raise RuntimeError("multimodal API config is incomplete")

        runtime_config = get_config()
        vision_input_tokens = _configured_multimodal_input_token_reservation(runtime_config)
        image_size = image_path.stat().st_size
        mime_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
        system_prompt = (
            "You extract durable knowledge from images for Mnemos. "
            "Return Markdown with visible text, key facts, entities, "
            "tables, uncertainty, and action-relevant details."
        )
        user_prompt = (
            "Parse this image into concise but complete Markdown. "
            "Preserve important wording and mark uncertain readings."
        )
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": user_prompt,
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": self._image_data_url(image_path)},
                        },
                    ],
                },
            ],
            "temperature": MULTIMODAL_API_TEMPERATURE,
            "max_tokens": MULTIMODAL_EXTRACT_MAX_TOKENS,
        }
        # The real data URL carries raw user image bytes and must never become
        # ledger input.  This shape-preserving descriptor binds every visible
        # text/role/request option plus mime and byte length; the configured
        # vision allowance is a strict whole-request upper bound for the
        # redacted image payload.
        provider_input = canonical_provider_input(
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": (
                                        f"redacted-data-url:{mime_type};"
                                        f"bytes={image_size}"
                                    )
                                },
                            },
                        ],
                    },
                ],
                "temperature": MULTIMODAL_API_TEMPERATURE,
                "max_tokens": MULTIMODAL_EXTRACT_MAX_TOKENS,
            }
        )
        reservation: ModelCallReservation | None = None
        subject_scope = ("path", str(image_path.expanduser().resolve(strict=False)))
        try:
            ledger = ModelCallLedger.for_config(runtime_config)
            run_id = ledger.start_run(
                f"multimodal-extract:{uuid.uuid4().hex}",
                subject_scope=subject_scope,
            )
            # Never give the ledger a base64 data URL or visible prompt text.  The
            # digest identifies the billed image request without retaining a
            # reversible preview of either the image or instruction.
            reservation = ledger.reserve(
                run_id=run_id,
                operation="multimodal_extract",
                provider=str(getattr(api_cfg, "provider", "") or "multimodal"),
                model=model,
                input_text=provider_input,
                input_tokens=vision_input_tokens,
                output_tokens=MULTIMODAL_EXTRACT_MAX_TOKENS,
                cache_status="miss",
                retry_attempt=0,
                subject_scopes=(subject_scope,),
            )
            reservation.mark_dispatched()
            started = time.perf_counter()
            response = requests.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
                allow_redirects=False,
            )
            status_code = getattr(response, "status_code", None)
            if isinstance(status_code, int) and 300 <= status_code < 400:
                raise requests.HTTPError("multimodal provider redirect response rejected")
            response.raise_for_status()
            data = response.json()
            usage = data.get("usage") if isinstance(data, dict) else None
            request_id = str(
                (data.get("request_id") if isinstance(data, dict) else "")
                or (data.get("id") if isinstance(data, dict) else "")
                or getattr(response, "headers", {}).get("x-request-id", "")
                or getattr(response, "headers", {}).get("request-id", "")
                or ""
            )
            metered_usage = metered_provider_usage(
                usage,
                request_id=request_id,
                output_required=True,
            )
            if metered_usage is None:
                reservation.preserve_incurred(error_code="multimodal_provider_usage_missing")
            else:
                reservation.settle(
                    usage=metered_usage,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            content = data["choices"][0]["message"]["content"]
        except requests.RequestException as e:
            if reservation is not None:
                if reservation.dispatched:
                    reservation.preserve_incurred(error_code="multimodal_provider_exception")
                else:
                    reservation.release(error_code="multimodal_pre_dispatch_exception")
            raise ProviderRequestError(safe_provider_error_category(e)) from None
        except (OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError) as e:
            if reservation is not None:
                if reservation.dispatched:
                    reservation.preserve_incurred(error_code="multimodal_response_exception")
                else:
                    reservation.release(error_code="multimodal_pre_dispatch_exception")
            raise ProviderRequestError(safe_provider_error_category(e)) from None
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("multimodal API returned empty content")
        return content.strip()

    def _store_multimodal_extraction(
        self,
        inbox_file: InboxFile,
        result: Dict,
        content: str,
        api_cfg: object,
    ) -> Dict:
        storage_content = self._build_storage_content(inbox_file, content, "markdown")
        inbox_session_id = f"inbox:{inbox_file.hash}:multimodal:{datetime.now().strftime('%Y%m%d%H%M%S')}"
        tags = [
            "source=multimodal",
            f"session={inbox_session_id}",
            f"time={datetime.now().strftime('%Y%m%d')}",
            "scope=restricted",
            "inbox:multimodal_image",
            f"file:{inbox_file.filename}",
            f"size:{inbox_file.size}",
            f"model:{getattr(api_cfg, 'model', '')}",
            f"provider:{getattr(api_cfg, 'provider', '')}",
        ]
        storage_result = self.backend.save(
            content=storage_content,
            tags=tags,
            title=f"multimodal-{inbox_file.filename}",
        )
        storage_uid = storage_result[0].uid if storage_result else None
        processed_path = self.processed_dir / f"{inbox_file.hash}_{inbox_file.filename}"
        shutil.move(str(inbox_file.path), str(processed_path))

        inbox_file.status = "done"
        inbox_file.processed_at = datetime.now().isoformat()
        inbox_file.storage_uid = storage_uid
        self._save_state(inbox_file.hash, inbox_file.filename, storage_uid, status="success")
        self._init_heat_tracking(inbox_file.filename, storage_uid, "multimodal_image")  # type: ignore[arg-type]
        self._enqueue_for_distillation(
            inbox_file,
            content,
            storage_uid,
            "multimodal_image",
            inbox_session_id,
        )

        result.update(
            {
                "success": True,
                "storage_uid": storage_uid,
                "recoverable": False,
                "multimodal_status": "processed",
                "image_path": str(processed_path),
                "provider": str(getattr(api_cfg, "provider", "") or ""),
                "model": str(getattr(api_cfg, "model", "") or ""),
            }
        )
        return result

    def _process_image_file(self, inbox_file: InboxFile, result: Dict) -> Dict:
        """处理图片文件：配置多模态时创建自动任务，否则保留可恢复人工降级。"""
        from core.llm_config import resolve_multimodal_api_config

        logger.info("[Inbox] 检测到图片文件: %s", inbox_file.filename)
        api_cfg = resolve_multimodal_api_config(get_config())
        configured = bool(getattr(api_cfg, "configured", False))
        if configured:
            try:
                extracted = self._call_multimodal_api(api_cfg, inbox_file.path)
                return self._store_multimodal_extraction(inbox_file, result, extracted, api_cfg)
            except (OSError, IOError, RuntimeError, ValueError, KeyError, IndexError) as e:
                error_msg = safe_provider_error_category(e)
                logger.warning(
                    "[Inbox] multimodal extraction failed; creating recoverable task: category=%s",
                    error_msg,
                )
            task_dir = self.inbox_dir / ".multimodal"
            task_dir.mkdir(exist_ok=True)
            target_path = task_dir / f"{inbox_file.hash}_{inbox_file.filename}"
            try:
                shutil.move(str(inbox_file.path), str(target_path))
            except (OSError, IOError) as e:
                logger.warning("[Inbox] 移动文件到 .multimodal 失败: %s", e)
                target_path = inbox_file.path
            task_path = self._write_multimodal_task(
                inbox_file=inbox_file,
                image_path=target_path,
                task_dir=task_dir,
                status="multimodal_processor_failed",
                api_cfg=api_cfg,
                error_msg=error_msg,
            )
            result.update(
                {
                    "success": False,
                    "recoverable": True,
                    "multimodal_status": "unreachable",
                    "task_path": str(task_path),
                    "image_path": str(target_path),
                    "error": f"多模态解析失败，已创建可恢复任务：{error_msg}",
                }
            )
            self._save_state(
                inbox_file.hash,
                inbox_file.filename,
                status="failed_multimodal",
                error_msg=result["error"],
            )
            return result

        logger.info("[Inbox] 多模态模型未配置，进入可恢复人工降级流程。")
        logger.info("        请用 Kimi/豆包等工具解析图片内容，保存为 .md 后重新放入 inbox。")

        manual_dir = self.inbox_dir / ".manual"
        manual_dir.mkdir(exist_ok=True)
        manual_path = manual_dir / f"{inbox_file.hash}_{inbox_file.filename}"
        try:
            shutil.move(str(inbox_file.path), str(manual_path))
        except (OSError, IOError) as e:
            logger.warning("[Inbox] 移动文件到 .manual 失败: %s", e)
            manual_path = inbox_file.path
        task_path = self._write_multimodal_task(
            inbox_file=inbox_file,
            image_path=manual_path,
            task_dir=manual_dir,
            status="needs_multimodal_config",
            api_cfg=api_cfg,
        )

        result.update(
            {
                "success": False,
                "recoverable": True,
                "multimodal_status": "skipped",
                "task_path": str(task_path),
                "image_path": str(manual_path),
                "error": "图片需人工解析：可配置多模态模型恢复自动处理，或解析后保存为 .md 重新放入 inbox",
            }
        )
        self._save_state(
            inbox_file.hash,
            inbox_file.filename,
            status="skipped",
            error_msg=result["error"],
        )
        return result

    def _process_document_file(self, inbox_file: InboxFile, result: Dict) -> Dict:
        """处理文档文件（Excel/PPT/PDF/Word/HTML）"""
        logger.info("[Inbox] 检测到文档文件: %s", inbox_file.filename)

        from core.application.document_import_service import DocumentImportService

        import_result = DocumentImportService(config=get_config()).import_document(
            inbox_file.path,
            mode="distill",
            title=Path(inbox_file.filename).stem,
            agent_name="trusted_user_document",
        )

        if not import_result.get("success"):
            error = import_result.get("message") or "文档处理失败或内容为空"
            result["error"] = error
            result["import_result"] = import_result
            self._move_to_failed(inbox_file, "文档处理失败或内容为空")
            self._save_state(
                inbox_file.hash,
                inbox_file.filename,
                status="failed",
                error_msg=error,
            )
            return result

        storage_uid = (
            import_result.get("storage_uid")
            or import_result.get("l1_uid")
            or import_result.get("queue_id")
            or import_result.get("source_hash")
        )
        if storage_uid:
            # 标记为已处理
            inbox_file.status = "done"
            inbox_file.processed_at = datetime.now().isoformat()
            inbox_file.storage_uid = storage_uid

            # 移动到已处理目录
            processed_path = self.processed_dir / f"{inbox_file.hash}_{inbox_file.filename}"
            shutil.move(str(inbox_file.path), str(processed_path))

            # 保存状态
            self._save_state(inbox_file.hash, inbox_file.filename, storage_uid, status="success")
            parse_result = import_result.get("parse_result", {})
            doc_type = parse_result.get("doc_type", "document") if isinstance(parse_result, dict) else "document"
            self._init_heat_tracking(inbox_file.filename, storage_uid, f"doc:{doc_type}")

            result["success"] = True
            result["storage_uid"] = storage_uid
            result["doc_type"] = doc_type
            result["validation_status"] = (
                parse_result.get("validation_status", "")
                if isinstance(parse_result, dict)
                else ""
            )
            result["title"] = (
                parse_result.get("title", inbox_file.filename)
                if isinstance(parse_result, dict)
                else inbox_file.filename
            )
            result["wiki_paths"] = import_result.get("wiki_paths", [])
            result["queue_id"] = import_result.get("queue_id", "")
            result["quality_decision"] = import_result.get("quality_decision", "")
            result["routing_result"] = import_result.get("routing_result", {})
            result["action_ledger_ref"] = import_result.get("action_ledger_ref", "")
            result["content_source"] = import_result.get("content_source", "external_file")
            result["user_supplied"] = bool(import_result.get("user_supplied", True))
            result["trusted_user_document"] = bool(
                import_result.get("trusted_user_document", True)
            )
        else:
            result["error"] = "保存到 backend 失败"
            self._move_to_failed(inbox_file, "保存到 backend 失败")
            self._save_state(
                inbox_file.hash,
                inbox_file.filename,
                status="failed",
                error_msg="保存到 backend 失败",
            )
            # NOTE: 来源追踪功能原由 IngestEngine 提供，该模块已移除。

        return result

    def _process_ebook_file(self, inbox_file: InboxFile, result: Dict) -> Dict:
        """处理电子书文件（epub/mobi/azw3）"""
        logger.info("[Inbox] 检测到电子书文件: %s", inbox_file.filename)

        if not EBOOKLIB_AVAILABLE and inbox_file.path.suffix.lower() == ".epub":
            # ebooklib 不可用，回退到文本提取
            logger.info("[Inbox] ebooklib 不可用，回退到文本提取")
            try:
                content = inbox_file.path.read_text(encoding="utf-8", errors="ignore")[:CONTENT]
                return self._process_ebook_as_text(inbox_file, result, content)
            except (OSError, IOError) as e:
                result["error"] = f"电子书处理失败: {e}"
                self._move_to_failed(inbox_file, str(e))
                self._save_state(
                    inbox_file.hash, inbox_file.filename, status="failed", error_msg=str(e)
                )
                return result

        # 使用 ebooklib 处理 epub
        if inbox_file.path.suffix.lower() == ".epub" and EBOOKLIB_AVAILABLE:
            try:
                book = epub.read_epub(str(inbox_file.path))
                # 提取元数据
                title = book.get_metadata("DC", "Title")
                title = title[0][0] if title else inbox_file.filename
                author = book.get_metadata("DC", "Creator")
                author = author[0][0] if author else "Unknown"

                # 提取文本内容
                content_parts = []
                for item in book.get_items():
                    if item.get_type() == ebooklib.ITEM_DOCUMENT:
                        try:
                            from bs4 import BeautifulSoup

                            soup = BeautifulSoup(item.get_content(), "html.parser")
                            text = soup.get_text(separator="\n", strip=True)
                            if text:
                                content_parts.append(text)
                        except (
                            OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError,
                            sqlite3.Error
                        ):
                            logging.getLogger(__name__).warning(
                                "Caught unexpected error", exc_info=True
                            )
                raw_full_content = "\n\n".join(content_parts)
                # L1 存储做 10 万字符保护截断；amphora 入队将使用原始完整内容，由蒸馏器按 token 限制自行截断
                full_content = raw_full_content
                if len(full_content) > KNOWLEDGE_INBOX_PROCESSOR__PROCESS_EBOOK_FILE_FULL_CONTENT:
                    logger.warning(
                        "[KnowledgeInbox] 电子书内容超过 100000 字符，L1 存储将截断: %s",
                        inbox_file.filename,
                    )
                    full_content = full_content[:FULL_CONTENT] + "\n\n... (内容截断)"

                # 构建存储内容
                storage_content = f"""# Ebook: {title}

**作者**: {author}
**文件**: {inbox_file.filename}
**导入时间**: {datetime.now().isoformat()}

---

{full_content[:STORAGE_CONTENT]}
"""
                if len(full_content) > KNOWLEDGE_INBOX_PROCESSOR__PROCESS_EBOOK_FILE_FULL_CONTENT_2:
                    storage_content += f"\n\n... (共 {len(full_content)} 字符，已截断)"

                tags = [
                    "source=human",
                    f"time={datetime.now().strftime('%Y%m%d')}",
                    "scope=restricted",
                    "inbox:ebook",
                    f"file:{inbox_file.filename}",
                    f"ebook:title={title[:50]}",
                ]

                storage_result = self.backend.save(
                    content=storage_content, tags=tags, title=f"ebook-{title[:STORAGE_RESULT]}"
                )
                storage_uid = storage_result[0].uid if storage_result else None

                if storage_uid:
                    inbox_file.status = "done"
                    inbox_file.processed_at = datetime.now().isoformat()
                    inbox_file.storage_uid = storage_uid

                    processed_path = self.processed_dir / f"{inbox_file.hash}_{inbox_file.filename}"
                    shutil.move(str(inbox_file.path), str(processed_path))

                    self._save_state(
                        inbox_file.hash, inbox_file.filename, storage_uid, status="success"
                    )
                    self._init_heat_tracking(inbox_file.filename, storage_uid, "ebook")

                    # [P0-5] L1 写入成功后入队 amphora 触发蒸馏
                    # 使用原始完整文本，由 DistillationEngine 按 token 限制自行截断，避免 L1 截断导致蒸馏丢失内容
                    self._enqueue_for_distillation(
                        inbox_file, raw_full_content, storage_uid, "ebook"
                    )

                    result["success"] = True
                    result["storage_uid"] = storage_uid
                    result["title"] = title
                    return result
                else:
                    raise RuntimeError("保存到 backend 失败")

            except (
                ImportError,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                KeyError,
                sqlite3.Error,
            ) as e:
                result["error"] = f"EPUB 处理失败: {e}"
                self._move_to_failed(inbox_file, str(e))
                self._save_state(
                    inbox_file.hash, inbox_file.filename, status="failed", error_msg=str(e)
                )
                return result
        else:
            # mobi/azw3 暂不支持 ebooklib，回退到文本提取
            try:
                content = inbox_file.path.read_text(encoding="utf-8", errors="ignore")[:CONTENT]
                return self._process_ebook_as_text(inbox_file, result, content)
            except (OSError, IOError) as e:
                result["error"] = f"电子书处理失败: {e}"
                self._move_to_failed(inbox_file, str(e))
                self._save_state(
                    inbox_file.hash, inbox_file.filename, status="failed", error_msg=str(e)
                )
                return result

    def _process_ebook_as_text(self, inbox_file: InboxFile, result: Dict, content: str) -> Dict:
        """将电子书作为纯文本处理（回退方案）"""
        storage_content = f"""# Ebook: {inbox_file.filename}

**文件**: {inbox_file.filename}
**导入时间**: {datetime.now().isoformat()}
**注意**: 回退到纯文本提取，格式可能丢失

---

{content[:STORAGE_CONTENT_2]}
"""
        if len(content) > KNOWLEDGE_INBOX_PROCESSOR__PROCESS_EBOOK_AS_TEXT_CONTENT:
            storage_content += f"\n\n... (共 {len(content)} 字符，已截断)"

        tags = [
            "source=human",
            f"time={datetime.now().strftime('%Y%m%d')}",
            "scope=restricted",
            "inbox:ebook",
            f"file:{inbox_file.filename}",
            "ebook:fallback=text",
        ]

        storage_result = self.backend.save(
            content=storage_content, tags=tags, title=f"ebook-{inbox_file.filename}"
        )
        storage_uid = storage_result[0].uid if storage_result else None

        if storage_uid:
            inbox_file.status = "done"
            inbox_file.processed_at = datetime.now().isoformat()
            inbox_file.storage_uid = storage_uid

            processed_path = self.processed_dir / f"{inbox_file.hash}_{inbox_file.filename}"
            shutil.move(str(inbox_file.path), str(processed_path))

            self._save_state(inbox_file.hash, inbox_file.filename, storage_uid, status="success")
            # NOTE: 来源追踪功能原由 IngestEngine 提供，该模块已移除。
            self._init_heat_tracking(inbox_file.filename, storage_uid, "ebook")

            # [P0-5] L1 写入成功后入队 amphora 触发蒸馏
            # 使用原始提取内容，避免 storage_content 中的展示截断
            self._enqueue_for_distillation(inbox_file, content, storage_uid, "ebook")

            result["success"] = True
            result["storage_uid"] = storage_uid
        else:
            result["error"] = "保存到 StorageBackend 失败"
            self._move_to_failed(inbox_file, "保存到 StorageBackend 失败")
            self._save_state(
                inbox_file.hash,
                inbox_file.filename,
                status="failed",
                error_msg="保存到 StorageBackend 失败",
            )
            # NOTE: 来源追踪功能原由 IngestEngine 提供，该模块已移除。

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
            logger.info("Processing: %s...", inbox_file.filename)
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

        logger.warning(
            "[Inbox] 处理完成: %s 成功, %s 失败, %s 跳过",
            success_count,
            failed_count,
            skipped_count,
        )
        if report_path:
            logger.info("[Inbox] 报告已生成: %s", report_path)

        return results

    def generate_report(
        self, results: List[Dict], success: int, failed: int, skipped: int
    ) -> Optional[str]:
        """生成处理报告"""
        if not results:
            return None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = self.report_dir / f"report_{timestamp}.md"

        lines = [
            "# 📥 Knowledge Inbox 处理报告",
            "",
            f"**时间**: {datetime.now().isoformat()}",
            f"**总文件数**: {len(results)}",
            f"**成功**: {success} | **失败**: {failed} | **跳过**: {skipped}",
            "",
            "---",
            "",
            "## 处理详情",
            "",
        ]

        for r in results:
            status = "OK" if r["success"] else ("❌" if r.get("error") else "SKIP")
            lines.append(f"### {status} {r['file']}")
            if r["success"]:
                lines.append(f"- Storage UID: {r.get('storage_uid', 'N/A')}")
                if "doc_type" in r:
                    lines.append(f"- 文档类型: {r['doc_type']}")
                if "title" in r:
                    lines.append(f"- 标题: {r['title']}")
            elif r.get("error"):
                lines.append(f"- 错误: {r['error']}")
            else:
                lines.append("- 状态: 跳过")
            lines.append("")

        # 失败文件列表
        failed_files = [r for r in results if r.get("error")]
        if failed_files:
            lines.extend(
                [
                    "---",
                    "",
                    "## 失败文件（位于 `.failed/` 目录）",
                    "",
                ]
            )
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
            "pending_files": [f.filename for f in pending],
        }

    def list_processed(self) -> List[Dict]:
        """列出已处理的文件（从 SQLite）"""
        with sqlite3.connect(str(self.state_db), timeout=10) as conn:
            cursor = conn.execute("""
                SELECT file_hash, filename, processed_at, storage_uid, status
                FROM processed_files ORDER BY processed_at DESC
            """)
            return [
                {
                    "hash": row[0],
                    "filename": row[1],
                    "processed_at": row[2],
                    "storage_uid": row[3],
                    "status": row[4],
                }
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
        logger.info("\n处理完成: %s 个文件", len(results))
        for r in results:
            status = "OK" if r["success"] else "❌"
            logger.warning("  %s %s: %s", status, r["file"], r.get("storage_uid", r.get("error")))

    elif args.list:
        processed = processor.list_processed()
        logger.info("已处理文件: %s", len(processed))
        for p in processed:
            logger.info("  - %s (%s)", p["filename"], p["processed_at"][:10])

    else:
        status = processor.get_status()
        logger.info(json.dumps(status, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()


# 提供兼容别名
KnowledgeInbox = KnowledgeInboxProcessor
